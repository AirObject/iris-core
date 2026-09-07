"""Explicit managed Persona commands with immutable publication receipts."""

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.contracts import load_contract
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope
from iris_memory_core.application.console.personas import ConsolePersonaCommands

router = APIRouter(prefix="/v1/personas")


def _validate(request: Request, agent_id: str) -> None:
    if request.query_params or not 1 <= len(agent_id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)


@router.get("/{agent_id}/commands", operation_id="consolePersonaCommands")
def commands(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    _validate(request, agent_id)
    record, allowed = ConsolePersonaCommands(security(request)).current(principal, agent_id)
    schemas = load_contract()["components"]["schemas"]
    actions = []
    for operation in allowed:
        request_name = (
            "ConsolePersona" + ("Publish" if operation == "publish" else "Rollback") + "Request"
        )
        actions.append(
            {
                "id": operation,
                "label": "发布新版本" if operation == "publish" else "回滚为新版本",
                "permission": "persona.publish",
                "method": "POST",
                "high_risk": True,
                "reason_codes": ["operator_request"],
                "request_schema": request_name,
                "fields": [
                    {"key": key, "label": label, "type": kind, "required": required}
                    for key, label, kind, required in (
                        [
                            ("core", "Core(JSON)", "json", True),
                            ("traits", "Traits(JSON)", "json", True),
                            ("narrative", "Narrative(JSON)", "json", True),
                            ("source_refs", "证据引用(JSON 数组)", "json", False),
                        ]
                        if operation == "publish"
                        else [("target_revision", "目标历史 Revision", "number", True)]
                    )
                ],
            }
        )
    return envelope(
        request,
        {
            "agent_id": agent_id,
            "expected_revision": record.revision,
            "expected_policy_revision": record.fields["policy_revision"],
            "actions": actions,
            "request_schemas": {
                name: schemas[name]
                for name in ("ConsolePersonaPublishRequest", "ConsolePersonaRollbackRequest")
            },
        },
        now_us=security(request).clock.now_us(),
    )


async def _mutate(
    request: Request, principal: Principal, agent_id: str, operation: str
) -> dict[str, Any]:
    _validate(request, agent_id)
    schema = "ConsolePersona" + ("Publish" if operation == "publish" else "Rollback") + "Request"
    value = await body(request, schema)
    receipt = await run_in_threadpool(
        ConsolePersonaCommands(security(request)).mutate,
        principal,
        agent_id,
        operation="persona." + operation,
        expected_revision=value["expected_revision"],
        expected_policy_revision=value["expected_policy_revision"],
        fields=value.get("fields", {}),
        source_refs=value.get("source_refs", []),
        target_revision=value.get("target_revision"),
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, asdict(receipt), now_us=security(request).clock.now_us())


@router.post("/{agent_id}/revisions", operation_id="consolePublishPersona", status_code=201)
async def publish(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "publish")


@router.post("/{agent_id}:rollback", operation_id="consoleRollbackPersona", status_code=201)
async def rollback(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "rollback")
