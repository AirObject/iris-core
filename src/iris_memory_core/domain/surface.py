"""Active Surface Coordinator domain rules (§25).

Leases are agent-level unique control-plane state — they never replace or
weaken Scope, Privacy, Revision, Idempotency or transaction rules in any
mode. Epochs are monotonic per (tenant, agent): acquire, preempt and
re-acquire all bump the epoch, and any request from a superseded epoch is
fenced (§25.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MIN_LEASE_TTL_US = 1_000_000  # 1s
MAX_LEASE_TTL_US = 600_000_000  # 10min
LEASE_GRACE_US = 2_000_000  # fixed short grace beyond locally-known expiry (§25.4)
MAX_SURFACE_PRIORITY = 100


class SurfaceMode(StrEnum):
    OFF = "off"
    ADVISORY = "advisory"
    REQUIRED = "required"


ALLOWED_SURFACE_MODES = frozenset(mode.value for mode in SurfaceMode)


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    RELEASED = "released"
    EXPIRED = "expired"


# Terminal statuses: a lease row never re-enters the active set from these.
TERMINAL_LEASE_STATUSES = frozenset({LeaseStatus.RELEASED.value, LeaseStatus.EXPIRED.value})


class InvalidLeaseError(ValueError):
    """Raised on malformed lease parameters (ttl, priority, holder)."""


def validate_ttl_us(ttl_us: int) -> int:
    if not MIN_LEASE_TTL_US <= ttl_us <= MAX_LEASE_TTL_US:
        raise InvalidLeaseError(
            f"lease ttl must be within {MIN_LEASE_TTL_US}..{MAX_LEASE_TTL_US} microseconds"
        )
    return ttl_us


def validate_priority(priority: int) -> int:
    if not 0 <= priority <= MAX_SURFACE_PRIORITY:
        raise InvalidLeaseError(f"surface priority must be within 0..{MAX_SURFACE_PRIORITY}")
    return priority


def validate_holder(*, holder_app_instance_id: str, holder_space_id: str | None) -> None:
    if not holder_app_instance_id or len(holder_app_instance_id) > 256:
        raise InvalidLeaseError("holder_app_instance_id must be 1..256 characters")
    if holder_space_id is not None and len(holder_space_id) > 128:
        raise InvalidLeaseError("holder_space_id must be at most 128 characters")


def is_expired(status: str, expires_us: int, now_us: int) -> bool:
    """An active/draining lease expires once its expiry passes."""
    if status not in {LeaseStatus.ACTIVE.value, LeaseStatus.DRAINING.value}:
        return False
    return expires_us <= now_us


def may_preempt(request_priority: int, holder_priority: int) -> bool:
    """Strictly higher priority may preempt; equal priority waits for expiry."""
    return request_priority > holder_priority


@dataclass(frozen=True, slots=True)
class LeaseView:
    """Contract-shaped lease projection returned to holders and hosts."""

    lease_id: str
    tenant_id: str
    agent_id: str
    holder_space_id: str | None
    holder_app_instance_id: str
    lease_epoch: int
    priority: int
    status: str
    acquired_us: int
    expires_us: int
    last_heartbeat_us: int
    revision: int


@dataclass(frozen=True, slots=True)
class SurfaceCheck:
    """Outcome of an online-plane lease check under the agent's mode."""

    mode: SurfaceMode
    valid: bool
    lease_warning: str | None = None
    lease_id: str | None = None
    lease_epoch: int | None = None
