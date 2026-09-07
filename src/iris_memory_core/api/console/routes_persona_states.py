"""Managed PersonaState read, update and expired-state baseline reset."""

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.resource_views import resource_view, source_ref
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope
from iris_memory_core.application.console.persona_states import ConsolePersonaStateCommands

router = APIRouter(prefix="/v1/personas")


def _validate(request: Request, agent_id: str) -> None:
    if request.query_params or not 1 <= len(agent_id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)


@router.get("/{agent_id}/state", operation_id="consolePersonaState")
def current(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    _validate(request, agent_id)
    view = ConsolePersonaStateCommands(security(request)).current(principal, agent_id)
    record = view.record
    fields = record.fields if record else {}
    actions = []
    for operation in view.available_actions:
        actions.append(
            {
                "id": operation,
                "label": "更新 Persona 状态" if operation == "update" else "清除过期 Persona 状态",
                "permission": "memory.write",
                "method": "PATCH" if operation == "update" else "POST",
                "high_risk": False,
                "reason_codes": ["operator_request"],
                "fields": [
                    {
                        "key": "state",
                        "label": "当前状态 (JSON)",
                        "type": "json",
                        "required": True,
                        "default": json.dumps(fields.get("state", {}), ensure_ascii=True),
                    },
                    {
                        "key": "baseline",
                        "label": "到期基线 (JSON)",
                        "type": "json",
                        "required": True,
                        "default": json.dumps(fields.get("baseline", {}), ensure_ascii=True),
                    },
                    {
                        "key": "ttl_us",
                        "label": "有效时长 (微秒)",
                        "type": "number",
                        "required": True,
                        "default": 3_600_000_000,
                    },
                    {
                        "key": "source_refs",
                        "label": "证据引用 (JSON 数组)",
                        "type": "json",
                        "default": json.dumps(
                            [source_ref(ref) for ref in record.source_refs] if record else [],
                            ensure_ascii=True,
                        ),
                    },
                ]
                if operation == "update"
                else [],
            }
        )
    return envelope(
        request,
        {
            "agent_id": agent_id,
            "current": resource_view(request, record, actions=view.available_actions)
            if record
            else None,
            "expected_revision": view.expected_revision,
            "available_actions": list(view.available_actions),
            "actions": actions,
        },
        now_us=security(request).clock.now_us(),
    )


async def _mutate(
    request: Request, principal: Principal, agent_id: str, operation: str
) -> dict[str, Any]:
    _validate(request, agent_id)
    schema = (
        "ConsolePersonaStateUpdateRequest"
        if operation == "update"
        else "ConsolePersonaStateClearRequest"
    )
    value = await body(request, schema)
    result = await run_in_threadpool(
        ConsolePersonaStateCommands(security(request)).mutate,
        principal,
        agent_id,
        operation="persona.state." + operation,
        expected_revision=value["expected_revision"],
        fields=value.get("fields", {}),
        source_refs=value.get("source_refs", []),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, asdict(result), now_us=security(request).clock.now_us())


@router.patch("/{agent_id}/state", operation_id="consoleUpdatePersonaState")
async def update(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "update")


@router.post("/{agent_id}/state:clear", operation_id="consoleClearPersonaState")
async def clear(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "clear")
