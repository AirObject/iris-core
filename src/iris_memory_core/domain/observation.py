"""Observation Journal domain rules (§8).

An Observation is a confirmed external effect, never an attempt: there is no
``pending`` or ``failed`` effect state here — failed sends, policy blocks and
cancelled outputs may only reach the Audit ledger. Streaming/voice segments
that provably took effect partially carry ``effect_state=partial`` plus the
saved proof range (§8.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from iris_memory_core.domain.hashing import content_hash

# Source cursors are restricted to decimal integer positions in this phase
# (ADR-0009); platforms without stable cursors use source_event_id plus the
# idempotency key (§8.3).
_CURSOR_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,17})$")

OBSERVATION_SCHEMA_VERSION = 1
MAX_OBSERVATION_CONTENT_CHARS = 200_000
MAX_BATCH_RECORDS = 1000


class ObservationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"
    EXTERNAL = "external"


ALLOWED_ROLES = frozenset(role.value for role in ObservationRole)


class EffectState(StrEnum):
    COMMITTED = "committed"
    PARTIAL = "partial"


class GapPolicy(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    MARK = "mark"


ALLOWED_GAP_POLICIES = frozenset(policy.value for policy in GapPolicy)


class InvalidObservationError(ValueError):
    """Raised when a record violates the confirmed-effect contract."""


def validate_cursor(cursor: str) -> int:
    """Validate and normalize a source cursor to its integer position.

    ``fullmatch`` keeps the anchor airtight: no leading zeros, no trailing
    junk, no unicode digit variants — identical to the JSON Schema and the
    TypeScript SDK (contract parity, ADR-0006).
    """
    if not _CURSOR_PATTERN.fullmatch(cursor):
        raise InvalidObservationError(
            "source_cursor must be a decimal integer position (no leading zeros)"
        )
    return int(cursor)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Placeholder reference to an externally stored artifact (never the blob)."""

    artifact_id: str
    kind: str

    def __post_init__(self) -> None:
        if not self.artifact_id or len(self.artifact_id) > 256:
            raise InvalidObservationError("artifact reference id must be 1..256 characters")
        if not self.kind or len(self.kind) > 64:
            raise InvalidObservationError("artifact reference kind must be 1..64 characters")


@dataclass(frozen=True, slots=True)
class ObservationDraft:
    """One record of a batch request, after request-level validation.

    Five identity semantics stay independent (§8.3, ADR-0009):

    - ``source_stream`` / ``source_cursor`` — ordering within one stream;
    - ``source_event_id`` — the platform's event identity (dedupe);
    - ``occurrence_id`` — the logical effect identity (the same real-world
      occurrence redelivered yields the same observation);
    - ``idempotency_key`` — transport-retry safety for this record.
    """

    tenant_id: str
    agent_id: str
    app_instance_id: str
    role: ObservationRole
    kind: str
    idempotency_key: str
    effect_state: EffectState
    occurred_us: int
    committed_us: int
    space_group_id: str | None = None
    space_id: str | None = None
    session_id: str | None = None
    source_stream: str | None = None
    source_cursor: int | None = None
    source_event_id: str | None = None
    occurrence_id: str | None = None
    actor_external_identity_id: str | None = None
    actor_entity_id_at_ingest: str | None = None
    content: str | None = None
    structured_payload: dict[str, object] | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    privacy_labels: tuple[str, ...] = ()
    effect_proof: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key or len(self.idempotency_key) > 256:
            raise InvalidObservationError("record idempotency_key must be 1..256 characters")
        if not self.kind or len(self.kind) > 128:
            raise InvalidObservationError("observation kind must be 1..128 characters")
        if self.effect_state is EffectState.PARTIAL:
            # Partial effects must prove what actually took effect (§8.2):
            # the saved confirmed range (stream/voice) or equivalent evidence.
            proof = self.effect_proof or {}
            if not proof.get("confirmed_range"):
                raise InvalidObservationError(
                    "effect_state=partial requires effect_proof.confirmed_range"
                )
        elif self.effect_proof is not None:
            raise InvalidObservationError("effect_proof is only meaningful for partial effects")
        if self.occurred_us > self.committed_us:
            raise InvalidObservationError("occurred_at must not be after committed_at")
        if self.content is not None and len(self.content) > MAX_OBSERVATION_CONTENT_CHARS:
            raise InvalidObservationError(
                f"content exceeds {MAX_OBSERVATION_CONTENT_CHARS} characters"
            )
        if (self.source_stream is None) != (self.source_cursor is None):
            raise InvalidObservationError("source_stream and source_cursor must be given together")
        if self.source_cursor is not None and self.source_cursor < 0:
            raise InvalidObservationError("source_cursor must be non-negative")
        if self.structured_payload is not None and not isinstance(self.structured_payload, dict):
            raise InvalidObservationError("structured_payload must be an object")

    def fingerprint(self) -> str:
        """Record-level fingerprint for idempotency-key reuse detection.

        Covers every caller-controlled field of the record — including
        ``committed_us`` and ``actor_entity_id_at_ingest`` — so reusing one
        idempotency key with ANY changed field fails as
        ``idempotency_key_reused`` instead of silently deduping.
        """
        return content_hash(
            {
                "role": self.role.value,
                "kind": self.kind,
                "effect_state": self.effect_state.value,
                "occurred_us": self.occurred_us,
                "committed_us": self.committed_us,
                "space_group_id": self.space_group_id,
                "space_id": self.space_id,
                "session_id": self.session_id,
                "source_stream": self.source_stream,
                "source_cursor": self.source_cursor,
                "source_event_id": self.source_event_id,
                "occurrence_id": self.occurrence_id,
                "actor_external_identity_id": self.actor_external_identity_id,
                "actor_entity_id_at_ingest": self.actor_entity_id_at_ingest,
                "content": self.content,
                "structured_payload": self.structured_payload,
                "artifact_refs": [
                    {"artifact_id": ref.artifact_id, "kind": ref.kind} for ref in self.artifact_refs
                ],
                "privacy_labels": list(self.privacy_labels),
                "effect_proof": self.effect_proof,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredObservation:
    """The persisted observation projection returned to callers."""

    id: str
    tenant_id: str
    agent_id: str
    app_instance_id: str
    role: ObservationRole
    kind: str
    idempotency_key: str
    effect_state: EffectState
    occurred_us: int
    committed_us: int
    created_us: int
    revision: int
    space_group_id: str | None = None
    space_id: str | None = None
    session_id: str | None = None
    source_stream: str | None = None
    source_cursor: int | None = None
    source_event_id: str | None = None
    occurrence_id: str | None = None
    actor_external_identity_id: str | None = None
    actor_entity_id_at_ingest: str | None = None
    content: str | None = None
    structured_payload: dict[str, object] | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    privacy_labels: tuple[str, ...] = ()
    effect_proof: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Contract-shaped result of ``POST /v1/observations:batch`` (§8.4)."""

    accepted_observation_ids: tuple[str, ...] = ()
    duplicate_observation_ids: tuple[str, ...] = ()
    source_watermark: int | None = None
    agent_watermark: int | None = None
    outbox_enqueued: int = 0
    cursors: dict[str, int] = field(default_factory=dict)
    lease_warning: str | None = None
