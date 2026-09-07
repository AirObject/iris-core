"""Bounded metadata adapter; every progress update uses a revision CAS."""

import sqlite3
from dataclasses import asdict, fields

from iris_memory_core.domain.console_operations import (
    ConsoleOperation,
    OperationProblem,
    OperationSummary,
)
from iris_memory_core.domain.errors import ConflictError


class ConsoleOperationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, tenant_id: str, identifier: str) -> ConsoleOperation | None:
        row = self._connection.execute(
            "SELECT * FROM console_operations WHERE tenant_id=? AND id=?",
            (tenant_id, identifier),
        ).fetchone()
        return ConsoleOperation(**dict(row)) if row is not None else None

    def insert(self, operation: ConsoleOperation) -> None:
        row = asdict(operation)
        self._connection.execute(
            f"INSERT INTO console_operations ({','.join(row)}) "
            f"VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )

    def advance(self, operation: ConsoleOperation, *, expected_revision: int) -> None:
        if operation.revision != expected_revision + 1:
            raise ConflictError("operation revision must advance exactly once")
        names = (
            "status",
            "revision",
            "processed",
            "payload_json",
            "expected_deletion_seq",
            "current_job_id",
            "blocked_reason",
            "updated_us",
            "started_us",
            "finished_us",
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
