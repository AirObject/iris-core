"""Host application-credential HTTP adapter with explicit secret-free views."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.key_views import decode_page, page_meta, time_us
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope, timestamp
from iris_memory_core.application.console.credentials import ServiceCredentialCommands
from iris_memory_core.domain.console import OperatorPrincipal
from iris_memory_core.domain.reflection import CredentialRecord

router = APIRouter(prefix="/v1")


def credential_view(record: CredentialRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "app_instance_id": record.app_instance_id,
        "plane": record.plane,
        "label": record.label,
        "description": record.description,
        "token_prefix": record.token_prefix,
        "created_by": record.created_by,
        "revision": record.console_revision,
        "created_at": timestamp(record.created_us),
        "expires_at": timestamp(record.expires_us),
        "revoked_at": timestamp(record.revoked_us) if record.revoked_us is not None else None,
        "revoke_after": timestamp(record.revoke_after_us)
        if record.revoke_after_us is not None
        else None,
        "rotated_from_id": record.rotated_from_id,
        "revoke_reason": record.revoke_reason,
        **{
            name: sorted(getattr(record, name))
            for name in (
                "agent_ids",
                "space_group_ids",
                "space_ids",
                "entity_ids",
                "capabilities",
                "data_purposes",
            )
        },
    }


@router.get("/service-credentials", operation_id="consoleServiceCredentials")
def credentials(request: Request, principal: Principal) -> Response:
    service = security(request)
    now = service.clock.now_us()
    limit, after = decode_page(request, principal, now)
    records = ServiceCredentialCommands(service).list(principal, limit=limit + 1, after=after)
    selected = records[:limit]
    value = envelope(request, [credential_view(row) for row in selected], now_us=now)
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
    service = security(request)
    payload = await body(request, schema)
    if "expires_at" in payload:
        payload["expires_us"] = time_us(payload.pop("expires_at"))
    record, secret = await run_in_threadpool(
        ServiceCredentialCommands(service).mutate,
        principal,
        operation=operation,
        payload=payload,
        request_key=idempotency_key(request),
        target_id=target,
    )
    data = credential_view(record)
    if operation in {"issue", "rotate"}:
        data = {
            "key": data,
            "secret_available": secret is not None,
            **({"secret": secret} if secret else {}),
        }
    return JSONResponse(
        envelope(request, data, now_us=service.clock.now_us()),
        status_code=201 if operation in {"issue", "rotate"} else 200,
    )


@router.post("/service-credentials", operation_id="consoleIssueServiceCredential", status_code=201)
async def issue(request: Request, principal: Principal) -> Response:
    return await _mutate(request, principal, "issue", "ServiceCredentialIssueRequest")


@router.patch("/service-credentials/{id}", operation_id="consoleUpdateServiceCredential")
async def update(request: Request, principal: Principal, id: str) -> Response:
    return await _mutate(request, principal, "update", "KeyUpdateRequest", id)


@router.post(
    "/service-credentials/{id}:rotate",
    operation_id="consoleRotateServiceCredential",
    status_code=201,
)
async def rotate(request: Request, principal: Principal, id: str) -> Response:
    return await _mutate(request, principal, "rotate", "ServiceCredentialRotateRequest", id)


@router.post("/service-credentials/{id}:revoke", operation_id="consoleRevokeServiceCredential")
async def revoke(request: Request, principal: Principal, id: str) -> Response:
    return await _mutate(request, principal, "revoke", "KeyActionRequest", id)
