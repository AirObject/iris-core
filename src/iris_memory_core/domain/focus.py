"""FocusItem domain rules: kinds, statuses, transitions, decay, capacity (§9.3).

A FocusItem is Canonical cognitive state with immutable revisions and a
current pointer (ADR-0004). Time decay is a PURE function of the activation
base and the last activation time — re-running maintenance at the same
instant is idempotent and can never double-decay, raise confidence, or touch
the item's sources. Decay changes Activation/status only; ``affect`` items
are ordinary focus that may at most become future Persona State evidence —
no path here writes Persona Trait/Core.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from iris_memory_core.domain.hashing import content_hash

MAX_FOCUS_SUMMARY_CHARS = 2_000

DEFAULT_ACTIVATION_HALF_LIFE_US = 3_600_000_000  # one hour
DEFAULT_DORMANT_FLOOR = 0.05
DEFAULT_ACTIVATION_BOOST = 0.25

#: Promotion targets are RESERVED seams: Phase 3 records the target type,
#: event and audit; the actual Note/Task/Episode/Claim objects belong to
#: Phase 4/5 and are created by their own services.
PROMOTION_TARGET_TYPES = frozenset({"note", "task", "episode", "claim"})


class FocusKind(StrEnum):
    GOAL = "goal"
    QUESTION = "question"
    ENTITY = "entity"
    CLUE = "clue"
    CONCERN = "concern"
    AFFECT = "affect"
    PENDING_INPUT = "pending_input"


ALL_FOCUS_KINDS = frozenset(item.value for item in FocusKind)


class FocusStatus(StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    PROMOTED = "promoted"
    DISMISSED = "dismissed"
    EXPIRED = "expired"


#: Legal transitions; everything else is a stable ``invalid_state_transition``.
#: promoted/dismissed/expired are terminal — history is never rewritten by
#: reactivating an old row (ADR-0004).
FOCUS_TRANSITIONS: dict[str, frozenset[str]] = {
    FocusStatus.ACTIVE.value: frozenset(
        {
            FocusStatus.DORMANT.value,
            FocusStatus.PROMOTED.value,
            FocusStatus.DISMISSED.value,
            FocusStatus.EXPIRED.value,
        }
    ),
    FocusStatus.DORMANT.value: frozenset(
        {
            FocusStatus.ACTIVE.value,
            FocusStatus.PROMOTED.value,
            FocusStatus.DISMISSED.value,
            FocusStatus.EXPIRED.value,
        }
    ),
    FocusStatus.PROMOTED.value: frozenset(),
    FocusStatus.DISMISSED.value: frozenset(),
    FocusStatus.EXPIRED.value: frozenset(),
}


class InvalidFocusItemError(ValueError):
    """Raised when a focus item violates the §9.3 contract."""


@dataclass(frozen=True, slots=True)
class FocusRevision:
    """One immutable focus revision (§9.3, ADR-0004)."""

    id: str
    item_id: str
    tenant_id: str
    revision: int
    kind: str
    summary: str
    structured_payload: dict[str, object] | None
    privacy_labels: tuple[str, ...]
    source_refs: tuple[dict[str, object], ...]
    salience: float
    activation: float
    activation_base: float
    importance: float
    status: str
    promotion_policy: str
    promotion_target_type: str | None
    promotion_target_id: str | None
    last_activated_us: int
    expires_us: int | None
    created_us: int
    created_by: str


@dataclass(frozen=True, slots=True)
class FocusItemCurrent:
    """The current-pointer row; full content lives in the revision rows."""

    id: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    kind: str
    status: str
    current_revision: int
    activation: float
    activation_base: float
    last_activated_us: int
    expires_us: int | None
    created_us: int
    updated_us: int


def validate_transition(current: str, target: str) -> None:
    if target not in ALL_FOCUS_KINDS and target not in {status.value for status in FocusStatus}:
        raise InvalidFocusItemError(f"unknown focus status: {target!r}")
    allowed = FOCUS_TRANSITIONS.get(current)
    if allowed is None:
        raise InvalidFocusItemError(f"unknown focus status: {current!r}")
    if target not in allowed:
        raise InvalidFocusItemError(f"focus item cannot transition from {current!r} to {target!r}")


def clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def decayed_activation(
    activation_base: float,
    last_activated_us: int,
    now_us: int,
    *,
    half_life_us: int = DEFAULT_ACTIVATION_HALF_LIFE_US,
) -> float:
    """Deterministic time decay: ``base * 0.5 ** (Δ / half_life)``.

    A pure function of the base and the elapsed time since the last real
    activation — NOT of the previously stored (already decayed) value. Two
    maintenance runs at the same instant therefore produce identical results,
    and decay can never compound by re-running.
    """
    if half_life_us <= 0:
        raise InvalidFocusItemError("half_life_us must be positive")
    if now_us <= last_activated_us:
        return clamp01(activation_base)
    factor = 0.5 ** ((now_us - last_activated_us) / half_life_us)
    return clamp01(activation_base * factor)


@dataclass(frozen=True, slots=True)
class FocusCapacityPolicy:
    """Bounded attention: item count, per-kind quotas and token budget (§9.3)."""

    max_items: int = 64
    kind_quotas: dict[str, int] | None = None
    token_budget: int = 2_000
    activation_boost: float = DEFAULT_ACTIVATION_BOOST
    dormant_floor: float = DEFAULT_DORMANT_FLOOR
    half_life_us: int = DEFAULT_ACTIVATION_HALF_LIFE_US

    def __post_init__(self) -> None:
        if self.max_items < 1:
            raise InvalidFocusItemError("max_items must be at least 1")
        if self.token_budget < 1:
            raise InvalidFocusItemError("token_budget must be at least 1")
        if not 0.0 < self.activation_boost <= 1.0:
            raise InvalidFocusItemError("activation_boost must be within (0, 1]")
        if not 0.0 <= self.dormant_floor <= 1.0:
            raise InvalidFocusItemError("dormant_floor must be within [0, 1]")
        if self.half_life_us <= 0:
            raise InvalidFocusItemError("half_life_us must be positive")
        quotas = self.kind_quotas or {}
        for kind, quota in quotas.items():
            if kind not in ALL_FOCUS_KINDS:
                raise InvalidFocusItemError(f"unknown kind in quota table: {kind!r}")
            if quota < 1:
                raise InvalidFocusItemError(f"kind quota for {kind} must be at least 1")


DEFAULT_FOCUS_CAPACITY = FocusCapacityPolicy(
    kind_quotas={
        FocusKind.GOAL.value: 16,
        FocusKind.QUESTION.value: 16,
        FocusKind.ENTITY.value: 16,
        FocusKind.CLUE.value: 12,
        FocusKind.CONCERN.value: 8,
        FocusKind.AFFECT.value: 8,
        FocusKind.PENDING_INPUT.value: 8,
    }
)


def validate_scores(*, salience: float, activation: float, importance: float) -> None:
    for name, value in (
        ("salience", salience),
        ("activation", activation),
        ("importance", importance),
    ):
        if not 0.0 <= value <= 1.0:
            raise InvalidFocusItemError(f"{name} must be within [0, 1]")


def validate_summary(summary: str) -> str:
    if not summary or len(summary) > MAX_FOCUS_SUMMARY_CHARS:
        raise InvalidFocusItemError(f"summary must be 1..{MAX_FOCUS_SUMMARY_CHARS} characters")
    return summary


def validate_promotion_target(target_type: str | None) -> str | None:
    if target_type is None:
        return None
    if target_type not in PROMOTION_TARGET_TYPES:
        raise InvalidFocusItemError(
            f"promotion target must be one of {sorted(PROMOTION_TARGET_TYPES)}"
        )
    return target_type


def focus_scope_key(
    tenant_id: str,
    agent_id: str,
    space_id: str | None,
    session_id: str | None,
    space_group_id: str | None = None,
) -> str:
    """Canonical NULL-free scope identity (see recent_target_key)."""
    return "|".join((tenant_id, agent_id, space_group_id or "", space_id or "", session_id or ""))


def focus_fingerprint(
    *,
    kind: str,
    summary: str,
    salience: float,
    importance: float,
    promotion_policy: str,
    expires_us: int | None,
    scope: dict[str, str | None],
    privacy_labels: tuple[str, ...],
    source_refs: tuple[dict[str, object], ...],
    structured_payload: dict[str, object] | None,
) -> str:
    return content_hash(
        {
            "kind": kind,
            "summary": summary,
            "salience": salience,
            "importance": importance,
            "promotion_policy": promotion_policy,
            "expires_us": expires_us,
            "scope": scope,
            "privacy_labels": list(privacy_labels),
            "source_refs": [dict(ref) for ref in source_refs],
            "structured_payload": structured_payload,
        }
    )
