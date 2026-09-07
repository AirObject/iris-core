"""Browser-only authentication, CSRF and bounded JSON decoding."""

from __future__ import annotations

import asyncio
import hmac
import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from fastapi import Request
from jsonschema import Draft202012Validator, FormatChecker

from iris_memory_core.api.console.config import ConsoleConfig
from iris_memory_core.api.console.contracts import load_contract
from iris_memory_core.api.console.crypto import ConsoleCrypto
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.application.console.security import OperatorSecurity
from iris_memory_core.domain.console import OperatorPrincipal
from iris_memory_core.domain.errors import AccessDeniedError

JSON_BODY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    """Small bootstrap view used by contract tests with a dependency override."""

    permissions: tuple[str, ...]


def security(request: Request) -> OperatorSecurity:
    service: OperatorSecurity | None = request.app.state.security
    if service is None:
        raise ConsoleError("access_denied", kind="authentication_required", status=401)
    return service


def crypto(request: Request) -> ConsoleCrypto:
    return cast(ConsoleCrypto, request.app.state.crypto)


def cookie_name(request: Request) -> str:
    config: ConsoleConfig = request.app.state.console_config
    return "imc_console_dev" if config.dev_http else "__Secure-imc_console"


def session_token(request: Request) -> str:
    return request.cookies.get(cookie_name(request), "")


def check_origin(request: Request) -> None:
    config: ConsoleConfig = request.app.state.console_config
    if (
        request.headers.get("origin") != config.origin
        or request.headers.get("x-imc-console") != "1"
    ):
        raise ConsoleError("access_denied", kind="csrf_failed", status=403)


def require_session(request: Request) -> OperatorPrincipal:
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
    service = security(request)
    token = session_token(request)
    alias = False
    try:
        principal = service.authenticate(token)
    except AccessDeniedError:
        if not request.url.path.endswith("/v1/auth/refresh"):
            raise
        principal = service.authenticate(token, alias=True)
        alias = True
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        epoch = principal.session.epoch - int(alias)
        expected = crypto(request).csrf(principal.session.id, epoch)
        supplied = request.headers.get("x-imc-csrf", "")
        if not hmac.compare_digest(expected.encode(), supplied.encode()):
            raise ConsoleError("access_denied", kind="csrf_failed", status=403)
    return principal


def idempotency_key(request: Request) -> str:
    value = request.headers.get("idempotency-key", "")
    try:
        identifier = UUID(value)
        if identifier.version != 4 or str(identifier) != value:
            raise ValueError
    except ValueError:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400) from None
    return value


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError


def _depth(value: Any, level: int = 0) -> None:
    if level > 20:
        raise ValueError
    if isinstance(value, dict):
        for key, item in value.items():
            _depth(key, level + 1)
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)
    elif isinstance(value, str):
        value.encode("utf-8")
        if "\0" in value:
            raise ValueError


async def body(request: Request, schema_name: str) -> dict[str, Any]:
    if request.url.query:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    media = request.headers.get("content-type", "").lower().replace(" ", "")
    if media not in {"application/json", "application/json;charset=utf-8"}:
        raise ConsoleError("invalid_request", kind="media_type_unsupported", status=415)
    collected = bytearray()
    try:
        async with asyncio.timeout(JSON_BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                if len(collected) + len(chunk) > 1_048_576:
                    raise ConsoleError("invalid_request", kind="upload_too_large", status=413)
                collected.extend(chunk)
    except TimeoutError:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400) from None
    return decode_json(bytes(collected), schema_name)


def decode_json(raw: bytes, schema_name: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant
        )
        _depth(value)
        components = load_contract()["components"]["schemas"]
        schema = {"components": {"schemas": components}, **components[schema_name]}
        if not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(value):
            raise ValueError
    except (ValueError, RecursionError, UnicodeError):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400) from None
    return cast(dict[str, Any], value)
