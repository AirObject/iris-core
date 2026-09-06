"""Session and operator-key HTTP routes; commands live in application services."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import (
    body,
    check_origin,
    cookie_name,
    crypto,
    idempotency_key,
    require_session,
    security,
    session_token,
)
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.key_views import (
    decode_page,
    key_view,
    page_meta,
    session_summary,
    session_view,
    time_us,
)
from iris_memory_core.api.console.views import envelope, timestamp
from iris_memory_core.domain.console import OperatorPrincipal

router = APIRouter(prefix="/v1")
Principal = Annotated[OperatorPrincipal, Depends(require_session)]
LOGIN_CONCURRENCY = threading.BoundedSemaphore(32)


def _result(request: Request, data: Any, *, status: int = 200) -> JSONResponse:
    return JSONResponse(
        envelope(request, data, now_us=security(request).clock.now_us()), status_code=status
    )


def _session_response(request: Request, principal: OperatorPrincipal, token: str) -> JSONResponse:
    response = _result(request, session_view(request, principal))
    response.set_cookie(
        cookie_name(request),
        token,
        path="/console",
        httponly=True,
        secure=not request.app.state.console_config.dev_http,
        samesite="strict",
        max_age=max(
            0, (principal.session.expires_us - security(request).clock.now_us()) // 1_000_000
        ),
    )
    return response


async def _pad_login(started: float) -> None:
    await asyncio.sleep(max(0, 0.100 - (time.monotonic() - started)))


@router.post("/auth/login", operation_id="consoleLogin")
async def login(request: Request) -> Response:
    check_origin(request)
    if not LOGIN_CONCURRENCY.acquire(blocking=False):
        raise ConsoleError(
            "access_denied", kind="rate_limited", status=429, details={"retry_after": 1}
        )
    started = time.monotonic()
    try:
        payload = await body(request, "LoginRequest")
        # Raw peer identity is used unless an explicitly trusted proxy supplies
        # one single literal client address; forwarded chains are not accepted.
        address = request.client.host if request.client else "unknown"
        if address in request.app.state.console_config.trusted_proxy_ips:
            import ipaddress

            try:
                address = str(ipaddress.ip_address(request.headers.get("x-forwarded-for", "")))
            except ValueError:
                raise ConsoleError("access_denied", kind="permission_denied", status=403) from None
        principal, token = await run_in_threadpool(
            security(request).login,
            payload["key"],
            client_digest=crypto(request).fingerprint("client:" + address),
            previous=session_token(request) or None,
        )
        return _session_response(request, principal, token)
    finally:
        # All credential outcomes have the same minimum response time. This
        # padding is outside the writer transaction and bounded concurrency.
        try:
            await _pad_login(started)
        finally:
            LOGIN_CONCURRENCY.release()


@router.get("/auth/session", operation_id="consoleSession")
def current_session(request: Request, principal: Principal) -> Response:
    return _result(request, session_view(request, principal))


@router.post("/auth/refresh", operation_id="consoleRefresh")
async def refresh(request: Request, principal: Principal) -> Response:
    await body(request, "EmptyRequest")
    updated, token = await run_in_threadpool(
        security(request).refresh,
        principal,
        token=session_token(request),
        request_key=idempotency_key(request),
    )
    return _session_response(request, updated, token)


@router.post("/auth/reauth", operation_id="consoleReauth")
async def reauth(request: Request, principal: Principal) -> Response:
    payload = await body(request, "LoginRequest")
    updated = await run_in_threadpool(security(request).reauth, principal, payload["key"])
    assert updated.session.reauth_until_us is not None
    return _result(request, {"reauth_until": timestamp(updated.session.reauth_until_us)})


@router.post("/auth/logout", operation_id="consoleLogout", status_code=204)
async def logout(request: Request, principal: Principal) -> Response:
    await body(request, "EmptyRequest")
    await run_in_threadpool(
        security(request).revoke_session, principal, principal.session.id, "operator_request"
    )
    response = Response(status_code=204)
    response.delete_cookie(
        cookie_name(request),
        path="/console",
        secure=not request.app.state.console_config.dev_http,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/auth/sessions", operation_id="consoleSessions")
def sessions(request: Request, principal: Principal) -> Response:
    now = security(request).clock.now_us()
    limit, after = decode_page(request, principal, now)
    records = security(request).list_sessions(principal, limit=limit + 1, after=after)
    selected = records[:limit]
    value = envelope(
        request, [session_summary(row, principal.session.id) for row in selected], now_us=now
    )
    value["meta"]["page"] = page_meta(
        request,
        principal,
        now,
        limit,
        last=(selected[-1].created_us, selected[-1].id) if selected else None,
        has_more=len(records) > limit,
    )
    return JSONResponse(value)


@router.post("/auth/sessions/{id}:revoke", operation_id="consoleRevokeSession", status_code=204)
async def revoke_session(request: Request, principal: Principal, id: str) -> Response:
    payload = await body(request, "SessionRevokeRequest")
    await run_in_threadpool(security(request).revoke_session, principal, id, payload["reason_code"])
    response = Response(status_code=204)
    if principal.session.id == id:
        response.delete_cookie(
            cookie_name(request),
            path="/console",
            secure=not request.app.state.console_config.dev_http,
            httponly=True,
            samesite="strict",
        )
    return response


@router.get("/keys", operation_id="consoleKeys")
def keys(request: Request, principal: Principal) -> Response:
    now = security(request).clock.now_us()
    limit, after = decode_page(
        request, principal, now, filters=frozenset({"status", "prefix", "label"})
    )
    records = security(request).list_keys(
        principal,
        limit=limit + 1,
        after=after,
        status=request.query_params.get("status", ""),
        prefix=request.query_params.get("prefix", ""),
        label=request.query_params.get("label", ""),
    )
    selected = records[:limit]
    value = envelope(request, [key_view(row, now) for row in selected], now_us=now)
    value["meta"]["page"] = page_meta(
        request,
        principal,
        now,
        limit,
        last=(selected[-1].created_us, selected[-1].id) if selected else None,
        has_more=len(records) > limit,
    )
    return JSONResponse(value)


async def _mutate(
    request: Request,
    principal: OperatorPrincipal,
    operation: str,
    schema: str,
    target: str | None = None,
) -> Response:
    payload = await body(request, schema)
    if "expires_at" in payload:
        payload["expires_us"] = time_us(payload.pop("expires_at"))
    record, secret = await run_in_threadpool(
        security(request).mutate_key,
        principal,
        operation=operation,
        payload=payload,
        target_id=target,
        request_key=idempotency_key(request),
    )
    data = key_view(record, security(request).clock.now_us())
    if operation in {"issue", "rotate"}:
        return _result(
            request,
            {
                "key": data,
                "secret_available": secret is not None,
                **({"secret": secret} if secret else {}),
            },
            status=201,
        )
    return _result(request, data)


@router.post("/keys", operation_id="consoleIssueKey", status_code=201)
async def issue_key(request: Request, principal: Principal) -> Response:
    return await _mutate(request, principal, "issue", "KeyIssueRequest")


@router.patch("/keys/{id}", operation_id="consoleUpdateKey")
async def update_key(request: Request, principal: Principal, id: str) -> Response:
    return await _mutate(request, principal, "update", "KeyUpdateRequest", id)


@router.post("/keys/{id}:rotate", operation_id="consoleRotateKey", status_code=201)
async def rotate_key(request: Request, principal: Principal, id: str) -> Response:
    return await _mutate(request, principal, "rotate", "KeyActionRequest", id)


@router.post("/keys/{id}:revoke", operation_id="consoleRevokeKey")
async def revoke_key(request: Request, principal: Principal, id: str) -> Response:
    return await _mutate(request, principal, "revoke", "KeyActionRequest", id)
