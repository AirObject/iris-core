"""SQLite adapter for operator credentials and durable authentication state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Any

from iris_memory_core.domain.console import (
    CommandPreview,
    OperatorGrant,
    OperatorKey,
    OperatorSession,
)
from iris_memory_core.domain.errors import ConflictError, RevisionMismatchError


def _key(row: sqlite3.Row) -> OperatorKey:
    data = dict(row)
    data["grant"] = OperatorGrant.parse(json.loads(data.pop("grants_json")))
    data["delegable_subject_entity_ids"] = frozenset(
        json.loads(data.pop("delegable_subjects_json"))
    )
    data["can_delegate"] = bool(data["can_delegate"])
    return OperatorKey(**data)


def _key_data(key: OperatorKey) -> dict[str, Any]:
    data = asdict(key)
    del data["grant"]
    del data["delegable_subject_entity_ids"]
    data["grants_json"] = json.dumps(key.grant.as_dict(), sort_keys=True, separators=(",", ":"))
    data["delegable_subjects_json"] = json.dumps(sorted(key.delegable_subject_entity_ids))
    return data


class ConsoleRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def command_preview(
        self, tenant_id: str, key_id: str, identifier: str
    ) -> CommandPreview | None:
        row = self._connection.execute(
            "SELECT * FROM console_command_previews WHERE tenant_id=? AND key_id=? AND id=?",
            (tenant_id, key_id, identifier),
        ).fetchone()
        return CommandPreview(**dict(row)) if row is not None else None

    def insert_command_preview(self, preview: CommandPreview) -> None:
        values = asdict(preview)
        self._connection.execute(
            f"INSERT INTO console_command_previews ({','.join(values)}) "
            f"VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def consume_command_preview(
        self,
        tenant_id: str,
        key_id: str,
        identifier: str,
        preview_hash: str,
        *,
        now_us: int,
        receipt_json: str,
    ) -> None:
        changed = self._connection.execute(
            "UPDATE console_command_previews SET status='consumed',consumed_us=?,receipt_json=?,"
            "payload_json='{}' WHERE tenant_id=? AND key_id=? AND id=? AND preview_hash=? "
            "AND status='ready' AND expires_us>?",
            (now_us, receipt_json, tenant_id, key_id, identifier, preview_hash, now_us),
        )
        if changed.rowcount != 1:
            raise ConflictError("preview changed before committing")

    def prune_expired_command_previews(self, *, now_us: int) -> None:
        # Bound housekeeping work in the operator transaction. Consumed receipt
        # retention is longer than the short preview validity window.
        self._connection.execute(
            "DELETE FROM console_command_previews WHERE id IN ("
            "SELECT id FROM console_command_previews WHERE status='ready' AND expires_us<=? "
            "ORDER BY expires_us,id LIMIT 200)",
            (now_us,),
        )
        self._connection.execute(
            "DELETE FROM console_command_previews WHERE id IN ("
            "SELECT id FROM console_command_previews WHERE status='consumed' AND expires_us<=? "
            "ORDER BY expires_us,id LIMIT 200)",
            (now_us - 7 * 86_400_000_000,),
        )

    def pending_successor(self, key_id: str) -> OperatorKey | None:
        row = self._connection.execute(
            "SELECT * FROM console_operator_keys WHERE rotated_from_id=? "
            "AND status='pending_confirmation'",
            (key_id,),
        ).fetchone()
        return _key(row) if row else None

    def expire_result(
        self, tenant_id: str, actor: str, operation: str, request_key: str, *, now_us: int
    ) -> None:
        if not operation.startswith("console."):
            raise ValueError("Console retention namespace required")
        row = self._connection.execute(
            "SELECT r.transaction_ref FROM idempotency_records r JOIN idempotency_outcomes o "
            "ON o.transaction_ref=r.transaction_ref WHERE r.tenant_id=? AND r.app_instance_id=? "
            "AND r.operation=? AND r.idempotency_key=? AND r.status='completed' "
            "AND o.completed_us<=?",
            (tenant_id, actor, operation, request_key, now_us - 86_400_000_000),
        ).fetchone()
        if row:
            self._connection.execute(
                "DELETE FROM idempotency_records WHERE tenant_id=? AND app_instance_id=? "
                "AND operation=? AND idempotency_key=?",
                (tenant_id, actor, operation, request_key),
            )
            self._connection.execute(
                "DELETE FROM idempotency_outcomes WHERE transaction_ref=?", (row[0],)
            )
        self._connection.execute(
            "UPDATE console_sessions SET previous_digest=NULL,refresh_request_hash=NULL,"
            "refresh_cipher=NULL,alias_expires_us=NULL WHERE alias_expires_us<=?",
            (now_us,),
        )

    def authentication_initialized(self) -> bool:
        return (
            self._connection.execute("SELECT 1 FROM console_sessions LIMIT 1").fetchone()
            is not None
        )

    def key(self, key_id: str) -> OperatorKey | None:
        row = self._connection.execute(
            "SELECT * FROM console_operator_keys WHERE id=?", (key_id,)
        ).fetchone()
        return _key(row) if row else None

    def insert_key(self, key: OperatorKey) -> None:
        data = _key_data(key)
        self._connection.execute(
            f"INSERT INTO console_operator_keys ({','.join(data)}) "
            f"VALUES ({','.join('?' for _ in data)})",
            tuple(data.values()),
        )

    def save_key(self, key: OperatorKey, *, expected_revision: int) -> None:
        data = _key_data(key)
        result = self._connection.execute(
            f"UPDATE console_operator_keys SET {','.join(name + '=?' for name in data)} "
            "WHERE id=? AND revision=?",
            (*data.values(), key.id, expected_revision),
        )
        if result.rowcount != 1:
            current = self.key(key.id)
            raise RevisionMismatchError(
                "console_key", key.id, expected_revision, current.revision if current else None
            )

    def keys(
        self, tenant_id: str, *, limit: int = 201, after: tuple[int, str] | None = None
    ) -> tuple[OperatorKey, ...]:
        condition = " AND (created_us,id)<(?,?)" if after else ""
        values: tuple[object, ...] = (tenant_id, *(after or ()), limit)
        rows = self._connection.execute(
            "SELECT * FROM console_operator_keys WHERE tenant_id=?"
            + condition
            + " ORDER BY created_us DESC,id DESC LIMIT ?",
            values,
        )
        return tuple(_key(row) for row in rows)

    def owners(self, tenant_id: str, *, now_us: int) -> tuple[OperatorKey, ...]:
        rows = self._connection.execute(
            "SELECT * FROM console_operator_keys WHERE tenant_id=? AND template='owner' "
            "AND status='active' AND expires_us>?",
            (tenant_id, now_us),
        )
        return tuple(_key(row) for row in rows)

    def expire_pending(self, key_id: str, *, now_us: int) -> None:
        self._connection.execute(
            "UPDATE console_operator_keys SET status='revoked',revoked_us=?,"
            "revoke_reason='confirmation_expired',revision=revision+1 "
            "WHERE rotated_from_id=? AND status='pending_confirmation' "
            "AND confirmation_expires_us<=?",
            (now_us, key_id, now_us),
        )

    def session(self, session_id: str) -> OperatorSession | None:
        row = self._connection.execute(
            "SELECT * FROM console_sessions WHERE id=?", (session_id,)
        ).fetchone()
        return OperatorSession(**dict(row)) if row else None

    def session_by_digest(self, digest: str, *, alias: bool = False) -> OperatorSession | None:
        column = "previous_digest" if alias else "token_sha256"
        row = self._connection.execute(
            f"SELECT * FROM console_sessions WHERE {column}=?", (digest,)
        ).fetchone()
        return OperatorSession(**dict(row)) if row else None

    def insert_session(self, session: OperatorSession) -> None:
        data = asdict(session)
        self._connection.execute(
            f"INSERT INTO console_sessions ({','.join(data)}) "
            f"VALUES ({','.join('?' for _ in data)})",
            tuple(data.values()),
        )

    def save_session(self, session: OperatorSession) -> None:
        data = asdict(session)
        self._connection.execute(
            f"UPDATE console_sessions SET {','.join(name + '=?' for name in data)} WHERE id=?",
            (*data.values(), session.id),
        )

    def sessions(
        self, key_id: str, *, now_us: int, limit: int = 201, after: tuple[int, str] | None = None
    ) -> tuple[OperatorSession, ...]:
        condition = " AND (created_us,id)<(?,?)" if after else ""
        values: tuple[object, ...] = (key_id, now_us, now_us, *(after or ()), limit)
        return tuple(
            OperatorSession(**dict(row))
            for row in self._connection.execute(
                "SELECT * FROM console_sessions WHERE key_id=? AND revoked_us IS NULL "
                "AND expires_us>? AND idle_expires_us>?"
                + condition
                + " ORDER BY created_us DESC,id DESC LIMIT ?",
                values,
            )
        )

    def revoke_sessions(self, key_id: str, *, now_us: int) -> None:
        self._connection.execute(
            "UPDATE console_sessions SET revoked_us=?,previous_digest=NULL,"
            "refresh_request_hash=NULL,refresh_cipher=NULL,alias_expires_us=NULL "
            "WHERE key_id=? AND revoked_us IS NULL",
            (now_us, key_id),
        )

    def consume_attempt(self, bucket: str, *, now_us: int, limit: int) -> int:
        # Fixed windows survive process restarts; keys are HMAC digests only.
        self._connection.execute(
            "DELETE FROM console_auth_attempts WHERE last_attempt_us<?", (now_us - 900_000_000,)
        )
        row = self._connection.execute(
            "SELECT * FROM console_auth_attempts WHERE bucket_key=?", (bucket,)
        ).fetchone()
        started = int(row["window_start_us"]) if row else now_us
        count = int(row["attempts"]) if row and now_us - started < 60_000_000 else 0
        if not count:
            started = now_us
        count += 1
        blocked = started + 60_000_000 if count > limit else 0
        self._connection.execute(
            "INSERT INTO console_auth_attempts VALUES (?,?,?,?,?) "
            "ON CONFLICT(bucket_key) DO UPDATE SET window_start_us=excluded.window_start_us,"
            "attempts=excluded.attempts,blocked_until_us=excluded.blocked_until_us,"
            "last_attempt_us=excluded.last_attempt_us",
            (bucket, started, count, blocked, now_us),
        )
        return max(0, (blocked - now_us + 999_999) // 1_000_000)

    def attempt_wait(self, bucket: str, *, now_us: int) -> int:
        row = self._connection.execute(
            "SELECT blocked_until_us FROM console_auth_attempts WHERE bucket_key=?", (bucket,)
        ).fetchone()
        return max(0, (int(row[0]) - now_us + 999_999) // 1_000_000) if row else 0
