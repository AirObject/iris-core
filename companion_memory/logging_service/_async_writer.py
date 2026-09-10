"""Bounded background writes to trusted, already prepared internal ports.

Each enabled sink has one worker and one active handle; one independent monitor
enforces monotonic I/O deadlines even while write is blocked. A shared memory
lock serializes queue accounting, deadlines and release of borrowed references.
Each slot also arbitrates completion publication with timeout decisions under a
short mutex: an acquired end time cannot be hidden behind delayed accounting.
Output calls and UTC callbacks run outside it. No bytes are copied for execution,
no event tasks or futures accumulate, and timed-out calls are never replaced.

Ports are borrowed, synchronous byte writers, supplied by trusted assembly;
they must not retain the borrowed payload after returning. This module neither
prepares physical resources nor provides Service, flush, close or recovery.
"""

from _thread import LockType
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Condition, Lock, RLock, Thread
from time import monotonic, monotonic_ns
from traceback import clear_frames
from typing import Literal, Protocol

from companion_memory.configuration import CheckedResolutionOk, EffectiveSnapshot

from ._delivery_state import _Admission, _FaultReason, _QueueView, _RejectedAdmission, _SinkName, _Work
from ._queues import _QueueController
from ._settings import _read_settings


class _OutputPort(Protocol):
    """Borrow one immutable JSONL buffer until a synchronous write returns.

    Return the byte count actually written; only an exact int equal to the
    complete buffer length confirms success. stream selects a prepared console
    destination, or is None for file. No retry, retention, or resource close is
    delegated to this port. Raising may mean a prefix was already written.
    """

    def write(self, jsonl: bytes, stream: Literal["stdout", "stderr"] | None) -> int: ...


@dataclass(slots=True)
class _WriterSlot:
    """One fixed sink slot with a scalar completion, never a result history.

    The completion mutex covers end-clock sampling/publication and the monitor's
    timeout decision. It is never held during output or while acquiring the
    shared accounting lock; nested locking always takes the shared lock first.
    """

    name: _SinkName
    port: _OutputPort | None
    work: _Work | None = None
    deadline_ns: int | None = None
    completion_lock: LockType = field(default_factory=Lock)
    completion: tuple[bool, int] | None = None


@dataclass(frozen=True, slots=True)
class _ExecutionView:
    """Finite resource observations without handles, payloads or error objects.

    live_threads counts actual thread liveness, including blocked calls after
    retirement is requested. Retirement never claims those calls are cancelled.
    """

    retiring: bool
    created_threads: int
    live_threads: int
    active_calls: tuple[bool, bool]
    armed_deadlines: tuple[bool, bool]


