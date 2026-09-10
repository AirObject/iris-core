"""Controlled memory ports and clocks for deterministic background write tests.

Events establish write entry and release; condition predicates observe committed
accounting. All waits are bounded test failure guards, never timing assumptions.
Captured output belongs to the test port and has a fixed record limit.
"""

from collections.abc import Callable
from threading import Event, Lock, current_thread
from typing import Literal

from companion_memory.configuration import CheckedResolutionOk, MetadataValue
from companion_memory.logging_service._async_writer import _AsyncWriter
from companion_memory.logging_service._delivery_state import _Admission
from tests.logging_service.support import EventTestCase


class ManualClock:
    """A monotonic nanosecond source with explicit monitor-pass notification."""

    def __init__(self):
        self.lock = Lock()
        self.now = 0
        self.observed = Event()

    def __call__(self) -> int:
        with self.lock:
            value = self.now
            if current_thread().name == "logging-io-monitor":
                self.observed.set()
            return value

    def set(self, value: int) -> None:
        with self.lock:
            if value < self.now:
                raise AssertionError("The controlled monotonic clock cannot regress.")
            self.now = value
            self.observed.clear()


class ControlledPort:
    """Hold the first call, then return full/short counts or a hostile exception.

    Only explicitly capturing ports keep bounded output bytes. Other ports keep
    scalar observations so work/payload release can be checked independently.
    """

    def __init__(self, *, blocked: bool = False, outcome: str = "full", capture: bool = False):
        self.entered = Event()
        self.release = Event()
        if not blocked:
            self.release.set()
        self.outcome = outcome
        self.capture = capture
        self.records: list[tuple[bytes, str | None]] = []
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.first_payload_id: int | None = None
        self.first_length = 0
        self.thread_ids: set[int | None] = set()
        self.close_calls = 0
        self.failure: str | None = None
        self.before_return: Callable[[], None] | None = None
        self.lock = Lock()

    def write(self, jsonl: bytes, stream: Literal["stdout", "stderr"] | None) -> int:
        with self.lock:
            self.calls += 1
            call = self.calls
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.thread_ids.add(current_thread().ident)
            if call == 1:
                self.first_payload_id, self.first_length = id(jsonl), len(jsonl)
            if self.capture:
                if len(self.records) >= 32:
                    raise AssertionError("The controlled capture limit was exceeded.")
                self.records.append((jsonl, stream))
        try:
            self.entered.set()
            if call == 1 and not self.release.wait(5):
                self.failure = "The controlled output was not released."
                raise AssertionError(self.failure)
            if self.before_return is not None:
                self.before_return()
            if self.outcome == "exception":
                raise SecretWriteError("secret-port-error", jsonl)
            if self.outcome == "process_exception":
                raise SystemExit("secret-process-error")
            if self.outcome == "short":
                return len(jsonl) - 1
            if self.outcome == "zero":
                return 0
            if self.outcome == "oversized":
                return len(jsonl) + 1
            if self.outcome == "bool":
                return True
            return len(jsonl)
        finally:
            with self.lock:
                self.active -= 1

    def close(self) -> None:
        self.close_calls += 1


class SecretWriteError(Exception):
    """The output error must never be formatted by a worker or thread hook."""

    def __str__(self) -> str:
        raise AssertionError("Output exceptions must not be formatted.")

    def __repr__(self) -> str:
        raise AssertionError("Output exceptions must not be represented.")


class AsyncTestCase(EventTestCase):
    """Own all created threads and release every held port even after failure."""

    def writer(
        self, console: ControlledPort, file: ControlledPort,
        values: dict[str, MetadataValue] | None = None, *, start: bool = True,
    ) -> tuple[_AsyncWriter, ManualClock]:
        selected: dict[str, MetadataValue] = {
            "logging.sink_capacity": 4, "logging.warning_reserve": 1,
            "logging.preparation_capacity": 2, "logging.io_timeout_ms": 20,
        }
        selected.update(values or {})
        checked = self.checked(self.logging_registry(), selected)
        if not isinstance(checked, CheckedResolutionOk):
            self.fail("The synthetic configuration must validate.")
        clock = ManualClock()
        writer = _AsyncWriter(checked, console=console, file=file,
                              id_source=self.sources.new_id, utc_clock=self.sources.now,
                              monotonic_clock=clock)

        def cleanup():
            console.release.set()
            file.release.set()
            writer.retire_workers()
            result = writer.join_workers(5)
            self.assertEqual(result.live_threads, 0, "Owned output threads must terminate after release.")
            self.assertEqual(result.active_calls, (False, False))
            self.assertEqual(result.armed_deadlines, (False, False))
            self.assertEqual((console.failure, file.failure), (None, None))
            self.assertEqual((console.close_calls, file.close_calls), (0, 0))

        self.addCleanup(cleanup)
        if start:
            writer.start_workers()
        return writer, clock

    def offer(self, writer: _AsyncWriter, **changes: object) -> _Admission:
        result = writer.offer("bootstrap", self.event(**changes))
        if not isinstance(result, _Admission):
            self.fail("The event must pass internal admission.")
        return result

    def wait_state(self, writer: _AsyncWriter, predicate: Callable[[], bool]) -> None:
        with writer._condition:
            self.assertTrue(writer._condition.wait_for(predicate, timeout=5),
                            "The expected committed background state was not observed.")

    def advance(self, writer: _AsyncWriter, clock: ManualClock, nanoseconds: int) -> None:
        clock.set(nanoseconds)
        writer.wake()
        self.assertTrue(clock.observed.wait(5), "The independent monitor must inspect the new clock.")
        # The monitor reads time under this same lock; reacquisition ensures its
        # entire fault/next-wait decision has finished before assertions run.
        with writer._condition:
            pass
