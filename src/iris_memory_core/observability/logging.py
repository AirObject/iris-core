"""Low-sensitivity structured logging (§31).

Records may only carry the allowlisted field vocabulary: event names, error
codes, counts, revisions, durations, id hashes and coarse status. Content
bodies, raw identifiers, credentials and payloads are dropped or redacted
BEFORE emission; the sanitizer is applied on every emit so a caller mistake
cannot leak. A scan test feeds canary content through every emitter.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from collections.abc import Callable
from typing import Any

#: The complete allowed field vocabulary (§31 structured-log field list).
ALLOWED_LOG_FIELDS = frozenset(
    {
        "event",
        "code",
        "reason_code",
        "status",
        "count",
        "revision",
        "attempt",
        "generation",
        "epoch",
        "duration_ms",
        "id_hash",
        "job_kind",
        "operation_class",
        "outcome",
        "warning",
    }
)

#: Field names whose content is inherently sensitive — never emit.
FORBIDDEN_LOG_FIELDS = frozenset(
    {
        "content",
        "payload",
        "body",
        "message_body",
        "text",
        "token",
        "secret",
        "password",
        "api_key",
        "authorization",
        "external_id",
        "tenant_id",
        "agent_id",
        "space_id",
        "entity_id",
        "task_id",
        "request_id",
        "cursor",
        "structured_payload",
        "artifact",
    }
)


def id_hash(value: str) -> str:
    """Short hash for identifiers referenced in logs (§31: ID hash only)."""
    return "h_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


#: Fields that must be integers (bools excluded): counts, revisions,
#: attempt/generation/epoch numbers.
_INTEGER_LOG_FIELDS = frozenset({"count", "revision", "attempt", "generation", "epoch"})

#: Fields that may be int or float (never bool, never free text).
_NUMERIC_LOG_FIELDS = frozenset({"duration_ms"}) | _INTEGER_LOG_FIELDS


def sanitize_log_record(record: dict[str, Any]) -> dict[str, Any]:
    """Drop forbidden fields, keep allowlisted ones, hash id-like values.

    The allowlist is necessary but not sufficient: numeric fields must carry
    actual numbers (bools excluded) and are REDACTED to a hash otherwise —
    free text smuggled into an allowed field like ``count`` never reaches
    the sink verbatim.
    """
    sanitized: dict[str, Any] = {}
    for key, value in record.items():
        if key in FORBIDDEN_LOG_FIELDS:
            continue
        if key not in ALLOWED_LOG_FIELDS:
            continue
        if key in {
            "event",
            "code",
            "reason_code",
            "status",
            "job_kind",
            "operation_class",
            "outcome",
            "warning",
        }:
            text = str(value)
            # Enum-like fields must stay short tokens; anything long or
            # free-form is hashed defensively.
            sanitized[key] = text if len(text) <= 96 and _is_safe_token(text) else id_hash(text)
        elif key in _NUMERIC_LOG_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                sanitized[key] = id_hash(str(value))
            else:
                sanitized[key] = value
        elif key == "id_hash":
            sanitized[key] = id_hash(str(value))
        else:
            sanitized[key] = value
    return sanitized


def _is_safe_token(text: str) -> bool:
    return all(char.isalnum() or char in {".", "_", "-", ":"} for char in text)


class SensitiveDataLeakedError(RuntimeError):
    """Raised in strict mode when a record contains forbidden material."""


class LowSensitivityLogger:
    """Emits sanitized JSON lines to a sink (stderr by default)."""

    def __init__(
        self,
        sink: Callable[[str], None] | None = None,
        *,
        strict: bool = False,
    ) -> None:
        self._sink = sink or (lambda line: sys.stderr.write(line + "\n"))
        self._strict = strict
        self._lock = threading.Lock()
        self.records: list[dict[str, Any]] = []

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        if _looks_like_content(event):
            raise SensitiveDataLeakedError("event names must be dotted tokens")
        record = sanitize_log_record({"event": event, **fields})
        if self._strict:
            for key in fields:
                if key in FORBIDDEN_LOG_FIELDS:
                    raise SensitiveDataLeakedError(f"forbidden log field: {key}")
        line = json.dumps(record, sort_keys=True, ensure_ascii=True)
        with self._lock:
            self.records.append(record)
            self._sink(line)
        return record

    def drain(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(self.records)


def _looks_like_content(value: str) -> bool:
    """Heuristic gate: long free text or whitespace suggests content, not a token."""
    return len(value) > 96 or any(char.isspace() for char in value)


__all__ = [
    "ALLOWED_LOG_FIELDS",
    "FORBIDDEN_LOG_FIELDS",
    "LowSensitivityLogger",
    "SensitiveDataLeakedError",
    "id_hash",
    "sanitize_log_record",
]