class _AsyncWriter:
    """Internal asynchronous queue owner with explicit worker management.

    Construction reads only a checked public configuration snapshot. Enabled
    ports must already be prepared and exclusively scheduled by this owner.
    UUID/UTC sources and the monotonic nanosecond clock are trusted, thread-safe
    and bounded; monotonic reads under the memory lock must not block or raise.
    start_workers is serial and one-shot. offer is available once started.
    """

    def __init__(
        self, checked: CheckedResolutionOk[EffectiveSnapshot], *,
        console: _OutputPort | None, file: _OutputPort | None,
        id_source: Callable[[], object], utc_clock: Callable[[], object],
        monotonic_clock: Callable[[], int] = monotonic_ns,
    ):
        settings = _read_settings(checked)
        if ((settings.console_enabled and console is None)
                or (settings.file_enabled and file is None)):
            raise TypeError("Enabled outputs require prepared internal ports.")
        lock = RLock()
        self._condition = Condition(lock)
        self._queues = _QueueController(
            settings, id_source, utc_clock, coordination_lock=lock,
        )
        self._clock = monotonic_clock
        self._utc_clock = utc_clock
        self._timeout_ns = settings.io_timeout_ms * 1_000_000
        self._slots = (
            _WriterSlot("console", console if settings.console_enabled else None),
            _WriterSlot("file", file if settings.file_enabled else None),
        )
        self._threads: list[Thread] = []
        self._started = False
        self._retiring = False

    def start_workers(self) -> None:
        """Start at most two writers and one monitor, with atomic publication.

        Startup failure stops and joins threads already created before exposing
        any write capability, then raises a fixed error without exception text.
        Repeated starts are rejected, including after retirement or failure.
        """
        failed = False
        with self._condition:
            if self._started or self._retiring:
                raise RuntimeError("Internal workers cannot be started again.")
            candidates = [Thread(target=self._worker, args=(slot,),
                                 name="logging-" + slot.name, daemon=True)
                          for slot in self._slots if slot.port is not None]
            if candidates:
                candidates.append(Thread(target=self._monitor, name="logging-io-monitor", daemon=True))
            try:
                for thread in candidates:
                    # Reserve the reference before starting, so an allocation
                    # failure cannot lose ownership of a running thread.
                    self._threads.append(thread)
                    try:
                        thread.start()
                    except BaseException:
                        self._threads.pop()
                        raise
                self._started = True
            except BaseException:
                failed = True
                self._retiring = True
                self._queues.stop_accepting()
            self._condition.notify_all()
        if failed:
            for thread in self._threads:
                thread.join()
            raise RuntimeError("Internal output workers could not start.")

    def offer(self, module: str, event: object) -> _Admission | _RejectedAdmission:
        """Prepare and admit normally, then coalesce a wakeup for both workers."""
        with self._condition:
            if not self._started:
                raise RuntimeError("Internal output workers have not started.")
        try:
            return self._queues.offer(module, event)
        finally:
            with self._condition:
                self._condition.notify_all()

    def observe(self) -> _QueueView:
        """Return the existing deep immutable queue and target accounting."""
        return self._queues.observe()

    def observe_execution(self) -> _ExecutionView:
        """Report actual resource occupancy without creating work or timers."""
        with self._condition:
            return _ExecutionView(
                self._retiring, len(self._threads),
                sum(thread.is_alive() for thread in self._threads),
                (self._slots[0].work is not None, self._slots[1].work is not None),
                (self._slots[0].deadline_ns is not None, self._slots[1].deadline_ns is not None),
            )

    def wake(self) -> None:
        """Recheck current state after an injected clock advances; no task list."""
        with self._condition:
            self._condition.notify_all()

    def retire_workers(self) -> None:
        """Stop admission, finish eligible queued work and retire owned threads.

        This nonwaiting, idempotent request does not flush or close any port,
        abandon queued work, cancel I/O, or impose a close deadline. A blocked
        call and its monitor remain owned until the call actually ends.
        """
        with self._condition:
            self._queues.stop_accepting()
            self._retiring = True
            self._condition.notify_all()

    def join_workers(self, wait_seconds: float) -> _ExecutionView:
        """Wait at most this host join budget after requesting retirement.

        The budget only bounds thread joins; it cannot override configured I/O
        deadlines or classify targets. Live threads remain owned and observable.
        The caller may repeat the join after a blocked borrowed port is released.
        """
        with self._condition:
            if not self._retiring:
                raise RuntimeError("Retire internal workers before joining them.")
        deadline = monotonic() + max(0.0, wait_seconds)
        for thread in self._threads:
            thread.join(max(0.0, deadline - monotonic()))
        return self.observe_execution()

    def _worker(self, slot: _WriterSlot) -> None:
        while True:
            with self._condition:
                slot.work = self._queues.begin(slot.name)
                if slot.work is None:
                    if self._retiring:
                        return
                    self._condition.wait()
                    continue
                slot.deadline_ns = self._clock() + self._timeout_ns
                self._condition.notify_all()
            # Only the slot retains a borrowed handle across this call. The
            # helper's payload/exception frames are gone before capacity release.
            succeeded, _ = self._write(slot)
            with self._condition:
                self._arbitrate_deadline(slot)
                # The write has returned. UTC observation is not output I/O and
                # must not extend the monitored operation or trigger IO_TIMEOUT.
            completed_at = self._completion_time() if succeeded else None
            with self._condition:
                timestamp = (None if completed_at is None else
                             completed_at.isoformat(timespec="microseconds").removesuffix("+00:00") + "Z")
                self._queues._finish(slot.work, succeeded=succeeded, timestamp=timestamp)
                slot.work = None
                slot.deadline_ns = None
                with slot.completion_lock:
                    slot.completion = None
                self._condition.notify_all()

    def _write(self, slot: _WriterSlot) -> tuple[bool, int]:
        """Discard all port exception data before publishing scalar completion.

        Even process-style exceptions raised inside a port are contained at the
        worker boundary, preventing threading.excepthook from leaking payloads.
        """
        work, port = slot.work, slot.port
        assert work is not None and port is not None
        try:
            count = port.write(work.jsonl, work.stream)
            succeeded = type(count) is int and count == len(work.jsonl)
        except BaseException as error:
            clear_frames(error.__traceback__)
            succeeded = False
        # Publish the outcome and its sampled end time before returning to the
        # worker's accounting path. The monitor cannot interleave between this
        # time read and publication, even if the worker is then descheduled.
        with slot.completion_lock:
            slot.completion = (succeeded, self._clock())
            return slot.completion

    def _arbitrate_deadline(self, slot: _WriterSlot) -> int | None:
        """Under the shared lock, settle a completed/expired I/O or keep its deadline.

        Both worker and monitor use the same arbitration. A published completion
        is judged by its end time, never by the later accounting time. With no
        completion, sampling the current clock and deciding timeout excludes
        concurrent end-time publication. Queue faults retain their first reason;
        arbitration never releases handles or reverses UNKNOWN classification.
        """
        deadline = slot.deadline_ns
        if deadline is None:
            return None
        reason: _FaultReason | None
        with slot.completion_lock:
            completion = slot.completion
            if completion is None:
                if self._clock() < deadline:
                    return deadline
                reason = "IO_TIMEOUT"
            else:
                succeeded, ended_ns = completion
                reason = ("IO_TIMEOUT" if ended_ns >= deadline else
                          "WRITE_FAILED" if not succeeded else None)
        slot.deadline_ns = None
        if reason is not None:
            self._queues.fault(slot.name, reason)
        self._condition.notify_all()
        return None

    def _completion_time(self) -> datetime | None:
        """Read an optional UTC observation without changing the write outcome."""
        try:
            instant = self._utc_clock()
            if type(instant) is datetime and instant.tzinfo is timezone.utc:
                return instant
        except BaseException as error:
            clear_frames(error.__traceback__)
        return None

    def _monitor(self) -> None:
        with self._condition:
            while True:
                nearest: int | None = None
                for slot in self._slots:
                    deadline = self._arbitrate_deadline(slot)
                    if deadline is None:
                        continue
                    nearest = deadline if nearest is None else min(nearest, deadline)
                if self._retiring and all(slot.work is None for slot in self._slots):
                    # Writers may still drain eligible queued targets. Remain
                    # available until no queued target can start another I/O.
                    if all(sink.queued_events == 0 for sink in self._queues.observe().sinks):
                        return
                now = self._clock()
                self._condition.wait(None if nearest is None else max(0, nearest - now) / 1_000_000_000)
