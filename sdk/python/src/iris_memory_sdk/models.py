"""Forward-compatible SDK models and validators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


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


def validate_contract(schema: str, value: object) -> tuple[str, ...]:
    validators = {
        "capabilities": _validate_capabilities,
        "error-envelope": _validate_error_envelope,
        "version-manifest": _validate_version_manifest,
    }
    validator = validators.get(schema)
    if validator is None:
        return (f"unknown schema: {schema}",)
    return validator(value)
