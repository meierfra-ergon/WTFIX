# This file is a part of WTFIX.
#
# Copyright (C) 2018,2019 John Cass <john.cass77@gmail.com>
#
# WTFIX is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at
# your option) any later version.
#
# WTFIX is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public
# License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import importlib
from asyncio import futures
from collections import OrderedDict

from unsync import unsync

from wtfix.conf import logger, SessionSettings
from wtfix.conf import settings
from wtfix.core.exceptions import (
    MessageProcessingError,
    StopMessageProcessing,
    ValidationError,
    ImproperlyConfigured,
)


class BasePipeline:
    """
    Propagates inbound messages up and down the layers of configured message handling apps instances.
    """

    INBOUND_PROCESSING = 0
    OUTBOUND_PROCESSING = 1

    def __init__(self, session_name=None, installed_apps=None):
        if session_name is None:
            session_name = settings.default_session_name

        self.settings = SessionSettings(session_name)
        self._installed_apps = self._load_apps(installed_apps=installed_apps)
        logger.info(f"Created new FIX application pipeline: {list(self.apps.keys())}.")

        self.stop_lock = asyncio.Lock()
        self.stopped_event = asyncio.Event()

    @property
    def apps(self):
        return self._installed_apps

    def _load_apps(self, installed_apps=None):
        """
        Loads the list of apps to be used for processing messages.

        :param installed_apps: The list of class paths for the installed apps.
        """
        loaded_apps = OrderedDict()

        if installed_apps is None:
            installed_apps = self.settings.PIPELINE_APPS

        if len(installed_apps) == 0:
            raise ImproperlyConfigured(
                f"At least one application needs to be added to the pipeline by using the PIPELINE_APPS setting."
            )

        for app in installed_apps:
            mod_name, class_name = app.rsplit(".", 1)
            module = importlib.import_module(mod_name)

            class_ = getattr(module, class_name)
            instance = class_(self)

            loaded_apps[instance.name] = instance

        return loaded_apps

    @unsync
    async def initialize(self):
        """
        Initialize all applications that have been configured for this pipeline.

        All apps are initialized concurrently.
        """
        logger.info(f"Initializing applications...")

        init_calls = (app.initialize for app in reversed(self.apps.values()))
        await asyncio.gather(*(call() for call in init_calls))

        logger.info("All apps initialized!")

    @unsync
    async def start(self):
        """
        Starts each of the applications in turn.

        :raises: TimeoutError if either INIT_TIMEOUT or STARTUP_TIMEOUT is exceeded.
        """
        logger.info("Starting pipeline...")

        try:
            # Initialize all apps first
            await asyncio.wait_for(self.initialize(), settings.INIT_TIMEOUT)

        except futures.TimeoutError as e:
            logger.error(f"Timeout waiting for apps to initialize!")
            raise e

        for app in reversed(self.apps.values()):
            logger.info(f"Starting app '{app}'...")

            try:
                await asyncio.wait_for(app.start(), settings.STARTUP_TIMEOUT)
            except futures.TimeoutError as e:
                logger.error(f"Timeout waiting for app '{app}' to start!")
                raise e

        logger.info("All apps in pipeline have been started!")

        # Block until the pipeline has been stopped again.
        await self.stopped_event.wait()

    @unsync
    async def stop(self):
        """
        Tries to shut down the pipeline in an orderly fashion.

        :raises: TimeoutError if STOP_TIMEOUT is exceeded.
        """
        async with self.stop_lock:  # Ensure that more than one app does not attempt to initiate a shutdown at once
            if self.stopped_event.is_set():
                # Pipeline has already been sotpped - nothing more to do.
                return

            logger.info("Shutting down pipeline...")
            try:
                for app in self.apps.values():
                    logger.info(f"Stopping app '{app}'...")
                    await asyncio.wait_for(app.stop(), settings.STOP_TIMEOUT)

                logger.info("Pipeline stopped.")
                self.stopped_event.set()
            except futures.TimeoutError:
                logger.error(
                    f"Timeout waiting for app '{app}' to stop, cancelling all outstanding tasks..."
                )
                # Stop all asyncio tasks
                for task in asyncio.Task.all_tasks():
                    task.cancel()

    def _prep_processing_pipeline(self, direction):
        if direction is self.INBOUND_PROCESSING:
            return "on_receive", reversed(self.apps.values())

        if direction is self.OUTBOUND_PROCESSING:
            return "on_send", iter(self.apps.values())

        raise ValidationError(
            f"Unknown application chain processing direction '{direction}'."
        )

    @unsync
    async def _process_message(self, message, direction):
        """
        Process a message by passing it on to the various apps in the pipeline.

        :param message: The GenericMessage instance to process
        :param direction: 0 if this is an inbound message, 1 otherwise.

        :return: The processed message.
        """

        process_func, app_chain = self._prep_processing_pipeline(direction)

        try:
            for app in app_chain:
                # Call the relevant 'on_send' or 'on_receive' method for each application
                message = getattr(app, process_func)(message)

        except MessageProcessingError as e:
            logger.exception(
                f"Error processing message {message}. Processing stopped at '{app.name}': {e}."
            )
        except StopMessageProcessing:
            logger.info(f"Processing of message {message} interrupted at '{app.name}'.")
        except Exception as e:
            # Log exception in case it is not handled properly in the Future object.
            logger.exception(
                f"Unhandled exception while doing {process_func} for message {message}."
            )
            raise e

        return message

    @unsync
    async def receive(self, message):
        """Receives a new message to be processed"""
        return await self._process_message(message, self.INBOUND_PROCESSING)

    @unsync
    async def send(self, message):
        """Processes a new message to be sent"""
        return await self._process_message(message, self.OUTBOUND_PROCESSING)
