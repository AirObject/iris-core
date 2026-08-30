"""Idempotency records: fingerprint partitioning, replay and crash recovery.

Protocol (§20.5): the ``in_progress`` marker commits before the business
transaction; the outcome row commits atomically with the business writes. A
crash between the two leaves an expired ``in_progress`` record that recovery
resolves from the outcome table — never by blindly repeating side effects.

Lease take-over is fenced by an owner token: claiming an expired record is a
compare-and-set on ``status='failed_replayable'``, and the business
transaction's completion update is conditional on the same owner token. A
second worker that repossesses the lease therefore invalidates the first
worker's commit, which rolls back its business writes instead of duplicating
them.

Recovery itself is fenced against stale snapshots: the demotion and the
outcome-based completion both compare-and-set on the ``owner_token`` and
``expires_us`` the caller actually observed. A worker holding a pre-claim
snapshot of an expired record can therefore never demote the fresh lease
another worker just claimed — it reloads and is blocked by the live lease
instead of executing a second time.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence

from iris_memory_core.domain.errors import (
    IdempotencyInProgressError,
    IdempotencyKeyReusedError,
)
from iris_memory_core.domain.model import IdempotencyRecord, IdempotentResult
from iris_memory_core.storage.uow import Store
from iris_memory_core.storage.uow import Transaction as StoreTransaction

DEFAULT_LEASE_US = 30_000_000  # 30 seconds in microseconds

_KEY_PREDICATE = "tenant_id = ? AND app_instance_id = ? AND operation = ? AND idempotency_key = ?"

#: Recovery only ever touches the exact record version its caller observed.
_OBSERVED_PREDICATE = " AND status = 'in_progress' AND owner_token IS ? AND expires_us = ?"


def _key_values(record: IdempotencyRecord) -> tuple[str, str, str, str]:
    return (
        record.tenant_id,
        record.app_instance_id,
        record.operation,
        record.idempotency_key,
    )


class _FencedOut(Exception):
    """Internal: the completion update lost the owner-token check."""


def _record_row(row: sqlite3.Row) -> IdempotencyRecord:
    return IdempotencyRecord(
        tenant_id=str(row["tenant_id"]),
        app_instance_id=str(row["app_instance_id"]),
        operation=str(row["operation"]),
        idempotency_key=str(row["idempotency_key"]),
        request_fingerprint=str(row["request_fingerprint"]),
        status=str(row["status"]),
        response_code=str(row["response_code"]) if row["response_code"] is not None else None,
        response_body=str(row["response_body"]) if row["response_body"] is not None else None,
        resource_refs=tuple(str(ref) for ref in json.loads(str(row["resource_refs"]))),
        transaction_ref=str(row["transaction_ref"]) if row["transaction_ref"] is not None else None,
        owner_token=str(row["owner_token"]) if row["owner_token"] is not None else None,
        created_us=int(row["created_us"]),
        expires_us=int(row["expires_us"]),
    )


class IdempotencyManager:
    """Implements the ``IdempotencyRunner`` port on top of a ``Store``."""

    def __init__(self, store: Store, *, lease_us: int = DEFAULT_LEASE_US) -> None:
        self._store = store
        self._lease_us = lease_us

    # -- lease acquisition -----------------------------------------------------

    def begin(
        self,
        *,
        tenant_id: str,
        app_instance_id: str,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> IdempotencyRecord:
        """Acquire the lease for one idempotent operation.

        Returns a record in ``in_progress`` (owned by this caller) or
        ``completed`` (replay). Raises on fingerprint reuse, on a live lease,
        and when another worker wins the claim on an expired record.
        """
        now_us = self._store.clock.now_us()
        owner_token = str(self._store.ids.new())
        existing = self.load(
            tenant_id=tenant_id,
            app_instance_id=app_instance_id,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return self._resolve_existing(existing, request_fingerprint, now_us, owner_token)
        try:
            with self._store.write() as tx:
                tx.raw().execute(
                    "INSERT INTO idempotency_records (tenant_id, app_instance_id, operation, "
                    "idempotency_key, request_fingerprint, status, owner_token, created_us, "
                    "expires_us) VALUES (?, ?, ?, ?, ?, 'in_progress', ?, ?, ?)",
                    (
                        tenant_id,
                        app_instance_id,
                        operation,
                        idempotency_key,
                        request_fingerprint,
                        owner_token,
                        now_us,
                        now_us + self._lease_us,
                    ),
                )
        except sqlite3.IntegrityError:
            # Lost an insert race (multi-process writer); resolve the winner.
            existing = self.load(
                tenant_id=tenant_id,
                app_instance_id=app_instance_id,
                operation=operation,
                idempotency_key=idempotency_key,
            )
            assert existing is not None
            return self._resolve_existing(existing, request_fingerprint, now_us, owner_token)
        record = self.load(
            tenant_id=tenant_id,
            app_instance_id=app_instance_id,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        assert record is not None
        return record

    def _resolve_existing(
        self,
        existing: IdempotencyRecord,
        fingerprint: str,
        now_us: int,
        owner_token: str,
    ) -> IdempotencyRecord:
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyKeyReusedError(
                "idempotency key was already used with a different payload",
                details={"operation": existing.operation},
            )
        # Bounded re-dispatch: a stale snapshot (another worker recovered or
        # re-claimed between our read and our CAS) reloads the fresh record
        # instead of acting on expired observations.
        for _ in range(3):
            if existing.status == "completed":
                return existing
            if existing.status == "in_progress" and existing.expires_us > now_us:
                raise IdempotencyInProgressError()
            if existing.status == "in_progress":
                # Expired lease: resolve from outcomes, demote to replayable,
                # or discover our snapshot was stale.
                recovered = self._recover(existing)
                if recovered is None:
                    fresh = self._reload(record=existing)
                    assert fresh is not None
                    existing = fresh
                    continue
                if recovered.status == "completed":
                    return recovered
                existing = recovered
            return self._claim(existing, now_us, owner_token)
        raise IdempotencyInProgressError(
            "the idempotency lease kept changing under this caller; retry"
        )

    def _recover(self, record: IdempotencyRecord) -> IdempotencyRecord | None:
        """Resolve one expired/failed record from the outcome table.

        Returns ``None`` when the compare-and-set matched zero rows — the
        record changed after the caller read it, so the observation is stale
        and must not be acted upon (a stale ``in_progress`` observation must
        never demote a lease another worker freshly claimed).
        """
        with self._store.write() as tx:
            row = None
            if record.transaction_ref is not None:
                row = (
                    tx.raw()
                    .execute(
                        "SELECT * FROM idempotency_outcomes WHERE transaction_ref = ?",
                        (record.transaction_ref,),
                    )
                    .fetchone()
                )
            if row is None:
                cursor = tx.raw().execute(
                    f"UPDATE idempotency_records SET status = 'failed_replayable' "
                    f"WHERE {_KEY_PREDICATE}{_OBSERVED_PREDICATE}",
                    (*_key_values(record), record.owner_token, record.expires_us),
                )
            else:
                cursor = tx.raw().execute(
                    f"UPDATE idempotency_records SET status = 'completed', response_code = ?, "
                    f"response_body = ?, resource_refs = ? "
                    f"WHERE {_KEY_PREDICATE}{_OBSERVED_PREDICATE}",
                    (
                        str(row["result_code"]),
                        str(row["result_body"]) if row["result_body"] is not None else None,
                        str(row["resource_refs"]),
                        *_key_values(record),
                        record.owner_token,
                        record.expires_us,
                    ),
                )
            stale = cursor.rowcount != 1
        if stale:
            return None
        updated = self._reload(record=record)
        assert updated is not None
        return updated

    def _reload(self, *, record: IdempotencyRecord) -> IdempotencyRecord | None:
        return self.load(
            tenant_id=record.tenant_id,
            app_instance_id=record.app_instance_id,
            operation=record.operation,
            idempotency_key=record.idempotency_key,
        )

    def _claim(self, record: IdempotencyRecord, now_us: int, owner_token: str) -> IdempotencyRecord:
        """Compare-and-set the lease; exactly one worker can win."""
        with self._store.write() as tx:
            cursor = tx.raw().execute(
                f"UPDATE idempotency_records SET status = 'in_progress', owner_token = ?, "
                f"transaction_ref = NULL, created_us = ?, expires_us = ? "
                f"WHERE {_KEY_PREDICATE} AND status = 'failed_replayable'",
                (owner_token, now_us, now_us + self._lease_us, *_key_values(record)),
            )
            loser = cursor.rowcount == 0
        if loser:
            winner = self.load(
                tenant_id=record.tenant_id,
                app_instance_id=record.app_instance_id,
                operation=record.operation,
                idempotency_key=record.idempotency_key,
            )
            if winner is not None and winner.status == "completed":
                return winner
            raise IdempotencyInProgressError(
                "another worker claimed the expired idempotency lease first"
            )
        claimed = self.load(
            tenant_id=record.tenant_id,
            app_instance_id=record.app_instance_id,
            operation=record.operation,
            idempotency_key=record.idempotency_key,
        )
        assert claimed is not None
        return claimed

    # -- completion ------------------------------------------------------------

    def complete(
        self,
        record: IdempotencyRecord,
        *,
        response_code: str,
        response_body: str,
        resource_refs: Sequence[str],
        transaction_ref: str,
    ) -> IdempotencyRecord:
        """Complete a lease this caller owns; fenced by the owner token."""
        refs_json = json.dumps(list(resource_refs), ensure_ascii=False, sort_keys=True)
        now_us = self._store.clock.now_us()
        with self._store.write() as tx:
            tx.raw().execute(
                "INSERT INTO idempotency_outcomes (transaction_ref, result_code, result_body, "
                "resource_refs, completed_us) VALUES (?, ?, ?, ?, ?)",
                (transaction_ref, response_code, response_body, refs_json, now_us),
            )
            self._owned_completion_update(
                tx, record, transaction_ref, response_code, response_body, refs_json
            )
        updated = self.load(
            tenant_id=record.tenant_id,
            app_instance_id=record.app_instance_id,
            operation=record.operation,
            idempotency_key=record.idempotency_key,
        )
        assert updated is not None
        return updated

    @staticmethod
    def _owned_completion_update(
        tx: StoreTransaction,
        record: IdempotencyRecord,
        transaction_ref: str,
        response_code: str,
        response_body: str,
        refs_json: str,
    ) -> None:
        cursor = tx.raw().execute(
            f"UPDATE idempotency_records SET status = 'completed', response_code = ?, "
            f"response_body = ?, resource_refs = ?, transaction_ref = ? "
            f"WHERE {_KEY_PREDICATE} AND status = 'in_progress' AND owner_token = ?",
            (
                response_code,
                response_body,
                refs_json,
                transaction_ref,
                *_key_values(record),
                record.owner_token,
            ),
        )
        if cursor.rowcount != 1:
            # The lease was repossessed; the caller's business writes in this
            # transaction must not survive.
            raise _FencedOut()

    # -- maintenance and reads ---------------------------------------------------

    def recover_expired(self) -> tuple[IdempotencyRecord, ...]:
        now_us = self._store.clock.now_us()
        recovered: list[IdempotencyRecord] = []
        with self._store.read() as tx:
            rows = (
                tx.raw()
                .execute(
                    "SELECT * FROM idempotency_records WHERE status = 'in_progress' "
                    "AND expires_us <= ?",
                    (now_us,),
                )
                .fetchall()
            )
        for row in rows:
            record = _record_row(row)
            outcome = self._recover(record)
            if outcome is not None:
                recovered.append(outcome)
        return tuple(recovered)

    def load(
        self,
        *,
        tenant_id: str,
        app_instance_id: str,
        operation: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        with self._store.read() as tx:
            row = (
                tx.raw()
                .execute(
                    f"SELECT * FROM idempotency_records WHERE {_KEY_PREDICATE}",
                    (tenant_id, app_instance_id, operation, idempotency_key),
                )
                .fetchone()
            )
        return _record_row(row) if row is not None else None

    # -- execution -----------------------------------------------------------------

    def run(
        self,
        *,
        tenant_id: str,
        app_instance_id: str,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        execute: Callable[[StoreTransaction], tuple[str, str, Sequence[str]]],
    ) -> IdempotentResult:
        """Run an operation under idempotency; replay returns the first outcome.

        ``execute`` receives the write transaction and returns
        ``(code, body, resource_refs)``. The outcome row and the owner-fenced
        record update commit inside the same business transaction: a crash
        cannot lose one half, and a fenced-out worker loses its whole
        transaction instead of duplicating side effects.
        """
        record = self.begin(
            tenant_id=tenant_id,
            app_instance_id=app_instance_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if record.status == "completed":
            return IdempotentResult(
                code=record.response_code or "ok",
                body=record.response_body or "",
                resource_refs=record.resource_refs,
                replayed=True,
            )
        transaction_ref = str(self._store.ids.new())
        fenced = False
        try:
            with self._store.write() as tx:
                code, body, refs = execute(tx)
                now_us = self._store.clock.now_us()
                refs_json = json.dumps(list(refs), ensure_ascii=False, sort_keys=True)
                tx.raw().execute(
                    "INSERT INTO idempotency_outcomes (transaction_ref, result_code, "
                    "result_body, resource_refs, completed_us) VALUES (?, ?, ?, ?, ?)",
                    (transaction_ref, code, body, refs_json, now_us),
                )
                self._owned_completion_update(tx, record, transaction_ref, code, body, refs_json)
        except _FencedOut:
            fenced = True
        except BaseException:
            # A business rejection (e.g. revision_mismatch) must not strand the
            # key as a live lease: that would turn the stable error into up to
            # a full lease term of idempotency_in_progress on retry. Abandon
            # the lease by owner-token CAS so an immediate retry re-executes
            # and re-raises the same stable error; failures are retryable by
            # design (only committed outcomes are recorded).
            self._abandon(record)
            raise
        if fenced:
            winner = self.load(
                tenant_id=tenant_id,
                app_instance_id=app_instance_id,
                operation=operation,
                idempotency_key=idempotency_key,
            )
            if winner is not None and winner.status == "completed":
                return IdempotentResult(
                    code=winner.response_code or "ok",
                    body=winner.response_body or "",
                    resource_refs=winner.resource_refs,
                    replayed=True,
                )
            raise IdempotencyInProgressError("lease lost to another worker before completion")
        return IdempotentResult(code=code, body=body, resource_refs=tuple(refs), replayed=False)

    def _abandon(self, record: IdempotencyRecord) -> None:
        """Best-effort owner-fenced demotion of a lease after a business error.

        If this fails (crash, busy database) the lease simply expires and the
        standard expired-lease recovery takes over — correctness never depends
        on the abandonment, only retry latency does.
        """
        try:
            with self._store.write() as tx:
                tx.raw().execute(
                    f"UPDATE idempotency_records SET status = 'failed_replayable' "
                    f"WHERE {_KEY_PREDICATE} AND status = 'in_progress' AND owner_token = ?",
                    (*_key_values(record), record.owner_token),
                )
        except Exception:
            pass
