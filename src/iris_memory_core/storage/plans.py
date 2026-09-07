"""Phase 4 repositories: notes, tasks, steps, dependencies, triggers, events.

Every method runs inside the caller's short transaction and returns DOMAIN
records (ADR-0007). Current pointers advance by compare-and-set on the
expected revision (ADR-0004): a lost race is ``revision_mismatch``, never a
silent overwrite. Occurrence rows are append-only and idempotent — the
UNIQUE(trigger_id, trigger_revision, scheduled_at_us, occurrence_key)
constraint collapses duplicate scans, restarts and clock back-slew onto one
logical occurrence.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Collection, Sequence
from typing import Any

from iris_memory_core.application.ports import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import (
    ConflictError,
    NotFoundError,
    NotReadyError,
    RevisionMismatchError,
)
from iris_memory_core.domain.event import (
    DEFAULT_MAX_DELIVERY_ATTEMPTS,
    CognitiveEventCurrent,
    CognitiveEventRevision,
    EventStatus,
)
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.note import NoteCurrent, NoteRevision
from iris_memory_core.domain.task import (
    TaskCurrent,
    TaskDependencyEdge,
    TaskRevision,
    TaskStepCurrent,
    TaskStepRevision,
    TaskTriggerCurrent,
    TaskTriggerRevision,
    TriggerOccurrence,
)


def _with_space_envelope(
    sql: str, params: list[object], allowed_space_ids: Collection[str] | None
) -> tuple[str, list[object]]:
    """Append the access-envelope space filter to an event query.

    ``None`` leaves the query untouched (caller-wide); an empty sequence
    keeps only agent-level rows (the app may see no space at all); a
    populated sequence keeps agent-level rows plus the named spaces. This
    mirrors ``require_same_tenant_agent`` exactly, so the SQL batch limit
    never fills with rows the application layer would drop.
    """
    if allowed_space_ids is None:
        return sql, params
    ids = sorted(allowed_space_ids)
    if not ids:
        return sql + "AND space_id IS NULL ", params
    placeholders = ", ".join("?" * len(ids))
    return sql + f"AND (space_id IS NULL OR space_id IN ({placeholders})) ", [*params, *ids]


_EVENT_TOMBSTONE_EXCLUSION = (
    "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
    "WHERE _rt.tenant_id = cognitive_events.tenant_id "
    "AND _rt.resource_type = 'cognitive_event' AND _rt.resource_id = cognitive_events.id) "
)


def _with_event_logical_validity(
    sql: str, params: list[object], now_us: int | None
) -> tuple[str, list[object]]:
    """Keep logically-dead rows out of the batch BEFORE the LIMIT.

    A ``pending`` event past its ``expires_us`` is awaiting the sweep, and a
    tombstoned event is forgotten — neither may occupy a LIMIT slot and
    starve the visible tail. Non-pending statuses (acknowledged/expired/
    cancelled/delivered) are deliberately untouched by the horizon clause.
    """
    if now_us is not None:
        sql += "AND (status != 'pending' OR expires_us IS NULL OR expires_us > ?) "
        params = [*params, now_us]
    return sql + _EVENT_TOMBSTONE_EXCLUSION, params


def _with_request_scope(
    sql: str,
    params: list[object],
    space_group_id: str | None,
    space_id: str | None,
    session_id: str | None,
) -> tuple[str, list[object]]:
    """Formal downward-visibility match against a CONCRETE request scope.

    Per §5.2, for each dimension: ``D is null OR (R is not null AND D = R)``.
    A request-side null is never a wildcard — it only admits data whose
    dimension is null. Applied in SQL so the LIMIT bounds only rows the
    request scope can actually see.
    """
    for column, value in (
        ("space_group_id", space_group_id),
        ("space_id", space_id),
        ("session_id", session_id),
    ):
        if value is None:
            sql += f"AND {column} IS NULL "
        else:
            sql += f"AND ({column} IS NULL OR {column} = ?) "
            params.append(value)
    return sql, params


def _one(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(sql, params).fetchone()
    return row


def _require(
    connection: sqlite3.Connection, sql: str, params: tuple[Any, ...], what: str
) -> sqlite3.Row:
    row = _one(connection, sql, params)
    if row is None:
        raise NotFoundError(f"{what} not found")
    return row


def _refs(raw: str | None) -> tuple[dict[str, object], ...]:
    if not raw:
        return ()
    decoded = json.loads(raw)
    return tuple(decoded) if isinstance(decoded, list) else ()


def _labels(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    decoded = json.loads(raw)
    return tuple(decoded) if isinstance(decoded, list) else ()


# ---------------------------------------------------------------------------
# Notes


def _note_current_from_row(row: sqlite3.Row) -> NoteCurrent:
    return NoteCurrent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=row["scope_key"],
        kind=row["kind"],
        title=row["title"],
        status=row["status"],
        current_revision=row["current_revision"],
        importance=row["importance"],
        review_after_us=row["review_after_us"],
        snooze_until_us=row["snooze_until_us"],
        due_at_us=row["due_at_us"],
        content_hash=row["content_hash"],
        archived_us=row["archived_us"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
    )


def _note_revision_from_row(row: sqlite3.Row) -> NoteRevision:
    return NoteRevision(
        id=row["id"],
        note_id=row["note_id"],
        tenant_id=row["tenant_id"],
        revision=row["revision"],
        kind=row["kind"],
        title=row["title"],
        body=row["body"],
        privacy_labels=_labels(row["privacy_labels"]),
        source_refs=_refs(row["source_refs"]),
        importance=row["importance"],
        status=row["status"],
        review_after_us=row["review_after_us"],
        snooze_until_us=row["snooze_until_us"],
        due_at_us=row["due_at_us"],
        promotion_target_type=row["promotion_target_type"],
        promotion_target_id=row["promotion_target_id"],
        archived_us=row["archived_us"],
        content_hash=row["content_hash"],
        created_us=row["created_us"],
        created_by=row["created_by"],
    )


class NoteRepository:
    """Note current rows plus immutable revisions."""

    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def get(self, note_id: str) -> NoteCurrent:
        row = _require(self._connection, "SELECT * FROM notes WHERE id = ?", (note_id,), "note")
        return _note_current_from_row(row)

    def get_revision(self, revision_id: str) -> NoteRevision:
        row = _require(
            self._connection,
            "SELECT * FROM note_revisions WHERE id = ?",
            (revision_id,),
            f"note revision {revision_id}",
        )
        return _note_revision_from_row(row)

    def current_revision_row(self, note_id: str) -> NoteRevision:
        row = _one(
            self._connection,
            "SELECT v.* FROM notes n JOIN note_revisions v ON v.id = n.current_revision_id "
            "WHERE n.id = ?",
            (note_id,),
        )
        if row is None:
            raise NotFoundError(f"note {note_id} has no current revision")
        return _note_revision_from_row(row)

    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        kind: str,
        title: str,
        status: str,
        importance: float,
        review_after_us: int | None,
        snooze_until_us: int | None,
        due_at_us: int | None,
        content_hash: str,
    ) -> str:
        note_id = str(self._ids.new())
        now_us = self._clock.now_us()
        try:
            self._connection.execute(
                "INSERT INTO notes (id, tenant_id, agent_id, space_group_id, space_id, "
                "session_id, scope_key, kind, title, status, current_revision, "
                "current_revision_id, importance, review_after_us, snooze_until_us, due_at_us, "
                "content_hash, created_us, updated_us) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,1,'',?,?,?,?,?,?,?)",
                (
                    note_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    scope_key,
                    kind,
                    title,
                    status,
                    importance,
                    review_after_us,
                    snooze_until_us,
                    due_at_us,
                    content_hash,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"note insert rejected: {error}") from error
        return note_id

    def insert_revision(
        self,
        *,
        note_id: str,
        tenant_id: str,
        revision: int,
        kind: str,
        title: str,
        body: str,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        importance: float,
        status: str,
        review_after_us: int | None,
        snooze_until_us: int | None,
        due_at_us: int | None,
        promotion_target_type: str | None,
        promotion_target_id: str | None,
        archived_us: int | None,
        content_hash: str,
        created_by: str,
    ) -> str:
        revision_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO note_revisions (id, note_id, tenant_id, revision, kind, title, body, "
            "privacy_labels, source_refs, importance, status, review_after_us, snooze_until_us, "
            "due_at_us, promotion_target_type, promotion_target_id, archived_us, content_hash, "
            "created_us, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                note_id,
                tenant_id,
                revision,
                kind,
                title,
                body,
                canonical_json(list(privacy_labels)),
                canonical_json([dict(ref) for ref in source_refs]),
                importance,
                status,
                review_after_us,
                snooze_until_us,
                due_at_us,
                promotion_target_type,
                promotion_target_id,
                archived_us,
                content_hash,
                self._clock.now_us(),
                created_by,
            ),
        )
        return revision_id

    def set_initial_pointer(self, note_id: str, revision_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE notes SET current_revision_id = ?, updated_us = ? "
            "WHERE id = ? AND current_revision_id = ''",
            (revision_id, self._clock.now_us(), note_id),
        )
        return cursor.rowcount

    def advance_pointer(
        self,
        note_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        importance: float | None = None,
        review_after_us: int | None = None,
        snooze_until_us: int | None = None,
        snooze_until_clear: bool = False,
        due_at_us: int | None = None,
        archived_us_set: bool = False,
        archived_us: int | None = None,
        archived_us_clear: bool = False,
    ) -> int:
        assignments = [
            "current_revision = ?",
            "current_revision_id = ?",
            "status = ?",
            "updated_us = ?",
        ]
        params: list[Any] = [revision, revision_id, status, self._clock.now_us()]
        if importance is not None:
            assignments.append("importance = ?")
            params.append(importance)
        if review_after_us is not None:
            assignments.append("review_after_us = ?")
            params.append(review_after_us)
        if snooze_until_us is not None:
            assignments.append("snooze_until_us = ?")
            params.append(snooze_until_us)
        elif snooze_until_clear:
            assignments.append("snooze_until_us = NULL")
        if due_at_us is not None:
            assignments.append("due_at_us = ?")
            params.append(due_at_us)
        if archived_us_set:
            assignments.append("archived_us = ?")
            params.append(archived_us)
        elif archived_us_clear:
            assignments.append("archived_us = NULL")
        cursor = self._connection.execute(
            f"UPDATE notes SET {', '.join(assignments)} WHERE id = ? AND current_revision = ?",
            (*params, note_id, expected_revision),
        )
        return cursor.rowcount

    def raise_pointer_mismatch(self, note_id: str, expected: int) -> None:
        row = self.get(note_id)
        raise RevisionMismatchError("note", note_id, expected, row.current_revision)

    def list_notes(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: tuple[str, ...] = ("inbox", "pinned", "snoozed"),
        kind: str | None = None,
        include_tombstoned: bool = False,
        limit: int = 100,
        cursor_created_us: int | None = None,
        cursor_id: str | None = None,
    ) -> list[NoteCurrent]:
        clauses = ["tenant_id = ?", "agent_id = ?"]
        params: list[Any] = [tenant_id, agent_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if not include_tombstoned:
            # Same SQL-level exclusion the claims/episode enumerations apply:
            # a committed tombstone must not feed ANY canonical consumer (the
            # FTS rebuild reads this directly; app-level listings re-check).
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
                "WHERE _rt.tenant_id = notes.tenant_id AND _rt.resource_type = 'note' "
                "AND _rt.resource_id = notes.id)"
            )
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if cursor_created_us is not None and cursor_id is not None:
            clauses.append("(created_us > ? OR (created_us = ? AND id > ?))")
            params.extend([cursor_created_us, cursor_created_us, cursor_id])
        rows = self._connection.execute(
            f"SELECT * FROM notes WHERE {' AND '.join(clauses)} ORDER BY created_us, id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_note_current_from_row(row) for row in rows]

    def review_due(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 500
    ) -> list[NoteCurrent]:
        """Bounded sweep of notes whose snooze or review time has arrived."""
        rows = self._connection.execute(
            "SELECT * FROM notes WHERE tenant_id = ? AND agent_id = ? "
            "AND status IN ('inbox', 'snoozed') AND ("
            "(status = 'snoozed' AND snooze_until_us IS NOT NULL AND snooze_until_us <= ?) OR "
            "(status = 'inbox' AND review_after_us IS NOT NULL AND review_after_us <= ?)"
            ") ORDER BY updated_us, id LIMIT ?",
            (tenant_id, agent_id, now_us, now_us, limit),
        ).fetchall()
        return [_note_current_from_row(row) for row in rows]

    def duplicate_candidates(self, note: NoteCurrent, *, limit: int = 50) -> list[NoteCurrent]:
        """Same-scope notes sharing the content hash (association, not delete)."""
        rows = self._connection.execute(
            "SELECT * FROM notes WHERE tenant_id = ? AND agent_id = ? AND content_hash = ? "
            "AND id != ? AND status IN ('inbox', 'pinned', 'snoozed') "
            "ORDER BY created_us, id LIMIT ?",
            (note.tenant_id, note.agent_id, note.content_hash, note.id, limit),
        ).fetchall()
        return [_note_current_from_row(row) for row in rows]

    def history(self, note_id: str, *, limit: int = 100) -> list[NoteRevision]:
        rows = self._connection.execute(
            "SELECT * FROM note_revisions WHERE note_id = ? ORDER BY revision DESC LIMIT ?",
            (note_id, limit),
        ).fetchall()
        return [_note_revision_from_row(row) for row in rows]

    def notes_for_session(self, tenant_id: str, space_id: str, session_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM notes WHERE tenant_id = ? AND space_id = ? AND session_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt WHERE "
            "_rt.tenant_id = notes.tenant_id AND _rt.resource_type = 'note' "
            "AND _rt.resource_id = notes.id)",
            (tenant_id, space_id, session_id),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def notes_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM notes WHERE tenant_id = ? AND space_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt WHERE "
            "_rt.tenant_id = notes.tenant_id AND _rt.resource_type = 'note' "
            "AND _rt.resource_id = notes.id)",
            (tenant_id, space_id),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def erase_content(self, note_id: str, *, now_us: int) -> None:
        """Compliance erasure: scrub title/body in every revision, tombstone
        the current row. Metadata (kinds, hashes, timestamps) survives."""
        self._connection.execute(
            "UPDATE note_revisions SET title = '<erased>', body = '' WHERE note_id = ?",
            (note_id,),
        )
        self._connection.execute(
            "UPDATE notes SET title = '<erased>', status = 'tombstoned', updated_us = ? "
            "WHERE id = ? AND status != 'tombstoned'",
            (now_us, note_id),
        )


# ---------------------------------------------------------------------------
# Tasks, steps, dependencies, triggers, occurrences


def _task_current_from_row(row: sqlite3.Row) -> TaskCurrent:
    return TaskCurrent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=row["scope_key"],
        parent_task_id=row["parent_task_id"],
        title=row["title"],
        owner_kind=row["owner_kind"],
        owner_entity_id=row["owner_entity_id"],
        status=row["status"],
        priority=row["priority"],
        next_action=row["next_action"],
        due_at_us=row["due_at_us"],
        completed_us=row["completed_us"],
        current_revision=row["current_revision"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
    )


def _task_revision_from_row(row: sqlite3.Row) -> TaskRevision:
    return TaskRevision(
        id=row["id"],
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        revision=row["revision"],
        title=row["title"],
        goal=row["goal"],
        owner_kind=row["owner_kind"],
        owner_entity_id=row["owner_entity_id"],
        privacy_labels=_labels(row["privacy_labels"]),
        source_refs=_refs(row["source_refs"]),
        status=row["status"],
        priority=row["priority"],
        next_action=row["next_action"],
        progress_note=row["progress_note"],
        due_at_us=row["due_at_us"],
        completed_us=row["completed_us"],
        created_us=row["created_us"],
        created_by=row["created_by"],
    )


def _step_current_from_row(row: sqlite3.Row) -> TaskStepCurrent:
    return TaskStepCurrent(
        id=row["id"],
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        stable_key=row["stable_key"],
        title=row["title"],
        ordinal=row["ordinal"],
        status=row["status"],
        current_revision=row["current_revision"],
        started_us=row["started_us"],
        completed_us=row["completed_us"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
    )


def _step_revision_from_row(row: sqlite3.Row) -> TaskStepRevision:
    return TaskStepRevision(
        id=row["id"],
        step_id=row["step_id"],
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        revision=row["revision"],
        stable_key=row["stable_key"],
        title=row["title"],
        description=row["description"],
        privacy_labels=_labels(row["privacy_labels"]),
        status=row["status"],
        ordinal=row["ordinal"],
        expected_effect=row["expected_effect"],
        completion_evidence_refs=_refs(row["completion_evidence_refs"]),
        started_us=row["started_us"],
        completed_us=row["completed_us"],
        created_us=row["created_us"],
        created_by=row["created_by"],
    )


def _dependency_from_row(row: sqlite3.Row) -> TaskDependencyEdge:
    return TaskDependencyEdge(
        dependency_id=row["id"],
        task_id=row["task_id"],
        predecessor_step_id=row["predecessor_step_id"],
        successor_step_id=row["successor_step_id"],
        condition=row["condition"],
        current_revision=row["current_revision"],
        current_revision_id=row["current_revision_id"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
        status=row["status"],
    )


def _trigger_current_from_row(row: sqlite3.Row) -> TaskTriggerCurrent:
    return TaskTriggerCurrent(
        id=row["id"],
        task_id=row["task_id"],
        task_step_id=row["task_step_id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        kind=row["kind"],
        timezone=row["timezone"],
        catch_up_policy=row["catch_up_policy"],
        misfire_grace_us=row["misfire_grace_us"],
        max_occurrences_per_run=row["max_occurrences_per_run"],
        enabled=bool(row["enabled"]),
        next_fire_at_us=row["next_fire_at_us"],
        last_scan_us=row["last_scan_us"],
        current_revision=row["current_revision"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
    )


def _trigger_revision_from_row(row: sqlite3.Row) -> TaskTriggerRevision:
    schedule_raw = row["schedule_spec"]
    condition_raw = row["condition_spec"]
    return TaskTriggerRevision(
        id=row["id"],
        trigger_id=row["trigger_id"],
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        revision=row["revision"],
        kind=row["kind"],
        task_step_id=row["task_step_id"],
        schedule_spec=json.loads(schedule_raw) if schedule_raw else None,
        condition_spec=json.loads(condition_raw) if condition_raw else None,
        timezone=row["timezone"],
        catch_up_policy=row["catch_up_policy"],
        misfire_grace_us=row["misfire_grace_us"],
        max_occurrences_per_run=row["max_occurrences_per_run"],
        enabled=bool(row["enabled"]),
        created_us=row["created_us"],
        created_by=row["created_by"],
    )


def _occurrence_from_row(row: sqlite3.Row) -> TriggerOccurrence:
    return TriggerOccurrence(
        id=row["id"],
        trigger_id=row["trigger_id"],
        trigger_revision=row["trigger_revision"],
        tenant_id=row["tenant_id"],
        scheduled_at_us=row["scheduled_at_us"],
        occurrence_key=row["occurrence_key"],
        status=row["status"],
        reason_code=row["reason_code"],
        cognitive_event_id=row["cognitive_event_id"],
        created_us=row["created_us"],
    )


class TaskRepository:
    """Task/step/dependency/trigger/occurrence current rows and revisions."""

    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- tasks ------------------------------------------------------------

    def deletion_children(
        self, tenant_id: str, task_id: str, *, limit: int = 501
    ) -> tuple[tuple[str, str], ...]:
        """Fixed child kinds and pending delivery facts; callers enforce the cap."""
        sql = (
            "SELECT 'task_step' AS kind,id FROM task_steps WHERE tenant_id=? AND task_id=? "
            "UNION ALL SELECT 'task_dependency',id FROM task_dependencies WHERE tenant_id=? "
            "AND task_id=? "
            "UNION ALL SELECT 'task_trigger',id FROM task_triggers WHERE tenant_id=? AND task_id=? "
            "UNION ALL SELECT 'cognitive_event',id FROM cognitive_events "
            "WHERE tenant_id=? AND object_type='task' AND object_id=? "
            "AND status IN ('pending','delivered') "
            "UNION ALL SELECT 'cognitive_event',e.id FROM task_steps s JOIN cognitive_events e "
            "ON e.tenant_id=s.tenant_id AND e.object_type='task_step' AND e.object_id=s.id "
            "WHERE s.tenant_id=? AND s.task_id=? AND e.status IN ('pending','delivered') "
        )
        sql = (
            "SELECT kind,id FROM (" + sql + ") child WHERE NOT EXISTS "
            "(SELECT 1 FROM resource_tombstones rt WHERE rt.tenant_id=? "
            "AND rt.resource_type=child.kind AND rt.resource_id=child.id) LIMIT ?"
        )
        deadline = time.monotonic() + 0.150
        steps = 0

        def stop() -> int:
            nonlocal steps
            steps += 1_000
            return int(steps > 2_000_000 or time.monotonic() > deadline)

        self._connection.set_progress_handler(stop, 1_000)
        try:
            rows = self._connection.execute(
                sql,
                (tenant_id, task_id) * 5
                + (
                    tenant_id,
                    limit,
                ),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "interrupt" in str(error).lower():
                raise NotReadyError("Task deletion inventory exceeds its query budget") from None
            raise
        finally:
            self._connection.set_progress_handler(None, 0)
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def has_live_child_task(self, tenant_id: str, task_id: str) -> bool:
        deadline = time.monotonic() + 0.150
        steps = 0

        def stop() -> int:
            nonlocal steps
            steps += 1_000
            return int(steps > 2_000_000 or time.monotonic() > deadline)

        self._connection.set_progress_handler(stop, 1_000)
        try:
            return (
                self._connection.execute(
                    "SELECT 1 FROM tasks t WHERE t.tenant_id=? AND t.parent_task_id=? "
                    "AND NOT EXISTS "
                    "(SELECT 1 FROM resource_tombstones r WHERE r.tenant_id=t.tenant_id "
                    "AND r.resource_type='task' AND r.resource_id=t.id) LIMIT 1",
                    (tenant_id, task_id),
                ).fetchone()
                is not None
            )

        except sqlite3.OperationalError as error:
            if "interrupt" in str(error).lower():
                raise NotReadyError("Task child-plan verification exceeds its budget") from None
            raise
        finally:
            self._connection.set_progress_handler(None, 0)

    def erase_task_content(self, task_id: str, *, now_us: int) -> None:
        """Keep identities and lifecycle while erasing every retained body."""
        deadline = time.monotonic() + 0.150
        steps = 0

        def stop() -> int:
            nonlocal steps
            steps += 1_000
            return int(steps > 2_000_000 or time.monotonic() > deadline)

        self._connection.set_progress_handler(stop, 1_000)
        try:
            self._connection.execute(
                "UPDATE tasks SET title='<erased>',next_action=NULL,updated_us=? WHERE id=?",
                (now_us, task_id),
            )
            self._connection.execute(
                "UPDATE task_revisions SET title='<erased>',goal='',next_action=NULL,"
                "progress_note=NULL,"
                "source_refs='[]',privacy_labels='[]' WHERE task_id=?",
                (task_id,),
            )
            self._connection.execute(
                "UPDATE task_steps SET title='<erased>',updated_us=? WHERE task_id=?",
                (now_us, task_id),
            )
            self._connection.execute(
                "UPDATE task_step_revisions SET title='<erased>',description=NULL,"
                "expected_effect=NULL,"
                "completion_evidence_refs='[]',privacy_labels='[]' WHERE task_id=?",
                (task_id,),
            )
            self._connection.execute(
                "UPDATE task_trigger_revisions SET schedule_spec=NULL,condition_spec=NULL "
                "WHERE task_id=?",
                (task_id,),
            )
        except sqlite3.OperationalError as error:
            if "interrupt" in str(error).lower():
                raise NotReadyError("Task erasure exceeds its transaction budget") from None
            raise
        finally:
            self._connection.set_progress_handler(None, 0)

    def get_task(self, task_id: str) -> TaskCurrent:
        row = _require(self._connection, "SELECT * FROM tasks WHERE id = ?", (task_id,), "task")
        return _task_current_from_row(row)

    def get_task_revision(self, revision_id: str) -> TaskRevision:
        row = _require(
            self._connection,
            "SELECT * FROM task_revisions WHERE id = ?",
            (revision_id,),
            f"task revision {revision_id}",
        )
        return _task_revision_from_row(row)

    def current_task_revision_row(self, task_id: str) -> TaskRevision:
        row = _one(
            self._connection,
            "SELECT v.* FROM tasks t JOIN task_revisions v ON v.id = t.current_revision_id "
            "WHERE t.id = ?",
            (task_id,),
        )
        if row is None:
            raise NotFoundError(f"task {task_id} has no current revision")
        return _task_revision_from_row(row)

    def insert_task(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        parent_task_id: str | None,
        title: str,
        owner_kind: str,
        owner_entity_id: str | None,
        status: str,
        priority: int,
        due_at_us: int | None,
    ) -> str:
        task_id = str(self._ids.new())
        now_us = self._clock.now_us()
        try:
            self._connection.execute(
                "INSERT INTO tasks (id, tenant_id, agent_id, space_group_id, space_id, "
                "session_id, scope_key, parent_task_id, title, owner_kind, owner_entity_id, "
                "status, priority, next_action, due_at_us, completed_us, current_revision, "
                "current_revision_id, created_us, updated_us) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,NULL,1,'',?,?)",
                (
                    task_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    scope_key,
                    parent_task_id,
                    title,
                    owner_kind,
                    owner_entity_id,
                    status,
                    priority,
                    due_at_us,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"task insert rejected: {error}") from error
        return task_id

    def insert_task_revision(
        self,
        *,
        task_id: str,
        tenant_id: str,
        revision: int,
        title: str,
        goal: str,
        owner_kind: str,
        owner_entity_id: str | None,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        status: str,
        priority: int,
        next_action: str | None,
        progress_note: str | None,
        due_at_us: int | None,
        completed_us: int | None,
        created_by: str,
    ) -> str:
        revision_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO task_revisions (id, task_id, tenant_id, revision, title, goal, "
            "owner_kind, owner_entity_id, privacy_labels, source_refs, status, priority, "
            "next_action, progress_note, due_at_us, completed_us, created_us, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                task_id,
                tenant_id,
                revision,
                title,
                goal,
                owner_kind,
                owner_entity_id,
                canonical_json(list(privacy_labels)),
                canonical_json([dict(ref) for ref in source_refs]),
                status,
                priority,
                next_action,
                progress_note,
                due_at_us,
                completed_us,
                self._clock.now_us(),
                created_by,
            ),
        )
        return revision_id

    def set_initial_task_pointer(self, task_id: str, revision_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE tasks SET current_revision_id = ?, updated_us = ? "
            "WHERE id = ? AND current_revision_id = ''",
            (revision_id, self._clock.now_us(), task_id),
        )
        return cursor.rowcount

    def advance_task_pointer(
        self,
        task_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        next_action: str | None = None,
        next_action_set: bool = False,
        due_at_us: int | None = None,
        due_at_set: bool = False,
        completed_us: int | None = None,
        completed_us_set: bool = False,
    ) -> int:
        assignments = [
            "current_revision = ?",
            "current_revision_id = ?",
            "status = ?",
            "updated_us = ?",
        ]
        params: list[Any] = [revision, revision_id, status, self._clock.now_us()]
        if next_action_set:
            assignments.append("next_action = ?")
            params.append(next_action)
        if due_at_set:
            assignments.append("due_at_us = ?")
            params.append(due_at_us)
        if completed_us_set:
            assignments.append("completed_us = ?")
            params.append(completed_us)
        cursor = self._connection.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ? AND current_revision = ?",
            (*params, task_id, expected_revision),
        )
        return cursor.rowcount

    def raise_task_pointer_mismatch(self, task_id: str, expected: int) -> None:
        row = self.get_task(task_id)
        raise RevisionMismatchError("task", task_id, expected, row.current_revision)

    def list_tasks(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: tuple[str, ...] = ("proposed", "active", "waiting", "blocked"),
        limit: int = 100,
    ) -> list[TaskCurrent]:
        clauses = [
            "tenant_id = ?",
            "agent_id = ?",
            "NOT EXISTS (SELECT 1 FROM resource_tombstones rt "
            "WHERE rt.tenant_id=tasks.tenant_id AND rt.resource_type='task' "
            "AND rt.resource_id=tasks.id)",
        ]
        params: list[Any] = [tenant_id, agent_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        rows = self._connection.execute(
            f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} "
            "ORDER BY COALESCE(due_at_us, 9223372036854775807), priority, created_us, id "
            "LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_task_current_from_row(row) for row in rows]

    def due_tasks(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 50
    ) -> list[TaskCurrent]:
        """Non-terminal tasks whose due time has arrived (Recall route input)."""
        rows = self._connection.execute(
            "SELECT * FROM tasks WHERE tenant_id = ? AND agent_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones rt "
            "WHERE rt.tenant_id=tasks.tenant_id "
            "AND rt.resource_type='task' AND rt.resource_id=tasks.id) "
            "AND status IN ('active', 'waiting') AND due_at_us IS NOT NULL AND due_at_us <= ? "
            "ORDER BY due_at_us, priority, id LIMIT ?",
            (tenant_id, agent_id, now_us, limit),
        ).fetchall()
        return [_task_current_from_row(row) for row in rows]

    def latest_task_transition(self, task_id: str) -> tuple[int, str, str | None]:
        """Anchor triggers to a status change, ignoring later metadata/child revisions."""
        row = _require(
            self._connection,
            "SELECT r.revision, r.status, previous.status AS previous_status "
            "FROM task_revisions r JOIN tasks current ON current.id = r.task_id "
            "LEFT JOIN task_revisions previous ON previous.task_id = r.task_id "
            "AND previous.revision = r.revision - 1 "
            "WHERE r.task_id = ? AND r.revision <= current.current_revision "
            "AND (r.revision = 1 OR r.status != previous.status) "
            "ORDER BY r.revision DESC LIMIT 1",
            (task_id,),
            "task status transition",
        )
        return int(row["revision"]), str(row["status"]), row["previous_status"]

    def task_history(self, task_id: str, *, limit: int = 100) -> list[TaskRevision]:
        rows = self._connection.execute(
            "SELECT * FROM task_revisions WHERE task_id = ? ORDER BY revision DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [_task_revision_from_row(row) for row in rows]

    # -- steps -------------------------------------------------------------

    def get_step(self, step_id: str) -> TaskStepCurrent:
        row = _require(
            self._connection, "SELECT * FROM task_steps WHERE id = ?", (step_id,), "task step"
        )
        return _step_current_from_row(row)

    def get_step_revision(self, revision_id: str) -> TaskStepRevision:
        row = _require(
            self._connection,
            "SELECT * FROM task_step_revisions WHERE id = ?",
            (revision_id,),
            f"task step revision {revision_id}",
        )
        return _step_revision_from_row(row)

    def current_step_revision_row(self, step_id: str) -> TaskStepRevision:
        row = _one(
            self._connection,
            "SELECT v.* FROM task_steps s JOIN task_step_revisions v "
            "ON v.id = s.current_revision_id WHERE s.id = ?",
            (step_id,),
        )
        if row is None:
            raise NotFoundError(f"task step {step_id} has no current revision")
        return _step_revision_from_row(row)

    def find_step_by_key(self, task_id: str, stable_key: str) -> TaskStepCurrent | None:
        row = _one(
            self._connection,
            "SELECT * FROM task_steps WHERE task_id = ? AND stable_key = ?",
            (task_id, stable_key),
        )
        return _step_current_from_row(row) if row is not None else None

    def insert_step(
        self,
        *,
        task_id: str,
        tenant_id: str,
        stable_key: str,
        title: str,
        ordinal: int,
        status: str,
    ) -> str:
        step_id = str(self._ids.new())
        now_us = self._clock.now_us()
        try:
            self._connection.execute(
                "INSERT INTO task_steps (id, task_id, tenant_id, stable_key, title, ordinal, "
                "status, current_revision, current_revision_id, created_us, updated_us) "
                "VALUES (?,?,?,?,?,?,?,1,'',?,?)",
                (step_id, task_id, tenant_id, stable_key, title, ordinal, status, now_us, now_us),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"task step insert rejected: {error}") from error
        return step_id

    def insert_step_revision(
        self,
        *,
        step_id: str,
        task_id: str,
        tenant_id: str,
        revision: int,
        stable_key: str,
        title: str,
        description: str | None,
        privacy_labels: tuple[str, ...],
        status: str,
        ordinal: int,
        expected_effect: str | None,
        completion_evidence_refs: tuple[dict[str, object], ...],
        started_us: int | None,
        completed_us: int | None,
        created_by: str,
    ) -> str:
        revision_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO task_step_revisions (id, step_id, task_id, tenant_id, revision, "
            "stable_key, title, description, privacy_labels, status, ordinal, expected_effect, "
            "completion_evidence_refs, started_us, completed_us, created_us, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                step_id,
                task_id,
                tenant_id,
                revision,
                stable_key,
                title,
                description,
                canonical_json(list(privacy_labels)),
                status,
                ordinal,
                expected_effect,
                canonical_json([dict(ref) for ref in completion_evidence_refs]),
                started_us,
                completed_us,
                self._clock.now_us(),
                created_by,
            ),
        )
        return revision_id

    def set_initial_step_pointer(self, step_id: str, revision_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE task_steps SET current_revision_id = ?, updated_us = ? "
            "WHERE id = ? AND current_revision_id = ''",
            (revision_id, self._clock.now_us(), step_id),
        )
        return cursor.rowcount

    def advance_step_pointer(
        self,
        step_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        started_us: int | None = None,
        started_us_set: bool = False,
        completed_us: int | None = None,
        completed_us_set: bool = False,
    ) -> int:
        assignments = [
            "current_revision = ?",
            "current_revision_id = ?",
            "status = ?",
            "updated_us = ?",
        ]
        params: list[Any] = [revision, revision_id, status, self._clock.now_us()]
        if started_us_set:
            assignments.append("started_us = ?")
            params.append(started_us)
        if completed_us_set:
            assignments.append("completed_us = ?")
            params.append(completed_us)
        cursor = self._connection.execute(
            f"UPDATE task_steps SET {', '.join(assignments)} WHERE id = ? AND current_revision = ?",
            (*params, step_id, expected_revision),
        )
        return cursor.rowcount

    def raise_step_pointer_mismatch(self, step_id: str, expected: int) -> None:
        row = self.get_step(step_id)
        raise RevisionMismatchError("task_step", step_id, expected, row.current_revision)

    def steps_for_task(self, task_id: str) -> list[TaskStepCurrent]:
        rows = self._connection.execute(
            "SELECT * FROM task_steps WHERE task_id = ? ORDER BY ordinal, created_us, id",
            (task_id,),
        ).fetchall()
        return [_step_current_from_row(row) for row in rows]

    def latest_step_transition(self, step_id: str) -> tuple[int, str, str | None]:
        """Anchor triggers to a status change, ignoring later metadata/child revisions."""
        row = _require(
            self._connection,
            "SELECT r.revision, r.status, previous.status AS previous_status "
            "FROM task_step_revisions r JOIN task_steps current ON current.id = r.step_id "
            "LEFT JOIN task_step_revisions previous ON previous.step_id = r.step_id "
            "AND previous.revision = r.revision - 1 "
            "WHERE r.step_id = ? AND r.revision <= current.current_revision "
            "AND (r.revision = 1 OR r.status != previous.status) "
            "ORDER BY r.revision DESC LIMIT 1",
            (step_id,),
            "step status transition",
        )
        return int(row["revision"]), str(row["status"]), row["previous_status"]

    def step_history(self, step_id: str, *, limit: int = 100) -> list[TaskStepRevision]:
        rows = self._connection.execute(
            "SELECT * FROM task_step_revisions WHERE step_id = ? ORDER BY revision DESC LIMIT ?",
            (step_id, limit),
        ).fetchall()
        return [_step_revision_from_row(row) for row in rows]

    # -- dependencies --------------------------------------------------------

    def dependencies_for_task(self, task_id: str) -> list[TaskDependencyEdge]:
        rows = self._connection.execute(
            "SELECT * FROM task_dependencies WHERE task_id = ? AND status = 'active' "
            "ORDER BY created_us, id",
            (task_id,),
        ).fetchall()
        return [_dependency_from_row(row) for row in rows]

    def dependency_at_revision(self, dependency_id: str, revision: int) -> TaskDependencyEdge:
        row = _require(
            self._connection,
            "SELECT d.id, d.task_id, r.predecessor_step_id, r.successor_step_id, r.condition, "
            "r.revision AS current_revision, r.id AS current_revision_id, d.created_us, "
            "r.created_us AS updated_us, r.status FROM task_dependencies d "
            "JOIN task_dependency_revisions r ON r.dependency_id = d.id "
            "WHERE d.id = ? AND r.revision = ?",
            (dependency_id, revision),
            "dependency revision",
        )
        return _dependency_from_row(row)

    def get_dependency(self, dependency_id: str) -> TaskDependencyEdge:
        row = _require(
            self._connection,
            "SELECT * FROM task_dependencies WHERE id = ?",
            (dependency_id,),
            "task dependency",
        )
        return _dependency_from_row(row)

    def insert_dependency(
        self,
        *,
        task_id: str,
        tenant_id: str,
        predecessor_step_id: str,
        successor_step_id: str,
        condition: str,
    ) -> str:
        dependency_id = str(self._ids.new())
        now_us = self._clock.now_us()
        try:
            self._connection.execute(
                "INSERT INTO task_dependencies (id, task_id, tenant_id, predecessor_step_id, "
                "successor_step_id, condition, current_revision, current_revision_id, "
                "created_us, updated_us) VALUES (?,?,?,?,?,?,1,'',?,?)",
                (
                    dependency_id,
                    task_id,
                    tenant_id,
                    predecessor_step_id,
                    successor_step_id,
                    condition,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"task dependency insert rejected: {error}") from error
        return dependency_id

    def insert_dependency_revision(
        self,
        *,
        dependency_id: str,
        task_id: str,
        tenant_id: str,
        revision: int,
        predecessor_step_id: str,
        successor_step_id: str,
        condition: str,
        created_by: str,
        status: str = "active",
    ) -> str:
        revision_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO task_dependency_revisions (id, dependency_id, task_id, tenant_id, "
            "revision, predecessor_step_id, successor_step_id, condition, "
            "created_us, created_by, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                dependency_id,
                task_id,
                tenant_id,
                revision,
                predecessor_step_id,
                successor_step_id,
                condition,
                self._clock.now_us(),
                created_by,
                status,
            ),
        )
        return revision_id

    def set_initial_dependency_pointer(self, dependency_id: str, revision_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE task_dependencies SET current_revision_id = ?, updated_us = ? "
            "WHERE id = ? AND current_revision_id = ''",
            (revision_id, self._clock.now_us(), dependency_id),
        )
        return cursor.rowcount

    def advance_dependency_pointer(
        self,
        dependency_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        condition: str,
    ) -> int:
        cursor = self._connection.execute(
            "UPDATE task_dependencies SET current_revision = ?, current_revision_id = ?, "
            "status = ?, condition = ?, updated_us = ? WHERE id = ? AND current_revision = ?",
            (
                revision,
                revision_id,
                status,
                condition,
                self._clock.now_us(),
                dependency_id,
                expected_revision,
            ),
        )
        return cursor.rowcount

    def dependency_for_pair(
        self, task_id: str, predecessor_step_id: str, successor_step_id: str
    ) -> TaskDependencyEdge | None:
        row = self._connection.execute(
            "SELECT * FROM task_dependencies WHERE task_id = ? "
            "AND predecessor_step_id = ? AND successor_step_id = ?",
            (task_id, predecessor_step_id, successor_step_id),
        ).fetchone()
        return _dependency_from_row(row) if row is not None else None

    def dependency_revision_number(self, revision_id: str) -> int:
        row = _one(
            self._connection,
            "SELECT revision FROM task_dependency_revisions WHERE id = ?",
            (revision_id,),
        )
        if row is None:
            raise NotFoundError("task dependency pointer does not resolve")
        return int(row["revision"])

    # -- triggers ------------------------------------------------------------

    def get_trigger(self, trigger_id: str) -> TaskTriggerCurrent:
        row = _require(
            self._connection, "SELECT * FROM task_triggers WHERE id = ?", (trigger_id,), "trigger"
        )
        return _trigger_current_from_row(row)

    def get_trigger_revision(self, revision_id: str) -> TaskTriggerRevision:
        row = _require(
            self._connection,
            "SELECT * FROM task_trigger_revisions WHERE id = ?",
            (revision_id,),
            f"trigger revision {revision_id}",
        )
        return _trigger_revision_from_row(row)

    def current_trigger_revision_row(self, trigger_id: str) -> TaskTriggerRevision:
        row = _one(
            self._connection,
            "SELECT v.* FROM task_triggers t JOIN task_trigger_revisions v "
            "ON v.id = t.current_revision_id WHERE t.id = ?",
            (trigger_id,),
        )
        if row is None:
            raise NotFoundError(f"trigger {trigger_id} has no current revision")
        return _trigger_revision_from_row(row)

    def insert_trigger(
        self,
        *,
        task_id: str,
        task_step_id: str | None,
        tenant_id: str,
        agent_id: str,
        kind: str,
        timezone: str,
        catch_up_policy: str,
        misfire_grace_us: int,
        max_occurrences_per_run: int,
        enabled: bool,
        next_fire_at_us: int | None,
    ) -> str:
        trigger_id = str(self._ids.new())
        now_us = self._clock.now_us()
        try:
            self._connection.execute(
                "INSERT INTO task_triggers (id, task_id, task_step_id, tenant_id, agent_id, "
                "kind, timezone, catch_up_policy, misfire_grace_us, max_occurrences_per_run, "
                "enabled, next_fire_at_us, last_scan_us, current_revision, current_revision_id, "
                "created_us, updated_us) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,1,'',?,?)",
                (
                    trigger_id,
                    task_id,
                    task_step_id,
                    tenant_id,
                    agent_id,
                    kind,
                    timezone,
                    catch_up_policy,
                    misfire_grace_us,
                    max_occurrences_per_run,
                    1 if enabled else 0,
                    next_fire_at_us,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"trigger insert rejected: {error}") from error
        return trigger_id

    def insert_trigger_revision(
        self,
        *,
        trigger_id: str,
        task_id: str,
        tenant_id: str,
        revision: int,
        kind: str,
        task_step_id: str | None,
        schedule_spec: dict[str, object] | None,
        condition_spec: dict[str, object] | None,
        timezone: str,
        catch_up_policy: str,
        misfire_grace_us: int,
        max_occurrences_per_run: int,
        enabled: bool,
        created_by: str,
    ) -> str:
        revision_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO task_trigger_revisions (id, trigger_id, task_id, tenant_id, revision, "
            "kind, task_step_id, schedule_spec, condition_spec, timezone, catch_up_policy, "
            "misfire_grace_us, max_occurrences_per_run, enabled, created_us, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                trigger_id,
                task_id,
                tenant_id,
                revision,
                kind,
                task_step_id,
                canonical_json(schedule_spec) if schedule_spec is not None else None,
                canonical_json(condition_spec) if condition_spec is not None else None,
                timezone,
                catch_up_policy,
                misfire_grace_us,
                max_occurrences_per_run,
                1 if enabled else 0,
                self._clock.now_us(),
                created_by,
            ),
        )
        return revision_id

    def set_initial_trigger_pointer(self, trigger_id: str, revision_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE task_triggers SET current_revision_id = ?, updated_us = ? "
            "WHERE id = ? AND current_revision_id = ''",
            (revision_id, self._clock.now_us(), trigger_id),
        )
        return cursor.rowcount

    def advance_trigger_pointer(
        self,
        trigger_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        enabled: bool | None = None,
        spec: TaskTriggerRevision | None = None,
        next_fire_at_us: int | None = None,
    ) -> int:
        """CAS the SPEC revision pointer (spec changes only, §11.4)."""
        assignments = [
            "current_revision = ?",
            "current_revision_id = ?",
            "updated_us = ?",
        ]
        params: list[Any] = [revision, revision_id, self._clock.now_us()]
        if enabled is not None:
            assignments.append("enabled = ?")
            params.append(1 if enabled else 0)
        if spec is not None:
            for field in (
                "kind",
                "task_step_id",
                "timezone",
                "catch_up_policy",
                "misfire_grace_us",
                "max_occurrences_per_run",
            ):
                assignments.append(f"{field} = ?")
                params.append(getattr(spec, field))
            assignments.extend(["next_fire_at_us = ?", "last_scan_us = NULL"])
            params.append(next_fire_at_us)
        cursor = self._connection.execute(
            f"UPDATE task_triggers SET {', '.join(assignments)} WHERE id = ? "
            "AND current_revision = ?",
            (*params, trigger_id, expected_revision),
        )
        return cursor.rowcount

    def raise_trigger_pointer_mismatch(self, trigger_id: str, expected: int) -> None:
        row = self.get_trigger(trigger_id)
        raise RevisionMismatchError("task_trigger", trigger_id, expected, row.current_revision)

    def set_trigger_schedule_state(
        self,
        trigger_id: str,
        *,
        expected_fire_at_us: int | None,
        next_fire_at_us: int | None,
        last_scan_us: int | None = None,
    ) -> int:
        """Scheduler bookkeeping CAS: next_fire_at/last_scan only.

        The spec revision is untouched on purpose: occurrence identity keys
        on the SPEC revision, and scheduler progress must not fork it. The
        CAS on the previously observed ``next_fire_at`` (NULL-aware ``IS``)
        means a stale worker that lost a concurrent advancement writes
        nothing instead of rewinding the marker.
        """
        assignments = ["next_fire_at_us = ?", "updated_us = ?"]
        params: list[Any] = [next_fire_at_us, self._clock.now_us()]
        if last_scan_us is not None:
            assignments.append("last_scan_us = ?")
            params.append(last_scan_us)
        params.append(trigger_id)
        params.append(expected_fire_at_us)
        cursor = self._connection.execute(
            f"UPDATE task_triggers SET {', '.join(assignments)} "
            "WHERE id = ? AND next_fire_at_us IS ?",
            tuple(params),
        )
        return cursor.rowcount

    def triggers_for_scan(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 500
    ) -> list[TaskTriggerCurrent]:
        """Enabled triggers with due time work or condition re-checks.

        Two disjoint selections so neither class starves the other at the
        batch limit: due time triggers come first (they carry wall-clock
        work), then condition triggers ordered by scan staleness — never
        scanned first — so a population larger than the batch converges
        across runs instead of re-reading one fixed head forever.
        """
        due_rows = self._connection.execute(
            "SELECT * FROM task_triggers WHERE tenant_id = ? AND agent_id = ? AND enabled = 1 "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones rt "
            "WHERE rt.tenant_id=task_triggers.tenant_id AND rt.resource_type='task' "
            "AND rt.resource_id=task_triggers.task_id) "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones rt "
            "WHERE rt.tenant_id=task_triggers.tenant_id AND rt.resource_type='task_trigger' "
            "AND rt.resource_id=task_triggers.id) "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones rt "
            "WHERE rt.tenant_id=task_triggers.tenant_id AND rt.resource_type='task_step' "
            "AND rt.resource_id=task_triggers.task_step_id) "
            "AND kind IN ('at_time', 'recurrence') "
            "AND next_fire_at_us IS NOT NULL AND next_fire_at_us <= ? "
            "ORDER BY next_fire_at_us, id LIMIT ?",
            (tenant_id, agent_id, now_us, limit),
        ).fetchall()
        condition_rows = self._connection.execute(
            "SELECT * FROM task_triggers WHERE tenant_id = ? AND agent_id = ? AND enabled = 1 "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones rt "
            "WHERE rt.tenant_id=task_triggers.tenant_id AND rt.resource_type='task' "
            "AND rt.resource_id=task_triggers.task_id) "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones rt "
            "WHERE rt.tenant_id=task_triggers.tenant_id AND rt.resource_type='task_trigger' "
            "AND rt.resource_id=task_triggers.id) "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones rt "
            "WHERE rt.tenant_id=task_triggers.tenant_id AND rt.resource_type='task_step' "
            "AND rt.resource_id=task_triggers.task_step_id) "
            "AND kind IN ('observation_kind', 'state_condition', 'task_transition') "
            "ORDER BY (last_scan_us IS NOT NULL), last_scan_us, id LIMIT ?",
            (tenant_id, agent_id, limit),
        ).fetchall()
        return [
            *[_trigger_current_from_row(row) for row in due_rows],
            *[_trigger_current_from_row(row) for row in condition_rows],
        ]

    def triggers_for_task(self, task_id: str) -> list[TaskTriggerCurrent]:
        rows = self._connection.execute(
            "SELECT * FROM task_triggers WHERE task_id = ? ORDER BY created_us, id",
            (task_id,),
        ).fetchall()
        return [_trigger_current_from_row(row) for row in rows]

    # -- occurrences -----------------------------------------------------------

    def insert_occurrence(
        self,
        *,
        trigger_id: str,
        trigger_revision: int,
        tenant_id: str,
        scheduled_at_us: int,
        occurrence_key: str,
        status: str,
        reason_code: str | None = None,
    ) -> tuple[str, bool]:
        """Append one occurrence; returns (id, created?) — idempotent by key."""
        try:
            occurrence_id = str(self._ids.new())
            self._connection.execute(
                "INSERT INTO task_trigger_occurrences (id, trigger_id, trigger_revision, "
                "tenant_id, scheduled_at_us, occurrence_key, status, reason_code, "
                "cognitive_event_id, created_us) VALUES (?,?,?,?,?,?,?,?,NULL,?)",
                (
                    occurrence_id,
                    trigger_id,
                    trigger_revision,
                    tenant_id,
                    scheduled_at_us,
                    occurrence_key,
                    status,
                    reason_code,
                    self._clock.now_us(),
                ),
            )
            return occurrence_id, True
        except sqlite3.IntegrityError:
            existing = _require(
                self._connection,
                "SELECT * FROM task_trigger_occurrences WHERE trigger_id = ? AND "
                "trigger_revision = ? AND scheduled_at_us = ? AND occurrence_key = ?",
                (trigger_id, trigger_revision, scheduled_at_us, occurrence_key),
                "trigger occurrence",
            )
            return existing["id"], False

    def get_occurrence(self, occurrence_id: str) -> TriggerOccurrence:
        row = _require(
            self._connection,
            "SELECT * FROM task_trigger_occurrences WHERE id = ?",
            (occurrence_id,),
            "trigger occurrence",
        )
        return _occurrence_from_row(row)

    def attach_occurrence_event(self, occurrence_id: str, *, cognitive_event_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE task_trigger_occurrences SET cognitive_event_id = ? WHERE id = ? AND "
            "cognitive_event_id IS NULL",
            (cognitive_event_id, occurrence_id),
        )
        return cursor.rowcount

    def occurrences_for_trigger(
        self, trigger_id: str, *, limit: int = 200
    ) -> list[TriggerOccurrence]:
        rows = self._connection.execute(
            "SELECT * FROM task_trigger_occurrences WHERE trigger_id = ? "
            "ORDER BY scheduled_at_us DESC LIMIT ?",
            (trigger_id, limit),
        ).fetchall()
        return [_occurrence_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Cognitive events


def _event_current_from_row(row: sqlite3.Row) -> CognitiveEventCurrent:
    return CognitiveEventCurrent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=row["scope_key"],
        kind=row["kind"],
        object_type=row["object_type"],
        object_id=row["object_id"],
        occurrence_id=row["occurrence_id"],
        scheduled_at_us=row["scheduled_at_us"],
        deliver_after_us=row["deliver_after_us"],
        expires_us=row["expires_us"],
        status=row["status"],
        delivery_target=row["delivery_target"],
        delivery_attempts=row["delivery_attempts"],
        last_delivery_us=row["last_delivery_us"],
        delivered_lease_id=row["delivered_lease_id"],
        delivered_lease_epoch=row["delivered_lease_epoch"],
        ack_id=row["ack_id"],
        acknowledged_us=row["acknowledged_us"],
        summary_of_count=row["summary_of_count"],
        current_revision=row["current_revision"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
    )


def _event_revision_from_row(row: sqlite3.Row) -> CognitiveEventRevision:
    return CognitiveEventRevision(
        id=row["id"],
        event_id=row["event_id"],
        tenant_id=row["tenant_id"],
        revision=row["revision"],
        status=row["status"],
        delivery_attempts=row["delivery_attempts"],
        last_delivery_us=row["last_delivery_us"],
        delivered_lease_id=row["delivered_lease_id"],
        delivered_lease_epoch=row["delivered_lease_epoch"],
        ack_id=row["ack_id"],
        acknowledged_us=row["acknowledged_us"],
        reason_code=row["reason_code"],
        created_us=row["created_us"],
        created_by=row["created_by"],
    )


class CognitiveEventRepository:
    """CognitiveEvent current rows plus the full delivery revision history."""

    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def get(self, event_id: str) -> CognitiveEventCurrent:
        row = _require(
            self._connection,
            "SELECT * FROM cognitive_events WHERE id = ?",
            (event_id,),
            "cognitive event",
        )
        return _event_current_from_row(row)

    def get_revision(self, revision_id: str) -> CognitiveEventRevision:
        row = _require(
            self._connection,
            "SELECT * FROM cognitive_event_revisions WHERE id = ?",
            (revision_id,),
            f"cognitive event revision {revision_id}",
        )
        return _event_revision_from_row(row)

    def current_revision_row(self, event_id: str) -> CognitiveEventRevision:
        row = _one(
            self._connection,
            "SELECT v.* FROM cognitive_events e JOIN cognitive_event_revisions v "
            "ON v.id = e.current_revision_id WHERE e.id = ?",
            (event_id,),
        )
        if row is None:
            raise NotFoundError(f"cognitive event {event_id} has no current revision")
        return _event_revision_from_row(row)

    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        kind: str,
        object_type: str,
        object_id: str,
        occurrence_id: str | None,
        scheduled_at_us: int,
        deliver_after_us: int,
        expires_us: int | None,
        delivery_target: str | None,
        summary_of_count: int = 0,
    ) -> str:
        event_id = str(self._ids.new())
        now_us = self._clock.now_us()
        try:
            self._connection.execute(
                "INSERT INTO cognitive_events (id, tenant_id, agent_id, space_group_id, "
                "space_id, session_id, scope_key, kind, object_type, object_id, occurrence_id, "
                "scheduled_at_us, deliver_after_us, expires_us, status, delivery_target, "
                "delivery_attempts, last_delivery_us, delivered_lease_id, delivered_lease_epoch, "
                "ack_id, acknowledged_us, summary_of_count, current_revision, "
                "current_revision_id, created_us, updated_us) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,0,NULL,NULL,NULL,NULL,NULL,?,1,'',?,?)",
                (
                    event_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    scope_key,
                    kind,
                    object_type,
                    object_id,
                    occurrence_id,
                    scheduled_at_us,
                    deliver_after_us,
                    expires_us,
                    EventStatus.PENDING.value,
                    delivery_target,
                    summary_of_count,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"cognitive event insert rejected: {error}") from error
        return event_id

    def insert_revision(
        self,
        *,
        event_id: str,
        tenant_id: str,
        revision: int,
        status: str,
        delivery_attempts: int,
        last_delivery_us: int | None,
        delivered_lease_id: str | None,
        delivered_lease_epoch: int | None,
        ack_id: str | None,
        acknowledged_us: int | None,
        reason_code: str | None,
        created_by: str,
    ) -> str:
        revision_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO cognitive_event_revisions (id, event_id, tenant_id, revision, status, "
            "delivery_attempts, last_delivery_us, delivered_lease_id, delivered_lease_epoch, "
            "ack_id, acknowledged_us, reason_code, created_us, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                event_id,
                tenant_id,
                revision,
                status,
                delivery_attempts,
                last_delivery_us,
                delivered_lease_id,
                delivered_lease_epoch,
                ack_id,
                acknowledged_us,
                reason_code,
                self._clock.now_us(),
                created_by,
            ),
        )
        return revision_id

    def set_initial_pointer(self, event_id: str, revision_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE cognitive_events SET current_revision_id = ?, updated_us = ? "
            "WHERE id = ? AND current_revision_id = ''",
            (revision_id, self._clock.now_us(), event_id),
        )
        return cursor.rowcount

    def advance_pointer(
        self,
        event_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        delivery_attempts: int | None = None,
        last_delivery_us: int | None = None,
        last_delivery_set: bool = False,
        delivered_lease_id: str | None = None,
        delivered_lease_epoch: int | None = None,
        lease_set: bool = False,
        ack_id: str | None = None,
        ack_id_set: bool = False,
        acknowledged_us: int | None = None,
        acknowledged_set: bool = False,
    ) -> int:
        """CAS the delivery state; a lost race is revision_mismatch."""
        assignments = [
            "current_revision = ?",
            "current_revision_id = ?",
            "status = ?",
            "updated_us = ?",
        ]
        params: list[Any] = [revision, revision_id, status, self._clock.now_us()]
        if delivery_attempts is not None:
            assignments.append("delivery_attempts = ?")
            params.append(delivery_attempts)
        if last_delivery_set:
            assignments.append("last_delivery_us = ?")
            params.append(last_delivery_us)
        if lease_set:
            assignments.append("delivered_lease_id = ?")
            assignments.append("delivered_lease_epoch = ?")
            params.append(delivered_lease_id)
            params.append(delivered_lease_epoch)
        if ack_id_set:
            assignments.append("ack_id = ?")
            params.append(ack_id)
        if acknowledged_set:
            assignments.append("acknowledged_us = ?")
            params.append(acknowledged_us)
        cursor = self._connection.execute(
            f"UPDATE cognitive_events SET {', '.join(assignments)} WHERE id = ? "
            "AND current_revision = ?",
            (*params, event_id, expected_revision),
        )
        return cursor.rowcount

    def raise_pointer_mismatch(self, event_id: str, expected: int) -> None:
        row = self.get(event_id)
        raise RevisionMismatchError("cognitive_event", event_id, expected, row.current_revision)

    def pullable_events(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        now_us: int,
        limit: int = 50,
        allowed_space_ids: Collection[str] | None = None,
    ) -> list[CognitiveEventCurrent]:
        """Agent-visible pullable events: due, pending, not expired.

        The space envelope is applied IN SQL: the batch limit must bound
        only events the pulling app could actually hold, otherwise a wall
        of envelope-invisible events permanently starves the visible tail.
        """
        sql = (
            "SELECT * FROM cognitive_events WHERE tenant_id = ? AND agent_id = ? "
            "AND status = 'pending' AND deliver_after_us <= ? "
            "AND (expires_us IS NULL OR expires_us > ?) "
        )
        params: list[object] = [tenant_id, agent_id, now_us, now_us]
        sql, params = _with_space_envelope(sql, params, allowed_space_ids)
        sql, params = _with_event_logical_validity(sql, params, now_us)
        rows = self._connection.execute(
            sql + "ORDER BY scheduled_at_us, id LIMIT ?", (*params, limit)
        ).fetchall()
        return [_event_current_from_row(row) for row in rows]

    def pending_events(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: Sequence[str] = ("pending", "delivered"),
        limit: int = 200,
        allowed_space_ids: Collection[str] | None = None,
        now_us: int | None = None,
        request_scope: tuple[str | None, str | None, str | None] | None = None,
    ) -> list[CognitiveEventCurrent]:
        """Events in the given statuses, visible to the caller's gates.

        ``allowed_space_ids`` applies the ACCESS ENVELOPE (listing
        semantics); ``request_scope`` — a ``(space_group, space, session)``
        tuple — applies the formal downward-visibility match of one concrete
        request (recall semantics). The two are mutually exclusive gates:
        envelope mode leaves ``request_scope`` unset, a concrete request
        needs no envelope because its space was authorized against the
        envelope up front. Logical validity (pending horizon, tombstones)
        applies in both modes. Everything runs in SQL before ORDER/LIMIT,
        so no application-layer drop can starve the batch.
        """
        if not statuses:
            return []
        sql = (
            "SELECT * FROM cognitive_events WHERE tenant_id = ? AND agent_id = ? "
            f"AND status IN ({', '.join('?' * len(statuses))}) "
        )
        params: list[object] = [tenant_id, agent_id, *statuses]
        sql, params = _with_space_envelope(sql, params, allowed_space_ids)
        if request_scope is not None:
            sql, params = _with_request_scope(sql, params, *request_scope)
        sql, params = _with_event_logical_validity(sql, params, now_us)
        rows = self._connection.execute(
            sql + "ORDER BY scheduled_at_us, id LIMIT ?", (*params, limit)
        ).fetchall()
        return [_event_current_from_row(row) for row in rows]

    def expiry_candidates(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        now_us: int,
        limit: int = 500,
        max_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
    ) -> list[CognitiveEventCurrent]:
        """Events that are past their horizon or out of delivery attempts."""
        rows = self._connection.execute(
            "SELECT * FROM cognitive_events WHERE tenant_id = ? AND agent_id = ? "
            "AND status IN ('pending', 'delivered') "
            "AND ((expires_us IS NOT NULL AND expires_us <= ?) OR delivery_attempts >= ?) "
            "ORDER BY scheduled_at_us, id LIMIT ?",
            (tenant_id, agent_id, now_us, max_attempts, limit),
        ).fetchall()
        return [_event_current_from_row(row) for row in rows]

    def fence_candidates(
        self, tenant_id: str, agent_id: str, *, limit: int = 500
    ) -> list[CognitiveEventCurrent]:
        """Delivered-but-unacknowledged events currently holding a lease.

        These are the fence-check candidates: their horizon may still be
        open, so expiry candidacy alone must never gate their requeue.
        """
        rows = self._connection.execute(
            "SELECT * FROM cognitive_events WHERE tenant_id = ? AND agent_id = ? "
            "AND status = 'delivered' AND acknowledged_us IS NULL "
            "AND delivered_lease_id IS NOT NULL "
            "ORDER BY last_delivery_us, id LIMIT ?",
            (tenant_id, agent_id, limit),
        ).fetchall()
        return [_event_current_from_row(row) for row in rows]

    def history(self, event_id: str, *, limit: int = 100) -> list[CognitiveEventRevision]:
        rows = self._connection.execute(
            "SELECT * FROM cognitive_event_revisions WHERE event_id = ? "
            "ORDER BY revision DESC LIMIT ?",
            (event_id, limit),
        ).fetchall()
        return [_event_revision_from_row(row) for row in rows]


__all__ = [
    "CognitiveEventRepository",
    "NoteRepository",
    "TaskRepository",
]
