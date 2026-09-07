"""Browser read views and signed, scope-bound keyset cursors."""

from __future__ import annotations

import base64
import hmac
import json
from typing import Any

from fastapi import Request

from iris_memory_core.api.console.auth import crypto
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.key_views import time_us
from iris_memory_core.api.console.views import timestamp
from iris_memory_core.application.console.resources import (
    BY_COLLECTION,
    ReadQuery,
    ReadRecord,
    ResourceRef,
)
from iris_memory_core.domain.console import OperatorPrincipal

FILTERS = frozenset(
    {
        "agent_id",
        "space_group_id",
        "space_id",
        "session_id",
        "status",
        "created_from",
        "created_to",
        "updated_from",
        "updated_to",
        "q",
        "sort",
        "include_total",
    }
)


def _binding(request: Request, principal: OperatorPrincipal) -> list[Any]:
    return [
        principal.key.tenant_id,
        principal.key.id,
        principal.key.grant.fingerprint,
        request.url.path,
        sorted(
            [key, value] for key, value in request.query_params.multi_items() if key != "cursor"
        ),
    ]


def read_query(
    request: Request, principal: OperatorPrincipal, now_us: int, *, filters: bool = True
) -> ReadQuery:
    try:
        params = request.query_params
        if len(params.multi_items()) != len(params) or set(params) - {"limit", "cursor"} - (
            FILTERS if filters else set()
        ):
            raise ValueError
        limit = int(params.get("limit", "50"))
        if not 1 <= limit <= 200 or params.get("sort", "created_at_desc") != "created_at_desc":
            raise ValueError
        if params.get("include_total", "false") not in {"true", "false"}:
            raise ValueError
        values = {
            key: value
            for key, value in params.items()
            if key in FILTERS - {"include_total", "sort"}
        }
        for key, value in values.items():
            if not value or len(value) > 256 or "\0" in value:
                raise ValueError
            value.encode("utf-8")
            if key in {"created_from", "created_to", "updated_from", "updated_to"}:
                values[key] = str(time_us(value))
        for prefix in ("created", "updated"):
            if (
                prefix + "_from" in values
                and prefix + "_to" in values
                and int(values[prefix + "_from"]) > int(values[prefix + "_to"])
            ):
                raise ValueError
    except (ValueError, UnicodeError, TypeError, OverflowError):
        raise ConsoleError("invalid_request", kind="validation_failed", status=400) from None
    after, ceiling = None, now_us
    cursor = params.get("cursor")
    if cursor:
        try:
            if len(cursor) > 4096:
                raise ValueError
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(crypto(request).sign_cursor(payload), signature):
                raise ValueError
            decoded = json.loads(payload)
            if (
                decoded["binding"] != _binding(request, principal)
                or int(decoded["expires_us"]) <= now_us
            ):
                raise ValueError
            ceiling = int(decoded["ceiling_us"])
            after = (int(decoded["created_us"]), str(decoded["id"]))
        except (ValueError, TypeError, KeyError):
            raise ConsoleError("invalid_request", kind="cursor_invalid", status=400) from None
    return ReadQuery(limit, ceiling, after, values, params.get("include_total") == "true")


def page(
    request: Request,
    principal: OperatorPrincipal,
    query: ReadQuery,
    now_us: int,
    last: tuple[int, str] | None,
    has_more: bool,
) -> dict[str, Any]:
    cursor = None
    if last and has_more:
        value = {
            "binding": _binding(request, principal),
            "expires_us": str(now_us + 900_000_000),
            "ceiling_us": str(query.ceiling_us),
            "created_us": str(last[0]),
            "id": last[1],
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        cursor = (
            base64.urlsafe_b64encode(encoded + crypto(request).sign_cursor(encoded))
            .decode()
            .rstrip("=")
        )
    return {"next_cursor": cursor, "has_more": has_more, "limit": query.limit}


def source_ref(ref: ResourceRef) -> dict[str, Any]:
    result: dict[str, Any] = {"resource_type": ref.resource_type, "resource_id": ref.resource_id}
    if ref.revision is not None:
        result["revision"] = ref.revision
    return result


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and abs(value) > 9_007_199_254_740_991
    ):
        return str(value)
    return value


