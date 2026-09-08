"""Phase 2 repositories: observations, cursors, outbox, schedules, leases.

Every method runs inside the caller's short transaction. Worker completion,
retry and dead-letter transitions are compare-and-set on owner, generation,
expiry and source revision — the database itself enforces fencing (§16.3).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import (
    ConflictError,
    IdempotencyKeyReusedError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.jobs import (
    MAX_PAYLOAD_BYTES,
    UNSETTLED_STATUSES,
    JobLane,
    NewOutboxJob,
    OutboxJob,
)
from iris_memory_core.domain.observation import (
    ArtifactRef,
    EffectState,
    GapPolicy,
    ObservationDraft,
    ObservationRole,
    StoredObservation,
)
from iris_memory_core.domain.schedule import (
    CatchUpPolicy,
    ScheduleRecord,
    TickRecord,
)
from iris_memory_core.domain.surface import (
    LeaseView,
    SurfaceMode,
)

_UNSETTLED = ",".join(f"'{status}'" for status in UNSETTLED_STATUSES)
_REQUEUEABLE = "'pending','retryable'"


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


def _decode_json(value: str | None) -> dict[str, object] | None:
    if value is None:
        return None
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else None


class ObservationRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- reads -----------------------------------------------------------

    def get(self, observation_id: str) -> StoredObservation:
        row = _require(
            self._connection,
            "SELECT * FROM observations WHERE id = ?",
            (observation_id,),
            f"observation {observation_id}",
        )
        return self._to_record(row)

    def record_fingerprint(self, observation_id: str) -> str:
        row = _require(
            self._connection,
            "SELECT record_fingerprint FROM observations WHERE id = ?",
            (observation_id,),
            f"observation {observation_id}",
        )
        return str(row["record_fingerprint"])

    def find_by_idempotency_key(
        self, tenant_id: str, agent_id: str, idempotency_key: str
    ) -> StoredObservation | None:
        row = _one(
            self._connection,
            "SELECT * FROM observations WHERE tenant_id = ? AND agent_id = ? "
            "AND idempotency_key = ?",
            (tenant_id, agent_id, idempotency_key),
        )
        return self._to_record(row) if row is not None else None

    def find_by_cursor(
        self, tenant_id: str, agent_id: str, source_stream: str, cursor: int
    ) -> StoredObservation | None:
        row = _one(
            self._connection,
            "SELECT * FROM observations WHERE tenant_id = ? AND agent_id = ? "
            "AND source_stream = ? AND source_cursor = ?",
            (tenant_id, agent_id, source_stream, cursor),
        )
        return self._to_record(row) if row is not None else None

    def find_by_occurrence(
        self, tenant_id: str, agent_id: str, occurrence_id: str
    ) -> StoredObservation | None:
        row = _one(
            self._connection,
            "SELECT * FROM observations WHERE tenant_id = ? AND agent_id = ? AND occurrence_id = ?",
            (tenant_id, agent_id, occurrence_id),
        )
        return self._to_record(row) if row is not None else None

    def find_by_source_event(
        self, tenant_id: str, agent_id: str, source_event_id: str
    ) -> StoredObservation | None:
        row = _one(
            self._connection,
            "SELECT * FROM observations WHERE tenant_id = ? AND agent_id = ? "
            "AND source_event_id = ?",
            (tenant_id, agent_id, source_event_id),
        )
        return self._to_record(row) if row is not None else None

    def for_trigger_scan(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        want_kind: str | None,
        want_role: str | None,
        after_us: int | None,
        limit: int,
        trigger_id: str | None = None,
        trigger_revision: int = 0,
    ) -> list[StoredObservation]:
        """Committed observations matching an observation_kind trigger.

        Ordered by committed time so the scan is deterministic; tombstones are
        re-checked by the caller (defense in depth). When ``trigger_id`` is
        given, observations that already have a ledger row under that trigger
        revision are excluded IN SQL — the batch limit must bound NEW work,
        not re-reads, or a large backlog never converges past one batch.
        """
        clauses = ["tenant_id = ?", "agent_id = ?", "effect_state = 'committed'"]
        params: list[Any] = [tenant_id, agent_id]
        if want_kind is not None:
            clauses.append("kind = ?")
            params.append(want_kind)
        if want_role is not None:
            clauses.append("role = ?")
            params.append(want_role)
        if after_us is not None:
            clauses.append("committed_us > ?")
            params.append(after_us)
        if trigger_id is not None:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM task_trigger_occurrences occ "
                "WHERE occ.trigger_id = ? AND occ.trigger_revision = ? "
                "AND occ.occurrence_key = ? || observations.id)"
            )
            params.extend((trigger_id, trigger_revision, f"o:{trigger_id}:{trigger_revision}:"))
        rows = self._connection.execute(
            f"SELECT * FROM observations WHERE {' AND '.join(clauses)} "
            "ORDER BY committed_us, id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._to_record(row) for row in rows]

    # -- writes ----------------------------------------------------------

    def insert(self, draft: ObservationDraft, fingerprint: str) -> StoredObservation:
        observation_id = str(self._ids.new())
        created_us = self._clock.now_us()
        columns = (
            "id, tenant_id, agent_id, space_group_id, space_id, session_id, app_instance_id, "
            "source_stream, source_cursor, source_event_id, occurrence_id, idempotency_key, "
            "record_fingerprint, actor_external_identity_id, actor_entity_id_at_ingest, role, "
            "kind, content, structured_payload, artifact_refs, privacy_labels, effect_state, "
            "effect_proof, occurred_us, committed_us, schema_version, revision, created_us"
        )
        values = (
            observation_id,
            draft.tenant_id,
            draft.agent_id,
            draft.space_group_id,
            draft.space_id,
            draft.session_id,
            draft.app_instance_id,
            draft.source_stream,
            draft.source_cursor,
            draft.source_event_id,
            draft.occurrence_id,
            draft.idempotency_key,
            fingerprint,
            draft.actor_external_identity_id,
            draft.actor_entity_id_at_ingest,
            draft.role.value,
            draft.kind,
            draft.content,
            canonical_json(draft.structured_payload)
            if draft.structured_payload is not None
            else None,
            json.dumps(
                [{"artifact_id": ref.artifact_id, "kind": ref.kind} for ref in draft.artifact_refs]
            ),
            json.dumps(list(draft.privacy_labels)),
            draft.effect_state.value,
            canonical_json(draft.effect_proof) if draft.effect_proof else None,
            draft.occurred_us,
            draft.committed_us,
            1,
            1,
            created_us,
        )
        assert len(columns.split(",")) == len(values), "observation insert arity"
        placeholders = ",".join("?" for _ in values)
        try:
            self._connection.execute(
                f"INSERT INTO observations ({columns}) VALUES ({placeholders})", values
            )
        except sqlite3.IntegrityError as error:
            # The service pre-checks duplicates inside the same transaction, so
            # reaching here means an unexpected constraint race — surface it,
            # never silently merge.
            raise ConflictError(f"observation insert rejected: {error}") from error
        return self.get(observation_id)

    # -- cursors ---------------------------------------------------------

    def cursor_state(
        self, tenant_id: str, agent_id: str, source_stream: str
    ) -> tuple[int | None, GapPolicy]:
        row = _one(
            self._connection,
            "SELECT cursor_position, gap_policy FROM source_cursors "
            "WHERE tenant_id = ? AND agent_id = ? AND source_stream = ?",
            (tenant_id, agent_id, source_stream),
        )
        if row is None:
            return None, GapPolicy.REJECT
        return int(row["cursor_position"]), GapPolicy(row["gap_policy"])

    def advance_cursor(
        self,
        tenant_id: str,
        agent_id: str,
        source_stream: str,
        position: int,
        gap_policy: GapPolicy,
    ) -> None:
        """Insert or move the stream cursor forward; never backwards (§8.3)."""
        now_us = self._clock.now_us()
        cursor = self._connection.execute(
            "INSERT INTO source_cursors (tenant_id, agent_id, source_stream, cursor_position, "
            "gap_policy, updated_us) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (tenant_id, agent_id, source_stream) DO UPDATE SET "
            "cursor_position = MAX(cursor_position, excluded.cursor_position), "
            "updated_us = excluded.updated_us "
            "WHERE excluded.cursor_position > source_cursors.cursor_position",
            (tenant_id, agent_id, source_stream, position, gap_policy.value, now_us),
        )
        _ = cursor

    def observations_by_actor_entity(
        self, tenant_id: str, entity_id: str, *, limit: int = 10_000
    ) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM observations WHERE tenant_id = ? "
            "AND actor_entity_id_at_ingest = ? LIMIT ?",
            (tenant_id, entity_id, limit),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def observations_for_session(
        self, tenant_id: str, space_id: str, session_id: str, *, limit: int = 10_000
    ) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM observations WHERE tenant_id = ? AND space_id = ? "
            "AND session_id = ? AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
            "WHERE _rt.tenant_id = observations.tenant_id "
            "AND _rt.resource_type = 'observation' AND _rt.resource_id = observations.id) "
            "LIMIT ?",
            (tenant_id, space_id, session_id, limit),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def observations_for_space(
        self, tenant_id: str, space_id: str, *, limit: int = 10_000
    ) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM observations WHERE tenant_id = ? AND space_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
            "WHERE _rt.tenant_id = observations.tenant_id "
            "AND _rt.resource_type = 'observation' AND _rt.resource_id = observations.id) "
            "LIMIT ?",
            (tenant_id, space_id, limit),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def scrub_content(self, observation_id: str) -> int:
        """Compliance erasure (§19.4, ADR-0013): destroy payload columns,
        keep the identity/timing/scope metadata the journal owes its audit."""
        cursor = self._connection.execute(
            "UPDATE observations SET content = NULL, structured_payload = NULL, "
            "effect_proof = NULL WHERE id = ?",
            (observation_id,),
        )
        return cursor.rowcount

    def _to_record(self, row: sqlite3.Row) -> StoredObservation:
        artifact_rows = json.loads(row["artifact_refs"]) if row["artifact_refs"] else []
        return StoredObservation(
            id=row["id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            app_instance_id=row["app_instance_id"],
            role=ObservationRole(row["role"]),
            kind=row["kind"],
            idempotency_key=row["idempotency_key"],
            effect_state=EffectState(row["effect_state"]),
            occurred_us=row["occurred_us"],
            committed_us=row["committed_us"],
            created_us=row["created_us"],
            revision=row["revision"],
            space_group_id=row["space_group_id"],
            space_id=row["space_id"],
            session_id=row["session_id"],
            source_stream=row["source_stream"],
            source_cursor=row["source_cursor"],
            source_event_id=row["source_event_id"],
            occurrence_id=row["occurrence_id"],
            actor_external_identity_id=row["actor_external_identity_id"],
            actor_entity_id_at_ingest=row["actor_entity_id_at_ingest"],
            content=row["content"],
            structured_payload=_decode_json(row["structured_payload"]),
            artifact_refs=tuple(
                ArtifactRef(item["artifact_id"], item["kind"]) for item in artifact_rows
            ),
            privacy_labels=tuple(json.loads(row["privacy_labels"])),
            effect_proof=_decode_json(row["effect_proof"]),
        )


class OutboxRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- enqueue ---------------------------------------------------------

    def enqueue(self, job: NewOutboxJob) -> tuple[OutboxJob, bool]:
        """Insert with stable dedupe; coalescable kinds merge pending rows.

        Returns (row, created). A dedupe key names ONE content and NEVER
        mutates its row: a retry whose canonical payload is byte-identical
        returns the existing row unchanged — queued, in-flight or settled,
        it is an absorbed duplicate (the leased job's completion already
        covers the effect, so no redundant follower) — while a DIFFERENT
        payload against an unsettled row is ``idempotency_key_reused``,
        unconditionally and regardless of coalescability. This ordering is
        what structurally closes cross-kind dedupe collisions: the merge
        below is reachable ONLY after a dedupe miss, through the coalesce
        lookup that matches the full (tenant, agent, kind, coalesce_key)
        tuple — so a row's payload can never be rewritten by a job of a
        foreign kind sharing its dedupe key. Revised content must ride a
        new dedupe key, which for coalescable kinds is exactly how the
        follower behind a leased row is created (§16.4). Leased rows are
        never touched.
        """
        payload_json = canonical_json(job.payload)
        if len(payload_json.encode()) > MAX_PAYLOAD_BYTES:
            raise ConflictError("outbox payload exceeds the byte ceiling")
        existing = self._by_dedupe_key(job.tenant_id, job.dedupe_key)
        if existing is not None:
            if existing.status not in ("completed", "dead") and (
                payload_json != canonical_json(existing.payload)
                or job.payload_version != existing.payload_version
            ):
                raise IdempotencyKeyReusedError(
                    "dedupe key already names different content",
                    details={"status": existing.status},
                )
            return existing, False
        return self._coalesce_or_insert(job, payload_json)

    def _coalesce_or_insert(self, job: NewOutboxJob, payload_json: str) -> tuple[OutboxJob, bool]:
        if job.coalesce_key is not None:
            row = _one(
                self._connection,
                "SELECT * FROM outbox_jobs WHERE tenant_id = ? AND agent_id IS ? "
                f"AND job_kind = ? AND coalesce_key = ? AND status IN ({_REQUEUEABLE})",
                (job.tenant_id, job.agent_id, job.job_kind, job.coalesce_key),
            )
            if row is not None:
                return self._merge_into(self._to_record(row), job, payload_json), False
        return self._insert(job, payload_json), True

    def _merge_into(self, existing: OutboxJob, job: NewOutboxJob, payload_json: str) -> OutboxJob:
        keep_revision = max(existing.source_revision, job.source_revision)
        payload = (
            payload_json
            if (job.payload_version, job.source_revision)
            >= (existing.payload_version, existing.source_revision)
            else (canonical_json(existing.payload))
        )
        cursor = self._connection.execute(
            "UPDATE outbox_jobs SET source_revision = ?, payload = ?, payload_version = ?, "
            "available_at_us = MIN(available_at_us, ?) WHERE id = ? AND status IN "
            f"({_REQUEUEABLE})",
            (
                keep_revision,
                payload,
                max(existing.payload_version, job.payload_version),
                job.available_at_us,
                existing.id,
            ),
        )
        if cursor.rowcount != 1:
            raise ConflictError("coalesce target changed inside the transaction")
        return self.get(existing.id)

    def _insert(self, job: NewOutboxJob, payload_json: str) -> OutboxJob:
        job_id = str(self._ids.new())
        columns = (
            "id, tenant_id, agent_id, job_kind, aggregate_type, aggregate_id, source_revision, "
            "payload, payload_version, dedupe_key, coalesce_key, priority, lane, status, "
            "available_at_us, attempt_count, max_attempts, created_us"
        )
        values = (
            job_id,
            job.tenant_id,
            job.agent_id,
            job.job_kind,
            job.aggregate_type,
            job.aggregate_id,
            job.source_revision,
            payload_json,
            job.payload_version,
            job.dedupe_key,
            job.coalesce_key,
            job.priority,
            job.lane.value,
            "pending",
            job.available_at_us or self._clock.now_us(),
            0,
            job.max_attempts,
            self._clock.now_us(),
        )
        assert len(columns.split(",")) == len(values), "outbox insert arity"
        placeholders = ",".join("?" for _ in values)
        try:
            self._connection.execute(
                f"INSERT INTO outbox_jobs ({columns}) VALUES ({placeholders})", values
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"outbox insert rejected: {error}") from error
        return self.get(job_id)

    def replay(self, original: OutboxJob, *, available_at_us: int) -> OutboxJob:
        """Dead-letter replay: a NEW outbox id referencing the original (§16.3).

        The payload is carried over BYTE-IDENTICAL (same dict, same
        payload_version): a replay must be the same replayable message the
        handler already knows, not a wrapper envelope — replay lineage lives
        in the ``replay_of`` column and the dedupe key, never in the payload
        a strict handler validates.
        """
        replay_number = 1 + self._count_replays(original.id)
        job = NewOutboxJob(
            tenant_id=original.tenant_id,
            job_kind=original.job_kind,
            aggregate_type=original.aggregate_type,
            aggregate_id=original.aggregate_id,
            source_revision=original.source_revision,
            payload=dict(original.payload),
            dedupe_key=f"replay:{original.id}:{replay_number}",
            payload_version=original.payload_version,
            agent_id=original.agent_id,
            coalesce_key=None,
            priority=original.priority,
            lane=JobLane(original.lane),
            available_at_us=available_at_us,
            max_attempts=original.max_attempts,
        )
        payload_json = canonical_json(job.payload)
        row = self._insert(job, payload_json)
        self._connection.execute(
            "UPDATE outbox_jobs SET replay_of = ? WHERE id = ?",
            (original.id, row.id),
        )
        return self.get(row.id)

    def _count_replays(self, original_id: str) -> int:
        row = _one(
            self._connection,
            "SELECT COUNT(*) AS c FROM outbox_jobs WHERE replay_of = ?",
            (original_id,),
        )
        return int(row["c"]) if row is not None else 0

    # -- claim / fencing -------------------------------------------------

    def release_expired_leases(self, now_us: int, *, requeue_delay_us: int) -> int:
        """Flip expired leases back to retryable so another worker may claim."""
        cursor = self._connection.execute(
            "UPDATE outbox_jobs SET status = 'retryable', lease_owner = NULL, "
            "lease_expires_us = NULL, available_at_us = ?, "
            "last_error_code = 'lease_expired' WHERE status = 'leased' "
            "AND lease_expires_us IS NOT NULL AND lease_expires_us <= ?",
            (now_us + requeue_delay_us, now_us),
        )
        return cursor.rowcount

    def claim(
        self,
        *,
        owner: str,
        now_us: int,
        lease_us: int,
        batch_size: int,
        enabled_kinds: tuple[str, ...] | frozenset[str],
        max_per_tenant: int,
        supported_payload_version: int = 1,
    ) -> tuple[OutboxJob, ...]:
        """Lease up to ``batch_size`` due jobs: safety lane first, then priority
        and availability, with a per-tenant fairness quota (§16.5). Unknown
        payload versions are never leased (fail closed, stay pending)."""
        if not enabled_kinds:
            return ()
        kinds = tuple(sorted(enabled_kinds))
        placeholders = ",".join("?" for _ in kinds)
        candidates = self._connection.execute(
            "SELECT * FROM outbox_jobs WHERE status IN ('pending', 'retryable') "
            f"AND available_at_us <= ? AND job_kind IN ({placeholders}) "
            "AND payload_version <= ? "
            "ORDER BY CASE lane WHEN 'safety' THEN 0 ELSE 1 END, priority, "
            "available_at_us, id LIMIT ?",
            (now_us, *kinds, supported_payload_version, batch_size * 8),
        ).fetchall()
        per_tenant: dict[str, int] = {}
        chosen: list[sqlite3.Row] = []
        for row in candidates:
            if len(chosen) >= batch_size:
                break
            tenant = str(row["tenant_id"])
            if per_tenant.get(tenant, 0) >= max_per_tenant:
                continue
            per_tenant[tenant] = per_tenant.get(tenant, 0) + 1
            chosen.append(row)
        claimed: list[OutboxJob] = []
        for row in chosen:
            cursor = self._connection.execute(
                "UPDATE outbox_jobs SET status = 'leased', lease_owner = ?, "
                "lease_generation = lease_generation + 1, lease_expires_us = ?, "
                "attempt_count = attempt_count + 1, last_heartbeat_us = ? WHERE id = ? "
                "AND status IN ('pending', 'retryable') AND available_at_us <= ?",
                (owner, now_us + lease_us, now_us, row["id"], now_us),
            )
            if cursor.rowcount == 1:
                claimed.append(self.get(str(row["id"])))
        return tuple(claimed)

    def heartbeat(
        self, job_id: str, *, owner: str, generation: int, now_us: int, extend_us: int
    ) -> int:
        cursor = self._connection.execute(
            "UPDATE outbox_jobs SET lease_expires_us = ?, last_heartbeat_us = ? WHERE id "
            "= ? AND lease_owner = ? "
            "AND lease_generation = ? AND status = 'leased' AND lease_expires_us > ?",
            (now_us + extend_us, now_us, job_id, owner, generation, now_us),
        )
        return cursor.rowcount

    def settle_unleased_kind(
        self,
        tenant_id: str,
        job_kind: str,
        *,
        reason_code: str,
        now_us: int,
        payload_version: int = 1,
    ) -> int:
        """Bulk-complete the tenant's UNLEASED jobs of one kind whose work a
        full projection rebuild provably covered (Phase 8, ADR-0016 §7 — the
        publish snapshot covers every committed change by construction, the
        same drain-by-construction the FTS trust gate documents). Leased
        rows are left to their workers; the fenced completion CAS still
        applies to them. Only jobs at or below ``payload_version`` settle:
        a rebuild compiled by THIS build cannot prove it covered the
        semantics of a future payload version, so those rows stay queued
        for a build that understands them (review round 3)."""
        cursor = self._connection.execute(
            "UPDATE outbox_jobs SET status = 'completed', completed_us = ?, "
            "last_error_code = ? WHERE tenant_id = ? AND job_kind = ? "
            "AND status IN ('pending', 'retryable') AND payload_version <= ?",
            (now_us, reason_code, tenant_id, job_kind, payload_version),
        )
        return cursor.rowcount

    def complete(
        self,
        job_id: str,
        *,
        owner: str,
        generation: int,
        now_us: int,
        source_revision: int,
    ) -> int:
        """Four-fold fencing CAS: owner, generation, live lease, source
        revision (§16.3). rowcount 0 means the worker lost its lease."""
        cursor = self._connection.execute(
            "UPDATE outbox_jobs SET status = 'completed', completed_us = ?, "
            "lease_owner = NULL, lease_expires_us = NULL WHERE id = ? AND lease_owner = ? "
            "AND lease_generation = ? AND lease_expires_us > ? AND source_revision = ? "
            "AND status = 'leased'",
            (now_us, job_id, owner, generation, now_us, source_revision),
        )
        return cursor.rowcount

    def mark_retryable(
        self,
        job_id: str,
        *,
        owner: str,
        generation: int,
        now_us: int,
        available_at_us: int,
        error_code: str,
        source_revision: int,
    ) -> int:
        """Four-fold fencing CAS (owner/generation/expiry/source revision):
        a worker whose lease or source revision moved on requeues nothing."""
        cursor = self._connection.execute(
            "UPDATE outbox_jobs SET status = 'retryable', lease_owner = NULL, "
            "lease_expires_us = NULL, available_at_us = ?, last_error_code = ? "
            "WHERE id = ? AND lease_owner = ? AND lease_generation = ? "
            "AND lease_expires_us > ? AND source_revision = ? AND status = 'leased'",
            (available_at_us, error_code, job_id, owner, generation, now_us, source_revision),
        )
        return cursor.rowcount

    def mark_dead(
        self,
        job_id: str,
        *,
        owner: str,
        generation: int,
        now_us: int,
        error_code: str,
        source_revision: int,
    ) -> int:
        cursor = self._connection.execute(
            "UPDATE outbox_jobs SET status = 'dead', lease_owner = NULL, "
            "lease_expires_us = NULL, last_error_code = ? WHERE id = ? AND lease_owner = ? "
            "AND lease_generation = ? AND lease_expires_us > ? AND source_revision = ? "
            "AND status = 'leased'",
            (error_code, job_id, owner, generation, now_us, source_revision),
        )
        return cursor.rowcount

    # -- reads -----------------------------------------------------------

    def get(self, job_id: str) -> OutboxJob:
        row = _require(
            self._connection, "SELECT * FROM outbox_jobs WHERE id = ?", (job_id,), "outbox job"
        )
        return self._to_record(row)

    def active_replay(self, original_id: str) -> OutboxJob | None:
        """A still-unsettled replay of a dead job, if any (replay idempotency)."""
        row = _one(
            self._connection,
            f"SELECT * FROM outbox_jobs WHERE replay_of = ? AND status IN ({_UNSETTLED}) "
            "ORDER BY created_us LIMIT 1",
            (original_id,),
        )
        return self._to_record(row) if row is not None else None

    def _by_dedupe_key(self, tenant_id: str, dedupe_key: str) -> OutboxJob | None:
        return self.by_dedupe_key(tenant_id, dedupe_key)

    def by_dedupe_key(self, tenant_id: str, dedupe_key: str) -> OutboxJob | None:
        """The row a dedupe key resolves to, if any (idempotency pre-check)."""
        row = _one(
            self._connection,
            "SELECT * FROM outbox_jobs WHERE tenant_id = ? AND dedupe_key = ?",
            (tenant_id, dedupe_key),
        )
        return self._to_record(row) if row is not None else None

    def by_coalesce_key(
        self, tenant_id: str, agent_id: str | None, job_kind: str, coalesce_key: str
    ) -> OutboxJob | None:
        """The requeueable row a coalescable enqueue would merge into, if any."""
        row = _one(
            self._connection,
            "SELECT * FROM outbox_jobs WHERE tenant_id = ? AND agent_id IS ? "
            f"AND job_kind = ? AND coalesce_key = ? AND status IN ({_REQUEUEABLE})",
            (tenant_id, agent_id, job_kind, coalesce_key),
        )
        return self._to_record(row) if row is not None else None

    def leased_count(self, owner: str) -> int:
        """Jobs currently leased by one owner (worker lease ceilings, §16.5)."""
        row = _one(
            self._connection,
            "SELECT COUNT(*) AS c FROM outbox_jobs WHERE lease_owner = ? AND status = 'leased'",
            (owner,),
        )
        return int(row["c"]) if row is not None else 0

    def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
        job_kind: str | None = None,
        limit: int = 100,
    ) -> tuple[OutboxJob, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if job_kind is not None:
            clauses.append("job_kind = ?")
            params.append(job_kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM outbox_jobs {where} ORDER BY created_us DESC, id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return tuple(self._to_record(row) for row in rows)

    def pressure(
        self,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        *,
        lane: str | None = None,
    ) -> dict[str, int]:
        """Unsettled job count and payload bytes for backpressure checks."""
        clauses = [f"status IN ({_UNSETTLED})"]
        params: list[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if lane is not None:
            clauses.append("lane = ?")
            params.append(lane)
        row = _one(
            self._connection,
            f"SELECT COUNT(*) AS jobs, COALESCE(SUM(LENGTH(payload)), 0) AS bytes "
            f"FROM outbox_jobs WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        assert row is not None  # aggregates always return one row
        return {"jobs": int(row["jobs"]), "bytes": int(row["bytes"])}

    def oldest_pending_us(self) -> int | None:
        row = _one(
            self._connection,
            f"SELECT MIN(available_at_us) AS oldest FROM outbox_jobs "
            f"WHERE status IN ({_UNSETTLED})",
            (),
        )
        return int(row["oldest"]) if row is not None and row["oldest"] is not None else None

    def unsettled_job_count(self, tenant_id: str, agent_id: str | None, job_kind: str) -> int:
        """Unsettled (queued/in-flight/retryable) jobs of one kind for one
        agent — the true consumption backlog, derived from queue state
        rather than from incrementally maintained watermark counters."""
        row = _one(
            self._connection,
            f"SELECT COUNT(*) AS c FROM outbox_jobs WHERE status IN ({_UNSETTLED}) "
            "AND tenant_id = ? AND agent_id = ? AND job_kind = ?",
            (tenant_id, agent_id, job_kind),
        )
        assert row is not None  # aggregates always return one row
        return int(row["c"])

    def unsettled_null_agent_job_count(self, tenant_id: str, job_kind: str) -> int:
        """Unsettled jobs of one kind with NO owning agent for a tenant.

        Ownerless projection jobs (the triggering event carried no agent and
        the resource was unresolvable) still represent unincorporated
        changes; freshness gates count them against EVERY requesting agent —
        fail-stale beats a false-fresh hole."""
        row = _one(
            self._connection,
            f"SELECT COUNT(*) AS c FROM outbox_jobs WHERE status IN ({_UNSETTLED}) "
            "AND tenant_id = ? AND agent_id IS NULL AND job_kind = ?",
            (tenant_id, job_kind),
        )
        assert row is not None  # aggregates always return one row
        return int(row["c"])

    def unsettled_tenant_job_count(self, tenant_id: str, job_kind: str) -> int:
        """Unsettled jobs of one kind for the WHOLE tenant (every owner,
        including ownerless rows). The verification gate uses this: a zero
        count means every committed change of that kind is incorporated, so
        any projection/canonical divergence is corruption, not lag."""
        row = _one(
            self._connection,
            f"SELECT COUNT(*) AS c FROM outbox_jobs WHERE status IN ({_UNSETTLED}) "
            "AND tenant_id = ? AND job_kind = ?",
            (tenant_id, job_kind),
        )
        assert row is not None  # aggregates always return one row
        return int(row["c"])

    def status_counts(self) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS c FROM outbox_jobs GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["c"]) for row in rows}

    def tick_completion(self, tick_id: str, *, now_us: int, error_code: str | None) -> None:
        """Mark a tick terminal after its outbox job settled (worker path)."""
        status = "completed" if error_code is None else "failed"
        self._connection.execute(
            "UPDATE schedule_ticks SET status = ?, completed_us = ?, reason_code = ? WHERE id = ?",
            (status, now_us, error_code, tick_id),
        )

    def _to_record(self, row: sqlite3.Row) -> OutboxJob:
        payload = _decode_json(row["payload"]) or {}
        return OutboxJob(
            id=row["id"],
            tenant_id=row["tenant_id"],
            job_kind=row["job_kind"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            source_revision=row["source_revision"],
            payload=payload,
            payload_version=row["payload_version"],
            dedupe_key=row["dedupe_key"],
            coalesce_key=row["coalesce_key"],
            priority=row["priority"],
            lane=row["lane"],
            status=row["status"],
            available_at_us=row["available_at_us"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            lease_owner=row["lease_owner"],
            lease_generation=row["lease_generation"],
            lease_expires_us=row["lease_expires_us"],
            last_error_code=row["last_error_code"],
            replay_of=row["replay_of"],
            created_us=row["created_us"],
            completed_us=row["completed_us"],
            agent_id=row["agent_id"],
        )


class ScheduleRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def insert(self, record: ScheduleRecord) -> ScheduleRecord:
        schedule_id = record.id or str(self._ids.new())
        now_us = self._clock.now_us()
        columns = (
            "id, tenant_id, agent_id, job_kind, schedule_spec, timezone, catch_up_policy, "
            "misfire_grace_us, max_ticks_per_run, enabled, next_tick_at_us, policy_version, "
            "revision, created_us, updated_us"
        )
        values = (
            schedule_id,
            record.tenant_id,
            record.agent_id,
            record.job_kind,
            canonical_json(record.spec),
            record.timezone,
            record.catch_up_policy.value,
            record.misfire_grace_us,
            record.max_ticks_per_run,
            1 if record.enabled else 0,
            record.next_tick_at_us,
            record.policy_version,
            1,
            now_us,
            now_us,
        )
        assert len(columns.split(",")) == len(values), "schedule insert arity"
        placeholders = ",".join("?" for _ in values)
        try:
            self._connection.execute(
                f"INSERT INTO schedules ({columns}) VALUES ({placeholders})", values
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"schedule insert rejected: {error}") from error
        return self.get(schedule_id)

    def get(self, schedule_id: str) -> ScheduleRecord:
        row = _require(
            self._connection, "SELECT * FROM schedules WHERE id = ?", (schedule_id,), "schedule"
        )
        return self._to_record(row)

    def due(self, now_us: int, *, limit: int = 100) -> tuple[ScheduleRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_tick_at_us <= ? "
            "ORDER BY next_tick_at_us LIMIT ?",
            (now_us, limit),
        ).fetchall()
        return tuple(self._to_record(row) for row in rows)

    def due_lag_by_kind(self, now_us: int) -> dict[str, int]:
        """Scheduler lag per job kind: now minus the earliest due marker (§31)."""
        rows = self._connection.execute(
            "SELECT job_kind, MIN(next_tick_at_us) AS oldest FROM schedules "
            "WHERE enabled = 1 AND next_tick_at_us <= ? GROUP BY job_kind",
            (now_us,),
        ).fetchall()
        return {
            str(row["job_kind"]): now_us - int(row["oldest"])
            for row in rows
            if row["oldest"] is not None
        }

    def advance(
        self,
        schedule_id: str,
        *,
        expected_revision: int,
        next_tick_at_us: int,
        last_tick_at_us: int | None,
    ) -> ScheduleRecord:
        """CAS the rolling next-tick marker; it never moves backwards (§17.3)."""
        now_us = self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE schedules SET next_tick_at_us = MAX(next_tick_at_us, ?), "
            "last_tick_at_us = ?, revision = revision + 1, updated_us = ? "
            "WHERE id = ? AND revision = ?",
            (next_tick_at_us, last_tick_at_us, now_us, schedule_id, expected_revision),
        )
        if cursor.rowcount != 1:
            current = self.get(schedule_id)
            raise RevisionMismatchError(
                "schedule", schedule_id, expected_revision, current.revision
            )
        return self.get(schedule_id)

    def set_enabled(
        self, schedule_id: str, *, enabled: bool, expected_revision: int
    ) -> ScheduleRecord:
        now_us = self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE schedules SET enabled = ?, revision = revision + 1, updated_us = ? "
            "WHERE id = ? AND revision = ?",
            (1 if enabled else 0, now_us, schedule_id, expected_revision),
        )
        if cursor.rowcount != 1:
            current = self.get(schedule_id)
            raise RevisionMismatchError(
                "schedule", schedule_id, expected_revision, current.revision
            )
        return self.get(schedule_id)

    # -- tick ledger -----------------------------------------------------

    def record_tick(
        self,
        *,
        schedule_id: str,
        scheduled_at_us: int,
        occurrence_key: str,
        status: str,
        observed_wall_us: int | None,
        observed_monotonic_delta_us: int | None,
        outbox_id: str | None,
        reason_code: str | None,
    ) -> TickRecord:
        """Idempotent tick insert: an existing occurrence key returns the
        recorded row so restarts and clock rollbacks cannot re-fire (§17.3)."""
        tick_id = str(self._ids.new())
        now_us = self._clock.now_us()
        columns = (
            "id, schedule_id, scheduled_at_us, occurrence_key, observed_wall_us, "
            "observed_monotonic_delta_us, status, outbox_id, reason_code, created_us"
        )
        values = (
            tick_id,
            schedule_id,
            scheduled_at_us,
            occurrence_key,
            observed_wall_us,
            observed_monotonic_delta_us,
            status,
            outbox_id,
            reason_code,
            now_us,
        )
        assert len(columns.split(",")) == len(values), "tick insert arity"
        placeholders = ",".join("?" for _ in values)
        try:
            self._connection.execute(
                f"INSERT INTO schedule_ticks ({columns}) VALUES ({placeholders})", values
            )
        except sqlite3.IntegrityError:
            return self.tick_by_occurrence(schedule_id, occurrence_key)
        return self.get_tick(tick_id)

    def get_tick(self, tick_id: str) -> TickRecord:
        row = _require(
            self._connection,
            "SELECT * FROM schedule_ticks WHERE id = ?",
            (tick_id,),
            "schedule tick",
        )
        return self._to_tick(row)

    def tick_by_occurrence(self, schedule_id: str, occurrence_key: str) -> TickRecord:
        row = _require(
            self._connection,
            "SELECT * FROM schedule_ticks WHERE schedule_id = ? AND occurrence_key = ?",
            (schedule_id, occurrence_key),
            f"tick {occurrence_key}",
        )
        return self._to_tick(row)

    def attach_outbox(self, tick_id: str, outbox_id: str) -> None:
        self._connection.execute(
            "UPDATE schedule_ticks SET outbox_id = ?, status = 'enqueued' WHERE id = ?",
            (outbox_id, tick_id),
        )

    def _to_record(self, row: sqlite3.Row) -> ScheduleRecord:
        return ScheduleRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            job_kind=row["job_kind"],
            spec=json.loads(row["schedule_spec"]),
            timezone=row["timezone"],
            catch_up_policy=CatchUpPolicy(row["catch_up_policy"]),
            misfire_grace_us=row["misfire_grace_us"],
            max_ticks_per_run=row["max_ticks_per_run"],
            enabled=bool(row["enabled"]),
            next_tick_at_us=row["next_tick_at_us"],
            last_tick_at_us=row["last_tick_at_us"],
            policy_version=row["policy_version"],
            revision=row["revision"],
            created_us=row["created_us"],
            updated_us=row["updated_us"],
        )

    def _to_tick(self, row: sqlite3.Row) -> TickRecord:
        return TickRecord(
            id=row["id"],
            schedule_id=row["schedule_id"],
            scheduled_at_us=row["scheduled_at_us"],
            occurrence_key=row["occurrence_key"],
            status=row["status"],
            created_us=row["created_us"],
            observed_wall_us=row["observed_wall_us"],
            observed_monotonic_delta_us=row["observed_monotonic_delta_us"],
            outbox_id=row["outbox_id"],
            started_us=row["started_us"],
            completed_us=row["completed_us"],
            reason_code=row["reason_code"],
        )


class SurfaceRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- mode / epoch state ----------------------------------------------

    def state(self, tenant_id: str, agent_id: str) -> tuple[str, int, int]:
        """(mode, current_epoch, revision); defaults without writing.

        Read-only: the default ('off', 0) is returned when the row is absent
        so read transactions (e.g. the required-mode online gate) never try
        to materialize rows. Write paths upsert explicitly.
        """
        row = _one(
            self._connection,
            "SELECT mode, current_epoch, revision FROM surface_lease_state "
            "WHERE tenant_id = ? AND agent_id = ?",
            (tenant_id, agent_id),
        )
        if row is not None:
            return str(row["mode"]), int(row["current_epoch"]), int(row["revision"])
        return SurfaceMode.OFF.value, 0, 1

    def set_mode(
        self, tenant_id: str, agent_id: str, mode: SurfaceMode, *, expected_revision: int
    ) -> int:
        cursor = self._connection.execute(
            "INSERT INTO surface_lease_state (tenant_id, agent_id, mode, updated_us) "
            "VALUES (?,?,?,?) ON CONFLICT (tenant_id, agent_id) DO UPDATE SET "
            "mode = excluded.mode, revision = revision + 1, updated_us = excluded.updated_us "
            "WHERE surface_lease_state.revision = ?",
            (tenant_id, agent_id, mode.value, self._clock.now_us(), expected_revision),
        )
        if cursor.rowcount != 1:
            raise RevisionMismatchError(
                "surface_state", agent_id, expected_revision, self.state(tenant_id, agent_id)[2]
            )
        return self.state(tenant_id, agent_id)[2]

    def next_epoch(self, tenant_id: str, agent_id: str) -> int:
        """Reserve the next monotonic epoch for this agent (§25.2)."""
        cursor = self._connection.execute(
            "INSERT INTO surface_lease_state (tenant_id, agent_id, current_epoch, updated_us) "
            "VALUES (?,?,1,?) ON CONFLICT (tenant_id, agent_id) DO UPDATE SET "
            "current_epoch = current_epoch + 1, revision = revision + 1, "
            "updated_us = excluded.updated_us RETURNING current_epoch",
            (tenant_id, agent_id, self._clock.now_us()),
        )
        row = cursor.fetchone()
        if row is None:
            raise ConflictError("surface lease state disappeared")
        return int(row[0])

    # -- leases ----------------------------------------------------------

    def insert_lease(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        holder_space_id: str | None,
        holder_app_instance_id: str,
        lease_epoch: int,
        priority: int,
        ttl_us: int,
    ) -> LeaseView:
        lease_id = str(self._ids.new())
        now_us = self._clock.now_us()
        columns = (
            "id, tenant_id, agent_id, holder_space_id, holder_app_instance_id, lease_epoch, "
            "priority, status, acquired_us, expires_us, last_heartbeat_us, revision, "
            "created_us, updated_us"
        )
        values = (
            lease_id,
            tenant_id,
            agent_id,
            holder_space_id,
            holder_app_instance_id,
            lease_epoch,
            priority,
            "active",
            now_us,
            now_us + ttl_us,
            now_us,
            1,
            now_us,
            now_us,
        )
        assert len(columns.split(",")) == len(values), "lease insert arity"
        placeholders = ",".join("?" for _ in values)
        try:
            self._connection.execute(
                f"INSERT INTO surface_leases ({columns}) VALUES ({placeholders})", values
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"lease insert rejected: {error}") from error
        return self.get_lease(lease_id)

    def get_lease(self, lease_id: str) -> LeaseView:
        row = _require(
            self._connection, "SELECT * FROM surface_leases WHERE id = ?", (lease_id,), "lease"
        )
        return self._to_view(row)

    def active_lease(self, tenant_id: str, agent_id: str) -> LeaseView | None:
        row = _one(
            self._connection,
            "SELECT * FROM surface_leases WHERE tenant_id = ? AND agent_id = ? "
            "AND status = 'active'",
            (tenant_id, agent_id),
        )
        return self._to_view(row) if row is not None else None

    def expire_stale(self, tenant_id: str, agent_id: str, *, now_us: int) -> int:
        """Expire lapsed leases AND append their ``expired`` events (§25.2).

        The lease ledger is append-only history: an expiry swept by a later
        acquire must leave the same ``expired`` event row a background sweep
        would, so audit consumers never depend on WHO noticed the lapse.
        """
        rows = self._connection.execute(
            "SELECT * FROM surface_leases WHERE tenant_id = ? AND agent_id = ? "
            "AND status IN ('active', 'draining') AND expires_us <= ?",
            (tenant_id, agent_id, now_us),
        ).fetchall()
        if not rows:
            return 0
        expired = 0
        for row in rows:
            cursor = self._connection.execute(
                "UPDATE surface_leases SET status = 'expired', revision = revision + 1, "
                "updated_us = ? WHERE id = ? AND status IN ('active', 'draining') "
                "AND expires_us <= ?",
                (now_us, row["id"], now_us),
            )
            expired += cursor.rowcount
            if cursor.rowcount == 1:
                self.record_event(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    lease_id=row["id"],
                    lease_epoch=row["lease_epoch"],
                    event="expired",
                    actor="coordinator:expiry_sweep",
                    details={"expires_us": row["expires_us"], "status": row["status"]},
                )
        return expired

    def fence(
        self, lease_id: str, *, expected_epoch: int, expected_revision: int, now_us: int
    ) -> int:
        """Preempt step one: fence the old holder before any notice goes out."""
        return self._connection.execute(
            "UPDATE surface_leases SET status = 'draining', revision = revision + 1, "
            "updated_us = ? WHERE id = ? AND lease_epoch = ? AND revision = ? "
            "AND status = 'active'",
            (now_us, lease_id, expected_epoch, expected_revision),
        ).rowcount

    def heartbeat(
        self,
        lease_id: str,
        *,
        expected_epoch: int,
        expected_owner: str,
        now_us: int,
        ttl_us: int,
    ) -> int:
        return self._connection.execute(
            "UPDATE surface_leases SET last_heartbeat_us = ?, expires_us = ?, "
            "revision = revision + 1, updated_us = ? WHERE id = ? AND lease_epoch = ? "
            "AND holder_app_instance_id = ? AND status = 'active' AND expires_us > ?",
            (now_us, now_us + ttl_us, now_us, lease_id, expected_epoch, expected_owner, now_us),
        ).rowcount

    def release(
        self, lease_id: str, *, expected_epoch: int, expected_owner: str, now_us: int
    ) -> int:
        """Only a LIVE lease can be released: expired leases have already
        lapsed on their own and fenced (draining) holders must not re-write
        state — both go through ``_reject_stale`` error paths instead."""
        return self._connection.execute(
            "UPDATE surface_leases SET status = 'released', revision = revision + 1, "
            "updated_us = ? WHERE id = ? AND lease_epoch = ? AND holder_app_instance_id = ? "
            "AND status = 'active' AND expires_us > ?",
            (now_us, lease_id, expected_epoch, expected_owner, now_us),
        ).rowcount

    def record_event(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        lease_id: str,
        lease_epoch: int,
        event: str,
        actor: str,
        reason_code: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO surface_lease_events (id, tenant_id, agent_id, lease_id, lease_epoch, "
            "event, actor, reason_code, details, created_us) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(self._ids.new()),
                tenant_id,
                agent_id,
                lease_id,
                lease_epoch,
                event,
                actor,
                reason_code,
                canonical_json(details or {}),
                self._clock.now_us(),
            ),
        )

    def event_count(self, tenant_id: str, agent_id: str) -> int:
        row = _one(
            self._connection,
            "SELECT COUNT(*) AS c FROM surface_lease_events WHERE tenant_id = ? AND agent_id = ?",
            (tenant_id, agent_id),
        )
        return int(row["c"]) if row is not None else 0

    def lease_counts(self) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS c FROM surface_leases GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["c"]) for row in rows}

    def _to_view(self, row: sqlite3.Row) -> LeaseView:
        return LeaseView(
            lease_id=row["id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            holder_space_id=row["holder_space_id"],
            holder_app_instance_id=row["holder_app_instance_id"],
            lease_epoch=row["lease_epoch"],
            priority=row["priority"],
            status=row["status"],
            acquired_us=row["acquired_us"],
            expires_us=row["expires_us"],
            last_heartbeat_us=row["last_heartbeat_us"],
            revision=row["revision"],
        )


__all__ = [
    "ObservationRepository",
    "OutboxRepository",
    "ScheduleRepository",
    "SurfaceRepository",
]
