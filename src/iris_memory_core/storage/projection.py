"""Phase 8 repositories: profile and graph projection state, generations,
current pointers, subjects/fields and nodes/edges (§13.3, §13.5, §22.5,
ADR-0016).

Every method runs inside the caller's short transaction and returns DOMAIN
records. Both projections live entirely in SQLite: the generation row IS
the authoritative manifest (counts + content checksum recomputed from the
persisted rows at build/verify time); the per-tenant pointer flips with a
fencing epoch CAS in the same transaction that retires the previous
generation. Incremental applies maintain the CURRENT generation's rows in
place and re-derive the manifest deterministically.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence

from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import ConflictError, NotFoundError
from iris_memory_core.domain.graph import (
    GraphCurrentPointer,
    GraphEdgeDraft,
    GraphEdgeRecord,
    GraphGenerationRecord,
    GraphNodeRecord,
    graph_edge_id,
    graph_edge_record_digest,
)
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.profile import (
    ProfileCurrentPointer,
    ProfileFieldDraft,
    ProfileFieldRecord,
    ProfileFieldSource,
    ProfileGenerationRecord,
    ProfileSubjectKey,
    ProfileSubjectRecord,
    subject_checksum_material,
)


def _labels(raw: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if isinstance(decoded, list):
        return tuple(str(label) for label in decoded)
    return ()


def _sources(raw: str) -> tuple[ProfileFieldSource, ...]:
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    sources: list[ProfileFieldSource] = []
    for item in decoded:
        if isinstance(item, dict) and "claim_id" in item and "revision" in item:
            sources.append(ProfileFieldSource(str(item["claim_id"]), int(item["revision"])))
    return tuple(sources)


class ProfileRepository:
    """Profile projection state, generations, pointer, subjects and fields."""

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def subjects_citing_claim(
        self, tenant_id: str, generation_id: str, claim_id: str
    ) -> tuple[ProfileSubjectKey, ...]:
        rows = self._connection.execute(
            "SELECT DISTINCT subject_kind, subject_id FROM profile_fields "
            "WHERE tenant_id = ? AND generation_id = ? AND EXISTS "
            "(SELECT 1 FROM json_each(source_refs_json) source "
            "WHERE json_extract(source.value, '$.claim_id') = ?)",
            (tenant_id, generation_id, claim_id),
        ).fetchall()
        return tuple(ProfileSubjectKey(str(row[0]), str(row[1])) for row in rows)

    # -- global projection state ---------------------------------------------

    def projection_state(self) -> str:
        row = self._connection.execute(
            "SELECT state FROM profile_projection_state WHERE id = 1"
        ).fetchone()
        return str(row["state"]) if row is not None else "never_built"

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None:
        if state not in ("never_built", "ready", "pending_rebuild"):
            raise ConflictError(f"unknown profile projection state: {state!r}")
        self._connection.execute(
            "INSERT INTO profile_projection_state (id, state, marked_us) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
            "marked_us = excluded.marked_us",
            (state, now_us if now_us is not None else self._clock.now_us()),
        )

    def reset_projection(self, *, now_us: int | None = None) -> None:
        """Restore reset (ADR-0016 §9): every tenant's generations, pointer,
        subjects and fields are wiped — a restore replaces the whole
        database, and an untrusted projection is never treated as restored
        state."""
        self._connection.execute("DELETE FROM profile_fields")
        self._connection.execute("DELETE FROM profile_subjects")
        self._connection.execute("DELETE FROM profile_current")
        self._connection.execute("DELETE FROM profile_generations")
        self.set_projection_state("pending_rebuild", now_us=now_us)

    # -- generations -----------------------------------------------------------

    def insert_generation(
        self,
        *,
        tenant_id: str,
        builder_version: int,
        source_watermark: int,
        tombstone_watermark: int,
        subject_count: int,
        field_count: int,
        content_checksum: str,
        agent_watermarks: dict[str, int],
        generation_id: str | None = None,
        now_us: int | None = None,
    ) -> ProfileGenerationRecord:
        resolved = generation_id or f"profg-{self._ids.new()}"
        stamp = now_us if now_us is not None else self._clock.now_us()
        self._connection.execute(
            "INSERT INTO profile_generations (id, tenant_id, builder_version, "
            "source_watermark, tombstone_watermark, subject_count, field_count, "
            "content_checksum, agent_watermarks_json, status, created_us, "
            "verified_us, retired_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'verified', ?, ?, NULL)",
            (
                resolved,
                tenant_id,
                builder_version,
                source_watermark,
                tombstone_watermark,
                subject_count,
                field_count,
                content_checksum,
                canonical_json(agent_watermarks),
                stamp,
                stamp,
            ),
        )
        return self.get_generation(resolved)

    def get_generation(self, generation_id: str) -> ProfileGenerationRecord:
        row = self._connection.execute(
            "SELECT * FROM profile_generations WHERE id = ?", (generation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("profile generation not found")
        return _profile_generation_from_row(row)

    def generations_for_tenant(self, tenant_id: str) -> tuple[ProfileGenerationRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM profile_generations WHERE tenant_id = ? ORDER BY created_us, id",
            (tenant_id,),
        ).fetchall()
        return tuple(_profile_generation_from_row(row) for row in rows)

    def retire_generation(self, generation_id: str, *, now_us: int | None = None) -> int:
        stamp = now_us if now_us is not None else self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE profile_generations SET status = 'retired', retired_us = ? "
            "WHERE id = ? AND status = 'verified'",
            (stamp, generation_id),
        )
        return cursor.rowcount

    def delete_retired_generations(
        self,
        tenant_id: str,
        *,
        keep: int = 2,
        older_than_us: int | None = None,
    ) -> tuple[str, ...]:
        """Remove retired generations beyond the retention window; their
        subject/field rows go with them (retired content is unserviceable —
        reads resolve through the pointer only)."""
        rows = self._connection.execute(
            "SELECT id, retired_us FROM profile_generations WHERE tenant_id = ? "
            "AND status = 'retired' ORDER BY retired_us DESC, id",
            (tenant_id,),
        ).fetchall()
        stale: list[str] = []
        for row in rows[keep:]:
            retired_us = row["retired_us"]
            if (
                older_than_us is not None
                and retired_us is not None
                and int(retired_us) > older_than_us
            ):
                continue
            stale.append(str(row["id"]))
        for generation_id in stale:
            self._connection.execute(
                "DELETE FROM profile_fields WHERE generation_id = ?", (generation_id,)
            )
            self._connection.execute(
                "DELETE FROM profile_subjects WHERE generation_id = ?", (generation_id,)
            )
            self._connection.execute(
                "DELETE FROM profile_generations WHERE id = ?", (generation_id,)
            )
        return tuple(stale)

    def bump_generation_watermark(
        self, tenant_id: str, generation_id: str, agent_id: str, watermark: int
    ) -> None:
        """Advance the generation's per-agent watermark to the max observed
        by a settled apply (ADR-0016 §4 — conservative minimum_watermark
        accounting; never decreases)."""
        generation = self.get_generation(generation_id)
        marks = generation.agent_watermarks()
        if agent_id and watermark > marks.get(agent_id, -1):
            marks[agent_id] = watermark
            self._connection.execute(
                "UPDATE profile_generations SET agent_watermarks_json = ? "
                "WHERE id = ? AND tenant_id = ?",
                (canonical_json(marks), generation_id, tenant_id),
            )

    # -- subjects and fields ---------------------------------------------------

    def subjects_for_generation(
        self, tenant_id: str, generation_id: str
    ) -> tuple[ProfileSubjectRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM profile_subjects WHERE tenant_id = ? AND generation_id = ? "
            "ORDER BY subject_kind, subject_id",
            (tenant_id, generation_id),
        ).fetchall()
        return tuple(
            ProfileSubjectRecord(
                tenant_id=str(row["tenant_id"]),
                generation_id=str(row["generation_id"]),
                subject_kind=str(row["subject_kind"]),
                subject_id=str(row["subject_id"]),
                field_count=int(row["field_count"]),
                subject_checksum=str(row["subject_checksum"]),
                updated_us=int(row["updated_us"]),
            )
            for row in rows
        )

    def fields_for_subject(
        self, tenant_id: str, generation_id: str, subject: ProfileSubjectKey
    ) -> tuple[ProfileFieldRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM profile_fields WHERE tenant_id = ? AND generation_id = ? "
            "AND subject_kind = ? AND subject_id = ? "
            "ORDER BY section, field, group_key",
            (tenant_id, generation_id, subject.kind, subject.subject_id),
        ).fetchall()
        return tuple(_profile_field_from_row(row) for row in rows)

    def field_count(self, tenant_id: str, generation_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM profile_fields WHERE tenant_id = ? AND generation_id = ?",
            (tenant_id, generation_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def subject_count(self, tenant_id: str, generation_id: str) -> int:
        """COUNT of subject rows — the read gate's structural check must not
        load every row (ADR-0016 §2)."""
        row = self._connection.execute(
            "SELECT COUNT(*) FROM profile_subjects WHERE tenant_id = ? AND generation_id = ?",
            (tenant_id, generation_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def insert_fields(
        self,
        tenant_id: str,
        generation_id: str,
        drafts: Sequence[ProfileFieldDraft],
        *,
        now_us: int | None = None,
    ) -> int:
        """Insert one subject's field rows + the subject summary row,
        deriving the subject checksum from the drafts (deterministic)."""
        if not drafts:
            return 0
        stamp = now_us if now_us is not None else self._clock.now_us()
        subject = drafts[0].subject
        for draft in drafts:
            if draft.subject != subject:
                raise ConflictError("insert_fields requires a single subject per call")
            self._connection.execute(
                "INSERT INTO profile_fields (tenant_id, generation_id, subject_kind, "
                "subject_id, section, field, group_key, agent_id, space_group_id, "
                "space_id, session_id, scope_key, privacy_labels_json, value_json, "
                "summary_text, source_refs_json, conflict_state, freshness_us, "
                "valid_from_us, valid_until_us, created_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    generation_id,
                    draft.subject.kind,
                    draft.subject.subject_id,
                    draft.section,
                    draft.field,
                    draft.group_key,
                    draft.agent_id,
                    draft.space_group_id,
                    draft.space_id,
                    draft.session_id,
                    draft.scope_key,
                    canonical_json(list(draft.privacy_labels)),
                    draft.value_json,
                    draft.summary_text,
                    canonical_json([source.as_ref() for source in draft.sources]),
                    draft.conflict_state,
                    draft.freshness_us,
                    draft.valid_from_us,
                    draft.valid_until_us,
                    stamp,
                ),
            )
        field_count, checksum = subject_checksum_material(tuple(drafts))
        self._connection.execute(
            "INSERT INTO profile_subjects (tenant_id, generation_id, subject_kind, "
            "subject_id, field_count, subject_checksum, updated_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, generation_id, subject_kind, subject_id) "
            "DO UPDATE SET field_count = excluded.field_count, "
            "subject_checksum = excluded.subject_checksum, "
            "updated_us = excluded.updated_us",
            (
                tenant_id,
                generation_id,
                subject.kind,
                subject.subject_id,
                field_count,
                checksum,
                stamp,
            ),
        )
        return len(drafts)

    def delete_subject_rows(
        self, tenant_id: str, generation_id: str, subject: ProfileSubjectKey
    ) -> int:
        """Remove one subject's rows ahead of an incremental re-derivation."""
        cursor = self._connection.execute(
            "DELETE FROM profile_fields WHERE tenant_id = ? AND generation_id = ? "
            "AND subject_kind = ? AND subject_id = ?",
            (tenant_id, generation_id, subject.kind, subject.subject_id),
        )
        self._connection.execute(
            "DELETE FROM profile_subjects WHERE tenant_id = ? AND generation_id = ? "
            "AND subject_kind = ? AND subject_id = ?",
            (tenant_id, generation_id, subject.kind, subject.subject_id),
        )
        return cursor.rowcount

    def recompute_generation_manifest(self, tenant_id: str, generation_id: str) -> None:
        """Re-derive counts + content checksum from the PERSISTED subject
        rows and stamp them onto the generation manifest — the incremental
        apply path keeps the manifest bound to the authoritative rows
        (ADR-0016 §2)."""
        subjects = self.subjects_for_generation(tenant_id, generation_id)
        digest_seed = hashlib.sha256()
        total_fields = 0
        for record in sorted(subjects, key=lambda item: f"{item.subject_kind}:{item.subject_id}"):
            digest_seed.update(
                f"{record.subject_kind}:{record.subject_id}\x1f{record.field_count}"
                f"\x1f{record.subject_checksum}".encode()
            )
            digest_seed.update(b"\x1e")
            total_fields += record.field_count
        self._connection.execute(
            "UPDATE profile_generations SET subject_count = ?, field_count = ?, "
            "content_checksum = ? WHERE id = ? AND tenant_id = ?",
            (len(subjects), total_fields, digest_seed.hexdigest(), generation_id, tenant_id),
        )

    # -- current pointer ---------------------------------------------------------

    def pointer(self, tenant_id: str) -> ProfileCurrentPointer | None:
        row = self._connection.execute(
            "SELECT * FROM profile_current WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return _profile_pointer_from_row(row) if row is not None else None

    def current_epoch(self, tenant_id: str) -> int:
        row = self._connection.execute(
            "SELECT switch_epoch FROM profile_current WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return int(row["switch_epoch"]) if row is not None else 0

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: ProfileGenerationRecord,
        expected_epoch: int,
        now_us: int | None = None,
    ) -> ProfileCurrentPointer:
        """Fenced CAS pointer switch (ADR-0016 §2): the epoch must be exactly
        what the publisher observed; a concurrent newer switch fails loudly.
        The flip, the retiring of the outgoing generation and the ready
        marker land in ONE transaction."""
        stamp = now_us if now_us is not None else self._clock.now_us()
        previous = self.pointer(tenant_id)
        if previous is not None:
            if previous.switch_epoch != expected_epoch:
                raise ConflictError(
                    "profile pointer epoch advanced during publish (fenced)",
                    details={"expected_epoch": expected_epoch},
                )
            new_epoch = previous.switch_epoch + 1
            if previous.generation_id != generation.id:
                self.retire_generation(previous.generation_id, now_us=stamp)
        else:
            if expected_epoch != 0:
                raise ConflictError(
                    "profile pointer epoch precondition failed",
                    details={"expected_epoch": expected_epoch},
                )
            new_epoch = 1
        self._connection.execute(
            "INSERT INTO profile_current (tenant_id, generation_id, switch_epoch, "
            "builder_version, source_watermark, tombstone_watermark, switched_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET generation_id = excluded.generation_id, "
            "switch_epoch = excluded.switch_epoch, "
            "builder_version = excluded.builder_version, "
            "source_watermark = excluded.source_watermark, "
            "tombstone_watermark = excluded.tombstone_watermark, "
            "switched_us = excluded.switched_us",
            (
                tenant_id,
                generation.id,
                new_epoch,
                generation.builder_version,
                generation.source_watermark,
                generation.tombstone_watermark,
                stamp,
            ),
        )
        self.set_projection_state("ready", now_us=stamp)
        result = self.pointer(tenant_id)
        assert result is not None
        return result

    def pointer_info(self, tenant_id: str) -> dict[str, object]:
        """Pointer summary for capabilities/health (no content)."""
        pointer = self.pointer(tenant_id)
        state = self.projection_state()
        if pointer is None:
            return {"state": state, "generation": None}
        return {
            "state": state,
            "generation": pointer.generation_id,
            "epoch": pointer.switch_epoch,
            "builder_version": pointer.builder_version,
            "source_watermark": pointer.source_watermark,
            "tombstone_watermark": pointer.tombstone_watermark,
        }


class GraphRepository:
    """Graph projection state, generations, pointer, nodes and edges."""

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- global projection state ---------------------------------------------

    def projection_state(self) -> str:
        row = self._connection.execute(
            "SELECT state FROM graph_projection_state WHERE id = 1"
        ).fetchone()
        return str(row["state"]) if row is not None else "never_built"

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None:
        if state not in ("never_built", "ready", "pending_rebuild"):
            raise ConflictError(f"unknown graph projection state: {state!r}")
        self._connection.execute(
            "INSERT INTO graph_projection_state (id, state, marked_us) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
            "marked_us = excluded.marked_us",
            (state, now_us if now_us is not None else self._clock.now_us()),
        )

    def reset_projection(self, *, now_us: int | None = None) -> None:
        """Restore reset (ADR-0016 §9): wipe every tenant's generations,
        pointer, nodes and edges; an untrusted graph is never treated as
        restored state."""
        self._connection.execute("DELETE FROM graph_edges")
        self._connection.execute("DELETE FROM graph_nodes")
        self._connection.execute("DELETE FROM graph_current")
        self._connection.execute("DELETE FROM graph_generations")
        self.set_projection_state("pending_rebuild", now_us=now_us)

    # -- generations -----------------------------------------------------------

    def insert_generation(
        self,
        *,
        tenant_id: str,
        builder_version: int,
        source_watermark: int,
        tombstone_watermark: int,
        node_count: int,
        edge_count: int,
        content_checksum: str,
        agent_watermarks: dict[str, int],
        generation_id: str | None = None,
        now_us: int | None = None,
    ) -> GraphGenerationRecord:
        resolved = generation_id or f"graphg-{self._ids.new()}"
        stamp = now_us if now_us is not None else self._clock.now_us()
        self._connection.execute(
            "INSERT INTO graph_generations (id, tenant_id, builder_version, "
            "source_watermark, tombstone_watermark, node_count, edge_count, "
            "content_checksum, agent_watermarks_json, status, created_us, "
            "verified_us, retired_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'verified', ?, ?, NULL)",
            (
                resolved,
                tenant_id,
                builder_version,
                source_watermark,
                tombstone_watermark,
                node_count,
                edge_count,
                content_checksum,
                canonical_json(agent_watermarks),
                stamp,
                stamp,
            ),
        )
        return self.get_generation(resolved)

    def get_generation(self, generation_id: str) -> GraphGenerationRecord:
        row = self._connection.execute(
            "SELECT * FROM graph_generations WHERE id = ?", (generation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("graph generation not found")
        return _graph_generation_from_row(row)

    def generations_for_tenant(self, tenant_id: str) -> tuple[GraphGenerationRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM graph_generations WHERE tenant_id = ? ORDER BY created_us, id",
            (tenant_id,),
        ).fetchall()
        return tuple(_graph_generation_from_row(row) for row in rows)

    def retire_generation(self, generation_id: str, *, now_us: int | None = None) -> int:
        stamp = now_us if now_us is not None else self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE graph_generations SET status = 'retired', retired_us = ? "
            "WHERE id = ? AND status = 'verified'",
            (stamp, generation_id),
        )
        return cursor.rowcount

    def delete_retired_generations(
        self,
        tenant_id: str,
        *,
        keep: int = 2,
        older_than_us: int | None = None,
    ) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id, retired_us FROM graph_generations WHERE tenant_id = ? "
            "AND status = 'retired' ORDER BY retired_us DESC, id",
            (tenant_id,),
        ).fetchall()
        stale: list[str] = []
        for row in rows[keep:]:
            retired_us = row["retired_us"]
            if (
                older_than_us is not None
                and retired_us is not None
                and int(retired_us) > older_than_us
            ):
                continue
            stale.append(str(row["id"]))
        for generation_id in stale:
            self._connection.execute(
                "DELETE FROM graph_edges WHERE generation_id = ?", (generation_id,)
            )
            self._connection.execute(
                "DELETE FROM graph_nodes WHERE generation_id = ?", (generation_id,)
            )
            self._connection.execute("DELETE FROM graph_generations WHERE id = ?", (generation_id,))
        return tuple(stale)

    def bump_generation_watermark(
        self, tenant_id: str, generation_id: str, agent_id: str, watermark: int
    ) -> None:
        generation = self.get_generation(generation_id)
        marks = generation.agent_watermarks()
        if agent_id and watermark > marks.get(agent_id, -1):
            marks[agent_id] = watermark
            self._connection.execute(
                "UPDATE graph_generations SET agent_watermarks_json = ? "
                "WHERE id = ? AND tenant_id = ?",
                (canonical_json(marks), generation_id, tenant_id),
            )

    # -- nodes and edges ---------------------------------------------------------

    def node_count(self, tenant_id: str, generation_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE tenant_id = ? AND generation_id = ?",
            (tenant_id, generation_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def edge_count(self, tenant_id: str, generation_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE tenant_id = ? AND generation_id = ?",
            (tenant_id, generation_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def node_exists(self, tenant_id: str, generation_id: str, node_id: str, node_kind: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM graph_nodes WHERE tenant_id = ? AND generation_id = ? "
            "AND node_id = ? AND node_kind = ?",
            (tenant_id, generation_id, node_id, node_kind),
        ).fetchone()
        return row is not None

    def edges_for_source(
        self,
        tenant_id: str,
        generation_id: str,
        node_id: str,
        *,
        node_kind: str,
        limit: int | None = None,
    ) -> tuple[GraphEdgeRecord, ...]:
        """Every edge leaving one node, in the deterministic traversal order
        (edge_kind, edge_type, resource_id) — the graph route's expansion
        cursor (ADR-0016 §4). ``limit`` bounds the read itself: the route
        never needs more than its per-level fanout window, so a malicious
        hub cannot make one fetch load an unbounded row set."""
        sql = (
            "SELECT * FROM graph_edges WHERE tenant_id = ? AND generation_id = ? "
            "AND source_node_id = ? AND source_node_kind = ? "
            "ORDER BY edge_kind, edge_type, resource_id"
        )
        params: list[object] = [tenant_id, generation_id, node_id, node_kind]
        if limit is not None:
            if limit < 0:
                raise ConflictError("edge read limit must be non-negative")
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._connection.execute(sql, tuple(params)).fetchall()
        return tuple(_graph_edge_from_row(row) for row in rows)

    def edges_for_resource(
        self, tenant_id: str, generation_id: str, resource_type: str, resource_id: str
    ) -> tuple[GraphEdgeRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM graph_edges WHERE tenant_id = ? AND generation_id = ? "
            "AND resource_type = ? AND resource_id = ? ORDER BY edge_id",
            (tenant_id, generation_id, resource_type, resource_id),
        ).fetchall()
        return tuple(_graph_edge_from_row(row) for row in rows)

    def edges_for_entity(
        self, tenant_id: str, generation_id: str, entity_id: str
    ) -> tuple[GraphEdgeRecord, ...]:
        """Every edge touching an entity as source OR target (either node
        kind for the source side) — the per-entity incremental re-derivation
        set (ADR-0016 §3)."""
        rows = self._connection.execute(
            "SELECT * FROM graph_edges WHERE tenant_id = ? AND generation_id = ? "
            "AND (source_node_id = ? OR target_node_id = ?) ORDER BY edge_id",
            (tenant_id, generation_id, entity_id, entity_id),
        ).fetchall()
        return tuple(_graph_edge_from_row(row) for row in rows)

    def insert_edges(
        self,
        tenant_id: str,
        generation_id: str,
        drafts: Sequence[GraphEdgeDraft],
        *,
        node_status: str | Mapping[str, str] = "canonical",
        now_us: int | None = None,
    ) -> int:
        """Insert edge rows and ensure both endpoint nodes exist. Node
        status is advisory metadata captured at build time — either one
        string for every node or a per-entity-id mapping."""
        if not drafts:
            return 0
        stamp = now_us if now_us is not None else self._clock.now_us()

        def _status_for(node_id: str) -> str:
            if isinstance(node_status, str):
                return node_status
            return node_status.get(node_id, "canonical")

        for draft in drafts:
            edge_id = graph_edge_id(draft)
            self._connection.execute(
                "INSERT INTO graph_edges (tenant_id, generation_id, edge_id, edge_kind, "
                "edge_type, source_node_id, source_node_kind, target_node_id, "
                "target_node_kind, resource_type, resource_id, resource_revision, "
                "agent_id, space_group_id, space_id, session_id, privacy_labels_json, "
                "status, confidence, importance, valid_from_us, valid_until_us, "
                "content_hash, created_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    generation_id,
                    edge_id,
                    draft.edge_kind,
                    draft.edge_type,
                    draft.source_node_id,
                    draft.source_node_kind,
                    draft.target_node_id,
                    draft.target_node_kind,
                    draft.resource_type,
                    draft.resource_id,
                    draft.resource_revision,
                    draft.agent_id,
                    draft.space_group_id,
                    draft.space_id,
                    draft.session_id,
                    canonical_json(list(draft.privacy_labels)),
                    draft.status,
                    draft.confidence,
                    draft.importance,
                    draft.valid_from_us,
                    draft.valid_until_us,
                    draft.content_hash,
                    stamp,
                ),
            )
            for node_id, node_kind in (
                (draft.source_node_id, draft.source_node_kind),
                (draft.target_node_id, draft.target_node_kind),
            ):
                self._connection.execute(
                    "INSERT INTO graph_nodes (tenant_id, generation_id, node_id, "
                    "node_kind, node_status) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(tenant_id, generation_id, node_id) DO NOTHING",
                    (tenant_id, generation_id, node_id, node_kind, _status_for(node_id)),
                )
        return len(drafts)

    def all_edges(self, tenant_id: str, generation_id: str) -> tuple[GraphEdgeRecord, ...]:
        """Every edge row of one generation in deterministic id order —
        the verification path's authoritative content set."""
        rows = self._connection.execute(
            "SELECT * FROM graph_edges WHERE tenant_id = ? AND generation_id = ? ORDER BY edge_id",
            (tenant_id, generation_id),
        ).fetchall()
        return tuple(_graph_edge_from_row(row) for row in rows)

    def delete_resource_edges(
        self, tenant_id: str, generation_id: str, resource_type: str, resource_id: str
    ) -> int:
        cursor = self._connection.execute(
            "DELETE FROM graph_edges WHERE tenant_id = ? AND generation_id = ? "
            "AND resource_type = ? AND resource_id = ?",
            (tenant_id, generation_id, resource_type, resource_id),
        )
        return cursor.rowcount

    def delete_entity_edges(self, tenant_id: str, generation_id: str, entity_id: str) -> int:
        """Remove every edge touching an entity (redirect/tombstone
        re-derivation set)."""
        cursor = self._connection.execute(
            "DELETE FROM graph_edges WHERE tenant_id = ? AND generation_id = ? "
            "AND (source_node_id = ? OR target_node_id = ?)",
            (tenant_id, generation_id, entity_id, entity_id),
        )
        return cursor.rowcount

    def prune_orphan_nodes(self, tenant_id: str, generation_id: str) -> int:
        """Remove nodes without any remaining edge (the node set is exactly
        the edge-referenced set — ADR-0016 §3)."""
        cursor = self._connection.execute(
            "DELETE FROM graph_nodes WHERE tenant_id = ? AND generation_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM graph_edges e WHERE e.tenant_id = "
            "graph_nodes.tenant_id AND e.generation_id = graph_nodes.generation_id "
            "AND (e.source_node_id = graph_nodes.node_id "
            "OR e.target_node_id = graph_nodes.node_id))",
            (tenant_id, generation_id),
        )
        return cursor.rowcount

    def recompute_generation_manifest(self, tenant_id: str, generation_id: str) -> None:
        """Re-derive node/edge counts + content checksum from the PERSISTED
        edge rows (deterministic: sorted record digests + sorted node set)."""
        rows = self._connection.execute(
            "SELECT * FROM graph_edges WHERE tenant_id = ? AND generation_id = ? ORDER BY edge_id",
            (tenant_id, generation_id),
        ).fetchall()
        edges = tuple(_graph_edge_from_row(row) for row in rows)
        digest = hashlib.sha256()
        for record in sorted(edges, key=graph_edge_record_digest):
            digest.update(graph_edge_record_digest(record).encode("utf-8"))
            digest.update(b"\x1e")
        nodes = set()
        for edge in edges:
            nodes.add((edge.source_node_kind, edge.source_node_id))
            nodes.add((edge.target_node_kind, edge.target_node_id))
        for kind, node_id in sorted(nodes):
            digest.update(f"{kind}\x1f{node_id}".encode())
            digest.update(b"\x1e")
        self._connection.execute(
            "UPDATE graph_generations SET node_count = ?, edge_count = ?, "
            "content_checksum = ? WHERE id = ? AND tenant_id = ?",
            (len(nodes), len(edges), digest.hexdigest(), generation_id, tenant_id),
        )

    # -- current pointer ---------------------------------------------------------

    def pointer(self, tenant_id: str) -> GraphCurrentPointer | None:
        row = self._connection.execute(
            "SELECT * FROM graph_current WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return _graph_pointer_from_row(row) if row is not None else None

    def current_epoch(self, tenant_id: str) -> int:
        row = self._connection.execute(
            "SELECT switch_epoch FROM graph_current WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return int(row["switch_epoch"]) if row is not None else 0

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: GraphGenerationRecord,
        expected_epoch: int,
        now_us: int | None = None,
    ) -> GraphCurrentPointer:
        """Fenced CAS pointer switch (ADR-0016 §3)."""
        stamp = now_us if now_us is not None else self._clock.now_us()
        previous = self.pointer(tenant_id)
        if previous is not None:
            if previous.switch_epoch != expected_epoch:
                raise ConflictError(
                    "graph pointer epoch advanced during publish (fenced)",
                    details={"expected_epoch": expected_epoch},
                )
            new_epoch = previous.switch_epoch + 1
            if previous.generation_id != generation.id:
                self.retire_generation(previous.generation_id, now_us=stamp)
        else:
            if expected_epoch != 0:
                raise ConflictError(
                    "graph pointer epoch precondition failed",
                    details={"expected_epoch": expected_epoch},
                )
            new_epoch = 1
        self._connection.execute(
            "INSERT INTO graph_current (tenant_id, generation_id, switch_epoch, "
            "builder_version, source_watermark, tombstone_watermark, switched_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET generation_id = excluded.generation_id, "
            "switch_epoch = excluded.switch_epoch, "
            "builder_version = excluded.builder_version, "
            "source_watermark = excluded.source_watermark, "
            "tombstone_watermark = excluded.tombstone_watermark, "
            "switched_us = excluded.switched_us",
            (
                tenant_id,
                generation.id,
                new_epoch,
                generation.builder_version,
                generation.source_watermark,
                generation.tombstone_watermark,
                stamp,
            ),
        )
        self.set_projection_state("ready", now_us=stamp)
        result = self.pointer(tenant_id)
        assert result is not None
        return result

    def pointer_info(self, tenant_id: str) -> dict[str, object]:
        pointer = self.pointer(tenant_id)
        state = self.projection_state()
        if pointer is None:
            return {"state": state, "generation": None}
        return {
            "state": state,
            "generation": pointer.generation_id,
            "epoch": pointer.switch_epoch,
            "builder_version": pointer.builder_version,
            "source_watermark": pointer.source_watermark,
            "tombstone_watermark": pointer.tombstone_watermark,
        }


# -- row mappers ---------------------------------------------------------------


def _profile_generation_from_row(row: sqlite3.Row) -> ProfileGenerationRecord:
    return ProfileGenerationRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        builder_version=int(row["builder_version"]),
        source_watermark=int(row["source_watermark"]),
        tombstone_watermark=int(row["tombstone_watermark"]),
        subject_count=int(row["subject_count"]),
        field_count=int(row["field_count"]),
        content_checksum=str(row["content_checksum"]),
        agent_watermarks_json=str(row["agent_watermarks_json"]),
        status=str(row["status"]),
        created_us=int(row["created_us"]),
        verified_us=int(row["verified_us"]),
        retired_us=int(row["retired_us"]) if row["retired_us"] is not None else None,
    )


def _profile_pointer_from_row(row: sqlite3.Row) -> ProfileCurrentPointer:
    return ProfileCurrentPointer(
        tenant_id=str(row["tenant_id"]),
        generation_id=str(row["generation_id"]),
        switch_epoch=int(row["switch_epoch"]),
        builder_version=int(row["builder_version"]),
        source_watermark=int(row["source_watermark"]),
        tombstone_watermark=int(row["tombstone_watermark"]),
        switched_us=int(row["switched_us"]),
    )


def _profile_field_from_row(row: sqlite3.Row) -> ProfileFieldRecord:
    return ProfileFieldRecord(
        tenant_id=str(row["tenant_id"]),
        generation_id=str(row["generation_id"]),
        subject_kind=str(row["subject_kind"]),
        subject_id=str(row["subject_id"]),
        section=str(row["section"]),
        field=str(row["field"]),
        group_key=str(row["group_key"]),
        agent_id=str(row["agent_id"]),
        space_group_id=str(row["space_group_id"]) if row["space_group_id"] else None,
        space_id=str(row["space_id"]) if row["space_id"] else None,
        session_id=str(row["session_id"]) if row["session_id"] else None,
        scope_key=str(row["scope_key"]),
        privacy_labels=_labels(str(row["privacy_labels_json"])),
        value_json=str(row["value_json"]),
        summary_text=str(row["summary_text"]),
        sources=_sources(str(row["source_refs_json"])),
        conflict_state=str(row["conflict_state"]),
        freshness_us=int(row["freshness_us"]),
        valid_from_us=int(row["valid_from_us"]) if row["valid_from_us"] is not None else None,
        valid_until_us=int(row["valid_until_us"]) if row["valid_until_us"] is not None else None,
        created_us=int(row["created_us"]),
    )


def _graph_generation_from_row(row: sqlite3.Row) -> GraphGenerationRecord:
    return GraphGenerationRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        builder_version=int(row["builder_version"]),
        source_watermark=int(row["source_watermark"]),
        tombstone_watermark=int(row["tombstone_watermark"]),
        node_count=int(row["node_count"]),
        edge_count=int(row["edge_count"]),
        content_checksum=str(row["content_checksum"]),
        agent_watermarks_json=str(row["agent_watermarks_json"]),
        status=str(row["status"]),
        created_us=int(row["created_us"]),
        verified_us=int(row["verified_us"]),
        retired_us=int(row["retired_us"]) if row["retired_us"] is not None else None,
    )


def _graph_pointer_from_row(row: sqlite3.Row) -> GraphCurrentPointer:
    return GraphCurrentPointer(
        tenant_id=str(row["tenant_id"]),
        generation_id=str(row["generation_id"]),
        switch_epoch=int(row["switch_epoch"]),
        builder_version=int(row["builder_version"]),
        source_watermark=int(row["source_watermark"]),
        tombstone_watermark=int(row["tombstone_watermark"]),
        switched_us=int(row["switched_us"]),
    )


def _graph_edge_from_row(row: sqlite3.Row) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        tenant_id=str(row["tenant_id"]),
        generation_id=str(row["generation_id"]),
        edge_id=str(row["edge_id"]),
        edge_kind=str(row["edge_kind"]),
        edge_type=str(row["edge_type"]),
        source_node_id=str(row["source_node_id"]),
        source_node_kind=str(row["source_node_kind"]),
        target_node_id=str(row["target_node_id"]),
        target_node_kind=str(row["target_node_kind"]),
        resource_type=str(row["resource_type"]),
        resource_id=str(row["resource_id"]),
        resource_revision=int(row["resource_revision"]),
        agent_id=str(row["agent_id"]) if row["agent_id"] else None,
        space_group_id=str(row["space_group_id"]) if row["space_group_id"] else None,
        space_id=str(row["space_id"]) if row["space_id"] else None,
        session_id=str(row["session_id"]) if row["session_id"] else None,
        privacy_labels=_labels(str(row["privacy_labels_json"])),
        status=str(row["status"]),
        confidence=float(row["confidence"]),
        importance=float(row["importance"]),
        valid_from_us=int(row["valid_from_us"]) if row["valid_from_us"] is not None else None,
        valid_until_us=int(row["valid_until_us"]) if row["valid_until_us"] is not None else None,
        content_hash=str(row["content_hash"]),
        created_us=int(row["created_us"]),
    )


__all__ = [
    "GraphNodeRecord",
    "GraphRepository",
    "ProfileRepository",
]
