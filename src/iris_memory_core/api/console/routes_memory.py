"""Statically registered resource routes backed by the application read service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request

from iris_memory_core.api.console.auth import security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.resource_views import (
    descriptor,
    page,
    read_query,
    resource_view,
    source_ref,
)
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope
from iris_memory_core.application.console.reads import ConsoleReadService
from iris_memory_core.application.console.resources import BY_COLLECTION, LOOKUPS, SUBRESOURCES

router = APIRouter(prefix="/v1")


def _service(request: Request) -> ConsoleReadService:
    return ConsoleReadService(security(request))


@router.get("/memory/resource-types", operation_id="consoleMemoryResourceTypes")
def resource_types(request: Request, principal: Principal) -> dict[str, Any]:
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    return envelope(
        request,
        [descriptor(name) for name in _service(request).descriptors(principal)],
        now_us=security(request).clock.now_us(),
    )


def _listing(
    collection: str, *, subresource: bool = False, persona: bool = False, history: bool = False
) -> Callable[..., dict[str, Any]]:
    def endpoint(
        request: Request, principal: Principal, id: str = "", agent_id: str = ""
    ) -> dict[str, Any]:
        now = security(request).clock.now_us()
        query = read_query(request, principal, now)
        result = _service(request).listing(
            principal,
            collection,
            query,
            parent_id=(id if subresource else agent_id) if (subresource or persona) else None,
            history_id=id if history else None,
        )
        value = envelope(
            request, [resource_view(request, row) for row in result.records], now_us=now
        )
        value["meta"]["page"] = page(
            request,
            principal,
            query,
            now,
            result.records[-1].key if result.records else None,
            result.has_more,
        )
        if query.include_total:
            value["meta"]["total"] = {
                "value": str(result.total) if result.total is not None else None,
                "exact": result.total_exact,
                "duration_us": str(result.total_duration_us),
            }
        if result.warnings:
            value["meta"]["warnings"] = list(result.warnings)
        return value

    return endpoint


def _detail(collection: str) -> Callable[..., dict[str, Any]]:
    def endpoint(request: Request, principal: Principal, id: str) -> dict[str, Any]:
        if request.query_params:
            raise ConsoleError("invalid_request", kind="validation_failed", status=400)
        return envelope(
            request,
            resource_view(request, _service(request).detail(principal, collection, id)),
            now_us=security(request).clock.now_us(),
        )

    return endpoint


def _references(collection: str) -> Callable[..., dict[str, Any]]:
    def endpoint(request: Request, principal: Principal, id: str) -> dict[str, Any]:
        now = security(request).clock.now_us()
        query = read_query(request, principal, now, filters=False)
        rows, more = _service(request).references(principal, collection, id, query)
        data = [
            {
                "id": row.id,
                "direction": row.direction,
                "relation": row.relation,
                "resource": source_ref(row.resource),
            }
            for row in rows
        ]
        result = envelope(request, data, now_us=now)
        result["meta"]["page"] = page(
            request, principal, query, now, rows[-1].key if rows else None, more
        )
        return result

    return endpoint


for collection in BY_COLLECTION:
    name = "".join(part.title() for part in collection.split("-"))
    path = "/memory/" + collection
    router.add_api_route(
        path, _listing(collection), methods=["GET"], operation_id="consoleList" + name
    )
    router.add_api_route(
        path + "/{id}", _detail(collection), methods=["GET"], operation_id="consoleGet" + name
    )
    router.add_api_route(
        path + "/{id}/history",
        _listing(collection, history=True),
        methods=["GET"],
        operation_id="consoleHistory" + name,
    )
    router.add_api_route(
        path + "/{id}/references",
        _references(collection),
        methods=["GET"],
        operation_id="consoleReferences" + name,
    )

for collection in LOOKUPS:
    name = "".join(part.title() for part in collection.split("-"))
    router.add_api_route(
        "/lookups/" + collection,
        _listing(collection),
        methods=["GET"],
        operation_id="consoleLookup" + name,
    )

for collection in SUBRESOURCES:
    router.add_api_route(
        "/memory/tasks/{id}/" + collection,
        _listing(collection, subresource=True),
        methods=["GET"],
        operation_id="consoleTask" + collection.title(),
    )


@router.get("/personas/{agent_id}", operation_id="consolePersona")
def persona(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    if request.query_params:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)
    return envelope(
        request,
        resource_view(request, _service(request).persona(principal, agent_id)),
        now_us=security(request).clock.now_us(),
    )


router.add_api_route(
    "/personas/{agent_id}/history",
    _listing("persona", persona=True),
    methods=["GET"],
    operation_id="consolePersonaHistory",
)
router.add_api_route(
    "/personas/{agent_id}/proposals",
    _listing("persona-proposals", persona=True),
    methods=["GET"],
    operation_id="consolePersonaProposals",
)
