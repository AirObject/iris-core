"""Phase 3 repositories: recent-context generations, state records, focus items.

Every method runs inside the caller's short transaction and returns DOMAIN
records — raw rows never cross into the application layer (ADR-0007).
Current pointers advance with compare-and-set on the expected revision
(ADR-0004): a lost race surfaces as ``revision_mismatch``, never as a silent
overwrite.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import (
    ConflictError,
    NotFoundError,
    NotReadyError,
    RevisionMismatchError,
)
from iris_memory_core.domain.focus import (
    FocusItemCurrent,
    FocusRevision,
)
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.recent import (
    BuiltProjection,
    ObservationRef,
    StoredGeneration,
    SummarySegment,
)
from iris_memory_core.domain.state import (
    ScopeRequirement,
    StateEntry,
    StateNamespacePolicy,
    StateRecord,
    StateRevision,
)


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


def stored_observation_from_row(row: sqlite3.Row) -> StoredObservation:
    """Rebuild a StoredObservation from a raw observations row."""
    from iris_memory_core.domain.observation import ArtifactRef, EffectState, ObservationRole

    artifacts = json.loads(row["artifact_refs"]) if row["artifact_refs"] else []
    payload = row["structured_payload"]
    decoded_payload = json.loads(payload) if payload else None
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
        structured_payload=decoded_payload if isinstance(decoded_payload, dict) else None,
        artifact_refs=tuple(ArtifactRef(item["artifact_id"], item["kind"]) for item in artifacts),
        privacy_labels=tuple(json.loads(row["privacy_labels"])),
        effect_proof=json.loads(row["effect_proof"]) if row["effect_proof"] else None,
    )


def _generation_from_row(row: sqlite3.Row) -> StoredGeneration:
    hot_raw = json.loads(row["hot_observation_refs"])
    segments_raw = json.loads(row["summary_segments"])
    projection = BuiltProjection(
        builder_version=int(row["builder_version"]),
        source_watermark=int(row["source_watermark"]),
        head_observation_id=row["head_observation_id"],
        tail_observation_id=row["tail_observation_id"],
        hot_observation_refs=tuple(
            ObservationRef(
                observation_id=item["observation_id"],
                revision=int(item["revision"]),
                occurred_us=int(item["occurred_us"]),
                token_estimate=int(item["token_estimate"]),
            )
            for item in hot_raw
        ),
        summary_segments=tuple(
            SummarySegment(
                segment_id=item["segment_id"],
                source_refs=tuple(
                    ObservationRef(
                        observation_id=ref["observation_id"],
                        revision=int(ref["revision"]),
                        occurred_us=int(ref["occurred_us"]),
                        token_estimate=int(ref["token_estimate"]),
                    )
                    for ref in item["source_refs"]
                ),
                token_estimate=int(item["token_estimate"]),
            )
            for item in segments_raw
        ),
        token_estimate=int(row["token_estimate"]),
        result_hash=row["result_hash"],
    )
    return StoredGeneration(
        generation_id=row["id"],
        target_key=row["target_key"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        projection=projection,
        status=row["status"],
        created_us=int(row["created_us"]),
        expires_us=row["expires_us"],
    )


def _state_record_from_row(row: sqlite3.Row) -> StateRecord:
    return StateRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=row["scope_key"],
        namespace=row["namespace"],
        key=row["key"],
        current_revision=int(row["current_revision"]),
        current_revision_id=row["current_revision_id"],
        created_us=int(row["created_us"]),
        updated_us=int(row["updated_us"]),
    )


def _state_revision_from_row(row: sqlite3.Row) -> StateRevision:
    return StateRevision(
        id=row["id"],
        record_id=row["record_id"],
        tenant_id=row["tenant_id"],
        revision=int(row["revision"]),
        value_json=row["value_json"],
        source_ref=row["source_ref"],
        source_authority=row["source_authority"],
        observed_us=int(row["observed_us"]),
        expires_us=row["expires_us"],
        coalesce_key=row["coalesce_key"],
        created_us=int(row["created_us"]),
    )


def _focus_current_from_row(row: sqlite3.Row) -> FocusItemCurrent:
    return FocusItemCurrent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=row["scope_key"],
        kind=row["kind"],
        status=row["status"],
        current_revision=int(row["current_revision"]),
        activation=float(row["activation"]),
        activation_base=float(row["activation_base"]),
        last_activated_us=int(row["last_activated_us"]),
        expires_us=row["expires_us"],
        created_us=int(row["created_us"]),
        updated_us=int(row["updated_us"]),
    )


def _focus_revision_from_row(row: sqlite3.Row) -> FocusRevision:
    payload = row["structured_payload"]
    decoded_payload = json.loads(payload) if payload else None
    return FocusRevision(
        id=row["id"],
        item_id=row["item_id"],
        tenant_id=row["tenant_id"],
        revision=int(row["revision"]),
        kind=row["kind"],
        summary=row["summary"],
        structured_payload=decoded_payload if isinstance(decoded_payload, dict) else None,
        privacy_labels=tuple(json.loads(row["privacy_labels"])),
        source_refs=tuple(json.loads(row["source_refs"])),
        salience=float(row["salience"]),
        activation=float(row["activation"]),
        activation_base=float(row["activation_base"]),
        importance=float(row["importance"]),
        status=row["status"],
        promotion_policy=row["promotion_policy"],
        promotion_target_type=row["promotion_target_type"],
        promotion_target_id=row["promotion_target_id"],
        last_activated_us=int(row["last_activated_us"]),
        expires_us=row["expires_us"],
        created_us=int(row["created_us"]),
        created_by=row["created_by"],
    )


class RecentContextRepository:
    """Immutable projection generations plus one current pointer per target."""

    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- canonical window reads (fallback + build source) -------------------

    def observation_window(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_id: str,
        session_id: str | None,
        limit: int,
    ) -> list[StoredObservation]:
        """Newest committed observations of exactly this target (§9.1, §5.2).

        A session window reads that session. A space window reads the space's
        SESSION-LESS observations: under the Scope Null rule a request with
        ``session_id = null`` may only read data whose session dimension is
        also null — session content is recovered through session windows, and
        the stricter default is the safe one. The WHERE clause pins
        tenant+agent+space equality in both cases, so an observation from
        another space can never enter the result — cross-space isolation is
        structural, not a post-filter.
        """
        clauses = [
            "tenant_id = ?",
            "agent_id = ?",
            "space_id = ?",
        ]
        params: list[Any] = [tenant_id, agent_id, space_id]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        else:
            clauses.append("session_id IS NULL")
        rows = self._connection.execute(
            f"SELECT * FROM observations WHERE {' AND '.join(clauses)} "
            "ORDER BY occurred_us DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [stored_observation_from_row(row) for row in reversed(rows)]

    # -- generations ----------------------------------------------------------

    def insert_generation(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str,
        session_id: str | None,
        target_key: str,
        projection: BuiltProjection,
        expires_us: int | None,
    ) -> str:
        generation_id = str(self._ids.new())
        hot = [
            {
                "observation_id": ref.observation_id,
                "revision": ref.revision,
                "occurred_us": ref.occurred_us,
                "token_estimate": ref.token_estimate,
            }
            for ref in projection.hot_observation_refs
        ]
        segments = [
            {
                "segment_id": segment.segment_id,
                "source_refs": [
                    {
                        "observation_id": ref.observation_id,
                        "revision": ref.revision,
                        "occurred_us": ref.occurred_us,
                        "token_estimate": ref.token_estimate,
                    }
                    for ref in segment.source_refs
                ],
                "token_estimate": segment.token_estimate,
            }
            for segment in projection.summary_segments
        ]
        try:
            self._connection.execute(
                "INSERT INTO recent_context_generations (id, tenant_id, agent_id, space_group_id, "
                "space_id, session_id, target_key, builder_version, source_watermark, "
                "head_observation_id, tail_observation_id, hot_observation_refs, summary_segments, "
                "token_estimate, result_hash, status, created_us, expires_us) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generation_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    target_key,
                    projection.builder_version,
                    projection.source_watermark,
                    projection.head_observation_id,
                    projection.tail_observation_id,
                    canonical_json(hot),
                    canonical_json(segments),
                    projection.token_estimate,
                    projection.result_hash,
                    "verified",
                    self._clock.now_us(),
                    expires_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"recent generation insert rejected: {error}") from error
        return generation_id

    def current(self, target_key: str) -> StoredGeneration | None:
        row = _one(
            self._connection,
            "SELECT g.* FROM recent_context_current c "
            "JOIN recent_context_generations g ON g.id = c.current_generation_id "
            "WHERE c.target_key = ?",
            (target_key,),
        )
        return _generation_from_row(row) if row is not None else None

    def swap_pointer(
        self,
        *,
        target_key: str,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str,
        session_id: str | None,
        current_generation_id: str,
    ) -> None:
        """Upsert the pointer; the generation insert and swap commit atomically."""
        now_us = self._clock.now_us()
        cursor = self._connection.execute(
            "INSERT INTO recent_context_current (target_key, tenant_id, agent_id, space_group_id, "
            "space_id, session_id, current_generation_id, updated_us) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (target_key) DO UPDATE SET "
            "current_generation_id = excluded.current_generation_id, "
            "updated_us = excluded.updated_us",
            (
                target_key,
                tenant_id,
                agent_id,
                space_group_id,
                space_id,
                session_id,
                current_generation_id,
                now_us,
            ),
        )
        _ = cursor

    def retire_pointer(self, target_key: str) -> bool:
        """Drop the pointer and retire whatever it referenced (invalidate)."""
        row = _one(
            self._connection,
            "SELECT current_generation_id FROM recent_context_current WHERE target_key = ?",
            (target_key,),
        )
        if row is None:
            return False
        self._connection.execute(
            "DELETE FROM recent_context_current WHERE target_key = ?", (target_key,)
        )
        self._connection.execute(
            "UPDATE recent_context_generations SET status = 'retired' WHERE id = ?",
            (row["current_generation_id"],),
        )
        return True

    def expire_stale(self, now_us: int) -> int:
        """Housekeeping: retire verified generations past their expiry."""
        stale = self._connection.execute(
            "SELECT id FROM recent_context_generations WHERE status = 'verified' "
            "AND expires_us IS NOT NULL AND expires_us <= ?",
            (now_us,),
        ).fetchall()
        for row in stale:
            self._connection.execute(
                "UPDATE recent_context_generations SET status = 'retired' WHERE id = ?",
                (row["id"],),
            )
        # Pointers that now reference retired generations are removed so reads
        # fall back to the canonical window instead of a stale generation.
        self._connection.execute(
            "DELETE FROM recent_context_current WHERE current_generation_id IN "
            "(SELECT id FROM recent_context_generations WHERE status = 'retired')"
        )
        return len(stale)


class StateRepository:
    """State namespace policies plus immutable revisions/current pointer."""

    def erase_content(self, record_id: str, *, now_us: int) -> None:
        deadline = time.monotonic() + 0.150
        steps = 0

        def stop() -> int:
            nonlocal steps
            steps += 1_000
            return int(steps > 2_000_000 or time.monotonic() > deadline)

        self._connection.set_progress_handler(stop, 1_000)
        try:
            self._connection.execute(
                "UPDATE state_record_revisions SET value_json='{}', source_ref=NULL, "
                "coalesce_key=NULL WHERE record_id=?",
                (record_id,),
            )
            self._connection.execute(
                "UPDATE state_records SET updated_us=? WHERE id=?",
                (now_us, record_id),
            )
        except sqlite3.OperationalError as error:
            if "interrupt" in str(error).lower():
                raise NotReadyError("State erasure exceeds its transaction budget") from None
            raise
        finally:
            self._connection.set_progress_handler(None, 0)

    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- namespace policies ------------------------------------------------

    def policy(self, tenant_id: str, namespace: str) -> StateNamespacePolicy | None:
        row = _one(
            self._connection,
            "SELECT * FROM state_namespace_policies WHERE tenant_id = ? AND namespace = ?",
            (tenant_id, namespace),
        )
        if row is None:
            return None
        return StateNamespacePolicy(
            namespace=str(row["namespace"]),
            default_ttl_us=int(row["default_ttl_us"]),
            max_ttl_us=int(row["max_ttl_us"]),
            retain_history=bool(row["retain_history"]),
            max_history_revisions=int(row["max_history_revisions"]),
            max_value_bytes=int(row["max_value_bytes"]),
            allowed_source_authorities=frozenset(json.loads(row["allowed_source_authorities"])),
            required_scope=ScopeRequirement(str(row["required_scope"])),
        )

    def upsert_policy(self, policy: StateNamespacePolicy, *, tenant_id: str) -> None:
        now_us = self._clock.now_us()
        cursor = self._connection.execute(
            "INSERT INTO state_namespace_policies (tenant_id, namespace, default_ttl_us, "
            "max_ttl_us, retain_history, max_history_revisions, max_value_bytes, "
            "allowed_source_authorities, required_scope, revision, created_us, updated_us) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?,?) "
            "ON CONFLICT (tenant_id, namespace) DO UPDATE SET "
            "default_ttl_us = excluded.default_ttl_us, max_ttl_us = excluded.max_ttl_us, "
            "retain_history = excluded.retain_history, "
            "max_history_revisions = excluded.max_history_revisions, "
            "max_value_bytes = excluded.max_value_bytes, "
            "allowed_source_authorities = excluded.allowed_source_authorities, "
            "required_scope = excluded.required_scope, revision = revision + 1, "
            "updated_us = excluded.updated_us",
            (
                tenant_id,
                policy.namespace,
                policy.default_ttl_us,
                policy.max_ttl_us,
                1 if policy.retain_history else 0,
                policy.max_history_revisions,
                policy.max_value_bytes,
                canonical_json(sorted(policy.allowed_source_authorities)),
                policy.required_scope.value,
                now_us,
                now_us,
            ),
        )
        _ = cursor

    # -- records -------------------------------------------------------------

    def find(self, scope_key: str, namespace: str, key: str) -> StateRecord | None:
        row = _one(
            self._connection,
            "SELECT * FROM state_records WHERE scope_key=? AND namespace=? AND key=? "
            "AND deleted_us IS NULL LIMIT 1",
            (scope_key, namespace, key),
        )
        if row is None:
            row = _one(
                self._connection,
                "SELECT * FROM state_records WHERE scope_key=? AND namespace=? AND key=? "
                "ORDER BY created_us DESC,id DESC LIMIT 1",
                (scope_key, namespace, key),
            )
        return _state_record_from_row(row) if row is not None else None

    def get(self, record_id: str) -> StateRecord:
        row = _require(
            self._connection,
            "SELECT * FROM state_records WHERE id = ?",
            (record_id,),
            f"state record {record_id}",
        )
        return _state_record_from_row(row)

    def current_revision(self, revision_id: str) -> StateRevision:
        row = _require(
            self._connection,
            "SELECT * FROM state_record_revisions WHERE id = ?",
            (revision_id,),
            f"state revision {revision_id}",
        )
        return _state_revision_from_row(row)

    def insert(
        self,
        *,
        scope_key: str,
        tenant_id: str,
        agent_id: str | None,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        namespace: str,
        key: str,
        revision_id: str,
        revision: int,
    ) -> str:
        record_id = str(self._ids.new())
        now_us = self._clock.now_us()
        try:
            self._connection.execute(
                "INSERT INTO state_records (id, tenant_id, agent_id, space_group_id, space_id, "
                "session_id, scope_key, namespace, key, current_revision, current_revision_id, "
                "created_us, updated_us) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    scope_key,
                    namespace,
                    key,
                    revision,
                    revision_id,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"state record insert rejected: {error}") from error
        return record_id

    def insert_revision(
        self,
        *,
        record_id: str,
        tenant_id: str,
        revision: int,
        value_json: str,
        source_ref: str | None,
        source_authority: str,
        observed_us: int,
        expires_us: int | None,
        coalesce_key: str | None,
    ) -> str:
        revision_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO state_record_revisions (id, record_id, tenant_id, revision, value_json, "
            "source_ref, source_authority, observed_us, expires_us, coalesce_key, created_us) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                record_id,
                tenant_id,
                revision,
                value_json,
                source_ref,
                source_authority,
                observed_us,
                expires_us,
                coalesce_key,
                self._clock.now_us(),
            ),
        )
        return revision_id

    def advance_pointer(
        self, record_id: str, *, expected_revision: int, revision: int, revision_id: str
    ) -> int:
        """CAS the current pointer: exactly one concurrent writer succeeds."""
        cursor = self._connection.execute(
            "UPDATE state_records SET current_revision = ?, current_revision_id = ?, "
            "updated_us = ? WHERE id = ? AND current_revision = ?",
            (revision, revision_id, self._clock.now_us(), record_id, expected_revision),
        )
        return cursor.rowcount

    def set_initial_pointer(self, record_id: str, revision_id: str) -> int:
        """Wire the freshly created record's pointer (empty → first revision)."""
        cursor = self._connection.execute(
            "UPDATE state_records SET current_revision_id = ?, updated_us = ? "
            "WHERE id = ? AND current_revision_id = ''",
            (revision_id, self._clock.now_us(), record_id),
        )
        return cursor.rowcount

    def revision_count(self, record_id: str) -> int:
        row = _one(
            self._connection,
            "SELECT COUNT(*) AS c FROM state_record_revisions WHERE record_id = ?",
            (record_id,),
        )
        return int(row["c"]) if row is not None else 0

    def prune_history(self, record_id: str, *, keep: int) -> int:
        """Policy-driven retention: keep the newest ``keep`` revisions.

        The current pointer target always survives, so ``keep`` is floored at
        1 — a keep of 0 ("current only") prunes every historical revision,
        never the current one.
        """
        keep = max(keep, 1)
        # Enforce the ceiling exactly in one statement. A capped batch would
        # leave a large key above policy after a tenant tightens retention.
        cursor = self._connection.execute(
            "DELETE FROM state_record_revisions WHERE record_id = ? "
            "AND id NOT IN ("
            "SELECT id FROM state_record_revisions WHERE record_id = ? "
            "ORDER BY revision DESC LIMIT ?"
            ") AND id != (SELECT current_revision_id FROM state_records WHERE id = ?)",
            (record_id, record_id, keep, record_id),
        )
        return cursor.rowcount

    def history(self, record_id: str, *, limit: int = 50) -> list[StateRevision]:
        rows = self._connection.execute(
            "SELECT * FROM state_record_revisions WHERE record_id = ? "
            "ORDER BY revision DESC LIMIT ?",
            (record_id, limit),
        ).fetchall()
        return [_state_revision_from_row(row) for row in rows]

    def list_scope(
        self,
        *,
        tenant_id: str,
        agent_id: str | None,
        namespace: str | None,
        space_id: str | None,
        session_id: str | None,
        prefix: str | None,
        limit: int = 100,
    ) -> list[StateEntry]:
        """Current records whose scope dims match the request (§5.2).

        A request-side null is NEVER a wildcard: an agent-level list reads
        only agent-level records, and a space-level list reads space-level
        plus agent-level records. Stored-null stays visible downward — a
        session request also sees the space-level and agent-level records
        above it.
        """
        clauses = ["r.tenant_id = ?", "r.deleted_us IS NULL"]
        params: list[Any] = [tenant_id]
        if agent_id is not None:
            clauses.append("r.agent_id = ?")
            params.append(agent_id)
        if namespace is not None:
            clauses.append("r.namespace = ?")
            params.append(namespace)
        if space_id is not None and session_id is not None:
            clauses.append("(r.space_id = ? OR r.space_id IS NULL)")
            params.append(space_id)
            # Stored-null is visible downward (§5.2): a session request sees
            # session-scoped records plus records written without a session.
            clauses.append("(r.session_id = ? OR r.session_id IS NULL)")
            params.append(session_id)
        elif space_id is not None:
            # Space-level request: request-side session-null must not match
            # session-scoped records.
            clauses.append("(r.space_id = ? OR r.space_id IS NULL) AND r.session_id IS NULL")
            params.append(space_id)
        else:
            # Agent-level request: only agent-level records are visible.
            clauses.append("r.space_id IS NULL AND r.session_id IS NULL")
        if prefix is not None:
            clauses.append("r.key LIKE ? ESCAPE '\\'")
            params.append(
                prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
            )
        rows = self._connection.execute(
            "SELECT r.*, v.value_json AS rev_value, v.expires_us AS rev_expires, "
            "v.observed_us AS rev_observed, v.source_authority AS rev_authority "
            "FROM state_records r JOIN state_record_revisions v ON v.id = r.current_revision_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY r.namespace, r.key LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [
            StateEntry(
                record=_state_record_from_row(row),
                value_json=row["rev_value"],
                source_authority=row["rev_authority"],
                observed_us=int(row["rev_observed"]),
                expires_us=row["rev_expires"],
            )
            for row in rows
        ]


