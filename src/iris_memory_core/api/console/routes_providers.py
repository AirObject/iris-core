"""Finite Embedding management routes, bound to deployment-owned policy."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.key_views import decode_page, page_meta
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.routes_operations import operation_timestamps, operation_view
from iris_memory_core.api.console.views import envelope, timestamp
from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.console.provider_activation_work import ProviderActivations
from iris_memory_core.application.console.provider_configs import (
    ProviderConfigCommands,
    ProviderSecretInput,
)
from iris_memory_core.application.console.provider_probes import ProviderProbes
from iris_memory_core.application.console.provider_views import ProviderViews
from iris_memory_core.domain.console import OperatorPrincipal
from iris_memory_core.domain.console_operations import ConsoleOperation
from iris_memory_core.domain.errors import ConflictError, NotFoundError
from iris_memory_core.domain.provider_configs import EmbeddingDefinition, ProviderLimits
from iris_memory_core.providers.configured import ConfiguredEmbeddingFactory
from iris_memory_core.providers.secrets import ProviderSecrets

router = APIRouter(prefix="/v1/providers")


def services(
    request: Request, *, write: bool = False
) -> tuple[ProviderConfigCommands, ProviderActivations, ProviderViews]:
    runtime = request.app.state.embedding_runtime
    if not isinstance(runtime, ConfiguredEmbeddingFactory):
        if write:
            raise ConflictError(
                "provider deployment is unavailable", details={"kind": "provider_unavailable"}
            )
        runtime = None
    commands = ProviderConfigCommands(
        security(request),
        runtime.secrets if runtime else ProviderSecrets(),
        runtime or ConfiguredEmbeddingFactory(ProviderSecrets()),
    )
    activations = ProviderActivations(
        security(request), runtime, request.app.state.provider_generations
    )
    return commands, activations, ProviderViews(commands, activations.planner)


def config_view(value: dict[str, Any]) -> dict[str, Any]:
    for name in ("created", "updated"):
        if name + "_us" in value:
            value[name + "_at"] = timestamp(value.pop(name + "_us"))
    if value.get("probe") is not None:
        value["probe"]["created_at"] = timestamp(value["probe"].pop("created_us"))
    return value


def rebuild_view(value: dict[str, Any]) -> dict[str, Any]:
    value["operation"] = operation_timestamps(value["operation"])
    return value


def page(request: Request, principal: OperatorPrincipal) -> tuple[int, tuple[int, str] | None]:
    limit, after = decode_page(request, principal, security(request).clock.now_us())
    if limit > 100:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    return limit, after


def no_query(request: Request) -> None:
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)


def adapter_catalog(request: Request) -> list[dict[str, Any]]:
    runtime = request.app.state.embedding_runtime
    configured = isinstance(runtime, ConfiguredEmbeddingFactory)
    limits = asdict(ProviderLimits())
    fields = [
        {"key": key, "type": kind, "required": True, "minimum": low, "maximum": high}
        for key, kind, low, high in (
            ("space.model", "string", 1, 128),
            ("space.dimension", "integer", 1, 65536),
            ("endpoint", "string", 1, 2048),
            ("limits.batch_size", "integer", 1, 512),
            ("limits.timeout_us", "integer", 1000, 30000000),
            ("limits.max_qps", "number", 0.1, 10000),
            ("limits.breaker_failures", "integer", 1, 100),
            ("limits.breaker_cooldown_us", "integer", 1000, 3600000000),
            ("limits.max_input_chars", "integer", 16, 400000),
        )
    ]
    return [
        {
            "id": "openai-compatible",
            "label": "OpenAI compatible HTTPS",
            "available": configured,
            "secret_modes": ["secret_ref", "sealed"],
            "default_limits": limits,
            "fields": fields,
            "space_constants": {
                "metric": "cosine",
                "normalization": "l2",
                "template_version": 1,
                "builder_version": 1,
            },
        },
        {
            "id": "deterministic",
            "label": "Deterministic development",
            "available": configured and runtime.development_embedding,
            "secret_modes": [],
            "default_limits": limits,
            "fields": [field for field in fields if field["key"] != "endpoint"],
            "space_constants": {
                "metric": "cosine",
                "normalization": "l2",
                "template_version": 1,
                "builder_version": 1,
            },
        },
    ]


@router.get("", operation_id="consoleProviders")
def directory(request: Request, principal: Principal) -> dict[str, Any]:
    no_query(request)
    _, _, views = services(request)
    summary = views.overview(principal, limit=1, after=None)
    summary.pop("configs")
    return envelope(
        request,
        {
            "embedding": summary,
            "adapters": adapter_catalog(request),
            "cognitive": {"read_only": True, "configuration_source": "deployment"},
        },
        now_us=security(request).clock.now_us(),
    )


@router.get("/embedding/adapters", operation_id="consoleEmbeddingAdapters")
def adapters(request: Request, principal: Principal) -> dict[str, Any]:
    no_query(request)
    commands, _, _ = services(request)
    with commands.context.uow.read() as tx:
        commands.principal(tx, principal)
    return envelope(request, adapter_catalog(request), now_us=security(request).clock.now_us())


@router.get("/embedding", operation_id="consoleEmbedding")
def overview(request: Request, principal: Principal) -> dict[str, Any]:
    limit, after = page(request, principal)
    _, _, views = services(request)
    value = views.overview(principal, limit=limit + 1, after=after)
    rows = value["configs"]
    selected = rows[:limit]
    last = (selected[-1]["created_us"], selected[-1]["id"]) if selected else None
    value["configs"] = [config_view(row) for row in selected]
    result = envelope(request, value, now_us=security(request).clock.now_us())
    result["meta"]["page"] = page_meta(
        request,
        principal,
        security(request).clock.now_us(),
        limit,
        last=last,
        has_more=len(rows) > limit,
    )
    return result


@router.get("/embedding/configs/{id}", operation_id="consoleEmbeddingConfig")
def detail(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    no_query(request)
    _, _, views = services(request)
    return envelope(
        request, config_view(views.detail(principal, id)), now_us=security(request).clock.now_us()
    )


@router.get("/embedding/configs/{id}/revisions", operation_id="consoleEmbeddingRevisions")
def revisions(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    limit, after = page(request, principal)
    _, _, views = services(request)
    rows = views.history(principal, id, before=after[0] if after else None, limit=limit + 1)
    selected = rows[:limit]
    value = envelope(
        request, [config_view(row) for row in selected], now_us=security(request).clock.now_us()
    )
    value["meta"]["page"] = page_meta(
        request,
        principal,
        security(request).clock.now_us(),
        limit,
        last=(selected[-1]["content_revision"], id) if selected else None,
        has_more=len(rows) > limit,
    )
    return value


def secret_input(value: dict[str, Any]) -> ProviderSecretInput | None:
    item = value.get("secret")
    return ProviderSecretInput(item["mode"], item["value"]) if item is not None else None


@router.post("/embedding/configs", operation_id="consoleCreateEmbeddingConfig", status_code=201)
async def create(request: Request, principal: Principal) -> JSONResponse:
    value = await body(request, "ConsoleEmbeddingConfigCreateRequest")
    commands, _, _ = services(request, write=True)
    result = await run_in_threadpool(
        commands.create,
        principal,
        definition=EmbeddingDefinition.decode(json.dumps(value["definition"])),
        secret=secret_input(value),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return JSONResponse(
        envelope(request, config_view(result), now_us=security(request).clock.now_us()),
        status_code=201,
    )


@router.patch("/embedding/configs/{id}", operation_id="consolePatchEmbeddingConfig")
async def patch(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleEmbeddingConfigPatchRequest")
    commands, _, _ = services(request, write=True)
    result = await run_in_threadpool(
        commands.patch,
        principal,
        id,
        expected_revision=value["expected_revision"],
        definition=EmbeddingDefinition.decode(json.dumps(value["definition"])),
        secret=secret_input(value),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, config_view(result), now_us=security(request).clock.now_us())


@router.post("/embedding/configs/{id}:discard", operation_id="consoleDiscardEmbeddingConfig")
async def discard(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleEmbeddingRevisionRequest")
    commands, _, _ = services(request, write=True)
    result = await run_in_threadpool(
        commands.discard,
        principal,
        id,
        expected_revision=value["expected_revision"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, config_view(result), now_us=security(request).clock.now_us())


@router.post(
    "/embedding/configs/{id}:test", operation_id="consoleTestEmbeddingConfig", status_code=202
)
async def probe(request: Request, principal: Principal, id: str) -> JSONResponse:
    value = await body(request, "ConsoleEmbeddingRevisionRequest")
    services(request, write=True)
    result = await run_in_threadpool(
        ProviderProbes(security(request), request.app.state.embedding_runtime).accept,
        principal,
        id,
        expected_revision=value["expected_revision"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return JSONResponse(
        envelope(
            request,
            operation_view(result, key_id=principal.key.id),
            now_us=security(request).clock.now_us(),
        ),
        status_code=202,
    )


def receipt(
    request: Request, principal: OperatorPrincipal, operation: ConsoleOperation
) -> dict[str, Any]:
    commands, _, views = services(request, write=True)
    with commands.context.uow.read() as tx:
        fresh = commands.principal(tx, principal, write=True)
        return envelope(
            request,
            rebuild_view(views.operation(tx, fresh, operation)),
            now_us=security(request).clock.now_us(),
        )


@router.post(
    "/embedding/configs/{id}:activate",
    operation_id="consoleActivateEmbeddingConfig",
    status_code=202,
)
async def activate(request: Request, principal: Principal, id: str) -> JSONResponse:
    value = await body(request, "ConsoleEmbeddingActivateRequest")
    _, activations, _ = services(request, write=True)
    operation = await run_in_threadpool(
        activations.accept,
        principal,
        id,
        expected_revision=value["expected_revision"],
        rebuild_ack=value.get("rebuild_ack"),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return JSONResponse(receipt(request, principal, operation), status_code=202)


@router.post("/embedding:rollback", operation_id="consoleRollbackEmbedding", status_code=202)
async def rollback(request: Request, principal: Principal) -> JSONResponse:
    value = await body(request, "ConsoleEmbeddingRollbackRequest")
    _, activations, _ = services(request, write=True)
    operation = await run_in_threadpool(
        activations.rollback,
        principal,
        value["target_config_id"],
        expected_revision=value["expected_revision"],
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return JSONResponse(receipt(request, principal, operation), status_code=202)


@router.get("/embedding/rebuilds", operation_id="consoleEmbeddingRebuilds")
def rebuilds(request: Request, principal: Principal) -> dict[str, Any]:
    limit, after = page(request, principal)
    _, _, views = services(request)
    rows = views.rebuilds(principal, limit=limit + 1, after=after)
    selected = rows[:limit]
    last = (
        (selected[-1]["operation"]["created_us"], selected[-1]["operation"]["id"])
        if selected
        else None
    )
    value = envelope(
        request, [rebuild_view(row) for row in selected], now_us=security(request).clock.now_us()
    )
    value["meta"]["page"] = page_meta(
        request,
        principal,
        security(request).clock.now_us(),
        limit,
        last=last,
        has_more=len(rows) > limit,
    )
    return value


@router.get("/embedding/rebuilds/{id}", operation_id="consoleEmbeddingRebuild")
def rebuild(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    no_query(request)
    _, _, views = services(request)
    return envelope(
        request, rebuild_view(views.rebuild(principal, id)), now_us=security(request).clock.now_us()
    )


@router.post("/embedding/rebuilds/{id}:cancel", operation_id="consoleCancelEmbeddingRebuild")
async def cancel(request: Request, principal: Principal, id: str) -> dict[str, Any]:
    value = await body(request, "ConsoleOperationCancelRequest")
    commands, _, _ = services(request, write=True)
    with commands.context.uow.read() as tx:
        fresh = commands.principal(tx, principal, write=True)
        operation = tx.console_operations.get(fresh.key.tenant_id, id)
        if (
            operation is None
            or operation.provider is None
            or operation.provider.action not in {"activate", "rollback"}
        ):
            raise NotFoundError("provider rebuild not found")
    operation = await run_in_threadpool(
        ConsoleOperations(security(request)).cancel,
        principal,
        id,
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return receipt(request, principal, operation)
