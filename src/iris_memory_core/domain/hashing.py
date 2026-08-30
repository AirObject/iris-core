"""Versioned canonical JSON serialization and content hashing (§4.4)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

CANONICAL_JSON_VERSION = 1


def canonical_json(value: Mapping[str, Any] | list[Any]) -> str:
    """Serialize with sorted keys and no whitespace; hash inputs exclude volatile fields."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Mapping[str, Any] | list[Any]) -> str:
    payload = json.dumps(
        {"canonical_json_version": CANONICAL_JSON_VERSION, "content": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def request_fingerprint(
    operation: str,
    payload: Mapping[str, Any],
) -> str:
    """Fingerprint an idempotent request from method, path semantics and business payload."""
    return content_hash({"operation": operation, "payload": dict(payload)})
