"""Forward-compatible SDK models and validators."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

#: Same anchored rule as the JSON Schema and the TypeScript SDK: decimal
#: integer, no leading zeros, no unicode digit variants (contract parity).
_CURSOR_PATTERN = re.compile(r"(0|[1-9][0-9]{0,17})")


class ContractValidationError(ValueError):
    def __init__(self, errors: tuple[str, ...]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True, slots=True)
class CapabilitiesEnvelope:
    api_version: str
    schema_version: int
    capabilities: tuple[str, ...]

    @classmethod
    def from_value(cls, value: object) -> CapabilitiesEnvelope:
        errors = validate_contract("capabilities", value)
        if errors:
            raise ContractValidationError(errors)
        assert isinstance(value, Mapping)
        api_version = value["api_version"]
        schema_version = value["schema_version"]
        capabilities = value["capabilities"]
        assert isinstance(api_version, str)
        assert type(schema_version) is int
        assert isinstance(capabilities, list)
        return cls(api_version, schema_version, tuple(str(item) for item in capabilities))


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    code: str
    message: str
    retryable: bool
    request_id: str

    @classmethod
    def from_value(cls, value: object) -> ErrorEnvelope:
        errors = validate_contract("error-envelope", value)
        if errors:
            raise ContractValidationError(errors)
        assert isinstance(value, Mapping)
        error = value["error"]
        request_id = value["request_id"]
        assert isinstance(error, Mapping)
        code = error["code"]
        message = error["message"]
        retryable = error["retryable"]
        assert isinstance(code, str)
        assert isinstance(message, str)
        assert isinstance(retryable, bool)
        assert isinstance(request_id, str)
        return cls(code, message, retryable, request_id)


def _validate_capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    api_version = value.get("api_version")
    schema_version = value.get("schema_version")
    capabilities = value.get("capabilities")
    if not isinstance(api_version, str) or not api_version.startswith("v"):
        errors.append("api_version must be a version string")
    if type(schema_version) is not int or schema_version < 1:
        errors.append("schema_version must be a positive integer")
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) and item for item in capabilities
    ):
        errors.append("capabilities must be an array of non-empty strings")
    elif len(set(capabilities)) != len(capabilities):
        errors.append("capabilities must be unique")
    return tuple(errors)


def _validate_error_envelope(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    request_id = value.get("request_id")
    error = value.get("error")
    if not isinstance(request_id, str) or not request_id:
        errors.append("request_id must be a non-empty string")
    if not isinstance(error, Mapping):
        errors.append("error must be an object")
        return tuple(errors)
    if not isinstance(error.get("code"), str) or not error.get("code"):
        errors.append("error.code must be a non-empty string")
    if not isinstance(error.get("message"), str) or not error.get("message"):
        errors.append("error.message must be a non-empty string")
    if not isinstance(error.get("retryable"), bool):
        errors.append("error.retryable must be a boolean")
    details = error.get("details")
    if details is not None and not isinstance(details, Mapping):
        errors.append("error.details must be an object")
    return tuple(errors)


def _validate_version_manifest(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("api_version", "contract_version", "package_version"):
        if not isinstance(value.get(key), str) or not value.get(key):
            errors.append(f"{key} must be a non-empty string")
    schema_version = value.get("schema_version")
    if type(schema_version) is not int or schema_version < 1:
        errors.append("schema_version must be a positive integer")
    source_hash = value.get("contract_source_sha256")
    if not isinstance(source_hash, str) or len(source_hash) != 64:
        errors.append("contract_source_sha256 must be a SHA-256 hex string")
    return tuple(errors)


_ROLES = frozenset({"user", "assistant", "tool", "system", "external"})
_EFFECT_STATES = frozenset({"committed", "partial"})
_GAP_POLICIES = frozenset({"accept", "reject", "mark"})
_LEASE_STATUSES = frozenset({"active", "draining", "released", "expired"})
_JOB_STATUSES = frozenset({"pending", "leased", "completed", "retryable", "dead"})
_CATCH_UP_POLICIES = frozenset({"all", "latest", "coalesce", "skip"})
_READINESS_STATUSES = frozenset({"ready", "degraded", "not_ready"})
_FOCUS_KINDS = frozenset(
    {"goal", "question", "entity", "clue", "concern", "affect", "pending_input"}
)
_KNOWN_FOCUS_STATUSES = frozenset({"active", "dormant", "promoted", "dismissed", "expired"})
_AUTHORITIES = frozenset({"host", "platform", "adapter", "system", "user", "model"})
_PROMOTION_TARGETS = frozenset({"note", "task", "episode", "claim"})
_RECENT_SOURCES = frozenset({"generation", "canonical"})
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _require_non_empty_str(value: object, key: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value:
        errors.append(f"{key} must be a non-empty string")


def _require_unit_interval(value: object, key: str, errors: list[str], prefix: str = "") -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
        errors.append(f"{prefix}{key} must be a number within [0, 1]")


def _validate_observation_batch_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    records = value.get("records")
    if not isinstance(records, list) or not records:
        errors.append("records must be a non-empty array")
        return tuple(errors)
    if len(records) > 1000:
        errors.append("records must not exceed 1000 items")
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            errors.append(f"records[{index}] must be an object")
            continue
        if record.get("role") not in _ROLES:
            errors.append(f"records[{index}].role must be a known role")
        if record.get("effect_state", "committed") not in _EFFECT_STATES:
            errors.append(f"records[{index}].effect_state must be committed or partial")
        for key in ("agent_id", "kind", "idempotency_key"):
            if not isinstance(record.get(key), str) or not record.get(key):
                errors.append(f"records[{index}].{key} must be a non-empty string")
        for key in ("occurred_us", "committed_us"):
            stamp = record.get(key)
            if type(stamp) is not int or stamp < 0:
                errors.append(f"records[{index}].{key} must be a non-negative integer")
        cursor = record.get("source_cursor")
        if cursor is not None and (
            not isinstance(cursor, str) or _CURSOR_PATTERN.fullmatch(cursor) is None
        ):
            errors.append(
                f"records[{index}].source_cursor must be a decimal integer "
                "string without leading zeros"
            )
        if record.get("session_id") is not None and record.get("space_id") is None:
            errors.append(f"records[{index}].session_id requires space_id")
    lease_epoch = value.get("lease_epoch")
    if lease_epoch is not None and (type(lease_epoch) is not int or lease_epoch < 0):
        errors.append("lease_epoch must be a non-negative integer")
    return tuple(errors)


def _validate_observation_batch_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("accepted_observation_ids", "duplicate_observation_ids"):
        items = value.get(key)
        if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
            errors.append(f"{key} must be an array of non-empty strings")
    for key in ("source_watermark", "agent_watermark"):
        item = value.get(key)
        if item is not None and (type(item) is not int or item < 0):
            errors.append(f"{key} must be null or a non-negative integer")
    if type(value.get("outbox_enqueued")) is not int or value.get("outbox_enqueued", -1) < 0:
        errors.append("outbox_enqueued must be a non-negative integer")
    cursors = value.get("cursors")
    if cursors is not None and not isinstance(cursors, Mapping):
        errors.append("cursors must be an object")
    return tuple(errors)


def _validate_source_cursor_envelope(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if not isinstance(value.get("source_stream"), str) or not value.get("source_stream"):
        errors.append("source_stream must be a non-empty string")
    if value.get("gap_policy") not in _GAP_POLICIES:
        errors.append("gap_policy must be accept, reject or mark")
    position = value.get("cursor_position")
    if position is not None and (type(position) is not int or position < 0):
        errors.append("cursor_position must be null or a non-negative integer")
    return tuple(errors)


def _validate_lease_acquire_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("agent_id", "holder_app_instance_id"):
        if not isinstance(value.get(key), str) or not value.get(key):
            errors.append(f"{key} must be a non-empty string")
    ttl = value.get("ttl_us")
    if type(ttl) is not int or not 1_000_000 <= ttl <= 600_000_000:
        errors.append("ttl_us must be within 1000000..600000000")
    priority = value.get("priority", 0)
    if type(priority) is not int or not 0 <= priority <= 100:
        errors.append("priority must be within 0..100")
    return tuple(errors)


def _validate_lease_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("lease_id", "tenant_id", "agent_id", "holder_app_instance_id"):
        if not isinstance(value.get(key), str) or not value.get(key):
            errors.append(f"{key} must be a non-empty string")
    if value.get("status") not in _LEASE_STATUSES:
        errors.append("status must be a known lease status")
    epoch = value.get("lease_epoch")
    if type(epoch) is not int or epoch < 0:
        errors.append("lease_epoch must be a non-negative integer")
    expires = value.get("expires_us")
    if type(expires) is not int or expires < 0:
        errors.append("expires_us must be a non-negative integer")
    return tuple(errors)


def _validate_readiness_report(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if value.get("status") not in _READINESS_STATUSES:
        errors.append("status must be ready, degraded or not_ready")
    if not isinstance(value.get("checks"), Mapping):
        errors.append("checks must be an object")
    return tuple(errors)


def _validate_admin_job(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("job_id", "job_kind"):
        if not isinstance(value.get(key), str) or not value.get(key):
            errors.append(f"{key} must be a non-empty string")
    if value.get("status") not in _JOB_STATUSES:
        errors.append("status must be a known outbox status")
    if value.get("lane") not in ("normal", "safety"):
        errors.append("lane must be normal or safety")
    return tuple(errors)


def _validate_schedule_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("schedule_id", "job_kind"):
        if not isinstance(value.get(key), str) or not value.get(key):
            errors.append(f"{key} must be a non-empty string")
    if value.get("catch_up_policy") not in _CATCH_UP_POLICIES:
        errors.append("catch_up_policy must be a known policy")
    if not isinstance(value.get("enabled"), bool):
        errors.append("enabled must be a boolean")
    return tuple(errors)


def _validate_recent_context_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("agent_id", "space_id"):
        _require_non_empty_str(value.get(key), key, errors)
    builder_version = value.get("builder_version")
    if type(builder_version) is not int or builder_version < 1:
        errors.append("builder_version must be a positive integer")
    watermark = value.get("source_watermark")
    if type(watermark) is not int or watermark < 0:
        errors.append("source_watermark must be a non-negative integer")
    if value.get("source") not in _RECENT_SOURCES:
        errors.append("source must be generation or canonical")
    token_estimate = value.get("token_estimate")
    if type(token_estimate) is not int or token_estimate < 0:
        errors.append("token_estimate must be a non-negative integer")
    result_hash = value.get("result_hash")
    if not isinstance(result_hash, str) or _HASH_PATTERN.fullmatch(result_hash) is None:
        errors.append("result_hash must be a SHA-256 hex string")
    hot_refs = value.get("hot_observation_refs")
    if hot_refs is not None:
        if not isinstance(hot_refs, list):
            errors.append("hot_observation_refs must be an array")
        else:
            for index, ref in enumerate(hot_refs):
                if not isinstance(ref, Mapping) or not isinstance(ref.get("observation_id"), str):
                    errors.append(f"hot_observation_refs[{index}].observation_id must be a string")
    segments = value.get("summary_segments")
    if segments is not None and not isinstance(segments, list):
        errors.append("summary_segments must be an array")
    expires = value.get("expires_us")
    if expires is not None and (type(expires) is not int or expires < 0):
        errors.append("expires_us must be null or a non-negative integer")
    return tuple(errors)


def _validate_state_put_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("agent_id"), "agent_id", errors)
    if not isinstance(value.get("value"), Mapping):
        errors.append("value must be an object")
    if value.get("source_authority") not in _AUTHORITIES:
        errors.append("source_authority must be a known authority")
    for key in ("observed_us", "ttl_us", "expires_us", "expected_revision"):
        item = value.get(key)
        if item is not None and (type(item) is not int or item < 0):
            errors.append(f"{key} must be null or a non-negative integer")
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append("session_id requires space_id")
    return tuple(errors)


def _validate_state_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("record_id", "namespace", "key", "agent_id"):
        _require_non_empty_str(value.get(key), key, errors)
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    if not isinstance(value.get("value"), Mapping):
        errors.append("value must be an object")
    authority = value.get("source_authority")
    if authority not in _AUTHORITIES and (not isinstance(authority, str) or not authority):
        # Forward compatibility: an authority added after this SDK build is
        # tolerated on read paths (the server remains authoritative).
        errors.append("source_authority must be a non-empty string")
    return tuple(errors)


def _validate_state_history_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("record_id", "namespace", "key"):
        _require_non_empty_str(value.get(key), key, errors)
    revisions = value.get("revisions")
    if not isinstance(revisions, list):
        errors.append("revisions must be an array")
        return tuple(errors)
    for index, item in enumerate(revisions):
        if not isinstance(item, Mapping):
            errors.append(f"revisions[{index}] must be an object")
            continue
        revision = item.get("revision")
        if type(revision) is not int or revision < 1:
            errors.append(f"revisions[{index}].revision must be a positive integer")
        if not isinstance(item.get("value"), Mapping):
            errors.append(f"revisions[{index}].value must be an object")
        if item.get("source_authority") not in _AUTHORITIES:
            errors.append(f"revisions[{index}].source_authority must be a known authority")
    return tuple(errors)


def _validate_focus_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("agent_id"), "agent_id", errors)
    if value.get("kind") not in _FOCUS_KINDS:
        errors.append("kind must be a known focus kind")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary or len(summary) > 2000:
        errors.append("summary must be 1..2000 characters")
    for key in ("salience", "importance", "activation"):
        if key in value:
            _require_unit_interval(value.get(key), key, errors)
    refs = value.get("source_refs")
    if refs is not None:
        if not isinstance(refs, list):
            errors.append("source_refs must be an array")
        else:
            for index, ref in enumerate(refs):
                if not isinstance(ref, Mapping):
                    errors.append(f"source_refs[{index}] must be an object")
                    continue
                for key in ("resource_type", "resource_id"):
                    _require_non_empty_str(ref.get(key), f"source_refs[{index}].{key}", errors)
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append("session_id requires space_id")
    return tuple(errors)


def _validate_focus_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("focus_item_id", "agent_id", "summary"):
        _require_non_empty_str(value.get(key), key, errors)
    if value.get("kind") not in _FOCUS_KINDS:
        errors.append("kind must be a known focus kind")
    # Forward compatibility on views: a status added after this SDK build is
    # tolerated (the server owns the state machine); known ones are checked.
    status = value.get("status")
    if not isinstance(status, str) or not status:
        errors.append("status must be a non-empty string")
    elif status not in _KNOWN_FOCUS_STATUSES:
        pass  # unknown future status — ignored, never fatal on a view
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    for key in ("salience", "activation", "importance"):
        if key in value:
            _require_unit_interval(value.get(key), key, errors)
    promotion_target = value.get("promotion_target_type")
    if (
        promotion_target is not None
        and promotion_target not in _PROMOTION_TARGETS
        and (not isinstance(promotion_target, str) or not promotion_target)
    ):
        errors.append("promotion_target_type must be a non-empty string")
    return tuple(errors)


# ---------------------------------------------------------------------------
# Phase 4: notes, tasks, steps, triggers, cognitive events (S10-S12)

_NOTE_KINDS = ("important", "idea", "follow_up", "promise", "question", "observation")
_TASK_ORIGINS = ("explicit_tool", "admin", "policy", "conversation", "background")
_TASK_TRANSITION_TARGETS = ("activate", "wait", "block", "complete", "cancel", "archive")
_STEP_TRANSITION_TARGETS = (
    "start",
    "wait",
    "block",
    "complete",
    "skip",
    "cancel",
    "requeue",
)
_TRIGGER_KINDS = (
    "at_time",
    "recurrence",
    "observation_kind",
    "state_condition",
    "task_transition",
)
_TRIGGER_CATCH_UP_POLICIES = ("all", "latest", "coalesce", "skip")


def _require_lease_proof(value: Mapping[str, object], errors: list[str]) -> None:
    """Optional §25.3 lease proof fields: validate shape when present."""
    lease_id = value.get("lease_id")
    if lease_id is not None and (not isinstance(lease_id, str) or not lease_id):
        errors.append("lease_id must be a non-empty string")
    lease_epoch = value.get("lease_epoch")
    if lease_epoch is not None and (type(lease_epoch) is not int or lease_epoch < 0):
        errors.append("lease_epoch must be a non-negative integer")


def _validate_note_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("agent_id"), "agent_id", errors)
    if value.get("kind") not in _NOTE_KINDS:
        errors.append("kind must be a known note kind")
    title = value.get("title")
    if not isinstance(title, str) or not title or len(title) > 500:
        errors.append("title must be 1..500 characters")
    body = value.get("body")
    if body is not None and (not isinstance(body, str) or len(body) > 20000):
        errors.append("body must be at most 20000 characters")
    if "importance" in value:
        _require_unit_interval(value.get("importance"), "importance", errors)
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append("session_id requires space_id")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_note_update_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    revision = value.get("expected_revision")
    if type(revision) is not int or revision < 1:
        errors.append("expected_revision must be a positive integer")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_note_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("note_id", "agent_id", "title"):
        _require_non_empty_str(value.get(key), key, errors)
    if value.get("kind") not in _NOTE_KINDS:
        # Forward compatibility: kinds added after this SDK build are the
        # server's to define; only malformed values are fatal.
        kind = value.get("kind")
        if not isinstance(kind, str) or not kind:
            errors.append("kind must be a non-empty string")
    status = value.get("status")
    if not isinstance(status, str) or not status:
        errors.append("status must be a non-empty string")
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    if "importance" in value:
        _require_unit_interval(value.get("importance"), "importance", errors)
    return tuple(errors)


def _validate_task_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("agent_id"), "agent_id", errors)
    # origin/owner_kind are optional server-defaulted fields: the JSON Schema
    # only requires agent_id+title, so the SDK accepts the same minimum.
    origin = value.get("origin")
    if origin is not None and origin not in _TASK_ORIGINS:
        errors.append("origin must be a known task origin")
    owner_kind = value.get("owner_kind")
    if owner_kind is not None and owner_kind not in ("agent", "joint", "entity", "space_group"):
        errors.append("owner_kind must be a known owner kind")
    title = value.get("title")
    if not isinstance(title, str) or not title or len(title) > 500:
        errors.append("title must be 1..500 characters")
    priority = value.get("priority")
    if priority is not None and (type(priority) is not int or not 0 <= priority <= 9):
        errors.append("priority must be within 0..9")
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append("session_id requires space_id")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_task_update_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    revision = value.get("expected_revision")
    if type(revision) is not int or revision < 1:
        errors.append("expected_revision must be a positive integer")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_task_transition_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if value.get("target") not in _TASK_TRANSITION_TARGETS:
        errors.append("target must be a known task transition")
    revision = value.get("expected_revision")
    if type(revision) is not int or revision < 1:
        errors.append("expected_revision must be a positive integer")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason:
        errors.append("reason is required")
    origin = value.get("origin")
    if origin is not None and origin not in _TASK_ORIGINS:
        errors.append("origin must be a known task origin")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_task_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("task_id", "agent_id", "title"):
        _require_non_empty_str(value.get(key), key, errors)
    owner_kind = value.get("owner_kind")
    if (
        owner_kind is not None
        and owner_kind not in ("agent", "joint", "entity", "space_group")
        and (not isinstance(owner_kind, str) or not owner_kind)
    ):
        errors.append("owner_kind must be a non-empty string")
    status = value.get("status")
    if not isinstance(status, str) or not status:
        errors.append("status must be a non-empty string")
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    steps = value.get("steps")
    if steps is not None:
        if not isinstance(steps, list):
            errors.append("steps must be an array")
        else:
            for index, step in enumerate(steps):
                step_errors = _validate_task_step_view(step)
                errors.extend(f"steps[{index}].{item}" for item in step_errors)
    return tuple(errors)


def _validate_task_step_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("task_step_id", "task_id", "stable_key", "title"):
        _require_non_empty_str(value.get(key), key, errors)
    status = value.get("status")
    if not isinstance(status, str) or not status:
        errors.append("status must be a non-empty string")
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    return tuple(errors)


def _validate_task_step_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    stable_key = value.get("stable_key")
    if not isinstance(stable_key, str) or not stable_key or len(stable_key) > 256:
        errors.append("stable_key must be 1..256 characters")
    title = value.get("title")
    if not isinstance(title, str) or not title or len(title) > 500:
        errors.append("title must be 1..500 characters")
    ordinal = value.get("ordinal")
    if ordinal is not None and (type(ordinal) is not int or ordinal < 0):
        errors.append("ordinal must be a non-negative integer")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_task_step_transition_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if value.get("target") not in _STEP_TRANSITION_TARGETS:
        errors.append("target must be a known step transition")
    revision = value.get("expected_revision")
    if type(revision) is not int or revision < 1:
        errors.append("expected_revision must be a positive integer")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason:
        errors.append("reason is required")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_task_dependency_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("predecessor_step_id"), "predecessor_step_id", errors)
    _require_non_empty_str(value.get("successor_step_id"), "successor_step_id", errors)
    condition = value.get("condition", "completed")
    if condition not in ("completed", "completed_or_skipped"):
        errors.append("condition must be a known dependency condition")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_task_trigger_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    kind = value.get("kind")
    if kind not in _TRIGGER_KINDS:
        errors.append("kind must be a known trigger kind")
    catch_up = value.get("catch_up_policy", "all")
    if catch_up not in _TRIGGER_CATCH_UP_POLICIES:
        errors.append("catch_up_policy must be a known policy")
    timezone_name = value.get("timezone", "UTC")
    if not isinstance(timezone_name, str) or not timezone_name:
        errors.append("timezone must be a non-empty IANA name")
    misfire = value.get("misfire_grace_us")
    if misfire is not None and (type(misfire) is not int or misfire < 0):
        errors.append("misfire_grace_us must be a non-negative integer")
    max_occ = value.get("max_occurrences_per_run")
    if max_occ is not None and (type(max_occ) is not int or not 1 <= max_occ <= 1000):
        errors.append("max_occurrences_per_run must be within 1..1000")
    if kind in ("at_time", "recurrence") and not isinstance(value.get("schedule_spec"), Mapping):
        errors.append("schedule_spec must be an object for time triggers")
    if kind not in ("at_time", "recurrence") and not isinstance(
        value.get("condition_spec"), Mapping
    ):
        errors.append("condition_spec must be an object for condition triggers")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_trigger_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("trigger_id", "task_id"):
        _require_non_empty_str(value.get(key), key, errors)
    kind = value.get("kind")
    if not isinstance(kind, str) or not kind:
        errors.append("kind must be a non-empty string")
    if not isinstance(value.get("enabled"), bool):
        errors.append("enabled must be a boolean")
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    return tuple(errors)


def _validate_cognitive_event_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("cognitive_event_id", "agent_id", "kind", "object_type", "object_id"):
        _require_non_empty_str(value.get(key), key, errors)
    status = value.get("status")
    if not isinstance(status, str) or not status:
        errors.append("status must be a non-empty string")
    attempts = value.get("delivery_attempts")
    if type(attempts) is not int or attempts < 0:
        errors.append("delivery_attempts must be a non-negative integer")
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    return tuple(errors)


def _validate_cognitive_event_ack_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    ack_token = value.get("ack_token")
    if ack_token is not None and (not isinstance(ack_token, str) or not ack_token):
        errors.append("ack_token must be a non-empty string")
    _require_lease_proof(value, errors)
    return tuple(errors)


# ---------------------------------------------------------------------------
# Phase 5: long-term memory — claims, episodes, relations, artifacts,
# forget/retention/legal holds (S13, S19)

_CLAIM_CATEGORIES = (
    "identity",
    "preference",
    "relationship",
    "fact",
    "community",
    "procedure",
    "self_narrative",
)
_CLAIM_STATUSES = (
    "active",
    "disputed",
    "superseded",
    "retracted",
    "expired",
    "archived",
    "tombstoned",
)
_CORRECT_MODES = ("supersede", "dispute", "retract")
_EVIDENCE_RELATIONS = ("supports", "contradicts", "corrects")
_MEMORY_AUTHORITIES = (
    "agent_inference",
    "extracted",
    "user_statement",
    "platform_verified",
    "admin_confirmed",
    "explicit_correction",
)
_EVIDENCE_SOURCE_TYPES = ("observation", "artifact", "episode", "claim", "note")
_EPISODE_TRANSITION_TARGETS = ("seal", "supersede", "archive", "reopen")
_ARTIFACT_STORAGE_KINDS = ("inline", "local_blob", "external_ref")
_FORGET_SELECTOR_KINDS = ("resource", "subject_predicate", "session", "space", "data_request")
_RETENTION_ACTIONS = ("decay", "archive", "delete")
_RETENTION_RESOURCE_TYPES = ("claim", "note", "episode", "relation", "observation")


def _require_known_or_non_empty(
    value: Mapping[str, object], key: str, known: tuple[str, ...], errors: list[str]
) -> None:
    """Forward-compatible enum check for VIEW fields.

    Known enum members pass; anything else only has to be a non-empty string
    (the server owns the state machine and may add members after this build).
    """
    item = value.get(key)
    if item in known:
        return
    if not isinstance(item, str) or not item:
        errors.append(f"{key} must be a non-empty string")


def _require_string_array(value: object, key: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{key} must be an array of strings")


def _require_resource_refs(value: object, key: str, errors: list[str]) -> None:
    """source_refs / observation_refs / evidence_refs rows (S13 resource ref)."""
    if value is None:
        return
    if not isinstance(value, list):
        errors.append(f"{key} must be an array")
        return
    for index, ref in enumerate(value):
        if not isinstance(ref, Mapping):
            errors.append(f"{key}[{index}] must be an object")
            continue
        for field in ("resource_type", "resource_id"):
            if not isinstance(ref.get(field), str) or not ref.get(field):
                errors.append(f"{key}[{index}].{field} must be a non-empty string")


def _require_evidence_rows(
    value: object,
    errors: list[str],
    *,
    field: str = "evidence",
    relations: tuple[str, ...] = _EVIDENCE_RELATIONS,
    minimum: int = 0,
) -> None:
    """Evidence rows (S13): every row names a source and how it relates.

    ``minimum=1`` mirrors the JSON Schema ``minItems: 1`` on remember/relation
    writes; the correct endpoint accepts an absent list but validates rows.
    """
    if not isinstance(value, list):
        errors.append(f"{field} must be an array")
        return
    if len(value) < minimum:
        errors.append(f"{field} must have at least {minimum} item(s)")
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            errors.append(f"{field}[{index}] must be an object")
            continue
        if row.get("source_type") not in _EVIDENCE_SOURCE_TYPES:
            errors.append(f"{field}[{index}].source_type must be a known source type")
        if not isinstance(row.get("source_id"), str) or not row.get("source_id"):
            errors.append(f"{field}[{index}].source_id must be a non-empty string")
        if row.get("relation") not in relations:
            errors.append(f"{field}[{index}].relation must be a known evidence relation")
        if row.get("source_authority") not in _MEMORY_AUTHORITIES:
            errors.append(f"{field}[{index}].source_authority must be a known authority")


def _validate_claim_remember_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("agent_id"), "agent_id", errors)
    predicate = value.get("predicate")
    if not isinstance(predicate, str) or not predicate or len(predicate) > 256:
        errors.append("predicate must be 1..256 characters")
    if "value" not in value:
        errors.append("value is required")
    _require_evidence_rows(value.get("evidence"), errors, minimum=1)
    if value.get("category", "fact") not in _CLAIM_CATEGORIES:
        errors.append("category must be a known claim category")
    if value.get("source_authority", "user_statement") not in _MEMORY_AUTHORITIES:
        errors.append("source_authority must be a known authority")
    for key in ("confidence", "importance", "accessibility"):
        if key in value:
            _require_unit_interval(value.get(key), key, errors)
    for key in ("subject_entity_id", "canonical_text", "extractor_version"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not item):
            errors.append(f"{key} must be a non-empty string")
    if "subject_is_self" in value and not isinstance(value.get("subject_is_self"), bool):
        errors.append("subject_is_self must be a boolean")
    for key in ("valid_from_us", "valid_until_us"):
        item = value.get(key)
        if item is not None and (type(item) is not int or item < 0):
            errors.append(f"{key} must be null or a non-negative integer")
    _require_string_array(value.get("privacy_labels"), "privacy_labels", errors)
    _require_resource_refs(value.get("source_refs"), "source_refs", errors)
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append("session_id requires space_id")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_claim_correct_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    revision = value.get("expected_revision")
    if type(revision) is not int or revision < 1:
        errors.append("expected_revision must be a positive integer")
    _require_non_empty_str(value.get("reason"), "reason", errors)
    if value.get("mode", "supersede") not in _CORRECT_MODES:
        errors.append("mode must be a known correction mode")
    if value.get("evidence") is not None:
        _require_evidence_rows(value.get("evidence"), errors)
    if value.get("source_authority") is not None and (
        value.get("source_authority") not in _MEMORY_AUTHORITIES
    ):
        errors.append("source_authority must be a known authority")
    canonical_text = value.get("canonical_text")
    if canonical_text is not None and not isinstance(canonical_text, str):
        errors.append("canonical_text must be a string")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_claim_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in (
        "claim_id",
        "agent_id",
        "subject_entity_id",
        "current_subject_entity_id",
        "predicate",
    ):
        _require_non_empty_str(value.get(key), key, errors)
    # Forward compatibility on views: category/status/source_authority are the
    # server's enums; unknown future members are tolerated if well-formed.
    _require_known_or_non_empty(value, "category", _CLAIM_CATEGORIES, errors)
    _require_known_or_non_empty(value, "status", _CLAIM_STATUSES, errors)
    _require_known_or_non_empty(value, "source_authority", _MEMORY_AUTHORITIES, errors)
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    for key in ("confidence", "importance", "accessibility"):
        if key in value:
            _require_unit_interval(value.get(key), key, errors)
    for key in ("evidence_count", "recorded_at_us"):
        item = value.get(key)
        if item is not None and (type(item) is not int or item < 0):
            errors.append(f"{key} must be a non-negative integer")
    _require_string_array(value.get("privacy_labels"), "privacy_labels", errors)
    return tuple(errors)


def _validate_claim_revision_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    _require_known_or_non_empty(value, "status", _CLAIM_STATUSES, errors)
    for key in ("canonical_text",):
        if value.get(key) is not None and not isinstance(value.get(key), str):
            errors.append(f"{key} must be a string")
    if "value" not in value:
        errors.append("value is required")
    item = value.get("recorded_at_us")
    if item is not None and (type(item) is not int or item < 0):
        errors.append("recorded_at_us must be a non-negative integer")
    _require_string_array(value.get("privacy_labels"), "privacy_labels", errors)
    content_hash = value.get("content_hash")
    if not isinstance(content_hash, str) or _HASH_PATTERN.fullmatch(content_hash) is None:
        errors.append("content_hash must be a SHA-256 hex string")
    superseded = value.get("superseded_at_us")
    if superseded is not None and (type(superseded) is not int or superseded < 0):
        errors.append("superseded_at_us must be null or a non-negative integer")
    return tuple(errors)


def _validate_claim_search_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    items = value.get("items")
    if not isinstance(items, list):
        errors.append("items must be an array")
        return tuple(errors)
    for index, item in enumerate(items):
        item_errors = _validate_claim_view(item)
        errors.extend(f"items[{index}].{error}" for error in item_errors)
    return tuple(errors)


def _validate_claim_history_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if value.get("claim_id") is not None:
        _require_non_empty_str(value.get("claim_id"), "claim_id", errors)
    revisions = value.get("revisions")
    if not isinstance(revisions, list):
        errors.append("revisions must be an array")
        return tuple(errors)
    for index, item in enumerate(revisions):
        item_errors = _validate_claim_revision_view(item)
        errors.extend(f"revisions[{index}].{error}" for error in item_errors)
    return tuple(errors)


def _validate_forget_selector(value: object, key: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{key} must be an object")
        return
    if value.get("kind") not in _FORGET_SELECTOR_KINDS:
        errors.append(f"{key}.kind must be a known forget selector")
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append(f"{key}.session_id requires space_id")


def _validate_memory_forget_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _validate_forget_selector(value.get("selector"), "selector", errors)
    _require_non_empty_str(value.get("reason"), "reason", errors)
    if "erase_content" in value and not isinstance(value.get("erase_content"), bool):
        errors.append("erase_content must be a boolean")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _require_forget_counters(value: Mapping[str, object], errors: list[str]) -> None:
    for key in ("target_count", "erased_count", "protected_skipped", "held_skipped"):
        item = value.get(key)
        if type(item) is not int or item < 0:
            errors.append(f"{key} must be a non-negative integer")
    low = value.get("tombstone_seq_lo")
    if type(low) is not int or low < 0:
        errors.append("tombstone_seq_lo must be a non-negative integer")
    high = value.get("tombstone_seq_hi")
    if high is not None and (type(high) is not int or high < 0):
        errors.append("tombstone_seq_hi must be null or a non-negative integer")


def _validate_memory_forget_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("request_id", "selector_key"):
        _require_non_empty_str(value.get(key), key, errors)
    _require_forget_counters(value, errors)
    return tuple(errors)


def _validate_forget_request_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("request_id", "selector_key", "reason_code"):
        _require_non_empty_str(value.get(key), key, errors)
    created = value.get("created_us")
    if type(created) is not int or created < 0:
        errors.append("created_us must be a non-negative integer")
    _require_forget_counters(value, errors)
    selector = value.get("selector")
    if selector is not None:
        if not isinstance(selector, Mapping):
            errors.append("selector must be an object")
        else:
            _require_known_or_non_empty(selector, "kind", _FORGET_SELECTOR_KINDS, errors)
    return tuple(errors)


def _validate_deletion_ledger_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    requests = value.get("requests")
    if not isinstance(requests, list):
        errors.append("requests must be an array")
        return tuple(errors)
    for index, item in enumerate(requests):
        item_errors = _validate_forget_request_view(item)
        errors.extend(f"requests[{index}].{error}" for error in item_errors)
    return tuple(errors)


def _validate_episode_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("agent_id"), "agent_id", errors)
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary or len(summary) > 8000:
        errors.append("summary must be 1..8000 characters")
    title = value.get("title")
    if title is not None and (not isinstance(title, str) or not title or len(title) > 500):
        errors.append("title must be 1..500 characters")
    for key in ("importance", "arousal"):
        if key in value:
            _require_unit_interval(value.get(key), key, errors)
    valence = value.get("valence")
    if valence is not None and (
        not isinstance(valence, (int, float)) or isinstance(valence, bool) or not -1 <= valence <= 1
    ):
        errors.append("valence must be a number within [-1, 1]")
    for key in ("started_at_us", "ended_at_us"):
        item = value.get(key)
        if item is not None and (type(item) is not int or item < 0):
            errors.append(f"{key} must be null or a non-negative integer")
    participants = value.get("participant_entity_ids")
    if participants is not None and (
        not isinstance(participants, list)
        or not all(isinstance(item, str) and item for item in participants)
    ):
        errors.append("participant_entity_ids must be an array of non-empty strings")
    _require_resource_refs(value.get("observation_refs"), "observation_refs", errors)
    _require_string_array(value.get("privacy_labels"), "privacy_labels", errors)
    extractor = value.get("extractor_version")
    if extractor is not None and not isinstance(extractor, str):
        errors.append("extractor_version must be a string")
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append("session_id requires space_id")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_episode_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("episode_id", "agent_id", "summary"):
        _require_non_empty_str(value.get(key), key, errors)
    # Forward compatibility: episode status is the server's state machine.
    status = value.get("status")
    if not isinstance(status, str) or not status:
        errors.append("status must be a non-empty string")
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    for key in ("importance", "arousal"):
        if key in value:
            _require_unit_interval(value.get(key), key, errors)
    valence = value.get("valence")
    if valence is not None and (
        not isinstance(valence, (int, float)) or isinstance(valence, bool) or not -1 <= valence <= 1
    ):
        errors.append("valence must be a number within [-1, 1]")
    for key in ("started_at_us", "ended_at_us"):
        item = value.get(key)
        if item is not None and (type(item) is not int or item < 0):
            errors.append(f"{key} must be null or a non-negative integer")
    _require_resource_refs(value.get("observation_refs"), "observation_refs", errors)
    _require_string_array(value.get("privacy_labels"), "privacy_labels", errors)
    return tuple(errors)


def _validate_episode_transition_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if value.get("target") not in _EPISODE_TRANSITION_TARGETS:
        errors.append("target must be a known episode transition")
    revision = value.get("expected_revision")
    if type(revision) is not int or revision < 1:
        errors.append("expected_revision must be a positive integer")
    _require_non_empty_str(value.get("reason"), "reason", errors)
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_relation_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("agent_id"), "agent_id", errors)
    for key in ("source_entity_id", "target_entity_id"):
        _require_non_empty_str(value.get(key), key, errors)
    relation_type = value.get("relation_type")
    if not isinstance(relation_type, str) or not relation_type or len(relation_type) > 128:
        errors.append("relation_type must be 1..128 characters")
    # Relation evidence only ever supports the edge (S13 relation semantics).
    _require_evidence_rows(value.get("evidence"), errors, relations=("supports",), minimum=1)
    for key in ("confidence", "importance", "accessibility"):
        if key in value:
            _require_unit_interval(value.get(key), key, errors)
    for key in ("valid_from_us", "valid_until_us"):
        item = value.get(key)
        if item is not None and (type(item) is not int or item < 0):
            errors.append(f"{key} must be null or a non-negative integer")
    _require_string_array(value.get("privacy_labels"), "privacy_labels", errors)
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append("session_id requires space_id")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_relation_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in (
        "relation_id",
        "agent_id",
        "source_entity_id",
        "relation_type",
        "target_entity_id",
    ):
        _require_non_empty_str(value.get(key), key, errors)
    status = value.get("status")
    if not isinstance(status, str) or not status:
        errors.append("status must be a non-empty string")
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        errors.append("revision must be a positive integer")
    for key in ("confidence", "importance", "accessibility"):
        if key in value:
            _require_unit_interval(value.get(key), key, errors)
    evidence_count = value.get("evidence_count")
    if evidence_count is not None and (type(evidence_count) is not int or evidence_count < 0):
        errors.append("evidence_count must be a non-negative integer")
    _require_resource_refs(value.get("evidence_refs"), "evidence_refs", errors)
    _require_string_array(value.get("privacy_labels"), "privacy_labels", errors)
    return tuple(errors)


def _validate_artifact_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("agent_id"), "agent_id", errors)
    if value.get("storage_kind") not in _ARTIFACT_STORAGE_KINDS:
        errors.append("storage_kind must be a known storage kind")
    media_type = value.get("media_type")
    if not isinstance(media_type, str) or not media_type or len(media_type) > 256:
        errors.append("media_type must be 1..256 characters")
    for key in ("content_base64", "external_url"):
        if value.get(key) is not None and not isinstance(value.get(key), str):
            errors.append(f"{key} must be a string")
    source_ref = value.get("source_ref")
    if source_ref is not None:
        _require_resource_refs([source_ref], "source_ref", errors)
    _require_string_array(value.get("privacy_labels"), "privacy_labels", errors)
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append("session_id requires space_id")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_artifact_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("artifact_id", "agent_id", "media_type", "locator"):
        _require_non_empty_str(value.get(key), key, errors)
    _require_known_or_non_empty(value, "storage_kind", _ARTIFACT_STORAGE_KINDS, errors)
    status = value.get("status")
    if not isinstance(status, str) or not status:
        errors.append("status must be a non-empty string")
    content_hash = value.get("content_hash")
    if not isinstance(content_hash, str) or _HASH_PATTERN.fullmatch(content_hash) is None:
        errors.append("content_hash must be a SHA-256 hex string")
    for key in ("size_bytes", "refcount"):
        item = value.get(key)
        if type(item) is not int or item < 0:
            errors.append(f"{key} must be a non-negative integer")
    _require_string_array(value.get("privacy_labels"), "privacy_labels", errors)
    return tuple(errors)


def _validate_retention_policy_set_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if value.get("resource_type") not in _RETENTION_RESOURCE_TYPES:
        errors.append("resource_type must be a known resource type")
    if value.get("action") not in _RETENTION_ACTIONS:
        errors.append("action must be a known retention action")
    threshold = value.get("threshold_days")
    if type(threshold) is not int or threshold < 1:
        errors.append("threshold_days must be a positive integer")
    _require_non_empty_str(value.get("reason"), "reason", errors)
    if value.get("privacy_label") is not None and not isinstance(value.get("privacy_label"), str):
        errors.append("privacy_label must be a string")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_retention_policy_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("policy_id"), "policy_id", errors)
    _require_known_or_non_empty(value, "resource_type", _RETENTION_RESOURCE_TYPES, errors)
    _require_known_or_non_empty(value, "action", _RETENTION_ACTIONS, errors)
    threshold = value.get("threshold_days")
    if type(threshold) is not int or threshold < 1:
        errors.append("threshold_days must be a positive integer")
    version = value.get("policy_version")
    if type(version) is not int or version < 1:
        errors.append("policy_version must be a positive integer")
    if not isinstance(value.get("enabled"), bool):
        errors.append("enabled must be a boolean")
    if value.get("privacy_label") is not None and not isinstance(value.get("privacy_label"), str):
        errors.append("privacy_label must be a string")
    return tuple(errors)


def _validate_retention_policy_list_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    items = value.get("items")
    if not isinstance(items, list):
        errors.append("items must be an array")
        return tuple(errors)
    for index, item in enumerate(items):
        item_errors = _validate_retention_policy_view(item)
        errors.extend(f"items[{index}].{error}" for error in item_errors)
    return tuple(errors)


def _validate_legal_hold_create_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("reason"), "reason", errors)
    for key in ("agent_id", "subject_entity_id"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not item):
            errors.append(f"{key} must be a non-empty string")
    if value.get("session_id") is not None and value.get("space_id") is None:
        errors.append("session_id requires space_id")
    _require_lease_proof(value, errors)
    return tuple(errors)


def _validate_legal_hold_view(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    for key in ("legal_hold_id", "reason_code"):
        _require_non_empty_str(value.get(key), key, errors)
    created = value.get("created_at_us")
    if type(created) is not int or created < 0:
        errors.append("created_at_us must be a non-negative integer")
    released = value.get("released_at_us")
    if released is not None and (type(released) is not int or released < 0):
        errors.append("released_at_us must be null or a non-negative integer")
    for key in ("space_id", "session_id", "agent_id", "subject_entity_id"):
        if value.get(key) is not None and not isinstance(value.get(key), str):
            errors.append(f"{key} must be a string or null")
    return tuple(errors)


def _validate_legal_hold_release_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("reason"), "reason", errors)
    _require_lease_proof(value, errors)
    return tuple(errors)


# -- Phase 6: recall protocol validators (forward-lax on enums) --------------

_CANDIDATE_ID_RE = re.compile(r"^cand:[0-9a-f]{16}$")


def _validate_external_actor_ref(value: object, key: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{key} must be an object")
        return
    _require_non_empty_str(value.get("provider"), f"{key}.provider", errors)
    _require_non_empty_str(value.get("external_id"), f"{key}.external_id", errors)


def _validate_recall_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if value.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    _require_non_empty_str(value.get("request_id"), "request_id", errors)
    scope = value.get("scope")
    if not isinstance(scope, dict):
        errors.append("scope must be an object")
    else:
        _require_non_empty_str(scope.get("agent_id"), "scope.agent_id", errors)
        _require_non_empty_str(scope.get("space_id"), "scope.space_id", errors)
    actors = value.get("actors")
    if not isinstance(actors, list) or not actors:
        errors.append("actors must be a non-empty array")
    else:
        for index, actor in enumerate(actors):
            _validate_external_actor_ref(actor, f"actors[{index}]", errors)
    topic = value.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        errors.append("topic must be a non-empty string")
    purpose = value.get("purpose")
    if not isinstance(purpose, str) or not purpose:
        errors.append("purpose must be a string")
    token_budget = value.get("token_budget")
    if not isinstance(token_budget, int) or isinstance(token_budget, bool) or token_budget < 0:
        errors.append("token_budget must be a non-negative integer")
    deadline_at = value.get("deadline_at")
    if not isinstance(deadline_at, str) or not deadline_at:
        errors.append("deadline_at must be a non-empty string")
    return tuple(errors)


def _validate_recall_candidate(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("candidate must be an object",)
    errors: list[str] = []
    candidate_id = value.get("candidate_id")
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID_RE.match(candidate_id):
        errors.append("candidate_id must match cand:<16 hex>")
    resource_ref = value.get("resource_ref")
    if not isinstance(resource_ref, dict):
        errors.append("resource_ref must be an object")
    else:
        _require_non_empty_str(
            resource_ref.get("resource_type"), "resource_ref.resource_type", errors
        )
        _require_non_empty_str(resource_ref.get("resource_id"), "resource_ref.resource_id", errors)
        revision = resource_ref.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            errors.append("resource_ref.revision must be a positive integer")
    _require_non_empty_str(value.get("content_hash"), "content_hash", errors)
    if not isinstance(value.get("text"), str):
        errors.append("text must be a string")
    if not isinstance(value.get("category"), str):
        errors.append("category must be a string")
    placement = value.get("placement")
    if placement not in ("working", "memory"):
        errors.append("placement must be working or memory")
    scores = value.get("scores")
    if not isinstance(scores, dict):
        errors.append("scores must be an object")
    final_score = value.get("final_score")
    if (
        not isinstance(final_score, (int, float))
        or isinstance(final_score, bool)
        or not 0 <= final_score <= 1
    ):
        errors.append("final_score must be within [0, 1]")
    token_estimate = value.get("token_estimate")
    if (
        not isinstance(token_estimate, int)
        or isinstance(token_estimate, bool)
        or token_estimate < 0
    ):
        errors.append("token_estimate must be a non-negative integer")
    return tuple(errors)


def _validate_degraded_route(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("degraded route must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("route"), "route", errors)
    _require_non_empty_str(value.get("reason_code"), "reason_code", errors)
    if not isinstance(value.get("retryable"), bool):
        errors.append("retryable must be a boolean")
    _require_non_empty_str(value.get("fallback"), "fallback", errors)
    return tuple(errors)


def _validate_recall_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if value.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    _require_non_empty_str(value.get("request_id"), "request_id", errors)
    _require_non_empty_str(value.get("source_watermark"), "source_watermark", errors)
    persona_revision = value.get("persona_revision")
    if (
        not isinstance(persona_revision, int)
        or isinstance(persona_revision, bool)
        or persona_revision < 0
    ):
        errors.append("persona_revision must be a non-negative integer")
    if not isinstance(value.get("persona_content_hash"), str):
        errors.append("persona_content_hash must be a string")
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        errors.append("candidates must be an array")
    else:
        for index, item in enumerate(candidates):
            errors.extend(f"candidates[{index}].{e}" for e in _validate_recall_candidate(item))
    for key in ("pending_event_ids", "completed_routes"):
        if not isinstance(value.get(key), list):
            errors.append(f"{key} must be an array")
    degraded = value.get("degraded_routes")
    if not isinstance(degraded, list):
        errors.append("degraded_routes must be an array")
    else:
        for index, item in enumerate(degraded):
            errors.extend(f"degraded_routes[{index}].{e}" for e in _validate_degraded_route(item))
    if not isinstance(value.get("partial"), bool):
        errors.append("partial must be a boolean")
    for key in ("cache_until", "next_wake_at"):
        if value.get(key) is not None and not isinstance(value.get(key), str):
            errors.append(f"{key} must be a string or null")
    trace = value.get("trace", None)
    if trace is not None and not isinstance(trace, dict):
        errors.append("trace must be an object or null")
    return tuple(errors)


def _validate_recall_usage_report_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("host_cycle_id"), "host_cycle_id", errors)
    persona_revision = value.get("persona_revision")
    if (
        not isinstance(persona_revision, int)
        or isinstance(persona_revision, bool)
        or persona_revision < 0
    ):
        errors.append("persona_revision must be a non-negative integer")
    for key in (
        "returned_candidate_ids",
        "host_selected_candidate_ids",
        "model_visible_candidate_ids",
    ):
        ids = value.get(key)
        if not isinstance(ids, list) or not all(
            isinstance(item, str) and _CANDIDATE_ID_RE.match(item) for item in ids
        ):
            errors.append(f"{key} must be an array of cand:<16 hex> ids")
    _require_non_empty_str(value.get("reported_at"), "reported_at", errors)
    return tuple(errors)


def _validate_recall_usage_report_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("report_id"), "report_id", errors)
    if not isinstance(value.get("created"), bool):
        errors.append("created must be a boolean")
    _require_non_empty_str(value.get("request_id"), "request_id", errors)
    stages = value.get("stages")
    if not isinstance(stages, dict):
        errors.append("stages must be an object")
    else:
        for key in (
            "retrieved_count",
            "returned_count",
            "host_selected_count",
            "model_visible_count",
        ):
            count = stages.get(key)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                errors.append(f"stages.{key} must be a non-negative integer")
    return tuple(errors)


def _validate_entity_profile_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    if value.get("subject_kind") not in ("entity", "relationship", "space_group"):
        errors.append("subject_kind must be entity|relationship|space_group")
    _require_non_empty_str(value.get("subject_id"), "subject_id", errors)
    if value.get("source") not in ("projection", "canonical_fallback"):
        errors.append("source must be projection|canonical_fallback")
    generation_id = value.get("generation_id")
    if generation_id is not None and not isinstance(generation_id, str):
        errors.append("generation_id must be a string or null")
    builder_version = value.get("builder_version")
    if (
        not isinstance(builder_version, int)
        or isinstance(builder_version, bool)
        or builder_version < 1
    ):
        errors.append("builder_version must be a positive integer")
    for key in ("source_watermark", "tombstone_watermark"):
        mark = value.get(key)
        if not isinstance(mark, int) or isinstance(mark, bool) or mark < 0:
            errors.append(f"{key} must be a non-negative integer")
    fields = value.get("fields")
    if not isinstance(fields, list):
        errors.append("fields must be an array")
    else:
        for index, item in enumerate(fields):
            if not isinstance(item, Mapping):
                errors.append(f"fields[{index}] must be an object")
                continue
            _require_non_empty_str(item.get("field"), f"fields[{index}].field", errors)
            _require_non_empty_str(item.get("agent_id"), f"fields[{index}].agent_id", errors)
            for key in ("space_group_id", "space_id", "session_id"):
                value = item.get(key)
                if value is not None and not isinstance(value, str):
                    errors.append(f"fields[{index}].{key} must be a string or null")
            labels = item.get("privacy_labels")
            if not isinstance(labels, list) or not all(
                isinstance(label, str) and label for label in labels
            ):
                errors.append(f"fields[{index}].privacy_labels must be an array of strings")
            if not isinstance(item.get("summary_text"), str):
                errors.append(f"fields[{index}].summary_text must be a string")
            if item.get("conflict_state") not in ("single", "conflict", "disputed"):
                errors.append(f"fields[{index}].conflict_state must be single|conflict|disputed")
            freshness = item.get("freshness_us")
            if not isinstance(freshness, int) or isinstance(freshness, bool) or freshness < 0:
                errors.append(f"fields[{index}].freshness_us must be a non-negative integer")
            sources = item.get("sources")
            if not isinstance(sources, list) or not sources:
                errors.append(f"fields[{index}].sources must be a non-empty array")
            else:
                for source_index, source in enumerate(sources):
                    if not isinstance(source, Mapping):
                        errors.append(f"fields[{index}].sources[{source_index}] must be an object")
                        continue
                    _require_non_empty_str(
                        source.get("claim_id"),
                        f"fields[{index}].sources[{source_index}].claim_id",
                        errors,
                    )
                    revision = source.get("revision")
                    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                        errors.append(
                            f"fields[{index}].sources[{source_index}].revision "
                            "must be a positive integer"
                        )
    return tuple(errors)


def _validate_search_request(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    _require_non_empty_str(value.get("agent_id"), "agent_id", errors)
    query = value.get("query")
    if not isinstance(query, str) or not query:
        errors.append("query must be a non-empty string")
    limit = value.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        errors.append("limit must be within 1..200")
    return tuple(errors)


def _validate_search_response(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ("root must be an object",)
    errors: list[str] = []
    results = value.get("results")
    if not isinstance(results, list):
        errors.append("results must be an array")
        return tuple(errors)
    for index, item in enumerate(results):
        errors.extend(f"results[{index}].{e}" for e in _validate_recall_candidate(item))
    return tuple(errors)


def validate_contract(schema: str, value: object) -> tuple[str, ...]:
    validators = {
        "capabilities": _validate_capabilities,
        "error-envelope": _validate_error_envelope,
        "version-manifest": _validate_version_manifest,
        "observation-batch-request": _validate_observation_batch_request,
        "observation-batch-response": _validate_observation_batch_response,
        "source-cursor-envelope": _validate_source_cursor_envelope,
        "lease-acquire-request": _validate_lease_acquire_request,
        "lease-view": _validate_lease_view,
        "readiness-report": _validate_readiness_report,
        "admin-job": _validate_admin_job,
        "schedule-view": _validate_schedule_view,
        "recent-context-view": _validate_recent_context_view,
        "state-put-request": _validate_state_put_request,
        "state-view": _validate_state_view,
        "state-history-response": _validate_state_history_response,
        "focus-create-request": _validate_focus_create_request,
        "focus-view": _validate_focus_view,
        "note-create-request": _validate_note_create_request,
        "note-update-request": _validate_note_update_request,
        "note-view": _validate_note_view,
        "task-create-request": _validate_task_create_request,
        "task-update-request": _validate_task_update_request,
        "task-transition-request": _validate_task_transition_request,
        "task-view": _validate_task_view,
        "task-step-create-request": _validate_task_step_create_request,
        "task-step-transition-request": _validate_task_step_transition_request,
        "task-dependency-create-request": _validate_task_dependency_create_request,
        "task-trigger-create-request": _validate_task_trigger_create_request,
        "trigger-view": _validate_trigger_view,
        "cognitive-event-view": _validate_cognitive_event_view,
        "cognitive-event-ack-request": _validate_cognitive_event_ack_request,
        "claim-remember-request": _validate_claim_remember_request,
        "claim-correct-request": _validate_claim_correct_request,
        "claim-view": _validate_claim_view,
        "claim-revision-view": _validate_claim_revision_view,
        "claim-search-response": _validate_claim_search_response,
        "claim-history-response": _validate_claim_history_response,
        "memory-forget-request": _validate_memory_forget_request,
        "memory-forget-view": _validate_memory_forget_view,
        "forget-request-view": _validate_forget_request_view,
        "deletion-ledger-response": _validate_deletion_ledger_response,
        "episode-create-request": _validate_episode_create_request,
        "episode-view": _validate_episode_view,
        "episode-transition-request": _validate_episode_transition_request,
        "relation-create-request": _validate_relation_create_request,
        "relation-view": _validate_relation_view,
        "artifact-create-request": _validate_artifact_create_request,
        "artifact-view": _validate_artifact_view,
        "retention-policy-set-request": _validate_retention_policy_set_request,
        "retention-policy-view": _validate_retention_policy_view,
        "retention-policy-list-response": _validate_retention_policy_list_response,
        "legal-hold-create-request": _validate_legal_hold_create_request,
        "legal-hold-view": _validate_legal_hold_view,
        "legal-hold-release-request": _validate_legal_hold_release_request,
        "recall-request": _validate_recall_request,
        "recall-response": _validate_recall_response,
        "entity-profile-response": _validate_entity_profile_response,
        "recall-usage-report-request": _validate_recall_usage_report_request,
        "recall-usage-report-response": _validate_recall_usage_report_response,
        "search-request": _validate_search_request,
        "search-response": _validate_search_response,
    }
    validator = validators.get(schema)
    if validator is None:
        return (f"unknown schema: {schema}",)
    return validator(value)
