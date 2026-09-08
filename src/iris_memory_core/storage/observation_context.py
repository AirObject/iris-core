"""Observation-only context scans and refs-only processing ledgers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.scope import Scope
from iris_memory_core.storage.cognitive import stored_observation_from_row


class ObservationContextRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._db, self._clock, self._ids = connection, clock, ids

    @staticmethod
    def _scope(scope: Scope) -> tuple[object, ...]:
        return (
            scope.tenant_id,
            scope.agent_id,
            scope.space_group_id,
            scope.space_id,
            scope.session_id,
        )

    def page(
        self,
        scope: Scope,
        *,
        watermark: int,
        start_us: int,
        end_us: int,
        after: tuple[int, int, str] | None,
        limit: int,
        pending_only: bool = False,
    ) -> tuple[StoredObservation, ...]:
        extra = " AND (o.occurred_us,o.committed_us,o.id)>(?,?,?)" if after else ""
        if pending_only:
            extra += """ AND o.effect_state='committed' AND NOT EXISTS (
                SELECT 1 FROM observation_context_members m
                JOIN observation_context_batches b ON b.id=m.batch_id
                JOIN outbox_jobs j ON j.id=b.job_id WHERE m.observation_id=o.id
                )"""
        rows = self._db.execute(
            """SELECT o.* FROM observations o
            JOIN agent_watermark_entries w ON w.tenant_id=o.tenant_id AND w.agent_id=o.agent_id
            AND w.aggregate_type='observation' AND w.aggregate_id=o.id
            AND w.aggregate_revision=o.revision
            WHERE o.tenant_id=? AND o.agent_id=? AND o.space_group_id IS ?
            AND o.space_id IS ? AND o.session_id IS ? AND w.seq<=?
            AND o.occurred_us>=? AND o.occurred_us<?
            AND NOT EXISTS (SELECT 1 FROM resource_tombstones t
                WHERE t.tenant_id=o.tenant_id AND t.resource_type='observation' AND
                t.resource_id=o.id)
            """
            + extra
            + " ORDER BY o.occurred_us,o.committed_us,o.id LIMIT ?",
            (*self._scope(scope), watermark, start_us, end_us, *(after or ()), limit),
        ).fetchall()
        return tuple(stored_observation_from_row(row) for row in rows)

    def targets(self, *, limit: int) -> tuple[Scope, ...]:
        rows = self._db.execute(
            """SELECT o.tenant_id,o.agent_id,o.space_group_id,o.space_id,o.session_id,
            MIN(o.created_us) AS oldest FROM observations o
            WHERE o.space_id IS NOT NULL AND o.effect_state='committed'
            AND NOT EXISTS (SELECT 1 FROM resource_tombstones t WHERE t.tenant_id=o.tenant_id
                AND t.resource_type='observation' AND t.resource_id=o.id)
            AND NOT EXISTS (SELECT 1 FROM observation_context_members m
                JOIN observation_context_batches b ON b.id=m.batch_id
                JOIN outbox_jobs j ON j.id=b.job_id WHERE m.observation_id=o.id
                )
            GROUP BY o.tenant_id,o.agent_id,o.space_group_id,o.space_id,o.session_id
            ORDER BY COALESCE((SELECT scanned_us FROM observation_context_scans c
                WHERE c.scope_key=json_object('agent_id',o.agent_id,'session_id',o.session_id,
                    'space_group_id',o.space_group_id,'space_id',o.space_id,'tenant_id',o.tenant_id)),0),
                oldest,o.tenant_id,o.agent_id,o.space_id LIMIT ?""",
            (limit,),
        ).fetchall()
        return tuple(
            Scope(
                *(
                    row[key]
                    for key in ("tenant_id", "agent_id", "space_group_id", "space_id", "session_id")
                )
            )
            for row in rows
        )

    def mark_scanned(self, scope: Scope) -> None:
        self._db.execute(
            "INSERT INTO observation_context_scans VALUES (?,?) "
            "ON CONFLICT(scope_key) DO UPDATE SET scanned_us=excluded.scanned_us",
            (canonical_json(scope.as_dict()), self._clock.now_us()),
        )

    def scan_position(self, admission_key: str) -> tuple[int, int, str] | None:
        row = self._db.execute(
            "SELECT position_json FROM observation_context_scan_positions WHERE admission_key=?",
            (admission_key,),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        return int(value[0]), int(value[1]), str(value[2])

    def advance_scan_position(
        self, admission_key: str, position: tuple[int, int, str] | None
    ) -> None:
        if position is None:
            self._db.execute(
                "DELETE FROM observation_context_scan_positions WHERE admission_key=?",
                (admission_key,),
            )
        else:
            self._db.execute(
                "INSERT INTO observation_context_scan_positions VALUES (?,?) "
                "ON CONFLICT(admission_key) DO UPDATE SET position_json=excluded.position_json",
                (admission_key, json.dumps(position)),
            )

    def insert_batch(
        self,
        batch_id: str,
        scope: Scope,
        *,
        job_id: str,
        watermark: int,
        observations: tuple[StoredObservation, ...],
    ) -> None:
        self._db.execute(
            """INSERT INTO observation_context_batches
            (id,tenant_id,agent_id,space_group_id,space_id,session_id,job_id,source_watermark,
             status,created_us) VALUES (?,?,?,?,?,?,?,?,'pending',?)""",
            (batch_id, *self._scope(scope), job_id, watermark, self._clock.now_us()),
        )
        for item in observations:
            row = self._db.execute(
                "SELECT record_fingerprint FROM observations WHERE id=?", (item.id,)
            ).fetchone()
            self._db.execute(
                "INSERT INTO observation_context_members VALUES (?,?,?,?)",
                (batch_id, item.id, item.revision, row[0]),
            )

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM observation_context_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["episode_ids"] = json.loads(value["episode_ids"])
        value["members"] = [
            dict(member)
            for member in self._db.execute(
                "SELECT * FROM observation_context_members WHERE batch_id=? "
                "ORDER BY observation_id",
                (batch_id,),
            )
        ]
        return value

    def complete(self, batch_id: str, episode_ids: list[str]) -> None:
        self._db.execute(
            """UPDATE observation_context_batches SET status='completed',
            episode_ids=?,completed_us=? WHERE id=? AND status='pending'""",
            (json.dumps(episode_ids), self._clock.now_us(), batch_id),
        )

    def summaries(self, scope: Scope, *, limit: int) -> tuple[str, ...]:
        rows = self._db.execute(
            """SELECT episode_ids FROM observation_context_batches
            WHERE tenant_id=? AND agent_id=? AND space_group_id IS ? AND space_id IS ?
            AND session_id IS ? AND status='completed' AND episode_ids!='[]'
            ORDER BY created_us DESC,id DESC LIMIT ?""",
            (*self._scope(scope), limit),
        ).fetchall()
        return tuple(x for row in rows for x in json.loads(row[0]))

    def processing(self, observation_id: str) -> str:
        rows = self._db.execute(
            """SELECT b.status,b.episode_ids,j.status AS job_status
            FROM observation_context_members m JOIN observation_context_batches b ON b.id=m.batch_id
            JOIN outbox_jobs j ON j.id=b.job_id WHERE m.observation_id=? ORDER BY b.created_us
            DESC""",
            (observation_id,),
        ).fetchall()
        for row in rows:
            if row["status"] == "completed":
                return "processed"
            if row["job_status"] in {"pending", "leased", "retryable"}:
                return "pending"
        return "failed" if rows else "unprocessed"

    def cursor(
        self, token: str, *, tenant_id: str, app_instance_id: str, query_hash: str
    ) -> dict[str, Any] | None:
        row = self._db.execute(
            """SELECT * FROM observation_context_cursors
            WHERE id=? AND tenant_id=? AND app_instance_id=? AND query_hash=? AND expires_us>?""",
            (token, tenant_id, app_instance_id, query_hash, self._clock.now_us()),
        ).fetchone()
        return dict(row) if row else None

    def save_cursor(
        self,
        *,
        tenant_id: str,
        app_instance_id: str,
        query_hash: str,
        watermark: int,
        position: tuple[int, int, str],
    ) -> str:
        token = str(self._ids.new())
        self._db.execute(
            "DELETE FROM observation_context_cursors WHERE id IN "
            "(SELECT id FROM observation_context_cursors WHERE expires_us<=? LIMIT 100)",
            (self._clock.now_us(),),
        )
        self._db.execute(
            "INSERT INTO observation_context_cursors VALUES (?,?,?,?,?,?,?)",
            (
                token,
                tenant_id,
                app_instance_id,
                query_hash,
                watermark,
                json.dumps(position),
                self._clock.now_us() + 900_000_000,
            ),
        )
        return token

    def expired_background(self, *, before_us: int, limit: int) -> tuple[StoredObservation, ...]:
        rows = self._db.execute(
            """SELECT o.* FROM observations o WHERE context_kind='background'
            AND created_us<? AND NOT EXISTS (SELECT 1 FROM resource_tombstones t
                WHERE t.tenant_id=o.tenant_id AND t.resource_type='observation' AND
                t.resource_id=o.id)
            AND NOT EXISTS (SELECT 1 FROM observation_context_dependencies l
                WHERE l.tenant_id=o.tenant_id
                AND l.observation_id=o.id
                AND NOT EXISTS (SELECT 1 FROM resource_tombstones t WHERE t.tenant_id=l.tenant_id
                    AND t.resource_type=l.target_type AND t.resource_id=l.target_id))
            AND NOT EXISTS (SELECT 1 FROM observation_context_members m
                JOIN observation_context_batches b ON b.id=m.batch_id
                JOIN outbox_jobs j ON j.id=b.job_id WHERE m.observation_id=o.id
                AND b.status='pending' AND j.status IN ('pending','leased','retryable'))
            AND NOT EXISTS (SELECT 1 FROM legal_holds h WHERE h.tenant_id=o.tenant_id
                AND h.released_us IS NULL AND (h.space_id IS NULL OR h.space_id=o.space_id)
                AND (h.session_id IS NULL OR h.session_id=o.session_id)
                AND (h.subject_entity_id IS NULL OR
                h.subject_entity_id=o.actor_entity_id_at_ingest))
            ORDER BY created_us,id LIMIT ?""",
            (before_us, limit),
        ).fetchall()
        return tuple(stored_observation_from_row(row) for row in rows)

    def has_live_dependency(self, tenant_id: str, observation_id: str) -> bool:
        # Any source link preserves evidence. Explicit Forget owns invalidation,
        # whereas routine expiry must never cascade into downstream memories.
        linked = self._db.execute(
            """SELECT 1 FROM observation_context_dependencies l
            WHERE l.tenant_id=? AND l.observation_id=?
            AND NOT EXISTS (SELECT 1 FROM resource_tombstones t WHERE t.tenant_id=l.tenant_id
                AND t.resource_type=l.target_type AND t.resource_id=l.target_id) LIMIT 1""",
            (tenant_id, observation_id),
        ).fetchone()
        pending = self._db.execute(
            """SELECT 1 FROM observation_context_members m
            JOIN observation_context_batches b ON b.id=m.batch_id
            JOIN outbox_jobs j ON j.id=b.job_id WHERE m.observation_id=? AND b.status='pending'
            AND j.status IN ('pending','leased','retryable') LIMIT 1""",
            (observation_id,),
        ).fetchone()
        return linked is not None or pending is not None
