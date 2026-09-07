"""Bounded metadata adapter; every progress update uses a revision CAS."""

import sqlite3
from dataclasses import asdict, fields

from iris_memory_core.domain.console_operations import (
    ConsoleOperation,
    ForgetOperationPayload,
    OperationProblem,
    OperationSummary,
    TrustedBackupPayload,
)
from iris_memory_core.domain.errors import ConflictError


class ConsoleOperationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._typed = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='console_operation_forget'"
            ).fetchone()
            is not None
        )

    def _operation(self, row: sqlite3.Row) -> ConsoleOperation:
        value = dict(row)
        if not self._typed:
            value["forget"] = ForgetOperationPayload(
                **{field.name: value.pop(field.name) for field in fields(ForgetOperationPayload)}
            )
        else:
            value.pop("forget_id")
            value.pop("backup_id")
            kind = value["kind"]
            payload_type = (
                ForgetOperationPayload if kind == "memory_forget" else TrustedBackupPayload
            )
            table = (
                "console_operation_forget"
                if kind == "memory_forget"
                else "console_operation_backups"
            )
            saved = self._connection.execute(
                f"SELECT {','.join(field.name for field in fields(payload_type))} FROM {table} "
                "WHERE operation_id=?",
                (value["id"],),
            ).fetchone()
            if saved is None:
                raise ValueError("operation typed payload is missing")
            value["forget" if kind == "memory_forget" else "backup"] = payload_type(**dict(saved))
        return ConsoleOperation(**value)

    def get(self, tenant_id: str, identifier: str) -> ConsoleOperation | None:
        row = self._connection.execute(
            "SELECT * FROM console_operations WHERE tenant_id=? AND id=?",
            (tenant_id, identifier),
        ).fetchone()
        return self._operation(row) if row is not None else None

    def insert(self, operation: ConsoleOperation) -> None:
        row = asdict(operation)
        forget, backup = row.pop("forget"), row.pop("backup")
        if operation.kind == "memory_forget" and forget is not None and backup is None:
            payload, table, reference = forget, "console_operation_forget", "forget_id"
        elif operation.kind == "trusted_backup" and backup is not None and forget is None:
            payload, table, reference = backup, "console_operation_backups", "backup_id"
            if (
                operation.status != "queued"
                or operation.processed
                or backup["result_ref"] is not None
            ):
                raise ValueError("new backup must be unexecuted")
        else:
            raise ValueError("operation kind and payload differ")
        row[reference] = operation.id
        self._connection.execute(
            f"INSERT INTO console_operations ({','.join(row)}) "
            f"VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
        detail = {
            "operation_id": operation.id,
            "tenant_id": operation.tenant_id,
            "key_id": operation.key_id,
            **payload,
        }
        self._connection.execute(
            f"INSERT INTO {table} ({','.join(detail)}) VALUES ({','.join('?' for _ in detail)})",
            tuple(detail.values()),
        )

    def advance(self, operation: ConsoleOperation, *, expected_revision: int) -> None:
        # Payload and common CAS remain atomic even if a caller catches a conflict.
        self._connection.execute("SAVEPOINT console_operation_advance")
        try:
            self._advance(operation, expected_revision=expected_revision)
        except BaseException:
            self._connection.execute("ROLLBACK TO console_operation_advance")
            self._connection.execute("RELEASE console_operation_advance")
            raise
        self._connection.execute("RELEASE console_operation_advance")

    def _advance(self, operation: ConsoleOperation, *, expected_revision: int) -> None:
        if operation.revision != expected_revision + 1:
            raise ConflictError("operation revision must advance exactly once")
        names: tuple[str, ...] = (
            "status",
            "revision",
            "processed",
            "current_job_id",
            "blocked_reason",
            "updated_us",
            "started_us",
            "finished_us",
        )
        if operation.kind == "memory_forget":
            payload = operation.forget_payload
            if self._typed:
                self._connection.execute(
                    "UPDATE console_operation_forget SET payload_json=?,expected_deletion_seq=? "
                    "WHERE operation_id=?",
                    (payload.payload_json, payload.expected_deletion_seq, operation.id),
                )
            else:
                names += ("payload_json", "expected_deletion_seq")
        elif operation.backup is not None:
            self._connection.execute(
                "UPDATE console_operation_backups SET result_ref=?,manifest_hash=?,verified_us=? "
                "WHERE operation_id=?",
                (
                    operation.backup.result_ref,
                    operation.backup.manifest_hash,
                    operation.backup.verified_us,
                    operation.id,
                ),
            )
        changed = self._connection.execute(
            f"UPDATE console_operations SET {','.join(name + '=?' for name in names)} "
            "WHERE tenant_id=? AND id=? AND revision=? AND processed<=? "
            "AND status NOT IN ('completed','completed_with_warnings','failed',"
            "'cancelled','cancelled_partial')",
            (
                *(getattr(operation, name) for name in names),
                operation.tenant_id,
                operation.id,
                expected_revision,
                operation.processed,
            ),
        )
        if changed.rowcount != 1:
            raise ConflictError("operation progress changed")

    def list_owned(
        self,
        tenant_id: str,
        key_id: str,
        grant_fingerprint: str,
        *,
        key_revision: int,
        kind: str | None = None,
        status: str | None = None,
        created_from: int | None = None,
        created_before: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int = 51,
    ) -> tuple[OperationSummary, ...]:
        if not 1 <= limit <= 201:
            raise ValueError("operation page exceeds its bound")
        clauses = ["tenant_id=?", "key_id=?", "grant_fingerprint=?", "key_revision=?"]
        values: list[str | int] = [tenant_id, key_id, grant_fingerprint, key_revision]
        if kind is not None:
            clauses.append("kind=?")
            values.append(kind)
        if status is not None:
            clauses.append("status=?")
            values.append(status)
        if created_from is not None:
            clauses.append("created_us>=?")
            values.append(created_from)
        if created_before is not None:
            clauses.append("created_us<?")
            values.append(created_before)
        if after is not None:
            clauses.append("(created_us,id)<(?,?)")
            values.extend(after)
        rows = self._connection.execute(
            f"SELECT {','.join(field.name for field in fields(OperationSummary))} "
            "FROM console_operations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_us DESC,id DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return tuple(OperationSummary(**dict(row)) for row in rows)

    def add_problem(self, problem: OperationProblem) -> None:
        inserted = self._connection.execute(
            "INSERT INTO console_operation_problems(operation_id,input_index,code,created_us) "
            "VALUES(?,?,?,?) ON CONFLICT(operation_id,input_index) DO NOTHING",
            (problem.operation_id, problem.input_index, problem.code, problem.created_us),
        )
        if inserted.rowcount == 1:
            self._connection.execute(
                "UPDATE console_operations SET problems_count=problems_count+1 WHERE id=?",
                (problem.operation_id,),
            )

    def problems(
        self, operation_id: str, *, after: int = -2, limit: int = 51
    ) -> tuple[OperationProblem, ...]:
        if not 1 <= limit <= 201:
            raise ValueError("problem page exceeds its bound")
        rows = self._connection.execute(
            "SELECT * FROM console_operation_problems WHERE operation_id=? AND input_index>? "
            "ORDER BY input_index LIMIT ?",
            (operation_id, after, limit),
        ).fetchall()
        return tuple(OperationProblem(**dict(row)) for row in rows)
