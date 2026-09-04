"""Phase 6 repositories: FTS projection state and recall usage accounting.

Every method runs inside the caller's short transaction and returns DOMAIN
records (ADR-0007). The FTS projection follows ADR-0014: generations are
immutable once verified, the per-tenant current pointer flips in the same
transaction that retires the previous generation, and documents carry only
rebuildable metadata. The external-content FTS5 virtual table is created
lazily after a capability probe — never in a migration — so a runtime
without FTS5 degrades the route instead of breaking the schema.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any, cast

from iris_memory_core.application.ports import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import ConflictError, NotFoundError
from iris_memory_core.domain.fts import (
    FtsCurrentPointer,
    FtsDocumentInput,
    FtsDocumentRecord,
    FtsGenerationRecord,
    normalize_index_text,
)
from iris_memory_core.domain.hashing import canonical_json

INDEX_TABLE = "fts_index"


def fts5_available(connection: sqlite3.Connection) -> bool:
    """Probe whether the FTS5 module is compiled into this runtime."""
    try:
        row = connection.execute(
            "SELECT 1 FROM pragma_module_list() WHERE name = 'fts5'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _labels(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    decoded = json.loads(raw)
    return tuple(decoded) if isinstance(decoded, list) else ()


def _document_from_row(row: sqlite3.Row) -> FtsDocumentRecord:
    return FtsDocumentRecord(
        id=int(row["id"]),
        tenant_id=str(row["tenant_id"]),
        generation_id=str(row["generation_id"]),
        resource_type=str(row["resource_type"]),
        resource_id=str(row["resource_id"]),
        resource_revision=int(row["resource_revision"]),
        agent_id=str(row["agent_id"]),
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=str(row["scope_key"]),
        canonical_status=str(row["canonical_status"]),
        privacy_labels=_labels(row["privacy_labels"]),
        subject_entity_id=row["subject_entity_id"],
        content_hash=str(row["content_hash"]),
        occurred_us=int(row["occurred_us"]),
        valid_from_us=row["valid_from_us"],
        valid_until_us=row["valid_until_us"],
        index_text=str(row["index_text"]),
        doc_status=str(row["doc_status"]),
        invalidated_us=row["invalidated_us"],
        builder_version=int(row["builder_version"]),
        source_watermark=int(row["source_watermark"]),
        tombstone_watermark=int(row["tombstone_watermark"]),
        created_us=int(row["created_us"]),
    )


def _generation_from_row(row: sqlite3.Row) -> FtsGenerationRecord:
    return FtsGenerationRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        builder_version=int(row["builder_version"]),
        tokenizer_version=int(row["tokenizer_version"]),
        config_json=str(row["config_json"]),
        source_watermark=int(row["source_watermark"]),
        tombstone_watermark=int(row["tombstone_watermark"]),
        document_count=int(row["document_count"]),
        content_checksum=str(row["content_checksum"]),
        status=str(row["status"]),
        created_us=int(row["created_us"]),
        verified_us=int(row["verified_us"]),
    )


class FtsRepository:
    """FTS generations, documents, pointer and global projection state."""

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- virtual table ------------------------------------------------------

    def ensure_index(self) -> bool:
        """Idempotently create the external-content FTS5 table.

        Returns False when the runtime lacks FTS5 — the caller degrades the
        FTS route instead of failing the schema (ADR-0014 §1).
        """
        if not fts5_available(self._connection):
            return False
        self._connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {INDEX_TABLE} USING fts5("
            "index_text, content='fts_documents', content_rowid='id', "
            "tokenize='unicode61')"
        )
        return True

    def drop_index(self) -> None:
        """Drop the virtual table (and its shadow tables) — restore reset."""
        self._connection.execute(f"DROP TABLE IF EXISTS {INDEX_TABLE}")

    # -- global projection state ---------------------------------------------

    def projection_state(self) -> str:
        row = self._connection.execute(
            "SELECT state FROM fts_projection_state WHERE id = 1"
        ).fetchone()
        return str(row["state"]) if row is not None else "never_built"

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None:
        if state not in ("never_built", "ready", "pending_rebuild"):
            raise ConflictError(f"unknown FTS projection state: {state!r}")
        self._connection.execute(
            "INSERT INTO fts_projection_state (id, state, marked_us) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
            "marked_us = excluded.marked_us",
            (state, now_us if now_us is not None else self._clock.now_us()),
        )

    def reset_projection(self, *, now_us: int | None = None) -> None:
        """Clear every projection row and mark the rebuild as pending.

        Used by restore: the FTS projection is never restored as a source of
        truth (ADR-0014 §9).
        """
        self.drop_index()
        self._connection.execute("DELETE FROM fts_documents")
        self._connection.execute("DELETE FROM fts_current")
        self._connection.execute("DELETE FROM fts_generations")
        self.set_projection_state("pending_rebuild", now_us=now_us)

    # -- generations ----------------------------------------------------------

    def insert_generation(
        self,
        *,
        tenant_id: str,
        builder_version: int,
        tokenizer_version: int,
        config_json: str,
        source_watermark: int,
        tombstone_watermark: int,
        document_count: int,
        content_checksum: str,
        now_us: int | None = None,
    ) -> FtsGenerationRecord:
        generation_id = f"ftsg-{self._ids.new()}"
        stamp = now_us if now_us is not None else self._clock.now_us()
        self._connection.execute(
            "INSERT INTO fts_generations (id, tenant_id, builder_version, "
            "tokenizer_version, config_json, source_watermark, tombstone_watermark, "
            "document_count, content_checksum, status, created_us, verified_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?)",
            (
                generation_id,
                tenant_id,
                builder_version,
                tokenizer_version,
                config_json,
                source_watermark,
                tombstone_watermark,
                document_count,
                content_checksum,
                stamp,
                stamp,
            ),
        )
        return FtsGenerationRecord(
            id=generation_id,
            tenant_id=tenant_id,
            builder_version=builder_version,
            tokenizer_version=tokenizer_version,
            config_json=config_json,
            source_watermark=source_watermark,
            tombstone_watermark=tombstone_watermark,
            document_count=document_count,
            content_checksum=content_checksum,
            status="verified",
            created_us=stamp,
            verified_us=stamp,
        )

    def get_generation(self, generation_id: str) -> FtsGenerationRecord:
        row = self._connection.execute(
            "SELECT * FROM fts_generations WHERE id = ?", (generation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("fts generation not found")
        return _generation_from_row(row)

    def generations_for_tenant(self, tenant_id: str) -> tuple[FtsGenerationRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM fts_generations WHERE tenant_id = ? ORDER BY created_us, id",
            (tenant_id,),
        ).fetchall()
        return tuple(_generation_from_row(row) for row in rows)

    def retire_generation(self, generation_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE fts_generations SET status = 'retired' WHERE id = ? AND status = 'verified'",
            (generation_id,),
        )
        return cursor.rowcount

    # -- current pointer ------------------------------------------------------

    def pointer(self, tenant_id: str) -> FtsCurrentPointer | None:
        row = self._connection.execute(
            "SELECT * FROM fts_current WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if row is None:
            return None
        return FtsCurrentPointer(
            tenant_id=tenant_id,
            generation_id=str(row["generation_id"]),
            builder_version=int(row["builder_version"]),
            tokenizer_version=int(row["tokenizer_version"]),
            source_watermark=int(row["source_watermark"]),
            tombstone_watermark=int(row["tombstone_watermark"]),
            switched_us=int(row["switched_us"]),
        )

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: FtsGenerationRecord,
        now_us: int | None = None,
    ) -> None:
        """Install the verified generation as current; retire the previous.

        One transaction upserts the pointer and retires the outgoing
        generation — a partially visible switch is not representable.
        """
        stamp = now_us if now_us is not None else self._clock.now_us()
        previous = self.pointer(tenant_id)
        self._connection.execute(
            "INSERT INTO fts_current (tenant_id, generation_id, builder_version, "
            "tokenizer_version, source_watermark, tombstone_watermark, switched_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET generation_id = excluded.generation_id, "
            "builder_version = excluded.builder_version, "
            "tokenizer_version = excluded.tokenizer_version, "
            "source_watermark = excluded.source_watermark, "
            "tombstone_watermark = excluded.tombstone_watermark, "
            "switched_us = excluded.switched_us",
            (
                tenant_id,
                generation.id,
                generation.builder_version,
                generation.tokenizer_version,
                generation.source_watermark,
                generation.tombstone_watermark,
                stamp,
            ),
        )
        if previous is not None and previous.generation_id != generation.id:
            self.retire_generation(previous.generation_id)
        self.set_projection_state("ready", now_us=stamp)

    # -- documents --------------------------------------------------------------

    def upsert_document(
        self,
        *,
        generation_id: str,
        document: FtsDocumentInput,
        source_watermark: int,
        tombstone_watermark: int,
        builder_version: int,
        now_us: int | None = None,
    ) -> FtsDocumentRecord:
        """Insert or replace the resource's document inside a generation.

        A revision advance replaces the row in place: the old indexed text is
        removed from the FTS index (external-content delete command) and the
        new one inserted in the same transaction. Old revisions can never
        remain searchable inside a live generation.
        """
        index_text = normalize_index_text(document.raw_text)
        if not index_text:
            raise ConflictError("normalized index text must not be empty")
        existing = self._connection.execute(
            "SELECT * FROM fts_documents WHERE generation_id = ? "
            "AND resource_type = ? AND resource_id = ?",
            (generation_id, document.resource_type, document.resource_id),
        ).fetchone()
        stamp = now_us if now_us is not None else self._clock.now_us()
        if existing is not None:
            self._connection.execute(
                f"INSERT INTO {INDEX_TABLE}({INDEX_TABLE}, rowid, index_text) "
                "VALUES ('delete', ?, ?)",
                (int(existing["id"]), existing["index_text"]),
            )
            self._connection.execute(
                "UPDATE fts_documents SET resource_revision = ?, scope_key = ?, "
                "space_group_id = ?, space_id = ?, session_id = ?, canonical_status = ?, "
                "privacy_labels = ?, subject_entity_id = ?, content_hash = ?, "
                "occurred_us = ?, valid_from_us = ?, valid_until_us = ?, index_text = ?, "
                "doc_status = 'active', invalidated_us = NULL, builder_version = ?, "
                "source_watermark = ?, tombstone_watermark = ?, created_us = ? "
                "WHERE id = ?",
                (
                    document.resource_revision,
                    document.scope_key,
                    document.space_group_id,
                    document.space_id,
                    document.session_id,
                    document.canonical_status,
                    canonical_json(sorted(set(document.privacy_labels))),
                    document.subject_entity_id,
                    document.content_hash,
                    document.occurred_us,
                    document.valid_from_us,
                    document.valid_until_us,
                    index_text,
                    builder_version,
                    source_watermark,
                    tombstone_watermark,
                    stamp,
                    int(existing["id"]),
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM fts_documents WHERE id = ?", (int(existing["id"]),)
            ).fetchone()
            assert row is not None
        else:
            self._connection.execute(
                "INSERT INTO fts_documents (tenant_id, generation_id, resource_type, "
                "resource_id, resource_revision, agent_id, space_group_id, space_id, "
                "session_id, scope_key, canonical_status, privacy_labels, "
                "subject_entity_id, content_hash, occurred_us, valid_from_us, "
                "valid_until_us, index_text, doc_status, invalidated_us, builder_version, "
                "source_watermark, tombstone_watermark, created_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'active', NULL, ?, ?, ?, ?)",
                (
                    document.tenant_id,
                    generation_id,
                    document.resource_type,
                    document.resource_id,
                    document.resource_revision,
                    document.agent_id,
                    document.space_group_id,
                    document.space_id,
                    document.session_id,
                    document.scope_key,
                    document.canonical_status,
                    canonical_json(sorted(set(document.privacy_labels))),
                    document.subject_entity_id,
                    document.content_hash,
                    document.occurred_us,
                    document.valid_from_us,
                    document.valid_until_us,
                    index_text,
                    builder_version,
                    source_watermark,
                    tombstone_watermark,
                    stamp,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM fts_documents WHERE generation_id = ? "
                "AND resource_type = ? AND resource_id = ?",
                (generation_id, document.resource_type, document.resource_id),
            ).fetchone()
            assert row is not None
        self._connection.execute(
            f"INSERT INTO {INDEX_TABLE}(rowid, index_text) VALUES (?, ?)",
            (int(row["id"]), index_text),
        )
        return _document_from_row(row)

    def invalidate_document(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        now_us: int | None = None,
    ) -> int:
        """Logically invalidate every live document of one resource (tombstone)."""
        stamp = now_us if now_us is not None else self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE fts_documents SET doc_status = 'invalid', invalidated_us = ? "
            "WHERE tenant_id = ? AND resource_type = ? AND resource_id = ? "
            "AND doc_status = 'active'",
            (stamp, tenant_id, resource_type, resource_id),
        )
        return cursor.rowcount

    def document_for_resource(
        self, tenant_id: str, resource_type: str, resource_id: str
    ) -> FtsDocumentRecord | None:
        row = self._connection.execute(
            "SELECT * FROM fts_documents WHERE tenant_id = ? AND resource_type = ? "
            "AND resource_id = ? ORDER BY id DESC LIMIT 1",
            (tenant_id, resource_type, resource_id),
        ).fetchone()
        return _document_from_row(row) if row is not None else None

    def delete_invalid_documents(self, tenant_id: str, *, limit: int = 500) -> int:
        """Physically delete logically-invalidated documents (async cleanup)."""
        rows = self._connection.execute(
            "SELECT id, index_text FROM fts_documents WHERE tenant_id = ? "
            "AND doc_status = 'invalid' ORDER BY invalidated_us LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
        for row in rows:
            self._connection.execute(
                f"INSERT INTO {INDEX_TABLE}({INDEX_TABLE}, rowid, index_text) "
                "VALUES ('delete', ?, ?)",
                (int(row["id"]), row["index_text"]),
            )
            self._connection.execute("DELETE FROM fts_documents WHERE id = ?", (int(row["id"]),))
        return len(rows)

    def delete_retired_generations(
        self, tenant_id: str, *, keep: int = 2, now_us: int | None = None
    ) -> int:
        """Remove retired generations beyond the retention window."""
        del now_us
        rows = self._connection.execute(
            "SELECT id FROM fts_generations WHERE tenant_id = ? AND status = 'retired' "
            "ORDER BY verified_us DESC, id",
            (tenant_id,),
        ).fetchall()
        stale = [str(row["id"]) for row in rows[keep:]]
        for generation_id in stale:
            # External-content FTS5 keeps its own postings: every row must
            # be removed through the 'delete' command BEFORE the content
            # row disappears, or the index accumulates ghost postings that
            # bloat every future MATCH (the join hides them from results,
            # not from cost).
            documents = self._connection.execute(
                "SELECT id, index_text FROM fts_documents WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
            for row in documents:
                self._connection.execute(
                    f"INSERT INTO {INDEX_TABLE}({INDEX_TABLE}, rowid, index_text) "
                    "VALUES ('delete', ?, ?)",
                    (int(row["id"]), row["index_text"]),
                )
            self._connection.execute(
                "DELETE FROM fts_documents WHERE generation_id = ?", (generation_id,)
            )
            self._connection.execute("DELETE FROM fts_generations WHERE id = ?", (generation_id,))
        return len(stale)

    def documents_for_generation(self, generation_id: str) -> tuple[FtsDocumentRecord, ...]:
        """Every stored document of one generation (verification re-read)."""
        rows = self._connection.execute(
            "SELECT * FROM fts_documents WHERE generation_id = ? "
            "ORDER BY resource_type, resource_id",
            (generation_id,),
        ).fetchall()
        return tuple(_document_from_row(row) for row in rows)

    def count_documents(self, generation_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM fts_documents WHERE generation_id = ? AND doc_status = 'active'",
            (generation_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def sample_query(
        self, generation_id: str, match_expression: str, *, limit: int = 5
    ) -> tuple[int, ...]:
        """Verification-time sample query restricted to one generation."""
        rows = self._connection.execute(
            f"SELECT d.id FROM {INDEX_TABLE} f JOIN fts_documents d ON d.id = f.rowid "
            "WHERE f.fts_index MATCH ? AND d.generation_id = ? AND d.doc_status = 'active' "
            "LIMIT ?",
            (match_expression, generation_id, limit),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def search(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        generation_id: str,
        match_expression: str,
        space_group_id: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        statuses: Sequence[str] = ("active", "disputed", "open", "sealed", "inbox", "pinned"),
        valid_at_us: int | None = None,
        limit: int = 50,
    ) -> tuple[tuple[FtsDocumentRecord, float], ...]:
        """FTS5 match joined to live documents with structural pre-filters.

        Scope dims use the canonical downward-visibility rule
        (``D IS NULL OR (R NOT NULL AND D = R)``, exactly like
        :func:`scope_allows` and the claims SQL): a space/session request
        sees agent- and group-level documents, and a request dimension that
        is absent only ever matches documents stored at that broader scope.
        Generation membership, live status and valid time are likewise
        pushed into SQL BEFORE the rank/limit; the rank is the FTS5 bm25
        score normalized deterministically (lower bm25 = more relevant).
        Tombstone exclusion for indexed resources happens here AND again at
        rehydrate — the projection is never trusted.
        """
        status_placeholders = ", ".join("?" * len(statuses))
        sql = (
            f"SELECT d.*, bm25(f.{INDEX_TABLE}) AS rank FROM {INDEX_TABLE} f "
            "JOIN fts_documents d ON d.id = f.rowid "
            "WHERE f.fts_index MATCH ? AND d.tenant_id = ? AND d.agent_id = ? "
            f"AND d.generation_id = ? AND d.doc_status = 'active' "
            f"AND d.canonical_status IN ({status_placeholders}) "
        )
        params: list[Any] = [match_expression, tenant_id, agent_id, generation_id]
        params.extend(statuses)
        for column, value in (
            ("d.space_group_id", space_group_id),
            ("d.space_id", space_id),
            ("d.session_id", session_id),
        ):
            if value is None:
                # A request-side null is never a wildcard: only documents
                # stored AT this broader scope match (scope.py).
                sql += f"AND {column} IS NULL "
            else:
                sql += f"AND ({column} IS NULL OR {column} = ?) "
                params.append(value)
        sql += (
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
            "WHERE _rt.tenant_id = d.tenant_id AND _rt.resource_type = d.resource_type "
            "AND _rt.resource_id = d.resource_id) "
        )
        if valid_at_us is not None:
            sql += (
                "AND (d.valid_from_us IS NULL OR d.valid_from_us <= ?) "
                "AND (d.valid_until_us IS NULL OR d.valid_until_us > ?) "
            )
            params.extend([valid_at_us, valid_at_us])
        sql += "ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self._connection.execute(sql, tuple(params)).fetchall()
        results: list[tuple[FtsDocumentRecord, float]] = []
        # bm25() returns negative scores (more negative = more relevant);
        # map to a monotonic relevance in [0, 1) that INCREASES with
        # relevance: r = |bm25| / (1 + |bm25|), deterministic.
        for row in rows:
            raw_rank = float(row["rank"])
            magnitude = abs(raw_rank)
            relevance = magnitude / (1.0 + magnitude)
            results.append((_document_from_row(row), round(relevance, 6)))
        return tuple(results)


class RecallUsageRepository:
    """Recall request archives and host usage reports (ADR-0014 §7)."""

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def insert_request(
        self,
        *,
        request_id: str,
        tenant_id: str,
        agent_id: str,
        persona_revision: int,
        source_watermark: int,
        tombstone_watermark: int,
        schema_version: int,
        ranker_version: int,
        token_estimator_version: int,
        retrieved_count: int,
        returned_candidate_ids: Sequence[str],
        request_fingerprint: str,
        resource_ids: Sequence[str] = (),
        response_json: str | None = None,
        now_us: int | None = None,
    ) -> None:
        """Persist the Core-side usage stages and the served response.

        First write wins on the unique anchor: a transport retry REPLAYS the
        first response (the service checks before executing), and the stored
        returned set stays the reference the usage report validates against
        (ADR-0014 §7). ``response_json`` carries candidate bodies for the
        replay path; the erasure path nulls it when any referenced resource
        is invalidated, so erased content can never resurrect via replay.
        """
        self._connection.execute(
            "INSERT INTO recall_requests (id, tenant_id, agent_id, persona_revision, "
            "source_watermark, tombstone_watermark, schema_version, ranker_version, "
            "token_estimator_version, retrieved_count, returned_candidate_ids, "
            "returned_count, request_fingerprint, resource_ids_json, response_json, "
            "created_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, id) DO NOTHING",
            (
                request_id,
                tenant_id,
                agent_id,
                persona_revision,
                source_watermark,
                tombstone_watermark,
                schema_version,
                ranker_version,
                token_estimator_version,
                retrieved_count,
                canonical_json(list(returned_candidate_ids)),
                len(returned_candidate_ids),
                request_fingerprint,
                canonical_json(sorted(set(resource_ids))),
                response_json,
                now_us if now_us is not None else self._clock.now_us(),
            ),
        )

    def scrub_request_responses(self, tenant_id: str, resource_ids: Sequence[str]) -> int:
        """Erase the replayed response bodies that reference invalidated
        resources (Forget/erasure): the ids stay for usage accounting, the
        bodies must not survive the erasure (ADR-0014 §7, §13)."""
        if not resource_ids:
            return 0
        # Deletion-ledger replay runs against NOT-YET-MIGRATED legacy
        # snapshots (Schema < 7): no stored responses exist there to scrub.
        has_table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recall_requests'"
        ).fetchone()
        if has_table is None:
            return 0
        placeholders = ", ".join("?" for _ in resource_ids)
        cursor = self._connection.execute(
            "UPDATE recall_requests SET response_json = NULL WHERE tenant_id = ? "
            "AND response_json IS NOT NULL "
            f"AND EXISTS (SELECT 1 FROM json_each(resource_ids_json) je "
            f"WHERE je.value IN ({placeholders}))",
            (tenant_id, *resource_ids),
        )
        return int(cursor.rowcount)

    def get_request(self, tenant_id: str, request_id: str) -> sqlite3.Row | None:
        row = self._connection.execute(
            "SELECT * FROM recall_requests WHERE tenant_id = ? AND id = ?",
            (tenant_id, request_id),
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def returned_candidate_ids(self, tenant_id: str, request_id: str) -> tuple[str, ...] | None:
        row = self.get_request(tenant_id, request_id)
        if row is None:
            return None
        return tuple(json.loads(str(row["returned_candidate_ids"])))

    def insert_report(
        self,
        *,
        tenant_id: str,
        request_id: str,
        agent_id: str,
        app_instance_id: str,
        host_cycle_id: str,
        persona_revision: int,
        host_selected_ids: Sequence[str],
        model_visible_ids: Sequence[str],
        reported_at_us: int,
        now_us: int | None = None,
    ) -> tuple[str, bool]:
        """Idempotent report write; returns (report_id, created)."""
        existing = self._connection.execute(
            "SELECT id FROM recall_usage_reports WHERE tenant_id = ? AND request_id = ? "
            "AND host_cycle_id = ?",
            (tenant_id, request_id, host_cycle_id),
        ).fetchone()
        if existing is not None:
            return str(existing["id"]), False
        report_id = f"usage-{self._ids.new()}"
        self._connection.execute(
            "INSERT INTO recall_usage_reports (id, tenant_id, request_id, agent_id, "
            "app_instance_id, host_cycle_id, persona_revision, host_selected_ids, "
            "model_visible_ids, reported_at_us, created_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                tenant_id,
                request_id,
                agent_id,
                app_instance_id,
                host_cycle_id,
                persona_revision,
                canonical_json(list(host_selected_ids)),
                canonical_json(list(model_visible_ids)),
                reported_at_us,
                now_us if now_us is not None else self._clock.now_us(),
            ),
        )
        return report_id, True

    def get_report(self, tenant_id: str, request_id: str, host_cycle_id: str) -> sqlite3.Row | None:
        """The stored report for one idempotency identity, if any."""
        row = self._connection.execute(
            "SELECT * FROM recall_usage_reports WHERE tenant_id = ? AND request_id = ? "
            "AND host_cycle_id = ?",
            (tenant_id, request_id, host_cycle_id),
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def reports_for_request(self, tenant_id: str, request_id: str) -> tuple[sqlite3.Row, ...]:
        rows = self._connection.execute(
            "SELECT * FROM recall_usage_reports WHERE tenant_id = ? AND request_id = ? "
            "ORDER BY created_us, id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(cast("sqlite3.Row", row) for row in rows)

    def insert_activation(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        request_id: str,
        host_cycle_id: str,
        candidate_id: str,
        stage: str,
        resource_type: str,
        resource_id: str,
        resource_revision: int,
        activation_delta: float,
        applied: bool,
        reject_reason: str | None,
        now_us: int,
    ) -> tuple[str, bool]:
        existing = self._connection.execute(
            "SELECT id FROM recall_usage_activations WHERE tenant_id=? AND request_id=? "
            "AND host_cycle_id=? AND candidate_id=? AND stage=?",
            (tenant_id, request_id, host_cycle_id, candidate_id, stage),
        ).fetchone()
        if existing is not None:
            return str(existing["id"]), False
        activation_id = f"activation-{self._ids.new()}"
        self._connection.execute(
            "INSERT INTO recall_usage_activations "
            "(id,tenant_id,agent_id,request_id,host_cycle_id,candidate_id,stage,"
            "resource_type,resource_id,resource_revision,activation_delta,applied,"
            "reject_reason,created_us) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                activation_id,
                tenant_id,
                agent_id,
                request_id,
                host_cycle_id,
                candidate_id,
                stage,
                resource_type,
                resource_id,
                resource_revision,
                activation_delta,
                int(applied),
                reject_reason,
                now_us,
            ),
        )
        return activation_id, True