FOCUS_NOT_TOMBSTONED = (
    "NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
    "WHERE _rt.tenant_id=focus_items.tenant_id AND _rt.resource_type='focus_item' "
    "AND _rt.resource_id=focus_items.id)"
)


class FocusRepository:
    """Focus current rows plus immutable revisions."""

    def erase_content(self, item_id: str, *, now_us: int) -> None:
        deadline = time.monotonic() + 0.150
        steps = 0

        def stop() -> int:
            nonlocal steps
            steps += 1_000
            return int(steps > 2_000_000 or time.monotonic() > deadline)

        self._connection.set_progress_handler(stop, 1_000)
        try:
            self._connection.execute(
                "UPDATE focus_item_revisions SET summary='<erased>', structured_payload=NULL, "
                "source_refs='[]', privacy_labels='[]', promotion_policy='' WHERE item_id=?",
                (item_id,),
            )
            self._connection.execute(
                "UPDATE focus_items SET summary='<erased>', updated_us=? WHERE id=?",
                (now_us, item_id),
            )
        except sqlite3.OperationalError as error:
            if "interrupt" in str(error).lower():
                raise NotReadyError("Focus erasure exceeds its transaction budget") from None
            raise
        finally:
            self._connection.set_progress_handler(None, 0)

    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def get(self, item_id: str) -> FocusItemCurrent:
        row = _require(
            self._connection, "SELECT * FROM focus_items WHERE id = ?", (item_id,), "focus item"
        )
        return _focus_current_from_row(row)

    def get_revision(self, revision_id: str) -> FocusRevision:
        row = _require(
            self._connection,
            "SELECT * FROM focus_item_revisions WHERE id = ?",
            (revision_id,),
            f"focus revision {revision_id}",
        )
        return _focus_revision_from_row(row)

    def current_revision_row(self, item_id: str) -> FocusRevision:
        row = _one(
            self._connection,
            "SELECT v.* FROM focus_items f JOIN focus_item_revisions v "
            "ON v.id = f.current_revision_id WHERE f.id = ?",
            (item_id,),
        )
        if row is None:
            raise NotFoundError(f"focus item {item_id} has no current revision")
        return _focus_revision_from_row(row)

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
        summary: str,
        status: str,
        revision_id: str,
        activation: float,
        activation_base: float,
        last_activated_us: int,
        expires_us: int | None,
    ) -> str:
        item_id = str(self._ids.new())
        now_us = self._clock.now_us()
        try:
            self._connection.execute(
                "INSERT INTO focus_items (id, tenant_id, agent_id, space_group_id, space_id, "
                "session_id, scope_key, kind, summary, status, current_revision, "
                "current_revision_id, activation, activation_base, last_activated_us, "
                "expires_us, created_us, updated_us) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    scope_key,
                    kind,
                    summary,
                    status,
                    1,
                    revision_id,
                    activation,
                    activation_base,
                    last_activated_us,
                    expires_us,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"focus item insert rejected: {error}") from error
        return item_id

    def insert_revision(
        self,
        *,
        item_id: str,
        tenant_id: str,
        revision: int,
        kind: str,
        summary: str,
        structured_payload: dict[str, object] | None,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        salience: float,
        activation: float,
        activation_base: float,
        importance: float,
        status: str,
        promotion_policy: str,
        promotion_target_type: str | None,
        promotion_target_id: str | None,
        last_activated_us: int,
        expires_us: int | None,
        created_by: str,
    ) -> str:
        revision_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO focus_item_revisions (id, item_id, tenant_id, revision, kind, summary, "
            "structured_payload, privacy_labels, source_refs, salience, activation, "
            "activation_base, importance, status, promotion_policy, promotion_target_type, "
            "promotion_target_id, last_activated_us, expires_us, created_us, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                item_id,
                tenant_id,
                revision,
                kind,
                summary,
                canonical_json(structured_payload) if structured_payload is not None else None,
                canonical_json(list(privacy_labels)),
                canonical_json([dict(ref) for ref in source_refs]),
                salience,
                activation,
                activation_base,
                importance,
                status,
                promotion_policy,
                promotion_target_type,
                promotion_target_id,
                last_activated_us,
                expires_us,
                self._clock.now_us(),
                created_by,
            ),
        )
        return revision_id

    def advance_pointer(
        self,
        item_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        activation: float | None = None,
        activation_base: float | None = None,
        last_activated_us: int | None = None,
    ) -> int:
        """CAS the pointer and the denormalized hot fields together."""
        assignments = [
            "current_revision = ?",
            "current_revision_id = ?",
            "status = ?",
            "updated_us = ?",
        ]
        params: list[Any] = [revision, revision_id, status, self._clock.now_us()]
        if activation is not None:
            assignments.append("activation = ?")
            params.append(activation)
        if activation_base is not None:
            assignments.append("activation_base = ?")
            params.append(activation_base)
        if last_activated_us is not None:
            assignments.append("last_activated_us = ?")
            params.append(last_activated_us)
        cursor = self._connection.execute(
            f"UPDATE focus_items SET {', '.join(assignments)} WHERE id = ? "
            "AND current_revision = ?",
            (*params, item_id, expected_revision),
        )
        return cursor.rowcount

    def set_initial_pointer(self, item_id: str, revision_id: str) -> int:
        """Wire a freshly created item's pointer (empty → first revision)."""
        cursor = self._connection.execute(
            "UPDATE focus_items SET current_revision_id = ?, updated_us = ? "
            "WHERE id = ? AND current_revision_id = ''",
            (revision_id, self._clock.now_us(), item_id),
        )
        return cursor.rowcount

    def active_items(self, tenant_id: str, agent_id: str) -> list[FocusItemCurrent]:
        """Active items in eviction order: lowest (activation, created, id) first."""
        rows = self._connection.execute(
            "SELECT * FROM focus_items WHERE tenant_id = ? AND agent_id = ? "
            "AND status = 'active' AND "
            + FOCUS_NOT_TOMBSTONED
            + " ORDER BY activation, created_us, id",
            (tenant_id, agent_id),
        ).fetchall()
        return [_focus_current_from_row(row) for row in rows]

    def items_for_agent(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: tuple[str, ...] = ("active", "dormant"),
        kind: str | None = None,
        limit: int = 500,
    ) -> list[FocusItemCurrent]:
        clauses = ["tenant_id = ?", "agent_id = ?", FOCUS_NOT_TOMBSTONED]
        params: list[Any] = [tenant_id, agent_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        rows = self._connection.execute(
            f"SELECT * FROM focus_items WHERE {' AND '.join(clauses)} "
            "ORDER BY created_us, id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_focus_current_from_row(row) for row in rows]

    def maintenance_items(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        now_us: int,
        limit: int = 500,
    ) -> list[FocusItemCurrent]:
        """Bounded sweep order that cannot starve the live working set.

        Active items are capacity-bounded and always come first. Expired
        dormant items follow; remaining dormant rows rotate by ``updated_us``
        because every actual refresh moves a row to the back of the queue.
        """
        rows = self._connection.execute(
            "SELECT * FROM focus_items WHERE tenant_id = ? AND agent_id = ? "
            "AND status IN ('active', 'dormant') AND " + FOCUS_NOT_TOMBSTONED + " ORDER BY "
            "CASE WHEN status = 'active' THEN 0 "
            "WHEN expires_us IS NOT NULL AND expires_us <= ? THEN 1 ELSE 2 END, "
            "updated_us, id LIMIT ?",
            (tenant_id, agent_id, now_us, limit),
        ).fetchall()
        return [_focus_current_from_row(row) for row in rows]

    def history(self, item_id: str, *, limit: int = 100) -> list[FocusRevision]:
        rows = self._connection.execute(
            "SELECT * FROM focus_item_revisions WHERE item_id = ? ORDER BY revision DESC LIMIT ?",
            (item_id, limit),
        ).fetchall()
        return [_focus_revision_from_row(row) for row in rows]

    def raise_pointer_mismatch(self, item_id: str, expected: int) -> None:
        row = self.get(item_id)
        raise RevisionMismatchError("focus_item", item_id, expected, row.current_revision)


__all__ = [
    "FocusRepository",
    "RecentContextRepository",
    "StateRepository",
    "stored_observation_from_row",
]
