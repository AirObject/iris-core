"""Explicit secret-free authentication serializers and signed list cursors."""

from __future__ import annotations

import base64
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import Request

from iris_memory_core.api.console.auth import crypto
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.views import timestamp
from iris_memory_core.domain.console import OperatorKey, OperatorPrincipal, OperatorSession


def time_us(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def key_view(key: OperatorKey, now_us: int) -> dict[str, Any]:
    return {
        "id": key.id,
        "tenant_id": key.tenant_id,
        "token_prefix": key.token_prefix,
        "label": key.label,
        "description": key.description,
        "template": key.template,
        "grants": key.grant.as_dict(),
        "can_delegate": key.can_delegate,
        "delegable_subject_entity_ids": sorted(key.delegable_subject_entity_ids),
        "status": "expired"
        if key.status != "revoked"
        and (
            key.expires_us <= now_us
            or (
                key.status == "pending_confirmation"
                and (key.confirmation_expires_us or 0) <= now_us
            )
        )
        else key.status,
        "revision": key.revision,
        "created_at": timestamp(key.created_us),
        "expires_at": timestamp(key.expires_us),
        "created_by": key.created_by,
        "rotated_from_id": key.rotated_from_id,
        "confirmation_expires_at": timestamp(key.confirmation_expires_us)
        if key.confirmation_expires_us is not None
        else None,
        "revoked_at": timestamp(key.revoked_us) if key.revoked_us is not None else None,
        "revoke_reason": key.revoke_reason,
    }


def session_view(request: Request, principal: OperatorPrincipal) -> dict[str, Any]:
    session, key = principal.session, principal.key
    return {
        "session": {
            "id": session.id,
            "expires_at": timestamp(session.expires_us),
            "idle_expires_at": timestamp(session.idle_expires_us),
            "reauth_until": timestamp(session.reauth_until_us) if session.reauth_until_us else None,
        },
        "operator": {
            "key_id": key.id,
            "label": key.label,
            "template": key.template,
            "tenant_id": key.tenant_id,
        },
        "permissions": list(principal.permissions),
        "grants": key.grant.as_dict(),
        "csrf_token": crypto(request).csrf(session.id, session.epoch),
    }


def session_summary(session: OperatorSession, current_id: str) -> dict[str, Any]:
    return {
        "id": session.id,
        "created_at": timestamp(session.created_us),
        "expires_at": timestamp(session.expires_us),
        "idle_expires_at": timestamp(session.idle_expires_us),
        "last_active_at": timestamp(session.last_active_us),
        "reauth_until": timestamp(session.reauth_until_us) if session.reauth_until_us else None,
        "current": session.id == current_id,
    }


def _binding(request: Request, principal: OperatorPrincipal) -> list[Any]:
    return [
        principal.key.id,
        principal.key.grant.fingerprint,
        request.url.path,
        sorted((k, v) for k, v in request.query_params.multi_items() if k != "cursor"),
    ]


def decode_page(
    request: Request,
    principal: OperatorPrincipal,
    now_us: int,
    *,
    filters: frozenset[str] = frozenset(),
) -> tuple[int, tuple[int, str] | None]:
    try:
        if set(request.query_params) - {"limit", "cursor"} - filters:
            raise ValueError
        if len(request.query_params.multi_items()) != len(request.query_params):
            raise ValueError
        limit = int(request.query_params.get("limit", "50"))
        if not 1 <= limit <= 200:
            raise ValueError
    except ValueError:
        raise ConsoleError("invalid_request", kind="validation_failed", status=400) from None
    cursor = request.query_params.get("cursor")
    if not cursor:
        return limit, None
    try:
        if len(cursor) > 4096:
            raise ValueError
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(crypto(request).sign_cursor(payload), signature):
            raise ValueError
        value = json.loads(payload)
        # Normalize tuples to the JSON array representation.
        if (
            value["binding"] != json.loads(json.dumps(_binding(request, principal)))
            or int(value["expires_us"]) <= now_us
        ):
            raise ValueError
        return limit, (int(value["created_us"]), str(value["id"]))
    except (ValueError, KeyError, TypeError):
        raise ConsoleError("invalid_request", kind="cursor_invalid", status=400) from None


def page_meta(
    request: Request,
    principal: OperatorPrincipal,
    now_us: int,
    limit: int,
    *,
    last: tuple[int, str] | None,
    has_more: bool,
) -> dict[str, Any]:
    cursor = None
    if last and has_more:
        payload = json.dumps(
            {
                "binding": _binding(request, principal),
                "expires_us": str(now_us + 900_000_000),
                "created_us": str(last[0]),
                "id": last[1],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        cursor = (
            base64.urlsafe_b64encode(payload + crypto(request).sign_cursor(payload))
            .decode()
            .rstrip("=")
        )
    return {"next_cursor": cursor, "has_more": has_more, "limit": limit}
