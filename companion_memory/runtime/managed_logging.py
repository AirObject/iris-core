"""Serial diagnostic generations retaining file ownership through replacement.

A replacement is initialized while business admission is fenced. It receives no
new events until publication. Before a durable decision, disposal reconstructs
the previous settings; failed cleanup keeps ownership and admission closed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import sys
import time
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
from companion_memory.configuration.execution_versions import ExecutionVersion
from companion_memory.configuration.managed_resolution import ManagedConfigurationCandidate
from companion_memory.logging_service import create_logging_service, LoggingResources, LoggingOk
from companion_memory.logging_service import RuntimeLogWindow, RuntimeLogReader, ObservationGrant
from companion_memory.logging_service.service import Service
from .managed_bootstrap import ManagedBootstrap
if TYPE_CHECKING:
    from companion_memory.management.managed_application import ManagedApplication


@dataclass(slots=True)
class LogGeneration:
    service: Service
    candidate: ManagedConfigurationCandidate | None
    version_id: str | None
    window: RuntimeLogWindow | None = None
    reader: RuntimeLogReader | None = None
    ready: bool = False
    closed: bool = False


@dataclass(slots=True)
class LogReplacement:
    owner: ManagedLogging
    previous: LogGeneration
    selected: LogGeneration
    published: bool = False
    restoration: LogGeneration | None = None


class ManagedLogging:
    def __init__(self) -> None:
        self.current = LogGeneration(create_logging_service(), None, None)
        self.pending: LogReplacement | None = None
        self.bootstrap: ManagedBootstrap | None = None
        self.entry_id: str | None = None
        self.started = False

    @property
    def service(self):
        return self.current.service

    @property
    def window(self):
        return self.current.window

    @property
    def reader(self):
        return self.current.reader

    def _initialize(self, generation: LogGeneration) -> None:
        bootstrap = self.bootstrap
        if bootstrap is None or bootstrap.resources is None:
            raise ValueError('Logging requires owned resources.')
        directories = bootstrap.resources.protected_directories()
        candidate = generation.candidate
        if candidate is not None:
            if self.entry_id is None:
                raise ValueError('Logging requires initialized entry scope.')
            generation.window = RuntimeLogWindow(candidate, bootstrap.resources.instance_id)
            generation.reader = generation.window.bind_runtime_log_reader(ObservationGrant(
                bootstrap.resources.instance_id, (self.entry_id,), True, True))
            generation.service = create_logging_service(observation_window=generation.window)
        snapshot = candidate.foundation if candidate is not None else bootstrap_snapshot(bootstrap.settings, directories)
        result = generation.service.initialize(snapshot, LoggingResources(
            sys.stdout.buffer, sys.stderr.buffer, tuple(directories.items()),
            lambda: str(uuid4()), lambda: datetime.now(timezone.utc), time.monotonic_ns))
        generation.ready = type(result) is LoggingOk
        if not generation.ready:
            raise OSError('Unified logging resource preparation failed.')

    def open(self, bootstrap: ManagedBootstrap) -> None:
        if bootstrap.resources is None or self.started:
            raise ValueError('Logging requires an open volume and closed previous resources.')
        self.bootstrap = bootstrap
        self.current = LogGeneration(create_logging_service(), None, None)
        self.started = True
        self._initialize(self.current)

    async def bind(self, version: ExecutionVersion, entry_id: str) -> None:
        """Apply initial business settings before exposing business readiness."""
        self.entry_id = entry_id
        if self.current.version_id == version.version_id and self.current.ready and not self.current.closed:
            return
        from .managed_activation import PreparationFailure
        if self.pending is not None and not self.pending.selected.ready:
            if not await self.dispose(self.pending):
                raise PreparationFailure(self.pending)
        resource = await self.prepare(version)
        self.publish(resource)

    async def prepare(self, version: ExecutionVersion) -> LogReplacement:
        from .managed_activation import PreparationFailure
        if self.pending is not None:
            if self.pending.selected.version_id != version.version_id:
                raise ValueError('An original logging replacement still owns resources.')
            if not self.pending.selected.ready:
                raise PreparationFailure(self.pending)
            return self.pending
        replacement = LogReplacement(self, self.current,
            LogGeneration(create_logging_service(), version.candidate, version.version_id))
        # Retain the resource before any close/open can fail or outlive its wait.
        self.pending = replacement
        if not await self._close(replacement.previous):
            raise PreparationFailure(replacement)
        try:
            await asyncio.to_thread(self._initialize, replacement.selected)
        except (OSError, ValueError):
            raise PreparationFailure(replacement) from None
        return replacement

    def publish(self, resource: object) -> None:
        if (type(resource) is not LogReplacement or resource.owner is not self or
                not resource.selected.ready or resource.selected.closed):
            raise ValueError('Logging replacement is not ready.')
        self.current = resource.selected
        resource.published = True
        if self.pending is resource:
            self.pending = None
        self.event('DIAGNOSTIC_READY', True)

    async def dispose(self, resource: object) -> bool:
        if type(resource) is not LogReplacement or resource.owner is not self:
            return False
        if resource.published:
            return True
        if not await self._close(resource.selected) or not await self._close(resource.previous):
            return False
        # Restore a fresh generation with the exact previous configuration.
        # Its window generation changes, so old cursors cannot silently continue.
        restoration = resource.restoration
        if restoration is not None and not restoration.ready:
            if not await self._close(restoration):
                return False
            restoration = None
        if restoration is None:
            previous = resource.previous
            restoration = LogGeneration(create_logging_service(), previous.candidate, previous.version_id)
            resource.restoration = restoration
            try:
                await asyncio.to_thread(self._initialize, restoration)
            except (OSError, ValueError):
                return False
        self.current = restoration
        if self.pending is resource:
            self.pending = None
        return True

    async def attach(self, application: ManagedApplication) -> None:
        """Connect before recovery; repeated attachment never replaces an active version."""
        application.business.logging = self
        host = application.business.host
        if host is None or host.assembly.work_configuration is None or not application.business.initialized:
            return
        if self.current.candidate is None:
            draft = cast(dict[str, object], (await application.identity.read_draft())['draft'])
            await self.bind(host.assembly.work_configuration.versions.active, cast(str, draft['entry_id']))

    def event(self, code: str, success: bool) -> None:
        logger = self.service.get_logger('management')
        if type(logger) is LoggingOk:
            logger.value.emit({'level': 'INFO' if success else 'WARNING', 'event_code': code,
                'context': {}, 'attributes': {'outcome': 'SUCCESS' if success else 'FAILURE'}})

    @staticmethod
    async def _close(generation: LogGeneration) -> bool:
        if generation.closed:
            return True
        # Stop and drain the producer before closing its observation output.
        await asyncio.to_thread(generation.service.close)
        if generation.service.get_sink_health().cleanup_pending:
            return False
        if generation.window is not None and not generation.window.close():
            return False
        generation.closed = True
        return True

    async def close(self) -> bool:
        if not self.started:
            return True
        if self.pending is not None:
            pending = self.pending
            for generation in (pending.selected, pending.previous, pending.restoration):
                if generation is not None and not await self._close(generation):
                    return False
            self.pending = None
        if not await self._close(self.current):
            return False
        self.started = False
        return True
