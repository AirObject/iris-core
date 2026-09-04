"""Versioned Persona domain rules (Phase 9, architecture §14).

Persona content is data, never an instruction.  The functions in this module
are intentionally provider-free and deterministic: every value accepted from
a model or host is revalidated here before a repository may persist it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from iris_memory_core.domain.errors import InvalidRequestError
from iris_memory_core.domain.hashing import canonical_json, content_hash

PERSONA_SCHEMA_VERSION = 1
PERSONA_POLICY_SCHEMA_VERSION = 1
PERSONA_STATE_SCHEMA_VERSION = 1
PERSONA_PROPOSAL_SCHEMA_VERSION = 1
MAX_PERSONA_JSON_BYTES = 32_768
MAX_PERSONA_TEXT_LENGTH = 4_096
MAX_STATE_TTL_US = 7 * 24 * 60 * 60 * 1_000_000


class PersonaPolicyMode(StrEnum):
    LOCKED = "locked"
    MANUAL = "manual"
    BOUNDED_AUTO = "bounded_auto"


class PersonaProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    EXPIRED = "expired"


class PersonaRevisionStatus(StrEnum):
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


CORE_FIELDS = frozenset(
    {
        "name",
        "identity",
        "values",
        "safety_boundaries",
        "relationship_constraints",
        # Frozen Phase 1 bootstrap keys (ADR-0008).
        "language",
        "name_placeholder",
    }
)
TRAIT_FIELDS = frozenset({"style", "interests", "habits", "tendencies", "weights"})
NARRATIVE_FIELDS = frozenset({"summary", "experiences", "relationships", "goals"})
STATE_FIELDS = frozenset({"mood", "energy", "engagement", "focus", "expression_tendency"})
EXPRESSION_TENDENCIES = frozenset({"neutral", "warm", "concise", "reflective", "energetic"})


@dataclass(frozen=True, slots=True)
class PersonaPolicy:
    id: str
    tenant_id: str
    agent_id: str
    revision: int
    mode: PersonaPolicyMode
    allowed_fields: tuple[str, ...]
    max_single_delta: float
    max_cumulative_delta: float
    cumulative_window_us: int
    min_evidence: int
    min_distinct_sources: int
    min_evidence_span_us: int
    min_confidence: float
    cooldown_us: int
    observation_us: int
    sensitive_fields: tuple[str, ...]
    rollback_threshold: float
    content_hash: str
    status: str
    created_by: str
    reason_code: str
    created_us: int


@dataclass(frozen=True, slots=True)
class PersonaRecord:
    id: str
    tenant_id: str
    agent_id: str
    revision: int
    core: str
    traits: str
    narrative: str
    policy_id: str
    previous_revision_id: str | None
    change_reason: str
    source_refs: str
    content_hash: str
    effective_from_us: int
    effective_until_us: int | None
    created_by: str
    created_us: int
    status: PersonaRevisionStatus
    schema_version: int


@dataclass(frozen=True, slots=True)
class PersonaState:
    id: str
    tenant_id: str
    agent_id: str
    revision: int
    state_json: str
    baseline_json: str
    source_refs: str
    started_us: int
    expires_us: int
    decay_policy: str
    created_by: str
    created_us: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class PersonaFieldDelta:
    field: str
    old_value: Any
    new_value: Any
    magnitude: float
    evidence_refs: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True, slots=True)
class PersonaProposal:
    id: str
    tenant_id: str
    agent_id: str
    base_revision: int
    target_fields: tuple[str, ...]
    patch_json: str
    field_deltas_json: str
    evidence_refs_json: str
    confidence: float
    generator: str
    generator_version: str
    policy_evaluation_json: str
    status: PersonaProposalStatus
    reviewed_by: str | None
    review_reason: str | None
    created_us: int
    expires_us: int
    published_revision_id: str | None
    schema_version: int


def default_locked_policy_content() -> dict[str, object]:
    """The one deterministic policy installed for every bootstrap Agent."""
    return {
        "schema_version": PERSONA_POLICY_SCHEMA_VERSION,
        "mode": PersonaPolicyMode.LOCKED.value,
        "allowed_fields": [],
        "max_single_delta": 0.0,
        "max_cumulative_delta": 0.0,
        "cumulative_window_us": 0,
        "min_evidence": 1,
        "min_distinct_sources": 1,
        "min_evidence_span_us": 0,
        "min_confidence": 1.0,
        "cooldown_us": 0,
        "observation_us": 0,
        "sensitive_fields": [],
        "rollback_threshold": 0.0,
    }


def default_locked_policy_hash() -> str:
    return content_hash(default_locked_policy_content())


def validate_content_layer(layer: str, value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"core": CORE_FIELDS, "traits": TRAIT_FIELDS, "narrative": NARRATIVE_FIELDS}.get(
        layer
    )
    if allowed is None:
        raise InvalidRequestError(f"unknown persona content layer: {layer}")
    unknown = set(value) - allowed
    if unknown:
        raise InvalidRequestError(f"unknown {layer} fields: {sorted(unknown)}")
    normalized = dict(value)
    _validate_json_value(normalized)
    if len(canonical_json(normalized).encode()) > MAX_PERSONA_JSON_BYTES:
        raise InvalidRequestError(f"{layer} exceeds the maximum encoded size")
    return normalized


def persona_content_hash(
    core: Mapping[str, Any], traits: Mapping[str, Any], narrative: Mapping[str, Any]
) -> str:
    return content_hash(
        {
            "core": validate_content_layer("core", core),
            "traits": validate_content_layer("traits", traits),
            "narrative": validate_content_layer("narrative", narrative),
        }
    )


def validate_state(value: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(value) - STATE_FIELDS
    if unknown:
        raise InvalidRequestError(f"unknown persona state fields: {sorted(unknown)}")
    normalized = dict(value)
    for field in ("mood", "energy", "engagement"):
        if field not in normalized:
            continue
        raw = normalized[field]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidRequestError(f"persona state {field} must be numeric")
        lower = -1.0 if field == "mood" else 0.0
        if not lower <= float(raw) <= 1.0:
            raise InvalidRequestError(f"persona state {field} is outside its allowed range")
        normalized[field] = float(raw)
    if "focus" in normalized:
        focus = normalized["focus"]
        if not isinstance(focus, list) or len(focus) > 8:
            raise InvalidRequestError("persona state focus must be a list of at most 8 strings")
        if any(not isinstance(item, str) or not item or len(item) > 128 for item in focus):
            raise InvalidRequestError("persona state focus contains an invalid item")
    if "expression_tendency" in normalized:
        tendency = normalized["expression_tendency"]
        if tendency not in EXPRESSION_TENDENCIES:
            raise InvalidRequestError("persona state expression_tendency is not allowed")
    _validate_json_value(normalized)
    return normalized


def validate_state_timing(*, started_us: int, expires_us: int) -> None:
    ttl = expires_us - started_us
    if ttl <= 0 or ttl > MAX_STATE_TTL_US:
        raise InvalidRequestError("persona state TTL must be positive and no more than 7 days")


def flatten_patch(patch: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a Proposal patch and return its deterministic field-path map."""
    if "core" in patch:
        raise InvalidRequestError("Persona Core cannot be changed by an evolution proposal")
    unknown_layers = set(patch) - {"traits", "narrative"}
    if unknown_layers:
        raise InvalidRequestError(f"unknown persona patch layers: {sorted(unknown_layers)}")
    flattened: dict[str, Any] = {}
    for layer in ("traits", "narrative"):
        raw = patch.get(layer)
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise InvalidRequestError(f"persona patch {layer} must be an object")
        validated = validate_content_layer(layer, raw)
        for field, value in validated.items():
            flattened[f"{layer}.{field}"] = value
    if not flattened:
        raise InvalidRequestError("persona proposal patch must change at least one field")
    return flattened


