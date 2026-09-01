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
    }
    validator = validators.get(schema)
    if validator is None:
        return (f"unknown schema: {schema}",)
    return validator(value)
