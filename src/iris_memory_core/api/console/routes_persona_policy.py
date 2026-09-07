"""Current Policy configuration and sensitive full replacement."""

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from iris_memory_core.api.console.auth import body, idempotency_key, security
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.routes_auth import Principal
from iris_memory_core.api.console.views import envelope, timestamp
from iris_memory_core.application.console.persona_policy import ConsolePersonaPolicyCommands
from iris_memory_core.domain.persona import PersonaPolicy

router = APIRouter(prefix="/v1/personas")
INTEGER_FIELDS = (
    "cumulative_window_us",
    "min_evidence",
    "min_distinct_sources",
    "min_evidence_span_us",
    "cooldown_us",
    "observation_us",
)
FRACTION_FIELDS = (
    "max_single_delta",
    "max_cumulative_delta",
    "min_confidence",
    "rollback_threshold",
)


def _config(policy: PersonaPolicy) -> dict[str, Any]:
    return {
        "mode": policy.mode.value,
        "allowed_fields": list(policy.allowed_fields),
        "sensitive_fields": list(policy.sensitive_fields),
        **{name: str(getattr(policy, name)) for name in INTEGER_FIELDS},
        **{name: getattr(policy, name) for name in FRACTION_FIELDS},
    }


def _fields(config: dict[str, Any]) -> list[dict[str, Any]]:
    labels = {
        "mode": "演进模式",
        "allowed_fields": "允许修改的字段 (JSON 数组)",
        "sensitive_fields": "必须人工审阅的字段 (JSON 数组)",
        "max_single_delta": "单次变化上限",
        "max_cumulative_delta": "累计变化上限",
        "cumulative_window_us": "累计窗口 (微秒)",
        "min_evidence": "最少证据数",
        "min_distinct_sources": "最少独立来源数",
        "min_evidence_span_us": "证据最小时间跨度 (微秒)",
        "min_confidence": "最低置信度",
        "cooldown_us": "发布冷却期 (微秒)",
        "observation_us": "观察期 (微秒)",
        "rollback_threshold": "回滚阈值",
    }
    fields = []
    for name, label in labels.items():
        kind = (
            "enum"
            if name == "mode"
            else "json"
            if isinstance(config[name], list)
            else "duration_us"
            if name.endswith("_us")
            else "number"
            if name in FRACTION_FIELDS
            else "string"
        )
        field = {
            "key": name,
            "label": label,
            "type": kind,
            "required": True,
            "default": json.dumps(config[name], ensure_ascii=True)
            if kind == "json"
            else config[name],
        }
        if name == "mode":
            field["options"] = ["locked", "manual", "bounded_auto"]
        fields.append(field)
    return fields


def _validate(request: Request, agent_id: str) -> None:
    if request.query_params or not 1 <= len(agent_id) <= 128:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400)


@router.get("/{agent_id}/policy", operation_id="consolePersonaPolicy")
def current(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    _validate(request, agent_id)
    policy, writable = ConsolePersonaPolicyCommands(security(request)).current(principal, agent_id)
    config = _config(policy)
    actions = (
        [
            {
                "id": "replace",
                "label": "替换人格演进策略",
                "method": "PUT",
                "permission": "persona.publish",
                "high_risk": True,
                "reason_codes": ["operator_request"],
                "fields": _fields(config),
            }
        ]
        if writable
        else []
    )
    return envelope(
        request,
        {
            "resource_id": policy.id,
            "agent_id": agent_id,
            "revision": policy.revision,
            "content_hash": policy.content_hash,
            "config": config,
            "created_at": timestamp(policy.created_us),
            "available_actions": ["replace"] if writable else [],
            "actions": actions,
        },
        now_us=security(request).clock.now_us(),
    )


@router.put("/{agent_id}/policy", operation_id="consoleReplacePersonaPolicy")
async def replace(request: Request, principal: Principal, agent_id: str) -> dict[str, Any]:
    _validate(request, agent_id)
    value = await body(request, "ConsolePersonaPolicyReplaceRequest")
    config = dict(value["config"])
    # Decimal strings preserve every SQLite integer through browser JSON parsing.
    for name in INTEGER_FIELDS:
        parsed = int(config[name])
        if parsed > 9_223_372_036_854_775_807:
            raise ConsoleError("invalid_request", kind="validation_failed", status=400)
        config[name] = parsed
    result = await run_in_threadpool(
        ConsolePersonaPolicyCommands(security(request)).replace,
        principal,
        agent_id,
        expected_revision=value["expected_revision"],
        config=config,
        reason=value["reason_code"],
        idempotency_key=idempotency_key(request),
    )
    return envelope(request, asdict(result), now_us=security(request).clock.now_us())
