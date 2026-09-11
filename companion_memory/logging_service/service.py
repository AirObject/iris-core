"""Public console/file diagnostic service assembled from bounded internal owners.

Create without I/O, initialize from a checked native configuration snapshot and
explicit trusted resources, then share restricted loggers with internal callers.
Lifecycle operations are serial; emit and memory health queries are concurrent.
Close returns one immutable report and never closes borrowed host streams.
"""

from dataclasses import dataclass
from threading import RLock
from typing import cast

from .runtime_window import RuntimeLogWindow
from ._delivery_state import _RejectedAdmission
from ._execution import _Execution
from ._results import _Failure
from ._routing import _check_module
from ._rules import _MAX_INTEGER
from ._service_settings import _service_settings, _ServiceSettings
from .resources import LoggingResources, _ConsoleResource, _FileResource, _Resource, _resource_shape
from .results import (
    CloseReport, EmitReceipt, FlushReport, HealthSnapshot, Lifecycle, LoggingErr,
    LoggingError, LoggingOk, LoggingResult, Operation, SinkHealth, SinkReport,
)


@dataclass(frozen=True, slots=True)
class Logger:
    """Restricted module-bound emit capability; closing its service invalidates it.

    Each event must remain stable during emit; retained fields and JSONL acquire
    independent immutable ownership before return. No lifecycle or file API is
    provided by this handle. It is not a malicious-code sandbox.
    """

    _service: "Service"
    _module: str

    def emit(self, event: object) -> LoggingResult[EmitReceipt]:
        """Validate and admit in bounded memory without waiting for output I/O."""
        return self._service._emit(self._module, event)


