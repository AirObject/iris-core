"""Coordinate bounded normalization and independent console/file target queues.

A short memory-only lock serializes admission, faults, completion and cutpoints.
No event source callback or output operation runs under it. Both target decisions
commit before stop_accepting can take its boundary. Preparation owns at most one
E-byte encoding; targets share that immutable allocation without copying it.
Each sink holds at most Q targets, including its sole in-flight UNKNOWN target.
Only one cutpoint accumulator and fixed counter labels are retained: there is
no completed-event ledger, replay queue, executor, timer, flush or close facade.
"""

from collections import deque
from _thread import RLock
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from traceback import clear_frames
from typing import Literal, cast

from ._delivery_state import (
    _Admission, _ConstantEmergencyNeed, _Counters, _Cutpoint, _CutView, _DeliveryDecision, _DropReason,
    _EmergencyNeed, _FAULT_REASONS, _FaultReason, _QueueView, _SinkName, _SinkView,
    _RejectedAdmission, _TargetCounts, _Work,
)
from ._encoding import _EncodedEvent, _encode_event
from ._events import _Header, _build_record, _check_header, _normalize_fields
from ._results import _Failure
from ._routing import _check_module, _resolve_thresholds, _route_encoded
from ._rules import _LEVEL_NUMBERS
from ._settings import _Settings


@dataclass(slots=True)
class _Pending:
    """One retained target; tracked marks membership of the current cutpoint."""

    work: _Work
    tracked: bool = False
    unknown: bool = False


class _Sink:
    """Mutable state accessed only with the owning controller's lock held."""

    def __init__(self, name: _SinkName, enabled: bool):
        self.name: _SinkName = name
        self.enabled = enabled
        self.queue: deque[_Pending] = deque()
        self.in_flight: _Pending | None = None
        self.fault_reason: _FaultReason | None = None
        self.last_success_at: str | None = None
        self.counts = _Counters()
        self.cut_counts = _Counters()

    def terminal(self, pending: _Pending, name: Literal["written", "dropped", "unknown"]) -> None:
        if name not in ("written", "dropped", "unknown"):
            raise ValueError("A fixed terminal category is required.")
        self.counts.add(name)
        if pending.tracked:
            self.cut_counts.add(name)

    def discard_queued(self, reason: _DropReason) -> None:
        while self.queue:
            pending = self.queue.popleft()
            self.counts.drop(pending.work.level_name, reason)
            self.terminal(pending, "dropped")

    def mark_unknown(self) -> None:
        if self.in_flight is not None and not self.in_flight.unknown:
            self.in_flight.unknown = True
            self.terminal(self.in_flight, "unknown")


