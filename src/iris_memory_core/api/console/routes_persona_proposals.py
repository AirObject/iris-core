"""Proposal-only creation and explicit, recently authenticated human review."""

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.resource_views import resource_view
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope
from iris_memory_core.application.console.persona_proposals import ConsolePersonaProposalCommands

router = APIRouter(prefix="/v1/personas")


def _validate(request: Request, agent_id: str, proposal_id: str | None = None) -> None:
    if (
        request.query_params
        or not 1 <= len(agent_id) <= 128
        or (proposal_id is not None and not 1 <= len(proposal_id) <= 128)
    ):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)


def _context(
    request: Request, principal: Principal, agent_id: str, proposal_id: str | None
) -> dict[str, Any]:
    _validate(request, agent_id, proposal_id)
    view = ConsolePersonaProposalCommands(security(request)).context(
        principal, agent_id, proposal_id
    )
    labels = {"create": "创建待审提案", "approve": "批准并发布提案", "reject": "拒绝提案"}
    actions = [
        {
            "id": action,
            "label": labels[action],
            "method": "POST",
            "permission": "memory.write" if action == "create" else "persona.publish",
            "high_risk": action != "create",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": "patch",
                    "label": "Trait / Narrative 修改 (JSON)",
                    "type": "json",
                    "required": True,
                    "default": "{}",
                },
                {
                    "key": "confidence",
                    "label": "证据置信度",
                    "type": "number",
                    "required": True,
                    "default": 0.9,
                },
                {
                    "key": "ttl_us",
                    "label": "提案有效时长 (微秒)",
                    "type": "number",
                    "required": True,
                    "default": 2_592_000_000_000,
                },
                {
                    "key": "evidence_refs",
                    "label": "证据引用 (JSON 数组)",
                    "type": "json",
                    "required": True,
                    "default": "[]",
                },
            ]
            if action == "create"
            else [],
        }
        for action in view.available_actions
    ]
    return envelope(
        request,
        {
            "agent_id": agent_id,
            "proposal": resource_view(request, view.proposal, actions=view.available_actions)
            if view.proposal
            else None,
            "base_revision": view.base_revision,
            "current_revision": view.current_revision,
            "expected_policy_revision": view.expected_policy_revision,
            "policy_mode": view.policy_mode,
            "available_actions": list(view.available_actions),
            "actions": actions,
        },
        now_us=security(request).clock.now_us(),
    )


@router.get("/{agent_id}/proposals/commands", operation_id="consolePersonaProposalCommands")
def commands(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    return _context(request, principal, agent_id, None)


@router.get("/{agent_id}/proposals/{proposal_id}", operation_id="consolePersonaProposal")
def detail(
    request: Request, principal: Principal, agent_id: str, proposal_id: str
) -> dict[str, Any]:
    return _context(request, principal, agent_id, proposal_id)


async def _mutate(
    request: Request,
    principal: Principal,
    agent_id: str,
    operation: str,
    proposal_id: str | None = None,
) -> dict[str, Any]:
    _validate(request, agent_id, proposal_id)
    value = await body(request, "ConsolePersonaProposal" + operation.title() + "Request")
    result = await run_in_threadpool(
        ConsolePersonaProposalCommands(security(request)).mutate,
        principal,
        agent_id,
        operation="persona.proposal." + operation,
        base_revision=value["base_revision"],
        expected_policy_revision=value.get("expected_policy_revision"),
        fields=value.get("fields", {}),
        evidence_refs=value.get("evidence_refs", []),
        proposal_id=proposal_id,
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, asdict(result), now_us=security(request).clock.now_us())


@router.post("/{agent_id}/proposals", operation_id="consoleCreatePersonaProposal", status_code=201)
async def create(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "create")


@router.post(
    "/{agent_id}/proposals/{proposal_id}:approve", operation_id="consoleApprovePersonaProposal"
)
async def approve(
    request: Request, principal: Principal, agent_id: str, proposal_id: str
) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "approve", proposal_id)


@router.post(
    "/{agent_id}/proposals/{proposal_id}:reject", operation_id="consoleRejectPersonaProposal"
)
async def reject(
    request: Request, principal: Principal, agent_id: str, proposal_id: str
) -> dict[str, Any]:
    return await _mutate(request, principal, agent_id, "reject", proposal_id)
