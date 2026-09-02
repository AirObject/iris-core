"""Retention, Legal Hold and Forget-selector domain rules (§19.3-19.5).

Three distinct fates that must never be conflated:

- **accessibility decay** lowers accessibility (a revision write, content and
  visibility untouched);
- **archive** moves content out of current reads while preserving history;
- **compliance deletion** erases content through the Forget ledger
  (tombstones + content scrubbing) and can never resurrect.

Protected resources (§19.5) are structurally outside ordinary automatic
forgetting: Persona core/published history, pinned notes, unfulfilled
promises, active tasks, security claims, tombstones themselves and the
minimal audit/deletion-ledger metadata. Legal holds freeze matching
resources against delete/archive actions until released.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from iris_memory_core.domain.errors import DomainError, InvalidRequestError
from iris_memory_core.domain.hashing import content_hash, request_fingerprint


class InvalidRetentionError(InvalidRequestError):
    """Raised when a retention policy or selector violates §19.5."""


class ProtectedResourceError(DomainError):
    """The selector names a resource excluded from ordinary forgetting."""

    code = "protected_resource"


class LegalHoldActiveError(DomainError):
    """A legal hold freezes the selected resources against deletion."""

    code = "legal_hold_active"


class RetentionAction(StrEnum):
    DECAY = "decay"
    ARCHIVE = "archive"
    DELETE = "delete"


ALL_RETENTION_ACTIONS = frozenset(item.value for item in RetentionAction)

#: Resource types the retention sweep knows how to evaluate. Widening the set
#: is a policy decision that must add matching protected-resource rules.
RETENTION_RESOURCE_TYPES = frozenset({"claim", "note", "episode", "relation", "observation"})

#: Privacy labels whose claims are "security relevant" (§19.5): security
#: claims never participate in ordinary automatic forgetting.
SECURITY_PRIVACY_LABELS = frozenset({"restricted"})

#: Reasons a resource was skipped by the retention sweep / Forget. Stable,
#: low-cardinality, and safe for audit details.
PROTECTION_REASONS = frozenset(
    {
        "pinned_note",
        "unfulfilled_promise",
        "active_task_source",
        "security_claim",
        "persona_resource",
        "tombstone_resource",
        "audit_metadata",
        "deletion_ledger",
        "legal_hold",
        "no_policy_match",
    }
)


class ForgetSelectorKind(StrEnum):
    RESOURCE = "resource"
    SUBJECT_PREDICATE = "subject_predicate"
    SESSION = "session"
    SPACE = "space"
    DATA_REQUEST = "data_request"


ALL_FORGET_SELECTOR_KINDS = frozenset(item.value for item in ForgetSelectorKind)


@dataclass(frozen=True, slots=True)
class ForgetSelector:
    """One Forget scope (§19.3). Exactly one kind; dimensions must be named,
    never wildcarded — ``None`` means "not part of this selector"."""

    kind: str
    resource_type: str | None = None
    resource_id: str | None = None
    agent_id: str | None = None
    subject_entity_id: str | None = None
    predicate: str | None = None
    space_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ALL_FORGET_SELECTOR_KINDS:
            raise InvalidRetentionError(f"unknown forget selector kind: {self.kind!r}")
        if self.kind == ForgetSelectorKind.RESOURCE.value:
            if not self.resource_type or not self.resource_id:
                raise InvalidRetentionError(
                    "resource selector requires resource_type and resource_id"
                )
        elif self.kind == ForgetSelectorKind.SUBJECT_PREDICATE.value:
            if not self.agent_id or not self.subject_entity_id:
                raise InvalidRetentionError(
                    "subject_predicate selector requires agent_id and subject_entity_id"
                )
        elif self.kind == ForgetSelectorKind.SESSION.value:
            if not self.session_id or not self.space_id:
                raise InvalidRetentionError("session selector requires space_id and session_id")
        elif self.kind == ForgetSelectorKind.SPACE.value:
            if not self.space_id:
                raise InvalidRetentionError("space selector requires space_id")
        elif self.kind == ForgetSelectorKind.DATA_REQUEST.value and not self.subject_entity_id:
            raise InvalidRetentionError(
                "data_request selector requires subject_entity_id (admin plane)"
            )

    def selector_key(self) -> str:
        """Stable identity for the deletion ledger (idempotent replay)."""
        return content_hash(
            {
                "kind": self.kind,
                "resource_type": self.resource_type,
                "resource_id": self.resource_id,
                "agent_id": self.agent_id,
                "subject_entity_id": self.subject_entity_id,
                "predicate": self.predicate,
                "space_id": self.space_id,
                "session_id": self.session_id,
            }
        )

    def as_audit_details(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "agent_id": self.agent_id,
            "subject_entity_id": self.subject_entity_id,
            "predicate": self.predicate,
            "space_id": self.space_id,
            "session_id": self.session_id,
        }


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Versioned retention rule (§19.5). One (resource_type, action,
    privacy filter) per tenant; ``threshold_days`` bounds the age before the
    action applies."""

    id: str
    tenant_id: str
    resource_type: str
    action: str
    privacy_label: str | None
    threshold_days: int
    policy_version: int
    enabled: bool
    created_us: int
    updated_us: int
    created_by: str

    def __post_init__(self) -> None:
        if self.resource_type not in RETENTION_RESOURCE_TYPES:
            raise InvalidRetentionError(
                f"retention resource type not supported: {self.resource_type!r}"
            )
        if self.action not in ALL_RETENTION_ACTIONS:
            raise InvalidRetentionError(f"unknown retention action: {self.action!r}")
        if self.threshold_days < 1:
            raise InvalidRetentionError("threshold_days must be at least 1")
        if self.policy_version < 1:
            raise InvalidRetentionError("policy_version must be at least 1")