def field_magnitude(old: Any, new: Any) -> float:
    if old == new:
        return 0.0
    if (
        isinstance(old, (int, float))
        and not isinstance(old, bool)
        and isinstance(new, (int, float))
        and not isinstance(new, bool)
    ):
        return min(1.0, abs(float(new) - float(old)))
    return 1.0


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise InvalidRequestError("persona JSON nesting exceeds 8 levels")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value) > MAX_PERSONA_TEXT_LENGTH:
            raise InvalidRequestError("persona text exceeds the maximum length")
        return
    if isinstance(value, list):
        if len(value) > 128:
            raise InvalidRequestError("persona JSON list exceeds 128 items")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise InvalidRequestError("persona JSON object exceeds 128 fields")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise InvalidRequestError("persona JSON contains an invalid field name")
            _validate_json_value(item, depth=depth + 1)
        return
    raise InvalidRequestError("persona JSON contains an unsupported value")


__all__ = [
    "MAX_STATE_TTL_US",
    "PERSONA_POLICY_SCHEMA_VERSION",
    "PERSONA_PROPOSAL_SCHEMA_VERSION",
    "PERSONA_SCHEMA_VERSION",
    "PERSONA_STATE_SCHEMA_VERSION",
    "PersonaFieldDelta",
    "PersonaPolicy",
    "PersonaPolicyMode",
    "PersonaProposal",
    "PersonaProposalStatus",
    "PersonaRecord",
    "PersonaRevisionStatus",
    "PersonaState",
    "default_locked_policy_content",
    "default_locked_policy_hash",
    "field_magnitude",
    "flatten_patch",
    "persona_content_hash",
    "validate_content_layer",
    "validate_state",
    "validate_state_timing",
]
