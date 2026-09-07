"""Reconcile committed deletion facts and disable restored destructive intent."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from iris_memory_core.application.entity_deletion import entity_selector_key
from iris_memory_core.application.ports.clock import SystemClock, Uuid7Generator
from iris_memory_core.domain.console_operations import ConsoleOperation, OperationProblem
from iris_memory_core.domain.retention import ForgetSelector
from iris_memory_core.storage.console_operations import ConsoleOperationRepository
from iris_memory_core.storage.repositories import LedgerRepository


def _confirmed(connection: sqlite3.Connection, operation: ConsoleOperation) -> int:
    payload = json.loads(operation.payload_json)
    states = payload.get("states") if isinstance(payload, dict) else None
    if not isinstance(states, list) or len(states) != operation.total:
        raise ValueError("restored operation has no valid fixed target set")
    confirmed = 0
    indices: set[int] = set()
    for position, state in enumerate(states):
        if not isinstance(state, dict):
            raise ValueError("restored operation target is invalid")
        kind, identifier, index = (
            state.get("resource_type"),
            state.get("id"),
            state.get("input_index"),
        )
        if (
            not isinstance(kind, str)
            or not isinstance(identifier, str)
            or type(index) is not int
            or not 0 <= index < operation.total
            or index in indices
        ):
            raise ValueError("restored operation target identity is invalid")
        indices.add(index)
        selector = (
            entity_selector_key(identifier)
            if kind == "entity"
            else ForgetSelector(
                kind="resource", resource_type=kind, resource_id=identifier
            ).selector_key()
        )
        # The full selector prefix uses the existing ledger uniqueness index.
        # Replay may record erased_count=0 if another replay already sealed the
        # root; the own successful request and tombstone together establish it.
        row = connection.execute(
            "SELECT 1 FROM forget_requests f WHERE tenant_id=? AND app_instance_id=? "
            "AND selector_key=? AND idempotency_key=? AND reason_code=? AND erase_content=? "
            "AND target_count=1 AND protected_skipped=0 AND held_skipped=0 "
            "AND EXISTS (SELECT 1 FROM resource_tombstones t WHERE t.tenant_id=f.tenant_id "
            "AND t.resource_type=? AND t.resource_id=?) LIMIT 1",
            (
                operation.tenant_id,
                "console:" + operation.key_id,
                selector,
                f"console-preview:{operation.preview_id}:{index}",
                operation.reason_code,
                int(operation.mode == "erase"),
                kind,
                identifier,
            ),
        ).fetchone()
        if row is not None:
            if confirmed != position:
                raise ValueError("restored operation ledger has noncontiguous progress")
            confirmed += 1
    if confirmed < operation.processed:
        raise ValueError("restored operation progress has no matching deletion facts")
    return confirmed


def reset_operations_for_restore(database: Path) -> None:
    """Run after ledger replay in isolated staging, before the offline switch."""
    connection = sqlite3.connect(database, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='console_operations'"
            ).fetchone()
            is None
        ):
            return  # Supported predecessor backups do not contain operations.
        connection.execute("BEGIN IMMEDIATE")
        repository = ConsoleOperationRepository(connection)
        clock = SystemClock()
        ledger = LedgerRepository(connection, clock, Uuid7Generator())
        typed = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='console_operation_backups' "
                "AND type='table'"
            ).fetchone()
            is not None
        )
        after = ""
        while True:
            # Load at most one private snapshot; no unbounded payload materialization.
            row = connection.execute(
                "SELECT * FROM console_operations WHERE id>? "
                "AND (status IN ('queued','running','paused','blocked') OR kind='trusted_backup') "
                "ORDER BY id LIMIT 1",
                (after,),
            ).fetchone()
            if row is None:
                break
            operation = repository.get(row["tenant_id"], row["id"])
            assert operation is not None
            after = operation.id
            if typed and operation.kind == "trusted_backup":
                if operation.blocked_reason == "restore_requires_review":
                    continue
                now = max(clock.now_us(), operation.updated_us)
                connection.execute(
                    "UPDATE console_operation_backups SET result_ref=NULL,manifest_hash=NULL,"
                    "verified_us=NULL WHERE operation_id=?",
                    (operation.id,),
                )
                connection.execute(
                    "UPDATE console_operations SET status='blocked',revision=revision+1,"
                    "processed=0,"
                    "current_job_id=NULL,blocked_reason='restore_requires_review',"
                    "finished_us=NULL,updated_us=? WHERE id=?",
                    (now, operation.id),
                )
                repository.add_problem(
                    OperationProblem(operation.id, -1, "restore_requires_review", now)
                )
                ledger.audit(
                    tenant_id=operation.tenant_id,
                    actor="restore:console_operations",
                    action="console.operation.restored",
                    resource_type="console_operation",
                    resource_id=operation.id,
                    revision=operation.revision + 1,
                    reason_code="restore_requires_review",
                    details={"kind": "trusted_backup"},
                )
                continue
            if (
                operation.blocked_reason == "restore_requires_review"
                and operation.payload_json == "{}"
            ):
                # A safely blocked operation cannot resume on another restore.
                continue
            processed = _confirmed(connection, operation)
            complete = processed == operation.total
            now = max(clock.now_us(), operation.updated_us)
            repository.advance(
                replace(
                    operation,
                    status="completed" if complete else "blocked",
                    revision=operation.revision + 1,
                    processed=processed,
                    forget=replace(operation.forget_payload, payload_json="{}"),
                    current_job_id=None,
                    blocked_reason=None if complete else "restore_requires_review",
                    updated_us=now,
                    finished_us=now if complete else None,
                ),
                expected_revision=operation.revision,
            )
            if not complete:
                repository.add_problem(
                    OperationProblem(operation.id, -1, "restore_requires_review", now)
                )
            ledger.audit(
                tenant_id=operation.tenant_id,
                actor="restore:console_operations",
                action="console.operation.restored",
                resource_type="console_operation",
                resource_id=operation.id,
                revision=operation.revision + 1,
                reason_code="restore_requires_review",
                details={"processed": processed, "total": operation.total, "completed": complete},
            )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
