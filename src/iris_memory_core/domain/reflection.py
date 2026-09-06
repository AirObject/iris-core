"""Deterministic Phase 10 consolidation and reflection rules.

Provider values are untrusted data.  This module performs shape, size, enum,
evidence-span and forbidden-action validation without importing persistence,
HTTP or provider SDKs (ADR-0007/0019).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, cast

from iris_memory_core.domain.errors import InvalidRequestError
from iris_memory_core.domain.hashing import content_hash

CONSOLIDATION_BUILDER_VERSION = "consolidation.v1"
REFLECTION_PROMPT_VERSION = "reflection.v1"
PROVIDER_SCHEMA_VERSION = "candidate.v1"
REFLECTION_POLICY_VERSION = "policy.v1"
RECONCILIATION_VERSION = "reconciliation.v1"
JOB_PAYLOAD_VERSION = 2

ProviderKind = Literal["extraction", "summarization", "reconciliation", "persona_evolution"]
ProviderOutcomeName = Literal[
    "success",
    "timeout",
    "rate_limited",
    "server_error",
    "invalid_output",
    "circuit_open",
    "budget_exhausted",
    "cancelled",
]

MAX_WINDOW_OBSERVATIONS = 500
MAX_WINDOW_CHARS = 200_000
MAX_WINDOW_SPAN_US = 7 * 24 * 60 * 60 * 1_000_000
MAX_PROVIDER_CANDIDATES = 256
MAX_CANDIDATE_PAYLOAD_BYTES = 131_072
MAX_CANDIDATE_EVIDENCE = 100


class CandidateType(StrEnum):
    CLAIM = "claim"
    RELATION = "relation"
    NOTE = "note"
    TASK = "task"
    PERSONA_PROPOSAL = "persona_proposal"


class RejectReason(StrEnum):
    INVALID_SCHEMA = "invalid_schema"
    EVIDENCE_REQUIRED = "evidence_required"
    EVIDENCE_OUTSIDE_WINDOW = "evidence_outside_window"
    EVIDENCE_SPAN_INVALID = "evidence_span_invalid"
    SELF_REFERENTIAL_EVIDENCE = "self_referential_evidence"
    UNKNOWN_ENTITY = "unknown_entity"
    SCOPE_VIOLATION = "scope_violation"
    PRIVACY_DENIED = "privacy_denied"
    AUTHORITY_DENIED = "authority_denied"
    UNSAFE_TRIGGER = "unsafe_trigger"
    ACTIVE_TASK_DENIED = "active_task_denied"
    PERSONA_CORE_DENIED = "persona_core_denied"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    TOO_LARGE = "too_large"
    SOURCE_STALE = "source_stale"
    LEASE_FENCED = "lease_fenced"
    POLICY_DENIED = "policy_denied"


class CandidateValidationError(InvalidRequestError):
    """A provider candidate failed an explicitly auditable admission rule."""

    code = "evidence_invalid"

    def __init__(self, reason: RejectReason, message: str) -> None:
        super().__init__(message, details={"reject_reason": reason.value})
        self.reason = reason


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    observation_id: str
    observation_revision: int
    start: int
    end: int

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "observation_revision": self.observation_revision,
            "start": self.start,
            "end": self.end,
        }


@dataclass(frozen=True, slots=True)
class VersionSet:
    prompt_version: str = REFLECTION_PROMPT_VERSION
    provider_schema_version: str = PROVIDER_SCHEMA_VERSION
    builder_version: str = CONSOLIDATION_BUILDER_VERSION
    policy_version: str = REFLECTION_POLICY_VERSION
    reconciliation_version: str = RECONCILIATION_VERSION
    model_id: str = "deterministic-fake-v1"

    def as_dict(self) -> dict[str, str]:
        return {
            "prompt_version": self.prompt_version,
            "provider_schema_version": self.provider_schema_version,
            "builder_version": self.builder_version,
            "policy_version": self.policy_version,
            "reconciliation_version": self.reconciliation_version,
            "model_id": self.model_id,
        }

    def __post_init__(self) -> None:
        for key, value in self.as_dict().items():
            if not value or len(value) > 256:
                raise ValueError(f"{key} must be 1..256 characters")


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_type: CandidateType
    payload: dict[str, Any]
    evidence: tuple[EvidenceSpan, ...]
    scope: dict[str, str | None]
    privacy_labels: tuple[str, ...]
    fingerprint: str
    candidate_id: str

    def as_provider_value(self) -> dict[str, object]:
        return {
            "type": self.candidate_type.value,
            "payload": self.payload,
            "evidence": [item.as_dict() for item in self.evidence],
            "scope": self.scope,
            "privacy_labels": list(self.privacy_labels),
        }


@dataclass(frozen=True, slots=True)
class ConsolidationWindow:
    id: str
    tenant_id: str
    agent_id: str
    source_watermark: int
    source_fingerprint: str
    builder_version: str
    status: str
    observation_refs: tuple[dict[str, object], ...]
    topic_key: str
    window_start_us: int
    window_end_us: int
    created_us: int
    space_group_id: str | None = None
    space_id: str | None = None
    session_id: str | None = None
    episode_id: str | None = None
    sealed_us: int | None = None


@dataclass(frozen=True, slots=True)
class ReflectionRecord:
    id: str
    tenant_id: str
    agent_id: str
    window_id: str
    run_fingerprint: str
    source_watermark: int
    versions: VersionSet
    commit_mode: str
    status: str
    candidate_count: int
    rejected_count: int
    created_us: int
    completed_us: int | None = None
    provider_outcome_id: str | None = None
    replay_of: str | None = None
    diff: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    id: str
    tenant_id: str
    agent_id: str
    reflection_id: str
    candidate_type: str
    fingerprint: str
    payload: dict[str, object]
    evidence_refs: tuple[dict[str, object], ...]
    scope: dict[str, str | None]
    privacy_labels: tuple[str, ...]
    decision: str
    created_us: int
    reject_reason: str | None = None
    canonical_resource_type: str | None = None
    canonical_resource_id: str | None = None
    decided_us: int | None = None


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    id: str
    token_sha256: str
    tenant_id: str
    app_instance_id: str
    plane: str
    agent_ids: frozenset[str]
    space_group_ids: frozenset[str]
    space_ids: frozenset[str]
    entity_ids: frozenset[str]
    capabilities: frozenset[str]
    data_purposes: frozenset[str]
    created_us: int
    expires_us: int
    revoked_us: int | None = None
    label: str | None = None
    description: str | None = None
    token_prefix: str | None = None
    created_by: str | None = None
    revoke_reason: str | None = None
    revoke_after_us: int | None = None
    console_revision: int = 1
    rotated_from_id: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceEvent:
    cursor: int
    id: str
    tenant_id: str
    event_type: str
    resource_refs: tuple[dict[str, object], ...]
    source_watermark: int
    occurred_us: int
    agent_id: str | None = None
    space_group_id: str | None = None
    space_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    provider_kind: ProviderKind
    outcome: ProviderOutcomeName
    retryable: bool
    request_hash: str
    response_hash: str | None
    cost_microunits: int
    duration_us: int
    diagnostic_code: str | None = None


def window_fingerprint(
    *,
    tenant_id: str,
    agent_id: str,
    source_watermark: int,
    observations: Sequence[Mapping[str, object]],
    scope: Mapping[str, str | None],
    topic_key: str,
    window_start_us: int,
    window_end_us: int,
    builder_version: str,
) -> str:
    """Stable input identity; wall-clock execution time is deliberately absent."""
    refs = sorted(
        (
            str(item["id"]),
            int(cast(int, item["revision"])),
            str(item["record_fingerprint"]),
        )
        for item in observations
    )
    return content_hash(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "source_watermark": source_watermark,
            "observations": refs,
            "scope": dict(scope),
            "topic_key": topic_key,
            "window_start_us": window_start_us,
            "window_end_us": window_end_us,
            "builder_version": builder_version,
        }
    )


def run_fingerprint(window_source_fingerprint: str, versions: VersionSet, commit_mode: str) -> str:
    if commit_mode not in {"dry_run", "commit"}:
        raise ValueError("commit_mode must be dry_run or commit")
    return content_hash(
        {
            "window_source_fingerprint": window_source_fingerprint,
            "versions": versions.as_dict(),
            "commit_mode": commit_mode,
        }
    )


def _forbidden_key(value: object) -> str | None:
    forbidden = {
        "code",
        "exec",
        "eval",
        "shell",
        "sql",
        "python",
        "javascript",
        "command",
        "script",
    }
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            if key in forbidden:
                return key
            nested = _forbidden_key(child)
            if nested is not None:
                return nested
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            nested = _forbidden_key(child)
            if nested is not None:
                return nested
    return None


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CandidateValidationError(
            RejectReason.INVALID_SCHEMA, f"candidate {field} must be an object"
        )
    return value


def validate_candidate(
    raw: Mapping[str, object],
    *,
    observations: Mapping[str, tuple[int, int]],
    versions: VersionSet,
) -> Candidate:
    """Validate and normalize one untrusted provider candidate.

    ``observations`` maps the exact allowlisted source id to
    ``(revision, content_length)``.  Reflection/candidate ids therefore cannot
    masquerade as evidence even when syntactically valid.
    """
    allowed_top = {"type", "payload", "evidence", "scope", "privacy_labels"}
    if set(raw) - allowed_top:
        raise CandidateValidationError(
            RejectReason.INVALID_SCHEMA, "candidate has unknown top-level fields"
        )
    try:
        candidate_type = CandidateType(str(raw.get("type", "")))
    except ValueError:
        raise CandidateValidationError(
            RejectReason.INVALID_SCHEMA, "candidate type is not allowed"
        ) from None
    payload_raw = _require_mapping(raw.get("payload"), "payload")
    payload = {str(key): value for key, value in payload_raw.items()}
    if len(str(payload).encode()) > MAX_CANDIDATE_PAYLOAD_BYTES:
        raise CandidateValidationError(RejectReason.TOO_LARGE, "candidate payload is too large")
    bad_key = _forbidden_key(payload)
    if bad_key is not None:
        raise CandidateValidationError(
            RejectReason.UNSAFE_TRIGGER, f"candidate contains forbidden executable key {bad_key}"
        )
    evidence_raw = raw.get("evidence")
    if not isinstance(evidence_raw, list) or not evidence_raw:
        raise CandidateValidationError(
            RejectReason.EVIDENCE_REQUIRED, "candidate requires evidence spans"
        )
    if len(evidence_raw) > MAX_CANDIDATE_EVIDENCE:
        raise CandidateValidationError(RejectReason.TOO_LARGE, "too many evidence spans")
    evidence: list[EvidenceSpan] = []
    for value in evidence_raw:
        item = _require_mapping(value, "evidence item")
        if set(item) != {"observation_id", "observation_revision", "start", "end"}:
            raise CandidateValidationError(
                RejectReason.INVALID_SCHEMA, "evidence span fields are incomplete or unknown"
            )
        observation_id = item.get("observation_id")
        revision = item.get("observation_revision")
        start = item.get("start")
        end = item.get("end")
        if not isinstance(observation_id, str) or not observation_id:
            raise CandidateValidationError(
                RejectReason.INVALID_SCHEMA, "evidence observation_id must be a string"
            )
        source = observations.get(observation_id)
        if source is None:
            reason = (
                RejectReason.SELF_REFERENTIAL_EVIDENCE
                if observation_id.startswith(("candidate:", "reflection:"))
                else RejectReason.EVIDENCE_OUTSIDE_WINDOW
            )
            raise CandidateValidationError(reason, "evidence is outside the fixed window")
        if not isinstance(revision, int) or revision != source[0]:
            raise CandidateValidationError(
                RejectReason.SOURCE_STALE, "evidence revision does not match the fixed source"
            )
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > source[1]
        ):
            raise CandidateValidationError(
                RejectReason.EVIDENCE_SPAN_INVALID, "evidence span is outside source content"
            )
        evidence.append(EvidenceSpan(observation_id, revision, start, end))

    scope_raw = _require_mapping(raw.get("scope", {}), "scope")
    allowed_scope = {"tenant_id", "agent_id", "space_group_id", "space_id", "session_id"}
    if set(scope_raw) - allowed_scope:
        raise CandidateValidationError(RejectReason.INVALID_SCHEMA, "unknown scope dimension")
    scope: dict[str, str | None] = {}
    for key in sorted(allowed_scope):
        value = scope_raw.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise CandidateValidationError(
                RejectReason.INVALID_SCHEMA, f"scope.{key} must be a non-empty string or null"
            )
        scope[key] = value
    labels_raw = raw.get("privacy_labels", [])
    if not isinstance(labels_raw, list) or not all(
        isinstance(item, str) and item for item in labels_raw
    ):
        raise CandidateValidationError(
            RejectReason.INVALID_SCHEMA, "privacy_labels must be non-empty strings"
        )
    labels = tuple(sorted(set(labels_raw)))

    if candidate_type is CandidateType.TASK:
        if str(payload.get("status", "proposed")) != "proposed":
            raise CandidateValidationError(
                RejectReason.ACTIVE_TASK_DENIED, "provider tasks must remain proposed"
            )
        if "trigger" in payload or "triggers" in payload:
            raise CandidateValidationError(
                RejectReason.UNSAFE_TRIGGER, "provider tasks cannot create triggers"
            )
    if candidate_type is CandidateType.PERSONA_PROPOSAL:
        patch = payload.get("patch", payload)
        if isinstance(patch, Mapping) and "core" in {str(key).lower() for key in patch}:
            raise CandidateValidationError(
                RejectReason.PERSONA_CORE_DENIED, "persona core cannot be model-modified"
            )
    if candidate_type is CandidateType.CLAIM:
        confidence = payload.get("confidence", 0.5)
        importance = payload.get("importance", 0.5)
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
            or not isinstance(importance, (int, float))
            or isinstance(importance, bool)
            or not 0 <= float(importance) <= 1
        ):
            raise CandidateValidationError(
                RejectReason.VALUE_OUT_OF_RANGE, "claim scores must be within [0,1]"
            )
        if str(payload.get("source_authority", "extracted")) not in {
            "agent_inference",
            "extracted",
        }:
            raise CandidateValidationError(
                RejectReason.AUTHORITY_DENIED, "provider cannot claim higher source authority"
            )

    material = {
        "type": candidate_type.value,
        "payload": payload,
        "evidence": [
            item.as_dict()
            for item in sorted(
                evidence,
                key=lambda item: (
                    item.observation_id,
                    item.start,
                    item.end,
                    item.observation_revision,
                ),
            )
        ],
        "scope": scope,
        "privacy_labels": labels,
        "versions": versions.as_dict(),
    }
    fingerprint = content_hash(material)
    return Candidate(
        candidate_type=candidate_type,
        payload=payload,
        evidence=tuple(
            sorted(
                evidence,
                key=lambda item: (
                    item.observation_id,
                    item.start,
                    item.end,
                    item.observation_revision,
                ),
            )
        ),
        scope=scope,
        privacy_labels=labels,
        fingerprint=fingerprint,
        candidate_id=f"candidate:{fingerprint[:32]}",
    )


def candidate_diff(
    previous: Sequence[Candidate], current: Sequence[Candidate]
) -> dict[str, tuple[str, ...]]:
    """Stable set diff for dry-run review; payload content is never copied."""
    old = {item.fingerprint for item in previous}
    new = {item.fingerprint for item in current}
    return {
        "added": tuple(sorted(new - old)),
        "removed": tuple(sorted(old - new)),
        "unchanged": tuple(sorted(old & new)),
    }