class Service:
    """Serial lifecycle owner with thread-safe logger and health publication."""

    def __init__(self, *, observation_window: RuntimeLogWindow | None = None):
        if observation_window is not None and type(observation_window) is not RuntimeLogWindow:
            raise TypeError("A native optional diagnostic observation output is required.")
        self._observation_window = observation_window
        self._lock = RLock()
        self._state: Lifecycle = "NEW"
        self._execution: _Execution | None = None
        self._settings: _ServiceSettings | None = None
        self._close_report: CloseReport | None = None
        self._rejected = 0

    def _state_error(self, operation: Operation) -> LoggingErr | None:
        if self._state == "READY":
            reason = "ALREADY_INITIALIZED" if operation == "initialize" else None
        elif self._state == "NEW":
            reason = None if operation == "initialize" else "NOT_INITIALIZED"
        else:
            reason = "SERVICE_FAULTED" if self._state == "FAULTED" else "SERVICE_CLOSED"
        return None if reason is None else LoggingErr(LoggingError("INVALID_STATE", operation, "state", reason))

    def initialize(self, snapshot: object, resources: object) -> LoggingResult[None]:
        """Publish READY only after complete configuration and resource preparation.

        Failures clean up only this initialization's resources. A timed-out or
        failed reclamation leaves FAULTED with cleanup_pending, permitting only
        health and close. Successful repeated initialization is always refused.
        """
        with self._lock:
            error = self._state_error("initialize")
            if error is not None:
                return error
        settings = _service_settings(snapshot)
        if isinstance(settings, LoggingErr):
            return settings
        if not _resource_shape(resources, settings.event.console_enabled and settings.event.console_stream == "split"):
            return LoggingErr(LoggingError("INITIALIZATION_FAILED", "initialize", "resources", "RESOURCE_INVALID"))
        supplied = cast(LoggingResources, resources)
        console, file, emergency = self._make_ports(settings, supplied)
        execution = _Execution(settings, supplied, console, file, emergency)
        execution.queues.observation_window = self._observation_window
        with self._lock:
            self._execution = execution
            self._settings = settings
        try:
            execution.start()
            reason = execution.prepare()
        except (RuntimeError, OSError):
            reason = "RESOURCE_OPEN_FAILED"
        if reason is not None:
            execution.request_cleanup()
            deadline = execution.clock() + execution.timeout
            execution.finish_close(deadline)
            with execution.condition:
                pending = execution.cleanup_pending()
            with self._lock:
                self._state = "FAULTED" if pending else "NEW"
                if not pending:
                    self._execution = None
                    self._settings = None
            return LoggingErr(LoggingError("INITIALIZATION_FAILED", "initialize", "resources", reason, pending))
        with self._lock:
            self._state = "READY"
        return LoggingOk(None)

    def _make_ports(self, settings: _ServiceSettings, resources: LoggingResources
                    ) -> tuple[_Resource | None, _Resource | None, _Resource]:
        """Construct inert adapters; preparation is performed exclusively by workers."""
        console = (_ConsoleResource(resources, settings.event.console_stream == "split")
                   if settings.event.console_enabled else None)
        file = (_FileResource(settings.file_directory, resources.protected_directories,
                              settings.rotation_bytes, settings.retained_segments, settings.event.event_max_bytes)
                if settings.event.file_enabled else None)
        return console, file, _ConsoleResource(resources, False)

    def get_logger(self, module: object) -> LoggingResult[Logger]:
        """Bind only an exact allowed module, after lifecycle checks, without I/O."""
        with self._lock:
            error = self._state_error("get_logger")
            if error is not None:
                return error
            checked = _check_module(module)
            if isinstance(checked, _Failure):
                return LoggingErr(LoggingError(checked.code, checked.operation, checked.field, checked.reason))
            return LoggingOk(Logger(self, checked))

    def _emit(self, module: str, event: object) -> LoggingResult[EmitReceipt]:
        with self._lock:
            error = self._state_error("emit")
            if error is not None:
                self._rejected = min(_MAX_INTEGER, self._rejected + 1)
                return error
            execution = self._execution
            assert execution is not None
        result = execution.queues.offer(module, event)
        emergency = execution.emergency(result.emergency_need)
        execution.wake()
        if isinstance(result, _RejectedAdmission):
            failure = result.failure
            return LoggingErr(LoggingError(failure.code, failure.operation, failure.field, failure.reason))
        return LoggingOk(EmitReceipt(result.event_id, result.targets, emergency))

    def flush(self) -> LoggingResult[FlushReport]:
        """Wait on one atomic cut and shared deadline; expiry abandons no event.

        Unstarted refresh requests are discarded at return. Already started I/O
        remains owned under its independent I/O deadline. No per-call background
        waiter survives this operation, and subsequent calls observe newer state.
        """
        with self._lock:
            error = self._state_error("flush")
            if error is not None:
                return error
            execution = self._execution
            assert execution is not None
        execution.begin_flush()
        reports = execution.wait_flush()
        with execution.condition:
            saturated = execution.saturated or execution.queues.observe().counters_saturated
        return LoggingOk(FlushReport(reports, saturated))

    def close(self) -> CloseReport:
        """Stop admission atomically, drain/flush/reclaim within one total budget.

        After the deadline only existing I/O and reclamation may continue. The
        first immutable report is returned by every later close, even if resource
        cleanup subsequently finishes. Outstanding ownership remains in health.
        """
        with self._lock:
            if self._close_report is not None:
                return self._close_report
            execution = self._execution
            self._state = "CLOSING"
        if execution is not None:
            execution.begin_flush(closing=True)
        if execution is None:
            report = CloseReport(self._empty_reports(), False, self._rejected == _MAX_INTEGER)
        else:
            execution.wait_flush()
            assert execution.close_deadline is not None
            execution.finish_close(execution.close_deadline)
            with execution.condition:
                execution._tick()
                execution.queues.abandon()
                report = CloseReport(execution.reports(closing=True), execution.cleanup_pending(),
                                     execution.saturated or execution.queues.observe().counters_saturated)
        with self._lock:
            self._close_report = report
            self._state = "CLOSED"
        return report

    @staticmethod
    def _empty_reports() -> tuple[SinkReport, SinkReport]:
        return (SinkReport("console", "DISABLED", "NONE", 0, 0, 0, 0, 0, 0),
                SinkReport("file", "DISABLED", "NONE", 0, 0, 0, 0, 0, 0))

    def get_sink_health(self) -> HealthSnapshot:
        """Return a deep immutable, memory-only view in every lifecycle state."""
        with self._lock:
            state, execution, rejected = self._state, self._execution, self._rejected
        if execution is None:
            sinks = tuple(SinkHealth(report.sink, "CLOSED" if state == "CLOSED" else "DISABLED", "NONE",
                                     None, None, 0, 0, None, 0, 0, 0, (), 0, "UNKNOWN")
                          for report in self._empty_reports())
            return HealthSnapshot(state, (sinks[0], sinks[1]), None, 0, rejected, 0, 0, 0, False,
                                  rejected == _MAX_INTEGER)
        with execution.condition:
            view = execution.queues.observe()
            health: list[SinkHealth] = []
            for slot, sink, threshold in zip(execution.slots[:2], view.sinks,
                                             (view.thresholds.console, view.thresholds.file), strict=True):
                health.append(SinkHealth(
                    sink.sink, "DISABLED" if slot is None else "CLOSED" if state == "CLOSED" else sink.state,
                    sink.last_reason if slot is None else slot.last_reason or ("RESOURCE_CLOSE_FAILED" if slot.close_failed else sink.last_reason),
                    threshold, sink.capacity, sink.queued_events, sink.in_flight_events,
                    sink.last_success_at, sink.written_events, 0 if slot is None else slot.flushed,
                    sink.unknown_events, sink.dropped_events, sink.filtered_events,
                    slot.port.disk_space if slot is not None and isinstance(slot.port, _FileResource) else "UNKNOWN",
                ))
            filtered = sum(sink.filtered_events for sink in view.sinks)
            total_rejected = rejected + view.rejected_events
            return HealthSnapshot(
                state, (health[0], health[1]), view.thresholds, min(_MAX_INTEGER, filtered),
                min(_MAX_INTEGER, total_rejected), execution.emergency_suppressed, execution.emergency_failed,
                execution.flush_deadline_exceeded, execution.closing and execution.cleanup_pending(),
                execution.saturated or view.counters_saturated or filtered >= _MAX_INTEGER
                or total_rejected >= _MAX_INTEGER,
            )


def create_logging_service(*, observation_window: RuntimeLogWindow | None = None) -> Service:
    """Create NEW with no I/O, worker, handler installation or ambient configuration."""
    return Service(observation_window=observation_window)
