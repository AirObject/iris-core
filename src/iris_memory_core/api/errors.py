"""Stable, low-disclosure HTTP error mapping."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from iris_memory_core.domain.errors import DomainError

ERROR_STATUS_BY_CODE: dict[str, int] = {
    "access_denied": 403,
    "artifact_invalid": 400,
    "binding_conflict": 409,
    "conflict": 409,
    "cursor_gap": 409,
    "database_busy": 503,
    "deadline_exceeded": 504,
    "evidence_invalid": 400,
    "evidence_required": 400,
    "history_unavailable": 410,
    "idempotency_in_progress": 409,
    "idempotency_key_reused": 409,
    "idempotency_unavailable": 503,
    "identity_not_found": 404,
    "internal_error": 500,
    "invalid_request": 400,
    "invalid_scope": 400,
    "invalid_state_transition": 409,
    "lease_expired": 409,
    "lease_fenced": 409,
    "lease_held": 409,
    "legal_hold_active": 409,
    "minimum_watermark_unavailable": 503,
    "not_found": 404,
    "not_ready": 503,
    "persona_base_revision_stale": 409,
    "persona_policy_denied": 403,
    "protected_resource": 409,
    "provider_unavailable": 503,
    "reason_required": 400,
    "redirect_cycle": 409,
    "redirect_depth_exceeded": 409,
    "revision_mismatch": 409,
    "schema_incompatible": 503,
    "scope_violation": 403,
    "sqlite_runtime_not_allowed": 503,
    "storage_full": 507,
    "subject_ambiguous": 409,
    "task_dependency_cycle": 409,
    "unsafe_procedure_claim": 400,
    "unsupported_version": 400,
}

_DENIED_DETAIL_PARTS = (
    "token",
    "secret",
    "password",
    "body",
    "content",
    "path",
    "locator",
    "stack",
    "traceback",
)


def request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else str(uuid.uuid4())


def _safe_details(details: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in details.items():
        lowered = key.lower()
        if any(part in lowered for part in _DENIED_DETAIL_PARTS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            # Raw resource identifiers are deliberately omitted from errors.
            if lowered.endswith("_id") or lowered in {"aggregate_id", "job_id"}:
                continue
            result[key] = value
    return result


def envelope(
    request: Request,
    *,
    code: str,
    message: str,
    retryable: bool,
    details: Mapping[str, object] | None = None,
    status_code: int | None = None,
) -> JSONResponse:
    status = status_code or ERROR_STATUS_BY_CODE.get(code, 500)
    error: dict[str, object] = {
        "code": code if code in ERROR_STATUS_BY_CODE else "internal_error",
        "message": message if code in ERROR_STATUS_BY_CODE else "internal service error",
        "retryable": retryable if code in ERROR_STATUS_BY_CODE else False,
    }
    safe = _safe_details(details or {})
    if safe:
        error["details"] = safe
    return JSONResponse(
        status_code=status,
        content={"error": error, "request_id": request_id(request)},
        headers={"X-Request-ID": request_id(request)},
    )


async def domain_error_handler(request: Request, error: DomainError) -> JSONResponse:
    status = ERROR_STATUS_BY_CODE.get(error.code, 500)
    if error.code == "access_denied" and error.details.get("authentication") is True:
        status = 401
    # An authorized-but-invisible resource and a denied resource share 404 on
    # resource routes, closing the existence oracle.
    elif error.code in {"access_denied", "scope_violation", "persona_policy_denied"} and not (
        request.url.path.startswith("/v1/admin")
    ):
        status = 404
    return envelope(
        request,
        code=error.code,
        message=str(error.args[0]) if error.args else "request failed",
        retryable=error.retryable,
        details=error.details,
        status_code=status,
    )


async def validation_error_handler(request: Request, error: RequestValidationError) -> JSONResponse:
    del error
    return envelope(
        request,
        code="invalid_request",
        message="request failed strict validation",
        retryable=False,
        status_code=400,
    )


async def unknown_error_handler(request: Request, error: Exception) -> JSONResponse:
    del error
    return envelope(
        request,
        code="internal_error",
        message="internal service error",
        retryable=False,
        status_code=500,
    )


async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
    return envelope(
        request,
        code="not_found" if error.status_code == 404 else "invalid_request",
        message="route not found" if error.status_code == 404 else "method is not allowed",
        retryable=False,
        status_code=error.status_code,
    )


__all__ = ["ERROR_STATUS_BY_CODE", "domain_error_handler", "envelope", "http_error_handler"]
