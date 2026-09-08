"""Bounded draft reads and explicit save, publish and discard requests."""

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.resource_views import page, read_query, resource_view
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope
from iris_memory_core.application.console.persona_drafts import ConsolePersonaDraftCommands
from iris_memory_core.application.console.reads import ConsoleReadService

router = APIRouter(prefix="/v1/personas")


def _validate(
    request: Request, agent_id: str, draft_id: str | None = None, *, listing: bool = False
) -> None:
    if (request.query_params and not listing) or any(
        not 1 <= len(value) <= 128 for value in (agent_id, draft_id) if value is not None
    ):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)


def _context(
    request: Request, principal: Principal, agent_id: str, draft_id: str | None
) -> dict[str, Any]:
    _validate(request, agent_id, draft_id)
    view = ConsolePersonaDraftCommands(security(request)).context(principal, agent_id, draft_id)
    fields = (
        dict(view.draft.fields["content"])
        if view.draft
        else dict(view.current.fields)
        if view.current
        else {}
    )
    defaults: dict[str, Any] = {}
    for layer in ("core", "traits", "narrative"):
        value = fields.get(layer, {})
        if isinstance(value, str):
            value = json.loads(value) if value else {}
        defaults[layer] = value if isinstance(value, dict) else {}
    refs = (
        view.draft.source_refs if view.draft else view.current.source_refs if view.current else ()
    )
    defaults["source_refs"] = [
        {key: value for key, value in asdict(ref).items() if value is not None} for ref in refs
    ]
    labels = {
        "create": "保存人格草稿",
        "update": "保存草稿并更新基准",
        "publish": "发布此人格草稿",
        "discard": "丢弃未发布人格草稿",
    }
    actions = [
        {
            "id": action,
            "label": labels[action],
            "method": "PUT" if action == "update" else "POST",
            "permission": "persona.publish",
            "high_risk": action in {"publish", "discard"},
            "reason_codes": ["operator_request"],
            "description": "请对照当前人格与草稿。明确审阅 Persona 和 Policy 基准。"
            if action == "update"
            else "",
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "json",
                    "required": True,
                    "default": json.dumps(defaults[key], ensure_ascii=True),
                }
                for key, label in (
                    ("core", "Core(JSON)"),
                    ("traits", "Traits(JSON)"),
                    ("narrative", "Narrative(JSON)"),
                    ("source_refs", "证据引用(JSON 数组)"),
                )
            ]
            if action in {"create", "update"}
            else [],
        }
        for action in view.available_actions
    ]
    return envelope(
        request,
        {
            "agent_id": agent_id,
            "draft": resource_view(request, view.draft, actions=()) if view.draft else None,
            "current": resource_view(request, view.current, actions=()) if view.current else None,
            "expected_revision": view.expected_revision,
            "base_revision": view.base_revision,
            "expected_policy_revision": view.expected_policy_revision,
            "available_actions": list(view.available_actions),
            "actions": actions,
        },
        now_us=security(request).clock.now_us(),
    )


@router.get("/{agent_id}/drafts/commands", operation_id="consolePersonaDraftCommands")
def commands(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    return _context(request, principal, agent_id, None)


@router.get("/{agent_id}/drafts", operation_id="consolePersonaDrafts")
def listing(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    _validate(request, agent_id, listing=True)
    now = security(request).clock.now_us()
    query = read_query(request, principal, now, filters=False)
    result = ConsoleReadService(security(request)).listing(
        principal, "persona-drafts", query, parent_id=agent_id
    )
    response = envelope(
        request, [resource_view(request, row, actions=()) for row in result.records], now_us=now
    )
    response["meta"]["page"] = page(
        request,
        principal,
        query,
        now,
        result.records[-1].key if result.records else None,
        result.has_more,
    )
    return response


@router.get("/{agent_id}/drafts/{draft_id}", operation_id="consolePersonaDraft")
def detail(request: Request, principal: Principal, agent_id: str, draft_id: str) -> dict[str, Any]:
    return _context(request, principal, agent_id, draft_id)


async def _mutate(
    request: Request,
    principal: Principal,
    agent_id: str,
    operation: str,
    draft_id: str | None = None,
) -> dict[str, Any]:
    _validate(request, agent_id, draft_id)
    value = await body(request, "ConsolePersonaDraft" + operation.title() + "Request")
    result = await run_in_threadpool(
        ConsolePersonaDraftCommands(security(request)).mutate,
        principal,
        agent_id,
        operation="persona.draft." + operation,
        draft_id=draft_id,
        expected_revision=value["expected_revision"],
        base_revision=value.get("base_revision"),
        policy_revision=value.get("expected_policy_revision"),
        fields=value.get("fields", {}),
        source_refs=value.get("source_refs", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, asdict(result), now_us=security(request).clock.now_us())


@router.post("/{agent_id}/drafts", operation_id="consoleCreatePersonaDraft", status_code=201)
async def create(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "create")


@router.put("/{agent_id}/drafts/{draft_id}", operation_id="consoleUpdatePersonaDraft")
async def update(
    request: Request, principal: Principal, agent_id: str, draft_id: str
) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "update", draft_id)


@router.post("/{agent_id}/drafts/{draft_id}:publish", operation_id="consolePublishPersonaDraft")
async def publish(
    request: Request, principal: Principal, agent_id: str, draft_id: str
) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "publish", draft_id)


@router.post("/{agent_id}/drafts/{draft_id}:discard", operation_id="consoleDiscardPersonaDraft")
async def discard(
    request: Request, principal: Principal, agent_id: str, draft_id: str
) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "discard", draft_id)