class _QueueController:
    """Internal in-memory owner, constructed from already checked settings.

    Sources are trusted, bounded UUID/UTC callbacks. offer requires a previously
    checked module and stable input during the call. begin returns a borrowed
    work item for a trusted external consumer; finish must be called only when
    that operation has actually ended. stop_accepting closes admission only:
    queued work may still begin, and no drain, resource release or flush is
    promised. No method performs IO, schedules emergency delivery or recovers a
    faulted sink. READY here means queue eligibility, not initialized resources.
    """

    def __init__(
        self, settings: _Settings, id_source: Callable[[], object],
        utc_clock: Callable[[], object],
        *, coordination_lock: RLock | None = None,
    ):
        self._settings = settings
        self._id_source = id_source
        self._utc_clock = utc_clock
        self._thresholds = _resolve_thresholds(settings)
        # An asynchronous owner can share this memory-only lock so releasing a
        # borrowed work reference and its queue capacity is one transaction.
        self._lock = Lock() if coordination_lock is None else coordination_lock
        self._accepting = True
        self._preparing = 0
        self._preparing_low = 0
        self._counts = _Counters()
        self._sinks = (_Sink("console", settings.console_enabled),
                       _Sink("file", settings.file_enabled))
        self._cutpoint: _Cutpoint | None = None
        self._stop_cutpoint: _Cutpoint | None = None

    def offer(self, module: str, event: object) -> _Admission | _RejectedAdmission:
        """Validate, reserve preparation, encode and atomically decide both sinks.

        Ordinary validation/build failures count as rejected, never target loss.
        Full preparation slots do not call either source. Process/allocation
        exceptions propagate, with preparation ownership released in finally.
        A stopped entrance takes precedence over inspecting the supplied event.
        The caller binds a checked module before using this internal operation.
        """
        with self._lock:
            if not self._accepting:
                return self._closed()
        if isinstance(_check_module(module), _Failure):
            raise ValueError("A previously checked module is required.")
        header = _check_header(event)
        if isinstance(header, _Failure):
            return self._rejected(header)
        low = header.level_number < _LEVEL_NUMBERS["WARNING"]
        with self._lock:
            if not self._accepting:
                return self._closed()
            # Low work uses only the ordinary P-1 slots. High work can also
            # use the reserved slot, while all work together remains at most P.
            if (self._preparing >= self._settings.preparation_capacity
                    or (low and self._preparing_low >= self._settings.preparation_capacity - 1)):
                self._counts.add("rejected")
                return _RejectedAdmission(
                    _Failure("ADMISSION_REJECTED", "emit", "event", "ADMISSION_BUSY"),
                    None if low else _ConstantEmergencyNeed(header.level_name, "ADMISSION_BUSY"),
                )
            self._preparing += 1
            self._preparing_low += int(low)
        try:
            # The helper frame releases its encoded reference before this slot
            # is returned, including on filtered, stopped or exceptional paths.
            return self._prepare_and_offer(module, cast(dict[str, object], event), header)
        except BaseException as error:
            # Clear completed frames so escaping process/allocation exceptions
            # cannot retain an unowned encoded payload through their traceback.
            clear_frames(error.__traceback__)
            raise
        finally:
            with self._lock:
                self._preparing -= 1
                self._preparing_low -= int(low)

    def _prepare_and_offer(self, module: str, event: dict[str, object], header: _Header) -> _Admission | _RejectedAdmission:
        """Own normalized state and bytes until rejection or dual admission."""
        safe = _normalize_fields(event, header)
        if isinstance(safe, _Failure):
            return self._rejected(safe)
        record = _build_record(safe, module, self._id_source, self._utc_clock)
        if isinstance(record, _Failure):
            return self._rejected(record, header.level_name)
        encoded = _encode_event(record, self._settings)
        if isinstance(encoded, _Failure):
            return self._rejected(encoded, header.level_name)
        try:
            return self._admit(encoded)
        finally:
            # An escaping exception's traceback must not retain the byte buffer
            # after offer releases its preparation slot.
            del encoded

    def _rejected(self, failure: _Failure, level: str | None = None) -> _RejectedAdmission:
        with self._lock:
            if not self._accepting:
                return self._closed()
            self._counts.add("rejected")
        need = None
        if level is not None and failure.reason in ("EVENT_BUILD_FAILED", "FORMAT_FAILED"):
            need = _ConstantEmergencyNeed(level, failure.reason)
        return _RejectedAdmission(failure, need)

    def _closed(self) -> _RejectedAdmission:
        self._counts.add("rejected")
        return _RejectedAdmission(_Failure("INVALID_STATE", "emit", "state", "SERVICE_CLOSED"), None)

    def _admit(self, encoded: _EncodedEvent) -> _Admission | _RejectedAdmission:
        staged: list[tuple[_Sink, _Pending]] = []
        pending: _Pending | None = None
        try:
            routes = _route_encoded(encoded, self._settings)
            with self._lock:
                if not self._accepting:
                    return self._closed()
                decisions: list[_DeliveryDecision] = []
                emergency: _EmergencyNeed | None = None
                for sink, route in zip(self._sinks, routes, strict=True):
                    if route.disposition != "ELIGIBLE":
                        decisions.append(_DeliveryDecision(
                            sink.name, route.disposition, route.reason, route.stream,
                        ))
                        continue
                    limit = self._settings.sink_capacity
                    if encoded.record.level_number < _LEVEL_NUMBERS["WARNING"]:
                        limit -= self._settings.warning_reserve
                    reason = ("SINK_UNAVAILABLE" if sink.fault_reason is not None else
                              "QUEUE_FULL" if len(sink.queue) + int(sink.in_flight is not None) >= limit
                              else None)
                    if reason is not None:
                        decisions.append(_DeliveryDecision(sink.name, "DROPPED", reason, route.stream))
                        if encoded.record.level_number >= _LEVEL_NUMBERS["WARNING"] and emergency is None:
                            emergency = _EmergencyNeed(encoded.record.event_id, encoded.record.level_name, reason)
                    else:
                        decisions.append(_DeliveryDecision(sink.name, "ENQUEUED", "NONE", route.stream))
                        staged.append((sink, _Pending(_Work(
                            sink.name, encoded.record.level_name, encoded.jsonl, route.stream,
                        ))))
                result = _Admission(encoded.record.event_id, encoded.record.level_name,
                                    (decisions[0], decisions[1]), emergency)
                # Counter increments can allocate. Prepare both complete banks
                # without modifying live accounting, including saturation and
                # filtered/dropped labels, before either queue takes ownership.
                original_counts = (self._sinks[0].counts, self._sinks[1].counts)
                prepared_counts = (original_counts[0].copy(), original_counts[1].copy())
                for counts, decision in zip(prepared_counts, decisions, strict=True):
                    if decision.disposition == "ENQUEUED":
                        counts.add("accepted")
                    elif decision.disposition == "FILTERED":
                        counts.add("filtered")
                    elif decision.disposition == "DROPPED":
                        counts.drop(encoded.record.level_name, cast(_DropReason, decision.reason))
                # Publish only after all allocating counter work succeeds.
                # Existing attribute replacements do not grow the banks. Keep
                # queue append and publication inside one rollback boundary.
                try:
                    for sink, pending in staged:
                        sink.queue.append(pending)
                    self._sinks[0].counts = prepared_counts[0]
                    self._sinks[1].counts = prepared_counts[1]
                except BaseException:
                    self._sinks[0].counts = original_counts[0]
                    self._sinks[1].counts = original_counts[1]
                    for sink, pending in staged:
                        if sink.queue and sink.queue[-1] is pending:
                            sink.queue.pop()
                    raise
                return result
        finally:
            # Escaping failures must not retain staged payloads after slot
            # release or admission rollback.
            staged.clear()
            del pending
            del encoded

    def begin(self, name: _SinkName) -> _Work | None:
        """Take the oldest target, retaining capacity until actual completion.

        At most one operation per sink can begin. Faulted and disabled queues
        produce no work; the other sink remains independently available.
        """
        with self._lock:
            sink = self._sink(name)
            if not sink.enabled or sink.fault_reason is not None or sink.in_flight is not None or not sink.queue:
                return None
            sink.in_flight = sink.queue.popleft()
            return sink.in_flight.work

    def finish(self, work: _Work, *, succeeded: bool, completed_at: datetime | None = None) -> bool:
        """Acknowledge actual operation end once; stale/foreign handles return false.

        Successful completion requires an exact UTC timestamp from the consumer.
        Failure marks an unconfirmed write UNKNOWN and faults/drains that sink.
        Late success releases capacity and updates actual success time, while
        preserving UNKNOWN classification and fault state. No flush is inferred.
        """
        if type(succeeded) is not bool:
            raise TypeError("A boolean completion outcome is required.")
        timestamp = None
        if succeeded:
            if type(completed_at) is not datetime or completed_at.tzinfo is not timezone.utc:
                raise TypeError("Successful completion requires an exact UTC datetime.")
            timestamp = completed_at.isoformat(timespec="microseconds").removesuffix("+00:00") + "Z"
        return self._finish(work, succeeded=succeeded, timestamp=timestamp)

    def _finish(self, work: _Work, *, succeeded: bool, timestamp: str | None) -> bool:
        """Apply actual completion, optionally lacking a UTC observation.

        The asynchronous consumer can confirm a full write even if its trusted
        UTC source failed. An absent time preserves the last known observation;
        it does not invent a timestamp or change the physical write outcome.
        """
        with self._lock:
            sink = self._sink(work.sink)
            pending = sink.in_flight
            if pending is None or pending.work is not work:
                return False
            if succeeded:
                if timestamp is not None:
                    sink.last_success_at = timestamp
                if not pending.unknown:
                    sink.terminal(pending, "written")
            else:
                self._fault(sink, "WRITE_FAILED")
            sink.in_flight = None
            return True

    def fault(self, name: _SinkName, reason: _FaultReason) -> None:
        """Record an externally established IO fault, without declaring IO ended.

        The first reason remains visible. Pending targets are dropped once;
        in-flight work becomes UNKNOWN and retains bytes/capacity until finish.
        This is an accounting notification, not a timer or recovery probe.
        """
        if type(reason) is not str or reason not in _FAULT_REASONS:
            raise ValueError("A fixed IO fault reason is required.")
        with self._lock:
            sink = self._sink(name)
            if sink.enabled:
                self._fault(sink, reason)

    def _fault(self, sink: _Sink, reason: _FaultReason) -> None:
        if sink.fault_reason is None:
            sink.fault_reason = reason
        sink.mark_unknown()
        sink.discard_queued("SINK_UNAVAILABLE")

    def capture_cutpoint(self) -> _Cutpoint:
        """Replace the single internal boundary; older tokens become invalid.

        Serial lifecycle consumers retain returned immutable observations, not
        an unbounded collection of live report trackers. After stopping, this
        returns the fixed stop boundary. It does not wait or request a flush.
        """
        with self._lock:
            if self._stop_cutpoint is not None:
                return self._stop_cutpoint
            return self._capture()

    def _capture(self) -> _Cutpoint:
        token = _Cutpoint()
        # Allocate both snapshots before replacing either accumulator.
        values = tuple(sink.counts.values.copy() for sink in self._sinks)
        for sink, counts in zip(self._sinks, values, strict=True):
            sink.cut_counts.values = counts
            sink.cut_counts.saturated = sink.counts.saturated
            for pending in sink.queue:
                pending.tracked = True
            if sink.in_flight is not None:
                sink.in_flight.tracked = True
        self._cutpoint = token
        return token

    def stop_accepting(self) -> _Cutpoint:
        """Atomically stop new admission and capture both sinks; idempotent.

        Preparations without delivery qualification will return SERVICE_CLOSED.
        Previously qualified events already have both target decisions. Existing
        targets remain owned and may be processed; this is not complete close.
        """
        with self._lock:
            if self._stop_cutpoint is None:
                self._stop_cutpoint = self._capture()
                self._accepting = False
            return self._stop_cutpoint

    def observe_cutpoint(self, token: _Cutpoint) -> _CutView:
        """Observe the current boundary without retaining history or waiting."""
        with self._lock:
            if token is not self._cutpoint:
                raise ValueError("The cutpoint is not the current internal boundary.")
            return _CutView((self._cut_view(self._sinks[0]), self._cut_view(self._sinks[1])),
                            self._saturated())

    def _cut_view(self, sink: _Sink) -> _TargetCounts:
        values = sink.cut_counts.values
        pending = sink.in_flight
        return _TargetCounts(values["accepted"], values["written"], values["dropped"], values["unknown"],
                             sum(item.tracked for item in sink.queue),
                             int(pending is not None and pending.tracked and not pending.unknown))

    def observe(self) -> _QueueView:
        """Copy fixed-size immutable accounting; excludes event text and paths."""
        with self._lock:
            return _QueueView(self._accepting, self._settings.preparation_capacity,
                              self._preparing, self._preparing_low, self._counts.values["rejected"],
                              self._thresholds, (self._sink_view(self._sinks[0]), self._sink_view(self._sinks[1])),
                              self._saturated())

    def _sink_view(self, sink: _Sink) -> _SinkView:
        in_flight = sink.in_flight
        return _SinkView(
            sink.name, "DISABLED" if not sink.enabled else "FAULTED" if sink.fault_reason else "READY",
            sink.fault_reason or "NONE", self._settings.sink_capacity, self._settings.warning_reserve,
            len(sink.queue), int(in_flight is not None),
            sum(len(item.work.jsonl) for item in sink.queue)
            + (len(in_flight.work.jsonl) if in_flight is not None else 0),
            sink.counts.values["written"], sink.counts.values["unknown"], sink.counts.values["filtered"],
            tuple((level, reason, count) for (level, reason), count in sink.counts.drops.items()),
            sink.last_success_at,
        )

    def _saturated(self) -> bool:
        return self._counts.saturated or any(sink.counts.saturated or sink.cut_counts.saturated
                                             for sink in self._sinks)

    def _sink(self, name: _SinkName) -> _Sink:
        if type(name) is not str or name not in ("console", "file"):
            raise ValueError("A fixed sink name is required.")
        return self._sinks[0 if name == "console" else 1]
