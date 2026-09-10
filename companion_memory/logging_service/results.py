"""Immutable public runtime diagnostic results, without payloads or resource paths.

A receipt confirms admission only. Reports distinguish full writes from actual
buffer flushes, and never promise durable storage. Returned observations cannot
change when outstanding I/O eventually finishes.
"""

from dataclasses import dataclass
from typing import Literal

from ._delivery_state import _DeliveryDecision
from ._routing import _Thresholds

type Lifecycle = Literal["NEW", "READY", "FAULTED", "CLOSING", "CLOSED"]
type EmergencyDisposition = Literal["NOT_NEEDED", "SCHEDULED", "SUPPRESSED", "UNAVAILABLE"]
type Operation = Literal["initialize", "get_logger", "emit", "flush"]


@dataclass(frozen=True, slots=True)
class LoggingError:
    """A fixed first error; cleanup_pending applies only to failed initialization."""

    code: str
    operation: Operation
    field: str
    reason: str
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True)
class LoggingOk[T]:
    """Successful operation result; incomplete delivery remains explicit in reports."""

    value: T


@dataclass(frozen=True, slots=True)
class LoggingErr:
    """Expected safe failure with no input values, exceptions, or partial receipt."""

    error: LoggingError


type LoggingResult[T] = LoggingOk[T] | LoggingErr
DeliveryDecision = _DeliveryDecision
Thresholds = _Thresholds


@dataclass(frozen=True, slots=True)
class EmitReceipt:
    """Console/file decisions and at most one scheduled safe emergency summary."""

    event_id: str
    targets: tuple[DeliveryDecision, DeliveryDecision]
    emergency: EmergencyDisposition


@dataclass(frozen=True, slots=True)
class SinkReport:
    """Mutually exclusive admitted-target categories at one atomic boundary."""

    sink: Literal["console", "file"]
    status: Literal["FLUSHED", "DRAINED", "INCOMPLETE", "DISABLED"]
    reason: str
    written: int
    flushed: int
    dropped: int
    unknown: int
    pending_queued: int
    pending_in_flight: int


@dataclass(frozen=True, slots=True)
class FlushReport:
    """One shared waiting deadline, with no abandonment or persistence promise."""

    sinks: tuple[SinkReport, SinkReport]
    counters_saturated: bool


@dataclass(frozen=True, slots=True)
class CloseReport:
    """First and final close observation; late cleanup never rewrites this value."""

    sinks: tuple[SinkReport, SinkReport]
    cleanup_pending: bool
    counters_saturated: bool


@dataclass(frozen=True, slots=True)
class SinkHealth:
    """Memory-only health and fixed-label counters for one output."""

    sink: Literal["console", "file"]
    state: Literal["DISABLED", "READY", "FAULTED", "CLOSED"]
    last_reason: str
    threshold: int | None
    capacity: int | None
    queued_events: int
    in_flight_events: int
    last_success_at: str | None
    written_events: int
    flushed_events: int
    unknown_events: int
    dropped_events: tuple[tuple[str, str, int], ...]
    filtered_events: int
    disk_space: Literal["OK", "LOW", "UNKNOWN"]


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Safe deep immutable lifecycle, thresholds, counters and cleanup ownership."""

    lifecycle: Lifecycle
    sinks: tuple[SinkHealth, SinkHealth]
    thresholds: Thresholds | None
    filtered_events: int
    rejected_events: int
    emergency_suppressed: int
    emergency_failed: int
    flush_deadline_exceeded: int
    cleanup_pending: bool
    counters_saturated: bool
