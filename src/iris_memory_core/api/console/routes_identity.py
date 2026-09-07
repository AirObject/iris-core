"""Static registry creation and binding lifecycle endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.resource_views import resource_view
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope
from iris_memory_core.application.console.identity import ConsoleIdentityCommands
from iris_memory_core.domain.console import OperatorPrincipal

router = APIRouter(prefix="/v1")


async def _create(
    request: Request, principal: OperatorPrincipal, schema: str, resource_type: str
) -> dict[str, Any]:
    value = await body(request, schema)
    record = await run_in_threadpool(
        ConsoleIdentityCommands(security(request)).create,
        principal,
        resource_type=resource_type,
        fields=value["fields"],
        privacy_labels=value.get("privacy_labels", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/entities", operation_id="consoleCreateEntity", status_code=201)
async def create_entity(request: Request, principal: Principal) -> dict[str, Any]:
    return await _create(request, principal, "ConsoleEntityCreateRequest", "entity")


@router.post("/memory/identities", operation_id="consoleCreateIdentity", status_code=201)
async def create_identity(request: Request, principal: Principal) -> dict[str, Any]:
    return await _create(request, principal, "ConsoleIdentityCreateRequest", "external_identity")


@router.post("/memory/bindings", operation_id="consoleCreateBinding", status_code=201)
async def create_binding(request: Request, principal: Principal) -> dict[str, Any]:
    return await _create(request, principal, "ConsoleBindingCreateRequest", "binding")


async def _mutate(
    request: Request, principal: OperatorPrincipal, identifier: str, operation: str
) -> dict[str, Any]:
    if not 1 <= len(identifier) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    value = await body(request, "ConsoleBindingActionRequest")
    record = await run_in_threadpool(
        ConsoleIdentityCommands(security(request)).mutate_binding,
        principal,
        identifier,
        operation=operation,
        expected_revision=value["expected_revision"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.post("/memory/bindings/{id}:confirm", operation_id="consoleConfirmBinding")
async def confirm_binding(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    return await _mutate(request, principal, id, "binding.confirm")


@router.post("/memory/bindings/{id}:revoke", operation_id="consoleRevokeBinding")
async def revoke_binding(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    return await _mutate(request, principal, id, "binding.revoke")


@router.post("/memory/entities/{id}:redirect", operation_id="consoleRedirectEntity")
async def redirect_entity(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleEntityRedirectRequest")
    record = await run_in_threadpool(
        ConsoleIdentityCommands(security(request)).redirect_entity,
        principal,
        id,
        target_id=value["target_id"],
        expected_revision=value["expected_revision"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )


@router.get("/memory/entities/{id}/attributes", operation_id="consoleEntityAttributes")
def entity_attributes(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    if request.query_params or not 1 <= len(id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    commands = ConsoleIdentityCommands(security(request))
    record = commands.attributes(principal, id)
    return envelope(
        request,
        resource_view(request, record, actions=commands.entity_actions(principal, record)),
        now_us=security(request).clock.now_us(),
    )


@router.post("/memory/entities/{id}/attributes", operation_id="consoleRecordEntityAttribute")
async def record_entity_attribute(
    request: Request, principal: Principal, id: str
) -> dict[str, Any]:
    if not 1 <= len(id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    value = await body(request, "ConsoleEntityAttributeRequest")
    record = await run_in_threadpool(
        ConsoleIdentityCommands(security(request)).record_attribute,
        principal,
        id,
        expected_revision=value["expected_revision"],
        expected_attributes_version=value["expected_attributes_version"],
        fields=value["fields"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(
        request, resource_view(request, record), now_us=security(request).clock.now_us()
    )