def resource_view(
    request: Request, record: ReadRecord, *, actions: tuple[str, ...] | None = None
) -> dict[str, Any]:
    fields = {}
    for key, value in record.fields.items():
        if record.resource_type == "task_trigger" and key == "misfire_grace_us":
            fields[key] = _json_value(value)
        elif key.endswith("_us"):
            name = key[:-3] if key[:-3].endswith("_at") else key[:-3] + "_at"
            fields[name] = timestamp(value) if value is not None else None
        elif key.endswith("_bytes"):
            fields[key] = str(value) if value is not None else None
        else:
            fields[key] = _json_value(value)
    result = {
        "id": record.id,
        "resource_type": record.resource_type,
        "revision": record.revision,
        "scope": {
            key: value for key, value in record.scope.as_dict().items() if key != "tenant_id"
        },
        "status": record.status,
        "fields": fields,
        "privacy_labels": list(record.privacy_labels),
        "source_refs": [source_ref(ref) for ref in record.source_refs],
        "created_at": timestamp(record.created_us),
        "updated_at": timestamp(record.updated_us),
        "available_actions": [],
        "blocked_actions": [
            {"action": action, "reason": "not_implemented"} for action in ("update", "forget")
        ],
    }
    if record.resource_type in {"persona_revision", "persona_state", "persona_proposal"}:
        result["available_actions"] = list(actions or ())
        result["blocked_actions"] = [
            {
                "action": action,
                "reason": "permission_or_state" if actions is not None else "open_detail",
            }
            for action in {
                "persona_revision": ("publish", "rollback"),
                "persona_state": ("update", "clear"),
                "persona_proposal": ("approve", "reject"),
            }[record.resource_type]
            if action not in (actions or ())
        ]
    if record.resource_type in {
        "entity",
        "external_identity",
        "binding",
        "artifact",
        "relation",
        "episode",
        "claim",
        "observation",
        "note",
        "focus_item",
        "state_record",
        "task",
        "task_step",
        "task_dependency",
        "task_trigger",
        "cognitive_event",
    }:
        result["available_actions"] = list(actions or ())
        action_names = {
            "entity": ("attributes", "redirect"),
            "external_identity": (),
            "binding": ("confirm", "revoke"),
            "artifact": (),
            "relation": ("correct", "transition"),
            "episode": ("update", "transition"),
            "claim": ("correct",),
            "observation": ("annotate",),
            "note": ("update", "transition"),
            "focus_item": ("update", "activate", "transition"),
            "state_record": ("update", "expire"),
            "task": ("update", "transition"),
            "task_step": ("transition",),
            "task_dependency": ("remove",),
            "task_trigger": ("update", "enabled"),
            "cognitive_event": ("dismiss",),
        }[record.resource_type]
        result["blocked_actions"] = [
            {
                "action": action,
                "reason": "permission_or_state" if actions is not None else "open_detail",
            }
            for action in action_names
            if action not in (actions or ())
        ] + (
            []
            if "forget" in (actions or ())
            else [
                {
                    "action": "forget",
                    "reason": "open_detail"
                    if record.resource_type
                    in {
                        "state_record",
                        "task",
                        "focus_item",
                        "entity",
                        "note",
                        "observation",
                        "claim",
                        "episode",
                        "relation",
                        "artifact",
                    }
                    else "not_implemented",
                }
            ]
        )
    material = json.dumps(result, sort_keys=True, separators=(",", ":"))
    result["version_token"] = crypto(request).fingerprint("resource-version:" + material)
    return result


def descriptor(
    collection: str, *, writable: bool = False, forgettable: bool = False
) -> dict[str, Any]:
    spec = BY_COLLECTION[collection]
    value: dict[str, Any] = {
        "collection": spec.collection,
        "resource_type": spec.resource_type,
        "label": spec.label,
        "list_columns": [{"key": key, "label": key, "type": "string"} for key in spec.columns],
        "filters": [
            {"key": key, "label": key, "type": "string"}
            for key in sorted(FILTERS - {"sort", "include_total"})
        ],
        "sorts": [{"key": "created_at_desc", "label": "创建时间", "direction": "desc"}],
        "create_schema": None,
        "update_schema": None,
        "actions": [],
        "supports": {"history": spec.history, "references": True, "forget": False},
    }
    from iris_memory_core.api.console.command_descriptors import with_commands

    value = with_commands(collection, value, writable=writable)
    if spec.resource_type in {
        "state_record",
        "task",
        "focus_item",
        "entity",
        "note",
        "observation",
        "claim",
        "episode",
        "relation",
        "artifact",
    }:
        value["supports"]["forget"] = forgettable
        value["supports"]["forget_max_targets"] = 500
        value["supports"]["forget_selector"] = True
        value["supports"]["forget_modes"] = (
            ["soft"] if spec.resource_type == "entity" else ["soft", "erase"]
        )
        if forgettable:
            value["actions"].append(
                {
                    "id": "forget",
                    "label": "预览删除",
                    "permission": "memory.forget",
                    "method": "POST",
                    "fields": [],
                    "reason_codes": ["operator_request"],
                    "description": (
                        "实体仅执行 soft tombstone。保留发生时身份历史。"
                        if spec.resource_type == "entity"
                        else "显式选择 1 至 50 项。soft 与 erase 均不可撤销。"
                        "提交 erase 需要最近重新认证。"
                    ),
                }
            )
    return value
