"""Explicit, typed error disclosure for the Console plane."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator

from iris_memory_core.api.console.contracts import load_contract
from iris_memory_core.api.errors import ERROR_STATUS_BY_CODE
from iris_memory_core.application.ports import Uuid7Generator
from iris_memory_core.domain.errors import DomainError

# Values as well as field names are checked against the explicit contract.
DETAIL_SCHEMA = load_contract()["components"]["schemas"]["ErrorDetails"]
MESSAGES = {
    "access_denied": "access denied",
    "not_found": "resource not found",
    "invalid_request": "request failed validation",
    "revision_mismatch": "revision conflict",
    "not_ready": "service is not ready",
    "internal_error": "internal service error",
}


class ConsoleError(Exception):
    def __init__(
        self,
        code: str,
        *,
        kind: str | None = None,
        status: int | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = dict(details or {})
        if kind is not None:
            self.details["kind"] = kind


def request_id(request: Request) -> str:
    if not getattr(request.state, "request_id", None):
        request.state.request_id = str(Uuid7Generator().new())
    return str(request.state.request_id)


def safe_details(details: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in details.items()
        if key in DETAIL_SCHEMA["properties"]
        and Draft202012Validator(DETAIL_SCHEMA["properties"][key]).is_valid(value)
    }


def error_response(request: Request, error: Exception) -> JSONResponse:
    code = "internal_error"
    status = 500
    details: Mapping[str, object] = {}
    retryable = False
    if isinstance(error, ConsoleError):
        code, details = error.code, error.details
        status = error.status or ERROR_STATUS_BY_CODE.get(code, 500)
    elif isinstance(error, DomainError):
        code, details, retryable = error.code, error.details, error.retryable
        status = ERROR_STATUS_BY_CODE.get(code, 500)
        if code == "revision_mismatch":
            details = {**details, "kind": "revision_conflict"}
    if code == "access_denied":
        status = {"authentication_required": 401, "rate_limited": 429}.get(
            str(details.get("kind")), status
        )
    if code not in ERROR_STATUS_BY_CODE:
        code, status, details, retryable = "internal_error", 500, {}, False
    # Never forward exception messages: even a known domain code may carry
    # SQL, provider response text, paths or submitted values in its message.
    body: dict[str, Any] = {
        "code": code,
        "message": MESSAGES.get(code, "request could not be completed"),
        "retryable": retryable,
        "details": safe_details(details),
    }
    retry_after = details.get("retry_after")
    return JSONResponse(
        {"error": body, "request_id": request_id(request)},
        status_code=status,
        headers={
            "X-Request-ID": request_id(request),
            **(
                {"Retry-After": str(min(900, max(1, retry_after)))}
                if status == 429 and isinstance(retry_after, int)
                else {}
            ),
        },
    )