@dataclass(frozen=True, slots=True)
class LegalHold:
    """Freezes matching resources against delete/archive until released."""

    id: str
    tenant_id: str
    space_id: str | None
    session_id: str | None
    subject_entity_id: str | None
    agent_id: str | None
    reason_code: str
    created_by: str
    created_us: int
    released_us: int | None

    def __post_init__(self) -> None:
        dims = (self.space_id, self.session_id, self.subject_entity_id)
        if not any(dims):
            raise InvalidRetentionError(
                "legal hold must name at least one of space_id/session_id/subject_entity_id"
            )
        if self.session_id is not None and self.space_id is None:
            raise InvalidRetentionError("legal hold session_id requires space_id")


@dataclass(frozen=True, slots=True)
class ForgetRequest:
    """One committed Forget execution — the compliance deletion ledger row.

    Backups older than this request cannot resurrect its targets: restore
    replays the ledger (identity tuple + created_us, idempotent) before
    the restored tree goes live.
    """

    id: str
    tenant_id: str
    selector_key: str
    selector_json: str
    reason_code: str
    requested_by: str
    created_us: int
    tombstone_seq_lo: int
    tombstone_seq_hi: int
    target_count: int
    erased_count: int
    protected_skipped: int
    held_skipped: int
    #: Rest of the logical identity (with selector_key/created_us/reason) and
    #: the original erasure mode: two same-microsecond requests from different
    #: app instances, or with different keys or modes, are distinct rows, and
    #: replay re-executes the original mode faithfully (ADR-0013 §7/§10).
    #: app_instance_id mirrors the idempotency namespace (tenant, app,
    #: operation, key) — the ledger distinguishes what the cache distinguishes.
    app_instance_id: str = ""
    idempotency_key: str = ""
    erase_content: bool = False


@dataclass(slots=True)
class RetentionSweepReport:
    """Low-cardinality outcome of one retention sweep (§19.5 audit).

    Deliberately mutable: the sweep accumulates counters as it goes and the
    final snapshot is what the audit row records."""

    decayed: int = 0
    archived: int = 0
    deleted: int = 0
    protected_skipped: int = 0
    held_skipped: int = 0
    policy_versions: tuple[int, ...] = ()


def forget_fingerprint(
    *,
    selector: ForgetSelector,
    reason: str,
    erase_content: bool,
) -> str:
    return request_fingerprint(
        "memory:forget",
        {
            "selector": selector.as_audit_details(),
            "reason": reason,
            "erase_content": erase_content,
        },
    )


def legal_hold_matches(
    hold: LegalHold,
    *,
    space_id: str | None,
    session_id: str | None,
    subject_entity_id: str | None,
) -> bool:
    """A hold matches when every named dimension of the hold equals the
    resource's own dimension (unnamed hold dimensions are wildcards)."""
    space_mismatch = hold.space_id is not None and hold.space_id != space_id
    session_mismatch = hold.session_id is not None and hold.session_id != session_id
    subject_mismatch = (
        hold.subject_entity_id is not None and hold.subject_entity_id != subject_entity_id
    )
    return not (space_mismatch or session_mismatch or subject_mismatch)


def policy_matches(
    policy: RetentionPolicy,
    *,
    resource_type: str,
    privacy_labels: tuple[str, ...],
    updated_us: int,
    now_us: int,
) -> bool:
    """Whether the policy applies to one resource right now."""
    if not policy.enabled or policy.resource_type != resource_type:
        return False
    if policy.privacy_label is not None and policy.privacy_label not in privacy_labels:
        return False
    age_us = now_us - updated_us
    return age_us >= policy.threshold_days * 86_400_000_000


def decayed_accessibility(current: float, *, factor: float = 0.5, floor: float = 0.1) -> float:
    """Monotone non-increasing decay with a floor (never reaches zero:
    accessibility decay is not deletion, §19.4)."""
    if not 0.0 < factor < 1.0:
        raise InvalidRetentionError("decay factor must be within (0, 1)")
    return max(floor, current * factor)


__all__ = [
    "ALL_FORGET_SELECTOR_KINDS",
    "ALL_RETENTION_ACTIONS",
    "PROTECTION_REASONS",
    "RETENTION_RESOURCE_TYPES",
    "SECURITY_PRIVACY_LABELS",
    "ForgetRequest",
    "ForgetSelector",
    "ForgetSelectorKind",
    "InvalidRetentionError",
    "LegalHold",
    "LegalHoldActiveError",
    "ProtectedResourceError",
    "RetentionAction",
    "RetentionPolicy",
    "RetentionSweepReport",
    "decayed_accessibility",
    "forget_fingerprint",
    "legal_hold_matches",
    "policy_matches",
]
