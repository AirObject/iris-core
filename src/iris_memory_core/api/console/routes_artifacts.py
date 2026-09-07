"""Authenticated, bounded raw Artifact uploads; never fetch URLs or accept paths."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from iris_memory_core.api.console.auth import decode_json, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.resource_views import resource_view
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope
from iris_memory_core.application.artifacts import MAX_CONSOLE_ARTIFACT_UPLOAD_BYTES
from iris_memory_core.application.console.artifacts import ConsoleArtifactCommands
from iris_memory_core.domain.scope import Scope

router = APIRouter(prefix="/v1")
UPLOAD_TIMEOUT_SECONDS = 60.0
UPLOAD_CONCURRENCY = threading.BoundedSemaphore(2)


@router.post("/memory/artifacts:upload", operation_id="consoleUploadArtifact", status_code=201)
async def upload_artifact(request: Request, principal: Principal) -> dict[str, Any]:
    if (
        len(request.scope.get("query_string", b"")) > 32768
        or list(request.query_params.keys()) != ["metadata"]
        or len(request.query_params.getlist("metadata")) != 1
    ):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    metadata = request.query_params["metadata"].encode("utf-8")
    if len(metadata) > 8192:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    value = decode_json(metadata, "ConsoleArtifactUploadMetadata")
    if request.headers.get("content-type", "").lower() != "application/octet-stream":
        raise ConsoleError("invalid_request", kind="media_type_unsupported", status=415)
    if "content-encoding" in request.headers:
        raise ConsoleError("invalid_request", kind="media_type_unsupported", status=415)
    if (
        len(request.headers.getlist("content-length")) > 1
        or len(request.headers.getlist("content-type")) > 1
    ):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    length = request.headers.get("content-length")
    if length is not None and "transfer-encoding" in request.headers:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            raise ConsoleError("invalid_request", kind="validation_failed", status=400)
        if int(length) > MAX_CONSOLE_ARTIFACT_UPLOAD_BYTES:
            raise ConsoleError("invalid_request", kind="upload_too_large", status=413)
    key = idempotency_key(request)
    service = ConsoleArtifactCommands(security(request))
    options: dict[str, Any] = dict(
        scope=Scope(principal.key.tenant_id, **value["scope"]),
        media_type=value["fields"]["media_type"],
        privacy_labels=value.get("privacy_labels", []),
        source_refs=value.get("source_refs", []),
        reason=value["reason_code"],
    )
    await run_in_threadpool(service.prepare_upload, principal, **options)
    if not UPLOAD_CONCURRENCY.acquire(blocking=False):
        raise ConsoleError(
            "access_denied", kind="rate_limited", status=429, details={"retry_after": 1}
        )
    try:
        collected = bytearray()
        try:
            async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS):
                async for chunk in request.stream():
                    if len(collected) + len(chunk) > MAX_CONSOLE_ARTIFACT_UPLOAD_BYTES:
                        raise ConsoleError("invalid_request", kind="upload_too_large", status=413)
                    collected.extend(chunk)
        except (TimeoutError, ClientDisconnect):
            raise ConsoleError("invalid_request", kind="validation_failed", status=400) from None
        if not collected or (length is not None and len(collected) != int(length)):
            raise ConsoleError("invalid_request", kind="validation_failed", status=400)
        payload = bytes(collected)
        del collected
        record = await run_in_threadpool(
            service.upload, principal, payload=payload, idempotency_key=key, **options
        )
        return envelope(
            request, resource_view(request, record), now_us=security(request).clock.now_us()
        )
    finally:
        UPLOAD_CONCURRENCY.release()
