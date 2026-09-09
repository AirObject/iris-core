"""Bounded accounting and immutable internal queue observations.

Counters have fixed labels and saturate; observations contain no event bodies.
Cutpoints describe admitted targets only and carry no flush or close promise.
Work items borrow immutable queue bytes until the consumer notifies completion.
"""

from dataclasses import dataclass
from typing import Literal

from ._results import _Failure
from ._rules import _LEVEL_NUMBERS, _MAX_INTEGER
from ._routing import _Thresholds

type _SinkName = Literal["console", "file"]
type _DropReason = Literal["QUEUE_FULL", "SINK_UNAVAILABLE", "SHUTDOWN_DROPPED"]
type _FaultReason = Literal[
    "WRITE_FAILED", "FLUSH_FAILED", "ROTATION_FAILED", "RETENTION_FAILED", "IO_TIMEOUT",
    "FILE_STATE_UNCONFIRMED", "RESOURCE_CLOSE_FAILED",
]
type _Disposition = Literal["DISABLED", "FILTERED", "ENQUEUED", "DROPPED"]
type _DeliveryReason = Literal[
    "NONE", "SINK_DISABLED", "MODULE_THRESHOLD", "SINK_THRESHOLD",
    "QUEUE_FULL", "SINK_UNAVAILABLE", "SHUTDOWN_DROPPED",
]
type _CountName = Literal["accepted", "written", "dropped", "unknown", "filtered", "rejected"]
_DROP_REASONS: tuple[_DropReason, ...] = ("QUEUE_FULL", "SINK_UNAVAILABLE", "SHUTDOWN_DROPPED")
_FAULT_REASONS: tuple[_FaultReason, ...] = (
    "WRITE_FAILED", "FLUSH_FAILED", "ROTATION_FAILED", "RETENTION_FAILED", "IO_TIMEOUT",
    "FILE_STATE_UNCONFIRMED", "RESOURCE_CLOSE_FAILED",
)
_COUNT_NAMES: tuple[_CountName, ...] = ("accepted", "written", "dropped", "unknown", "filtered", "rejected")


@dataclass(frozen=True, slots=True)
class _DeliveryDecision:
    """One internal target decision; ENQUEUED conveys bounded ownership only."""

    sink: _SinkName
    disposition: _Disposition
    reason: _DeliveryReason
    stream: Literal["stdout", "stderr"] | None


@dataclass(frozen=True, slots=True)
class _EmergencyNeed:
    """Safe input for a future single emergency attempt, never a sent receipt."""

    event_id: str
    level_name: str
    reason: Literal["QUEUE_FULL", "SINK_UNAVAILABLE"]
    message: Literal["高等级诊断未完成常规投递。"] = "高等级诊断未完成常规投递。"


@dataclass(frozen=True, slots=True)
class _Admission:
    """Body-free internal admission result with at most one emergency need."""

    event_id: str
    level_name: str
    targets: tuple[_DeliveryDecision, _DeliveryDecision]
    emergency_need: _EmergencyNeed | None


@dataclass(frozen=True, slots=True)
class _ConstantEmergencyNeed:
    """Safe reason for an ID-free notice; no delivery attempt has occurred."""

    level_name: str
    reason: Literal["ADMISSION_BUSY", "EVENT_BUILD_FAILED", "FORMAT_FAILED"]


@dataclass(frozen=True, slots=True)
class _RejectedAdmission:
    """Internal refusal plus optional constant-notice input; no event receipt."""

    failure: _Failure
    emergency_need: _ConstantEmergencyNeed | None


@dataclass(frozen=True, slots=True, eq=False)
class _Work:
    """Identity-based completion handle; no retries or retained completion ledger.

    The trusted consumer may borrow jsonl until actual operation completion,
    including after UNKNOWN. It must release the handle afterwards. Passing an
    old or foreign handle cannot complete a different operation, even if IDs or
    payloads match. The controller never launches an output operation itself.
    """

    sink: _SinkName
    level_name: str
    jsonl: bytes
    stream: Literal["stdout", "stderr"] | None


@dataclass(frozen=True, slots=True, eq=False)
class _Cutpoint:
    """Opaque identity for the one current observation boundary; no event list."""


@dataclass(frozen=True, slots=True)
class _TargetCounts:
    """Disjoint admitted-target categories; pending counts are current occupancy."""

    accepted: int
    written: int
    dropped: int
    unknown: int
    pending_queued: int
    pending_in_flight: int


@dataclass(frozen=True, slots=True)
class _CutView:
    """An immutable observation of console then file, without refresh claims."""

    targets: tuple[_TargetCounts, _TargetCounts]
    counters_saturated: bool


@dataclass(frozen=True, slots=True)
class _SinkView:
    """Memory-only sink state; no resource health or persistence assertion."""

    sink: _SinkName
    state: Literal["DISABLED", "READY", "FAULTED"]
    last_reason: _FaultReason | Literal["NONE"]
    capacity: int
    warning_reserve: int
    queued_events: int
    in_flight_events: int
    encoded_bytes: int
    written_events: int
    unknown_events: int
    filtered_events: int
    dropped_events: tuple[tuple[str, _DropReason, int], ...]
    last_success_at: str | None


@dataclass(frozen=True, slots=True)
class _QueueView:
    """Fixed-size observation, with no mutable maps, records, or queue handles."""

    accepting: bool
    preparation_capacity: int
    preparing: int
    preparing_low: int
    rejected_events: int
    thresholds: _Thresholds
    sinks: tuple[_SinkView, _SinkView]
    counters_saturated: bool


class _Counters:
    """Finite counter vocabulary; callers synchronize updates and observations."""

    def __init__(self):
        self.values: dict[_CountName, int] = dict.fromkeys(_COUNT_NAMES, 0)
        self.drops: dict[tuple[str, _DropReason], int] = {
            (level, reason): 0 for level in _LEVEL_NUMBERS for reason in _DROP_REASONS
        }
        self.saturated = False

    def copy(self) -> "_Counters":
        """Stage independent fixed-size accounting before publishing mutations.

        Integer/tuple values are immutable. Either mapping copy may allocate;
        a failure leaves the original values, labels and saturation unchanged.
        """
        staged = _Counters()
        staged.values = self.values.copy()
        staged.drops = self.drops.copy()
        staged.saturated = self.saturated
        return staged

    def add(self, name: _CountName) -> None:
        """Increment once, marking saturation as soon as the ceiling is reached."""
        self.values[name] = self._next(self.values[name])

    def drop(self, level: str, reason: _DropReason) -> None:
        """Increment an existing fixed label; never insert caller-controlled keys."""
        key = (level, reason)
        self.drops[key] = self._next(self.drops[key])

    def _next(self, value: int) -> int:
        if value >= _MAX_INTEGER - 1:
            self.saturated = True
            return _MAX_INTEGER
        return value + 1
