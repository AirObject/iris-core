"""Fixed resource owners for preparation, delivery, flush, recovery and cleanup.

One worker per regular output and one emergency worker execute at most one call
apiece. One coordinator arbitrates published end times and total wait deadlines
under a short memory lock; output and completion timestamps run outside it.
There is one live flush boundary, no futures or per-call background waiters.
Timed-out calls retain their slot and borrowed bytes until the actual return.
"""

from _thread import LockType
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Condition, Lock, RLock, Thread
from time import monotonic
from traceback import clear_frames
from typing import Literal, cast
import json

from ._delivery_state import _ConstantEmergencyNeed, _Cutpoint, _EmergencyNeed, _FaultEmergencyNeed, _FaultReason, _Work
from ._queues import _QueueController
from ._rules import _MAX_INTEGER
from ._service_settings import _ServiceSettings
from .resources import LoggingResources, _Resource, _ResourceFailure
from .results import EmergencyDisposition, SinkReport

type _Job = Literal["prepare", "write", "flush", "probe", "close", "emergency"]


@dataclass(slots=True)
class _Slot:
    """A scalar publication, resource ownership and one request's progress.

    request_flushed and request_reason belong only to the current cut. Live
    health and saturated statistics cannot rewrite a completed cut or control
    whether another flush is needed. flush_covers_request marks the one active
    I/O when it covers every target in that cut, including across waiting calls.
    """

    name: Literal["console", "file", "emergency"]
    port: _Resource
    job: _Job | None = None
    work: _Work | None = None
    payload: bytes | None = None
    deadline: int = 0
    publication_lock: LockType = field(default_factory=Lock)
    publication: tuple[str | None, int] | None = None
    returned: bool = False
    accounted: bool = False
    expired: bool = False
    timestamp: str | None = None
    prepared: bool = False
    prepare_reason: str | None = None
    cleanup_requested: bool = False
    closed: bool = False
    close_failed: bool = False
    last_reason: str | None = None
    probe_after: int = 0
    flushed: int = 0
    flushing_written: int = 0
    flush_count: int = 0
    request_flushed: bool = False
    request_reason: str | None = None
    flush_covers_request: bool = False
    close_ended_at: int | None = None
    shutdown_abandoned: bool = False
    recovered_losses: int | None = None
    first_fault: str | None = None


