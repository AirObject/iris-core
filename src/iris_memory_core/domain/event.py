"""CognitiveEvent domain rules: delivery lifecycle and lease semantics (§12).

Events are the host's to-do list. Delivery is at-least-once: the host must
deduplicate by Event ID. An ACK only records that the host RECEIVED the
event and accepted responsibility — it never completes a task, a step or an
external effect. A fenced holder that never ACKed gets the SAME event id
re-delivered to the new holder.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from iris_memory_core.domain.errors import InvalidRequestError

MAX_EVENT_KIND_CHARS = 128
MAX_DELIVERY_TARGET_CHARS = 256

#: Default visibility horizon of an undelivered event (§12.2 "按有效期").
DEFAULT_EVENT_TTL_US = 7 * 86_400_000_000  # one week

#: Events older than this many delivery attempts without an ACK are expired
#: by the sweep instead of being pulled forever.
DEFAULT_MAX_DELIVERY_ATTEMPTS = 50

#: Above this many expired events per sweep, the sweep folds them into ONE
#: bounded summary event instead of leaving a wall of expired rows (§12.2).
DEFAULT_SUMMARY_MERGE_THRESHOLD = 20


class EventStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


#: Delivery lifecycle. ``pending`` is the stable home state: delivered events
#: whose holder never ACKs return here on fence/expiry-check, and cancelled/
#: acknowledged/expired are terminal bookkeeping states.
EVENT_TRANSITIONS: dict[str, frozenset[str]] = {
    EventStatus.PENDING.value: frozenset(
        {EventStatus.DELIVERED.value, EventStatus.EXPIRED.value, EventStatus.CANCELLED.value}
    ),
    EventStatus.DELIVERED.value: frozenset(
        {
            EventStatus.ACKNOWLEDGED.value,
            EventStatus.PENDING.value,  # holder fenced without ACK: re-deliver
            EventStatus.EXPIRED.value,
            EventStatus.CANCELLED.value,
        }
    ),
    EventStatus.ACKNOWLEDGED.value: frozenset(),
    EventStatus.EXPIRED.value: frozenset(),
    EventStatus.CANCELLED.value: frozenset(),
}

#: Stable event kinds produced by the core in Phase 4.
EVENT_KIND_TASK_DUE = "task.due"
EVENT_KIND_TASK_TRANSITION = "task.transition"
EVENT_KIND_NOTE_REVIEW = "note.review"
EVENT_KIND_SUMMARY = "summary.expired_events"


class InvalidEventError(InvalidRequestError):
    """Raised when an event write violates the §12 contract."""


def validate_event_transition(current: str, target: str) -> None:
    if current not in EVENT_TRANSITIONS:
        raise InvalidEventError(f"unknown cognitive event status: {current!r}")
    if target not in EVENT_TRANSITIONS:
        raise InvalidEventError(f"unknown cognitive event status: {target!r}")
    if target not in EVENT_TRANSITIONS[current]:
        raise InvalidEventError(f"cognitive event cannot transition from {current!r} to {target!r}")


def event_scope_key(
    tenant_id: str,
    agent_id: str,
    space_group_id: str | None,
    space_id: str | None,
    session_id: str | None,
) -> str:
    return "|".join((tenant_id, agent_id, space_group_id or "", space_id or "", session_id or ""))


@dataclass(frozen=True, slots=True)
class CognitiveEventCurrent:
    id: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    kind: str
    object_type: str
    object_id: str
    occurrence_id: str | None
    scheduled_at_us: int
    deliver_after_us: int
    expires_us: int | None
    status: str
    delivery_target: str | None
    delivery_attempts: int
    last_delivery_us: int | None
    delivered_lease_id: str | None
    delivered_lease_epoch: int | None
    ack_id: str | None
    acknowledged_us: int | None
    summary_of_count: int
    current_revision: int
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class CognitiveEventRevision:
    """One immutable delivery-history revision (§12.1 "完整 Revision 历史")."""

    id: str
    event_id: str
    tenant_id: str
    revision: int
    status: str
    delivery_attempts: int
    last_delivery_us: int | None
    delivered_lease_id: str | None
    delivered_lease_epoch: int | None
    ack_id: str | None
    acknowledged_us: int | None
    reason_code: str | None
    created_us: int
    created_by: str


def new_ack_id() -> str:
    """A fresh ack token; hosts echo it back for idempotent ACK."""
    import uuid

    return f"ack_{uuid.uuid4()}"


def pullable(status: str, *, deliver_after_us: int, expires_us: int | None, now_us: int) -> bool:
    """Delivery eligibility: due, pending, not expired (§12.2)."""
    if status != EventStatus.PENDING.value or deliver_after_us > now_us:
        return False
    return expires_us is None or expires_us > now_us


def should_expire(
    *,
    status: str,
    expires_us: int | None,
    acknowledged_us: int | None,
    delivery_attempts: int,
    now_us: int,
    max_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
) -> bool:
    """Expiry policy: past the horizon, or hopelessly attempted, never ACKed."""
    if status in (EventStatus.ACKNOWLEDGED.value, EventStatus.CANCELLED.value):
        return False
    if status in (EventStatus.EXPIRED.value,):
        return False
    if acknowledged_us is not None:
        return False
    if expires_us is not None and expires_us <= now_us:
        return True
    return delivery_attempts >= max_attempts


__all__ = [
    "DEFAULT_EVENT_TTL_US",
    "DEFAULT_MAX_DELIVERY_ATTEMPTS",
    "DEFAULT_SUMMARY_MERGE_THRESHOLD",
    "EVENT_KIND_NOTE_REVIEW",
    "EVENT_KIND_SUMMARY",
    "EVENT_KIND_TASK_DUE",
    "EVENT_KIND_TASK_TRANSITION",
    "EVENT_TRANSITIONS",
    "CognitiveEventCurrent",
    "CognitiveEventRevision",
    "EventStatus",
    "InvalidEventError",
    "event_scope_key",
    "new_ack_id",
    "pullable",
    "should_expire",
    "validate_event_transition",
]
