"""Explicit, bounded deletion preview/commit routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.routes_operations import operation_timestamps
from iris_memory_core.api.console.views import envelope, timestamp
from iris_memory_core.application.console.forget import ConsoleForgetCommands

router = APIRouter(prefix="/v1")


@router.post("/memory:forget-preview", operation_id="consoleForgetPreview")
async def preview(request: Request, principal: Principal) -> dict[str, Any]:
    value = await body(request, "ConsoleForgetPreviewRequest")
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    result = await run_in_threadpool(
        ConsoleForgetCommands(security(request)).preview,
        principal,
        targets=value.get("targets"),
        selector=value.get("selector"),
        mode=value["mode"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    result["expires_at"] = timestamp(result.pop("expires_us"))
    return envelope(request, result, now_us=security(request).clock.now_us())


@router.post("/memory:forget", operation_id="consoleForget")
async def commit(request: Request, principal: Principal) -> Response:
    value = await body(request, "ConsoleForgetCommitRequest")
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    result = await run_in_threadpool(
        ConsoleForgetCommands(security(request)).commit,
        principal,
        preview_id=value["preview_id"],
        preview_hash=value["preview_hash"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    if "operation" in result:
        return JSONResponse(
            envelope(
                request,
                operation_timestamps(result["operation"]),
                now_us=security(request).clock.now_us(),
            ),
            status_code=202,
        )
    return JSONResponse(envelope(request, result, now_us=security(request).clock.now_us()))
