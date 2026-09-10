"""Service fixtures with complete public configuration and owned temporary files.

All five protected categories are created under a unique nonsecret test root.
No fixture uses production directories. Optional controlled memory resources
inject failures only after the same public configuration/initialize boundary.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic_ns
from typing import cast
from uuid import uuid4
from unittest.mock import patch

from companion_memory.configuration import (
    CheckedResolutionOk, EffectiveSnapshot, MetadataValue, resolve_configuration_with_logging_validation,
)
from companion_memory.logging_service import (
    Logger, LoggingOk, LoggingResources, Service, create_logging_service,
)
from companion_memory.logging_service._execution import _Execution
from companion_memory.logging_service.resources import _Resource
from tests.logging_service.support import EventTestCase


class MemoryResource:
    """Controlled serial output with bounded capture and explicit operation barriers."""

    def __init__(self):
        self.records: list[bytes] = []
        self.calls = {name: 0 for name in ("prepare", "write", "flush", "probe", "close")}
        self.hooks: dict[str, Callable[[], None]] = {}
        self.entered = {name: Event() for name in self.calls}
        self.lock = Lock()
        self.active = 0
        self.maximum_active = 0
        self.short = False
        self.releases: list[Event] = []

    def block(self, operation: str) -> Event:
        release = Event()
        self.releases.append(release)

        def wait():
            if not release.wait(10):
                raise AssertionError("Controlled resource was not released.")
        self.hooks[operation] = wait
        return release

    def _call(self, operation: str) -> None:
        with self.lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.calls[operation] += 1
        try:
            self.entered[operation].set()
            hook = self.hooks.get(operation)
            if hook:
                hook()
        finally:
            with self.lock:
                self.active -= 1

    def prepare(self) -> None:
        self._call("prepare")

    def write(self, data: bytes, stream: object) -> int:
        self._call("write")
        if len(self.records) < 128:
            self.records.append(data)
        return len(data) - int(self.short)

    def flush(self) -> None:
        self._call("flush")

    def probe(self) -> None:
        self._call("probe")

    def close(self) -> None:
        self._call("close")


class ServiceTestCase(EventTestCase):
    """Own fixtures and ensure released resource workers terminate after each test."""

    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory(prefix="iris-diagnostics-test-")
        self.addCleanup(temporary.cleanup)
        # Configuration and physical checks share the actual directory path,
        # even when the system temporary directory contains symbolic links.
        self.root = Path(temporary.name).resolve(strict=True)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.directories: dict[str, list[str] | tuple[str, ...]] = {}
        for category in ("media", "database", "audit", "provider_usage", "backup"):
            directory = self.root / category
            directory.mkdir()
            self.directories[category] = [str(directory)]
        self.stdout, self.stderr = BytesIO(), BytesIO()
        self.resources = LoggingResources(
            self.stdout, self.stderr,
            tuple((key, tuple(paths)) for key, paths in self.directories.items()),
            lambda: str(uuid4()), lambda: datetime.now(timezone.utc), monotonic_ns,
        )
        self.services: list[Service] = []
        self.ports: list[MemoryResource] = []
        self.addCleanup(self.cleanup_services)

    def snapshot(self, values: dict[str, MetadataValue] | None = None) -> EffectiveSnapshot:
        selected: dict[str, MetadataValue] = {
            "logging.file_directory": str(self.logs), "logging.sink_capacity": 4,
            "logging.warning_reserve": 1, "logging.preparation_capacity": 2,
            "logging.io_timeout_ms": 1000, "logging.flush_timeout_ms": 500,
            "logging.close_timeout_ms": 500, "logging.probe_interval_ms": 1000,
        }
        selected.update(values or {})
        result = resolve_configuration_with_logging_validation(self.logging_registry(), selected, self.directories)
        if not isinstance(result, CheckedResolutionOk):
            self.fail("Complete temporary configuration must validate.")
        return result.value

    def service(self, values: dict[str, MetadataValue] | None = None, *,
                ports: tuple[_Resource | None, _Resource | None, _Resource] | None = None,
                initialize: bool = True) -> Service:
        service = create_logging_service()
        self.services.append(service)
        if initialize:
            if ports is None:
                result = service.initialize(self.snapshot(values), self.resources)
            else:
                self.ports.extend(port for port in ports if isinstance(port, MemoryResource))
                with patch.object(service, "_make_ports", return_value=ports):
                    result = service.initialize(self.snapshot(values), self.resources)
            self.assertIsInstance(result, LoggingOk, str(result))
        return service

    def logger(self, service: Service) -> Logger:
        result = service.get_logger("bootstrap")
        self.assertIsInstance(result, LoggingOk)
        return cast(LoggingOk[Logger], result).value

    def execution(self, service: Service) -> _Execution:
        self.assertIsNotNone(service._execution)
        return cast(_Execution, service._execution)

    def wait_for(self, service: Service, predicate: Callable[[], bool]) -> None:
        execution = self.execution(service)
        with execution.condition:
            self.assertTrue(execution.condition.wait_for(predicate, timeout=5), "Expected coordinated state was not reached.")

    def wait_cleanup(self, service: Service) -> None:
        """Thread joins are the completion barrier for already released resources."""
        execution = self.execution(service)
        for thread in execution.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        if execution.monitor:
            execution.monitor.join(5)
            self.assertFalse(execution.monitor.is_alive())
        self.assertFalse(service.get_sink_health().cleanup_pending)

    def background(self, function: Callable[[], object]) -> tuple[Thread, list[object]]:
        result: list[object] = []
        thread = Thread(target=lambda: result.append(function()), daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(5))
        return thread, result

    def cleanup_services(self):
        for port in self.ports:
            for release in port.releases:
                release.set()
        for service in self.services:
            service.close()
            execution = service._execution
            if execution is not None:
                for thread in execution.threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive(), "A released resource worker must terminate.")
                if execution.monitor:
                    execution.monitor.join(5)
                    self.assertFalse(execution.monitor.is_alive())