class _Execution:
    """Own fixed workers and queue admission for one service initialization.

    Lifecycle operations are serial; emit and health can run concurrently.
    All mutable coordination fields below are protected by condition. Trusted
    resource callbacks can block, but are never invoked while holding that lock.
    """

    def __init__(self, settings: _ServiceSettings, resources: LoggingResources,
                 console: _Resource | None, file: _Resource | None, emergency: _Resource):
        self.settings = settings
        self.resources = resources
        lock = RLock()
        self.condition = Condition(lock)
        self.queues = _QueueController(settings.event, resources.id_source, resources.utc_clock,
                                       coordination_lock=lock)
        self.clock = resources.monotonic_clock
        self.timeout = settings.event.io_timeout_ms * 1_000_000
        self.probe_interval = settings.probe_interval_ms * 1_000_000
        self.slots: tuple[_Slot | None, _Slot | None, _Slot] = (
            None if console is None else _Slot("console", console),
            None if file is None else _Slot("file", file), _Slot("emergency", emergency),
        )
        self.threads: list[Thread] = []
        self.ready = False
        self.closing = False
        self.abandoned = False
        self.close_deadline: int | None = None
        self.cut: _Cutpoint | None = None
        self.flush_deadline: int | None = None
        self.flush_expired = False
        self.report_deadline: int | None = None
        self.emergencies: deque[bytes] = deque()
        self.emergency_after: int | None = None
        self.emergency_suppressed = 0
        self.emergency_failed = 0
        self.flush_deadline_exceeded = 0
        self.saturated = False
        self.monitor: Thread | None = None

    def increment(self, field: str, amount: int = 1) -> None:
        value = getattr(self, field) + amount
        if value >= _MAX_INTEGER:
            value = _MAX_INTEGER
            self.saturated = True
        setattr(self, field, value)

    def start(self) -> None:
        """Start fixed owners; partial startup still has a live cleanup coordinator."""
        with self.condition:
            self.monitor = Thread(target=self._coordinate, name="logging-service-monitor", daemon=True)
            try:
                self.monitor.start()
            except BaseException:
                for slot in self.slots:
                    if slot is not None:
                        slot.closed = True
                self.monitor = None
                raise
            started: list[_Slot] = []
            try:
                for slot in self.slots:
                    if slot is not None:
                        thread = Thread(target=self._worker, args=(slot,), name="logging-service-" + slot.name,
                                        daemon=True)
                        thread.start()
                        self.threads.append(thread)
                        started.append(slot)
            except BaseException:
                for slot in self.slots:
                    if slot is not None and not any(slot is item for item in started):
                        slot.closed = True
                self.request_cleanup()
                raise

    def prepare(self) -> str | None:
        """Prepare regular outputs in console/file order, then emergency capability."""
        for slot in self.slots:
            if slot is None:
                continue
            with self.condition:
                self._dispatch(slot, "prepare")
                while not slot.prepared and slot.prepare_reason is None:
                    self._tick()
                    if not slot.prepared and slot.prepare_reason is None:
                        self._wait(slot.deadline)
                if slot.prepare_reason is not None:
                    return slot.prepare_reason
        with self.condition:
            self.ready = True
            self.condition.notify_all()
        return None

    def wake(self) -> None:
        """Notify coordination after a controlled clock advance or event admission."""
        with self.condition:
            self.condition.notify_all()

    def _wait(self, deadline: int) -> None:
        remaining = deadline - self.clock()
        if remaining > 0:
            self.condition.wait(remaining / 1_000_000_000)

    def emergency(self, need: _EmergencyNeed | _ConstantEmergencyNeed | _FaultEmergencyNeed | None) -> EmergencyDisposition:
        """Reserve finite capacity before encoding a <=512 byte safe summary."""
        if need is None:
            return "NOT_NEEDED"
        with self.condition:
            slot = self.slots[2]
            now = self.clock()
            if self.closing or slot.last_reason is not None or slot.closed:
                self.increment("emergency_failed")
                return "UNAVAILABLE"
            if self.emergency_after is not None and now < self.emergency_after:
                self.increment("emergency_suppressed")
                return "SUPPRESSED"
            if len(self.emergencies) + int(slot.payload is not None) >= self.settings.emergency_capacity:
                self.increment("emergency_failed")
                return "UNAVAILABLE"
            summary = {"level": need.level_name, "reason": need.reason,
                       "message": "诊断输出不可用。" if type(need) is _FaultEmergencyNeed else "高等级诊断未完成常规投递。"}
            if type(need) is _EmergencyNeed:
                summary["event_id"] = need.event_id
            encoded = (json.dumps(summary, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            if len(encoded) > 512:
                self.increment("emergency_failed")
                return "UNAVAILABLE"
            self.emergencies.append(encoded)
            self.emergency_after = now + self.settings.emergency_interval_ms * 1_000_000
            self.condition.notify_all()
            return "SCHEDULED"

    def begin_flush(self, *, closing: bool = False) -> None:
        """Capture both targets and one shared deadline atomically with admission."""
        with self.condition:
            self._tick()
            if closing:
                self.closing = True
                self.close_deadline = self.clock() + self.settings.close_timeout_ms * 1_000_000
                self.cut = self.queues.stop_accepting()
                self.flush_deadline = self.close_deadline
            else:
                self.cut = self.queues.capture_cutpoint()
                self.flush_deadline = self.clock() + self.settings.flush_timeout_ms * 1_000_000
            self.report_deadline = self.flush_deadline
            views = self.queues.observe_cutpoint(self.cut).targets
            for slot, view in zip(self.slots[:2], views, strict=True):
                if slot is not None:
                    slot.request_flushed = False
                    slot.request_reason = slot.first_fault or slot.last_reason
                    # An outstanding refresh can satisfy this request only if
                    # no target in its new cut still needs a write. Published
                    # completions already accounted above require a fresh flush.
                    slot.flush_covers_request = (slot.job == "flush" and not slot.accounted
                                                 and not view.pending_queued and not view.pending_in_flight)
            self.flush_expired = False
            self.condition.notify_all()

    def wait_flush(self) -> tuple[SinkReport, SinkReport]:
        with self.condition:
            assert self.flush_deadline is not None
            deadline = self.flush_deadline
            while True:
                self._tick()
                if self._flush_finished() or self.clock() >= deadline:
                    break
                self._wait(deadline)
            if not self._flush_finished():
                self.flush_expired = True
                if not self.closing:
                    self.increment("flush_deadline_exceeded")
            reports = self.reports(closing=False)
            self.flush_deadline = None
            self.condition.notify_all()
            return reports

    def _flush_finished(self) -> bool:
        """Consult current request outcomes, never monotonic counter increments."""
        assert self.cut is not None
        return all(slot is None or slot.request_flushed or slot.request_reason is not None
                   for slot in self.slots[:2])

    def request_cleanup(self) -> None:
        """Stop all new work and reclaim each resource after its outstanding call."""
        with self.condition:
            self.closing = True
            self.ready = False
            self.queues.stop_accepting()
            self.queues.abandon()
            self.abandoned = True
            self.increment("emergency_failed", len(self.emergencies))
            self.emergencies.clear()
            for slot in self.slots:
                if slot is not None:
                    slot.cleanup_requested = True
            self.condition.notify_all()

    def finish_close(self, deadline: int) -> None:
        """Include emergency drain, resource cleanup and worker joins in one budget."""
        with self.condition:
            while True:
                self._tick()
                if all(slot is None or slot.closed for slot in self.slots) or self.clock() >= deadline:
                    break
                self._wait(deadline)
            self._tick()
            if self.clock() >= deadline and self.cleanup_pending():
                self.flush_expired = True
            self.request_cleanup()
            remaining = max(0, deadline - self.clock()) / 1_000_000_000
        # Joining is not resource I/O and creates no replacement work. Use both
        # the injected total deadline and one shared host join budget so test
        # clocks cannot accidentally multiply the timeout by the thread count.
        join_deadline = monotonic() + remaining
        threads = (*self.threads, *((self.monitor,) if self.monitor is not None else ()))
        for thread in threads:
            budget = min(max(0, deadline - self.clock()) / 1_000_000_000,
                         max(0, join_deadline - monotonic()))
            thread.join(budget)

    def cleanup_pending(self) -> bool:
        """Include unreclaimed descriptors and every still-live owned worker."""
        return (any(slot is not None and (not slot.closed or slot.close_failed) for slot in self.slots)
                or any(thread.is_alive() for thread in self.threads)
                or (self.monitor is not None and self.monitor.is_alive()))

    def reports(self, *, closing: bool) -> tuple[SinkReport, SinkReport]:
        assert self.cut is not None
        counts = self.queues.observe_cutpoint(self.cut).targets
        result: list[SinkReport] = []
        for slot, view, name in zip(self.slots[:2], counts, ("console", "file"), strict=True):
            reason = "NONE"
            status: Literal["FLUSHED", "DRAINED", "INCOMPLETE", "DISABLED"] = "DISABLED"
            flushed = 0
            if slot is not None:
                flushed = min(view.written, slot.flushed)
                incomplete = (view.dropped or view.unknown or view.pending_queued or view.pending_in_flight
                              or not slot.request_flushed
                              or (closing and (not slot.closed or slot.close_failed
                                  or (self.close_deadline is not None and slot.close_ended_at is not None
                                      and slot.close_ended_at >= self.close_deadline))))
                reason = (slot.request_reason or ("DEADLINE_EXCEEDED" if self.flush_expired and incomplete else
                          "DELIVERY_LOSS" if view.dropped or view.unknown else
                          "RESOURCE_CLOSE_FAILED" if slot.close_failed else
                          "DEADLINE_EXCEEDED" if incomplete else "NONE"))
                status = "INCOMPLETE" if reason != "NONE" else "DRAINED" if closing else "FLUSHED"
            result.append(SinkReport(cast(Literal["console", "file"], name), status, reason,
                                     view.written, flushed, view.dropped, view.unknown,
                                     view.pending_queued, view.pending_in_flight))
        return result[0], result[1]

    def _dispatch(self, slot: _Slot, job: _Job) -> None:
        assert slot.job is None and not slot.closed
        slot.job = job
        slot.deadline = self.clock() + self.timeout
        slot.publication = None
        slot.returned = slot.accounted = slot.expired = slot.shutdown_abandoned = False
        slot.timestamp = None
        slot.flush_covers_request = job == "flush" and self.flush_deadline is not None
        if job == "flush" and slot.name != "emergency":
            slot.flushing_written = self.queues.observe().sinks[0 if slot.name == "console" else 1].written_events
        self.condition.notify_all()

    def _invoke(self, slot: _Slot) -> tuple[str | None, int]:
        """Publish completion before returning; no exception text survives this call."""
        reason = None
        try:
            if slot.job == "write":
                assert slot.work is not None
                count = slot.port.write(slot.work.jsonl, slot.work.stream)
                if type(count) is not int or count != len(slot.work.jsonl):
                    reason = "WRITE_FAILED"
            elif slot.job == "emergency":
                assert slot.payload is not None
                count = slot.port.write(slot.payload, "stderr")
                if type(count) is not int or count != len(slot.payload):
                    reason = "WRITE_FAILED"
                if reason is None:
                    slot.port.flush()
            elif slot.job == "prepare":
                slot.port.prepare()
            elif slot.job == "flush":
                slot.port.flush()
            elif slot.job == "probe":
                slot.port.probe()
            elif slot.job == "close":
                slot.port.close()
        except BaseException as error:
            reason = ({"prepare": "RESOURCE_OPEN_FAILED", "flush": "FLUSH_FAILED",
                       "probe": "FILE_STATE_UNCONFIRMED" if slot.name == "file" else "IO_TIMEOUT",
                       "close": "RESOURCE_CLOSE_FAILED"}.get(slot.job or "", "WRITE_FAILED"))
            if type(error) is _ResourceFailure:
                allowed = ({"RESOURCE_CONFLICT", "RESOURCE_INVALID", "RESOURCE_OPEN_FAILED", "IO_TIMEOUT"}
                           if slot.job == "prepare" else
                           {"WRITE_FAILED", "FLUSH_FAILED", "ROTATION_FAILED", "RETENTION_FAILED",
                            "FILE_STATE_UNCONFIRMED", "IO_TIMEOUT", "RESOURCE_CLOSE_FAILED"})
                reason = error.reason if error.reason in allowed else (
                    "RESOURCE_INVALID" if slot.job == "prepare" else "FILE_STATE_UNCONFIRMED")
            clear_frames(error.__traceback__)
        with slot.publication_lock:
            slot.publication = (reason, self.clock())
            return slot.publication

    def _worker(self, slot: _Slot) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: (slot.job is not None and not slot.returned) or slot.closed)
                if slot.closed:
                    return
            reason, _ = self._invoke(slot)
            timestamp = None
            if slot.job == "write" and reason is None:
                try:
                    instant = self.resources.utc_clock()
                    if type(instant) is datetime and instant.tzinfo is timezone.utc:
                        timestamp = instant.isoformat(timespec="microseconds").removesuffix("+00:00") + "Z"
                except BaseException as error:
                    clear_frames(error.__traceback__)
            with self.condition:
                slot.timestamp = timestamp
                slot.returned = True
                self.condition.notify_all()
                self.condition.wait_for(lambda: not slot.returned or slot.closed)

    def _record_request_fault(self, slot: _Slot, reason: str) -> None:
        """Keep the first applicable obstacle without borrowing future health.

        Once a cut's writes and refresh are confirmed, later event faults affect
        health and subsequent cuts only. Close still requires resource recycling,
        so its reclamation I/O faults remain applicable after data is flushed.
        """
        if (self.cut is not None and slot.name != "emergency" and slot.request_reason is None
                and (not slot.request_flushed or (self.closing and slot.job == "close"))):
            slot.request_reason = reason

    def _fault(self, slot: _Slot, reason: str) -> None:
        if slot.last_reason is None:
            slot.last_reason = reason
        newly_faulted = slot.first_fault is None
        if newly_faulted:
            slot.first_fault = reason
        if slot.name == "emergency":
            self.increment("emergency_failed", 1 + len(self.emergencies))
            self.emergencies.clear()
        else:
            self._record_request_fault(slot, slot.first_fault or reason)
            self.queues.fault(slot.name, cast(_FaultReason, reason))
            if newly_faulted:
                self.emergency(_FaultEmergencyNeed(cast(_FaultReason, reason)))

    def _settle(self, slot: _Slot) -> None:
        if slot.job is None:
            return
        with slot.publication_lock:
            publication = slot.publication
            if publication is None and self.clock() < slot.deadline:
                return
            reason = "IO_TIMEOUT" if publication is None or publication[1] >= slot.deadline else publication[0]
            if not slot.accounted:
                slot.accounted = True
                slot.expired = reason == "IO_TIMEOUT"
                if slot.job == "prepare":
                    slot.prepare_reason = reason
                    slot.prepared = reason is None
                elif slot.job == "close":
                    slot.close_failed = reason is not None
                    if reason == "IO_TIMEOUT" and slot.first_fault is None:
                        slot.first_fault = reason
                        slot.last_reason = reason
                    if reason == "IO_TIMEOUT":
                        self._record_request_fault(slot, slot.first_fault or reason)
                elif slot.job == "probe":
                    if reason is None and not self.closing:
                        slot.last_reason = None
                        slot.first_fault = None
                    elif slot.name == "file":
                        slot.last_reason = "FILE_STATE_UNCONFIRMED"
                        self._record_request_fault(slot, slot.first_fault or slot.last_reason)
                elif reason is not None:
                    self._fault(slot, reason)
                elif slot.job == "write":
                    assert slot.work is not None
                    self.queues.publish_written(slot.work)
                elif slot.job == "flush" and not slot.shutdown_abandoned:
                    assert publication is not None
                    if (slot.flush_covers_request and self.report_deadline is not None
                            and publication[1] < self.report_deadline):
                        slot.request_flushed = True
                    slot.flushed = max(slot.flushed, slot.flushing_written)
                    slot.flush_count = min(_MAX_INTEGER, slot.flush_count + 1)
                    self.saturated |= slot.flush_count == _MAX_INTEGER
        if not slot.returned:
            return
        if slot.job == "write":
            assert slot.work is not None and publication is not None
            self.queues._finish(slot.work, succeeded=publication[0] is None, timestamp=slot.timestamp)
            slot.work = None
        elif slot.job == "probe" and slot.last_reason is None and not self.closing:
            if slot.name != "emergency":
                slot.recovered_losses = self.queues.recover(slot.name)
        elif slot.job == "close":
            slot.closed = True
            slot.close_ended_at = publication[1] if publication is not None else None
            # A timeout is no longer cleanup_pending after confirmed late close.
            slot.close_failed = publication is None or publication[0] is not None
        slot.probe_after = self.clock() + self.probe_interval
        slot.payload = None
        slot.job = None
        slot.flush_covers_request = False
        slot.returned = False
        slot.publication = None
        self.condition.notify_all()

    def _tick(self) -> None:
        closing_now = (self.close_deadline is not None and self.clock() >= self.close_deadline
                       and not self.abandoned)
        if closing_now:
            assert self.close_deadline is not None
            # Observe known completions and I/O deadlines that precede the close
            # cutoff first. Later completion cannot turn close-abandoned work
            # into written, even if the coordinator itself was descheduled.
            for slot in self.slots:
                if slot is None or slot.job is None:
                    continue
                with slot.publication_lock:
                    publication = slot.publication
                    before_close = (publication is not None and publication[1] < self.close_deadline)
                if before_close or slot.deadline < self.close_deadline:
                    self._settle(slot)
            self.flush_expired = True
            self.request_cleanup()
            for slot in self.slots:
                if slot is not None and slot.job is not None:
                    slot.shutdown_abandoned = True
                    if slot.job == "emergency" and not slot.accounted:
                        self.increment("emergency_failed")
                        slot.accounted = True
        for slot in self.slots:
            if slot is not None:
                self._settle(slot)

    def _select(self, slot: _Slot, index: int) -> None:
        if slot.job is not None or slot.closed:
            return
        if slot.cleanup_requested:
            self._dispatch(slot, "close")
            return
        if not self.ready:
            return
        if slot.name == "emergency":
            if self.emergencies:
                slot.payload = self.emergencies.popleft()
                self._dispatch(slot, "emergency")
            elif self.closing:
                slot.cleanup_requested = True
                self._dispatch(slot, "close")
            return
        if slot.last_reason is not None:
            if self.closing:
                slot.cleanup_requested = True
                self._dispatch(slot, "close")
            elif self.clock() >= slot.probe_after:
                self._dispatch(slot, "probe")
            return
        if self.cut is not None and self.flush_deadline is not None and self.clock() < self.flush_deadline:
            view = self.queues.observe_cutpoint(self.cut).targets[index]
            if not view.pending_queued and not view.pending_in_flight:
                if not slot.request_flushed:
                    self._dispatch(slot, "flush")
                    return
                if self.closing:
                    slot.cleanup_requested = True
                    self._dispatch(slot, "close")
                    return
        # Once closing drain finished, cleanup also proceeds after wait_flush
        # removes its request. Never start a post-deadline write or probe.
        if self.closing and self.flush_deadline is None:
            slot.cleanup_requested = True
            self._dispatch(slot, "close")
            return
        work = self.queues.begin(slot.name)
        if work is not None:
            slot.work = work
            self._dispatch(slot, "write")

    def _coordinate(self) -> None:
        while True:
            recovered: list[int] = []
            with self.condition:
                self._tick()
                for index, slot in enumerate(self.slots):
                    if slot is not None:
                        if slot.recovered_losses is not None:
                            recovered.append(slot.recovered_losses)
                            slot.recovered_losses = None
                        self._select(slot, index)
                if all(slot is None or slot.closed for slot in self.slots):
                    return
                deadlines = [slot.deadline for slot in self.slots
                             if slot is not None and slot.job is not None and not slot.accounted]
                if self.ready and not self.closing:
                    deadlines.extend(slot.probe_after for slot in self.slots[:2]
                                     if slot is not None and slot.job is None and slot.last_reason is not None)
                if self.close_deadline is not None and not self.abandoned:
                    deadlines.append(self.close_deadline)
                self.condition.notify_all()
                if not recovered:
                    self.condition.wait(None if not deadlines else max(0, min(deadlines) - self.clock()) / 1_000_000_000)
            for count in recovered:
                result = self.queues.offer("logging_service", {
                    "level": "WARNING", "event_code": "DELIVERY_RECOVERED", "attributes": {"count": count},
                })
                self.emergency(result.emergency_need)
                self.wake()
