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
    }
    validator = validators.get(schema)
    if validator is None:
        return (f"unknown schema: {schema}",)
    return validator(value)
