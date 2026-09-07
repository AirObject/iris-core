"""Phase 7 repositories: vector projection state, surrogate ID map, delta
ledger and the FAISS generation pointer (§22.2-22.4, ADR-0015).

Every method runs inside the caller's short transaction and returns DOMAIN
records. FAISS index bytes live on the filesystem; these tables hold the
authoritative pointer, the space identity, the surrogate map and the delta
ledger. Generations are immutable once verified; the per-tenant pointer flips
with a fencing epoch CAS in the same transaction that retires the previous
generation and clears the delta ledger.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import ConflictError, NotFoundError
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.vector import (
    SURROGATE_ID_MAX,
    VectorCurrentPointer,
    VectorGenerationRecord,
    VectorIdMapRecord,
    VectorSpaceConfig,
    validate_surrogate_id,
)

#: Surrogate ids bound per UPDATE in :meth:`VectorRepository.id_map_stamp_generation`.
_STAMP_CHUNK = 500


def _id_map_from_row(row: sqlite3.Row) -> VectorIdMapRecord:
    return VectorIdMapRecord(
        tenant_id=str(row["tenant_id"]),
        resource_type=str(row["resource_type"]),
        resource_id=str(row["resource_id"]),
        resource_revision=int(row["resource_revision"]),
        surrogate_id=int(row["surrogate_id"]),
        agent_id=str(row["agent_id"]),
        model=str(row["model"]),
        dimension=int(row["dimension"]),
        content_hash=str(row["content_hash"]),
        status=str(row["status"]),
        created_us=int(row["created_us"]),
        invalidated_us=row["invalidated_us"],
        incorporated_generation=row["incorporated_generation"],
    )


def _pointer_from_row(row: sqlite3.Row) -> VectorCurrentPointer:
    return VectorCurrentPointer(
        tenant_id=str(row["tenant_id"]),
        generation_id=str(row["generation_id"]),
        switch_epoch=int(row["switch_epoch"]),
        space=VectorSpaceConfig(
            model=str(row["model"]),
            dimension=int(row["dimension"]),
            metric=str(row["metric"]),
            normalization=str(row["normalization"]),
            template_version=int(row["template_version"]),
            builder_version=int(row["builder_version"]),
        ),
        source_watermark=int(row["source_watermark"]),
        tombstone_watermark=int(row["tombstone_watermark"]),
        switched_us=int(row["switched_us"]),
    )


def _generation_from_row(row: sqlite3.Row) -> VectorGenerationRecord:
    return VectorGenerationRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        space=VectorSpaceConfig(
            model=str(row["model"]),
            dimension=int(row["dimension"]),
            metric=str(row["metric"]),
            normalization=str(row["normalization"]),
            template_version=int(row["template_version"]),
            builder_version=int(row["builder_version"]),
        ),
        source_watermark=int(row["source_watermark"]),
        tombstone_watermark=int(row["tombstone_watermark"]),
        vector_count=int(row["vector_count"]),
        content_checksum=str(row["content_checksum"]),
        id_map_checksum=str(row["id_map_checksum"]),
        index_checksum=str(row["index_checksum"]),
        agent_watermarks_json=str(row["agent_watermarks_json"]),
        status=str(row["status"]),
        created_us=int(row["created_us"]),
        verified_us=int(row["verified_us"]),
        retired_us=row["retired_us"],
    )


class VectorRepository:
    """Vector generations, pointer, id map, delta ledger and global state."""

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- global projection state ---------------------------------------------

    def projection_state(self) -> str:
        row = self._connection.execute(
            "SELECT state FROM vector_projection_state WHERE id = 1"
        ).fetchone()
        return str(row["state"]) if row is not None else "never_built"

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None:
        if state not in ("never_built", "ready", "pending_rebuild"):
            raise ConflictError(f"unknown vector projection state: {state!r}")
        self._connection.execute(
            "INSERT INTO vector_projection_state (id, state, marked_us, last_surrogate_id) "
            "VALUES (1, ?, ?, 0) "
            "ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
            "marked_us = excluded.marked_us",
            (state, now_us if now_us is not None else self._clock.now_us()),
        )

    def reset_projection(self, *, now_us: int | None = None) -> None:
        """Restore reset (ADR-0015 §10): generations/pointer/delta are wiped
        for EVERY tenant — a restore replaces the whole database, so the
        reset is whole-database too; the id map and the surrogate counter
        SURVIVE so surrogate assignment stays stable across restores. State
        flips to pending_rebuild."""
        from iris_memory_core.storage.provider_restore import reset_provider_projection

        reset_provider_projection(
            self._connection, now_us=now_us if now_us is not None else self._clock.now_us()
        )
        self._connection.execute("DELETE FROM vector_delta_ledger")
        self._connection.execute("DELETE FROM vector_current")
        self._connection.execute("DELETE FROM vector_generations")
        self.set_projection_state("pending_rebuild", now_us=now_us)

    # -- surrogate allocation --------------------------------------------------

    def allocate_surrogate_ids(self, count: int) -> tuple[int, ...]:
        """Server-side monotonic allocation of ``count`` surrogate IDs.

        The per-tenant counter lives in ``vector_projection_state`` and only
        ever moves forward inside the caller's write transaction: IDs are
        never reused for a different resource and never derived from UUIDs
        (ADR-0015 §3)."""
        if count < 0:
            raise ConflictError("surrogate count must be non-negative")
        if count == 0:
            return ()
        row = self._connection.execute(
            "SELECT last_surrogate_id FROM vector_projection_state WHERE id = 1"
        ).fetchone()
        current = int(row["last_surrogate_id"]) if row is not None else 0
        if row is None:
            self.set_projection_state("never_built")
        first = current + 1
        last = current + count
        if last > SURROGATE_ID_MAX:
            raise ConflictError("surrogate id space exhausted for this tenant")
        self._connection.execute(
            "UPDATE vector_projection_state SET last_surrogate_id = ? WHERE id = 1",
            (last,),
        )
        return tuple(range(first, last + 1))

    # -- id map -----------------------------------------------------------------

    def id_map_get(
        self, tenant_id: str, resource_type: str, resource_id: str
    ) -> VectorIdMapRecord | None:
        row = self._connection.execute(
            "SELECT * FROM vector_id_map WHERE tenant_id = ? AND resource_type = ? "
            "AND resource_id = ?",
            (tenant_id, resource_type, resource_id),
        ).fetchone()
        return _id_map_from_row(row) if row is not None else None

    def id_map_by_surrogate(self, tenant_id: str, surrogate_id: int) -> VectorIdMapRecord | None:
        row = self._connection.execute(
            "SELECT * FROM vector_id_map WHERE tenant_id = ? AND surrogate_id = ?",
            (tenant_id, surrogate_id),
        ).fetchone()
        return _id_map_from_row(row) if row is not None else None

    def id_map_count(self, tenant_id: str, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM vector_id_map WHERE tenant_id = ?"
        if active_only:
            sql += " AND status = 'active'"
        row = self._connection.execute(sql, (tenant_id,)).fetchone()
        return int(row[0]) if row is not None else 0

    def id_map_active_for_tenant(self, tenant_id: str) -> tuple[VectorIdMapRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM vector_id_map WHERE tenant_id = ? AND status = 'active' "
            "ORDER BY surrogate_id",
            (tenant_id,),
        ).fetchall()
        return tuple(_id_map_from_row(row) for row in rows)

    def id_map_upsert(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        resource_revision: int,
        surrogate_id: int,
        agent_id: str,
        space: VectorSpaceConfig,
        content_hash: str,
        now_us: int | None = None,
        incorporated_generation: str | None = None,
    ) -> VectorIdMapRecord:
        """Insert or refresh one mapping inside the caller's transaction.

        A revision advance reactivates the row with the new revision and
        clears the invalidation stamp — logical serviceability follows the
        canonical current revision immediately; the FAISS content catches up
        via the delta ledger/rebuild (never by mutating the live handle).
        The upsert also CLEARS the membership stamp: a refreshed row is not
        provably part of any published generation until the switch
        transaction stamps it (ADR-0015 §4)."""
        validate_surrogate_id(surrogate_id)
        stamp = now_us if now_us is not None else self._clock.now_us()
        self._connection.execute(
            "INSERT INTO vector_id_map (tenant_id, resource_type, resource_id, "
            "resource_revision, surrogate_id, agent_id, model, dimension, "
            "content_hash, status, created_us, invalidated_us, incorporated_generation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?) "
            "ON CONFLICT(tenant_id, resource_type, resource_id) DO UPDATE SET "
            "resource_revision = excluded.resource_revision, "
            "surrogate_id = excluded.surrogate_id, "
            "agent_id = excluded.agent_id, "
            "model = excluded.model, dimension = excluded.dimension, "
            "content_hash = excluded.content_hash, status = 'active', "
            "invalidated_us = NULL, "
            "incorporated_generation = excluded.incorporated_generation",
            (
                tenant_id,
                resource_type,
                resource_id,
                resource_revision,
                surrogate_id,
                agent_id,
                space.model,
                space.dimension,
                content_hash,
                stamp,
                incorporated_generation,
            ),
        )
        stored = self.id_map_get(tenant_id, resource_type, resource_id)
        assert stored is not None
        return stored

    def id_map_stamp_generation(
        self,
        tenant_id: str,
        generation_id: str,
        surrogates: Sequence[int],
    ) -> int:
        """Stamp id-map rows as incorporated by ``generation_id``.

        Called ONLY inside the pointer-switch transaction: the stamp, the
        exact-content survivor check and the fenced CAS commit atomically, so
        ``incorporated_generation == pointer.generation_id`` is a durable
        proof the CURRENT generation contains the row at its stamped
        revision. Only active rows are stamped (invalid rows can never satisfy
        the skip path regardless); rows at a different revision keep their
        revision — the skip check compares both."""
        stamped = 0
        for offset in range(0, len(surrogates), _STAMP_CHUNK):
            chunk = surrogates[offset : offset + _STAMP_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            cursor = self._connection.execute(
                "UPDATE vector_id_map SET incorporated_generation = ? "
                f"WHERE tenant_id = ? AND status = 'active' AND surrogate_id IN ({placeholders})",
                (generation_id, tenant_id, *chunk),
            )
            stamped += cursor.rowcount
        return stamped

    def id_map_invalidate(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        now_us: int | None = None,
    ) -> int:
        """Logically invalidate a mapping (tombstone/correct/forget): the old
        revision is unserviceable from this instant (ADR-0015 §3)."""
        stamp = now_us if now_us is not None else self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE vector_id_map SET status = 'invalid', invalidated_us = ? "
            "WHERE tenant_id = ? AND resource_type = ? AND resource_id = ? "
            "AND status = 'active'",
            (stamp, tenant_id, resource_type, resource_id),
        )
        return cursor.rowcount

    def id_map_delete_invalid(self, tenant_id: str, *, limit: int = 500) -> int:
        """Physical deletion of logically-invalidated mappings (async)."""
        rows = self._connection.execute(
            "SELECT resource_type, resource_id FROM vector_id_map WHERE tenant_id = ? "
            "AND status = 'invalid' ORDER BY invalidated_us LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
        for row in rows:
            self._connection.execute(
                "DELETE FROM vector_id_map WHERE tenant_id = ? AND resource_type = ? "
                "AND resource_id = ?",
                (tenant_id, str(row["resource_type"]), str(row["resource_id"])),
            )
        return len(rows)

    # -- delta ledger -------------------------------------------------------------

    def delta_upsert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        resource_type: str,
        resource_id: str,
        resource_revision: int,
        op: str,
        source_watermark: int,
        now_us: int | None = None,
    ) -> None:
        if op not in ("upsert", "remove"):
            raise ConflictError(f"unknown delta op: {op!r}")
        stamp = now_us if now_us is not None else self._clock.now_us()
        self._connection.execute(
            "INSERT INTO vector_delta_ledger (tenant_id, agent_id, resource_type, "
            "resource_id, resource_revision, op, source_watermark, created_us, "
            "updated_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, resource_type, resource_id) DO UPDATE SET "
            "agent_id = excluded.agent_id, resource_revision = excluded.resource_revision, "
            "op = excluded.op, source_watermark = excluded.source_watermark, "
            "updated_us = excluded.updated_us",
            (
                tenant_id,
                agent_id,
                resource_type,
                resource_id,
                resource_revision,
                op,
                source_watermark,
                stamp,
                stamp,
            ),
        )

    def delta_count(self, tenant_id: str, agent_id: str | None = None) -> int:
        """Unincorporated delta rows — the rebuild-backlog half of the vector
        freshness proof (ADR-0015 §4)."""
        if agent_id is None:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM vector_delta_ledger WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM vector_delta_ledger WHERE tenant_id = ? AND agent_id = ?",
                (tenant_id, agent_id),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def delta_clear(self, tenant_id: str) -> int:
        """Clear every delta row of the tenant (full-coverage form)."""
        cursor = self._connection.execute(
            "DELETE FROM vector_delta_ledger WHERE tenant_id = ?", (tenant_id,)
        )
        return cursor.rowcount

    def insert_generation(
        self,
        *,
        tenant_id: str,
        space: VectorSpaceConfig,
        source_watermark: int,
        tombstone_watermark: int,
        vector_count: int,
        content_checksum: str,
        id_map_checksum: str,
        index_checksum: str,
        agent_watermarks: dict[str, int],
        generation_id: str | None = None,
        now_us: int | None = None,
    ) -> VectorGenerationRecord:
        """Insert one verified generation row.

        ``generation_id`` pins the row to the id the on-disk generation
        directory already carries (the publisher names the directory before
        the switch transaction); the storage-generated id is only a fallback
        for callers without files."""
        resolved = generation_id or f"vecg-{self._ids.new()}"
        stamp = now_us if now_us is not None else self._clock.now_us()
        self._connection.execute(
            "INSERT INTO vector_generations (id, tenant_id, model, dimension, metric, "
            "normalization, template_version, builder_version, source_watermark, "
            "tombstone_watermark, vector_count, content_checksum, id_map_checksum, "
            "index_checksum, agent_watermarks_json, status, created_us, verified_us, "
            "retired_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'verified', ?, ?, NULL)",
            (
                resolved,
                tenant_id,
                space.model,
                space.dimension,
                space.metric,
                space.normalization,
                space.template_version,
                space.builder_version,
                source_watermark,
                tombstone_watermark,
                vector_count,
                content_checksum,
                id_map_checksum,
                index_checksum,
                canonical_json(agent_watermarks),
                stamp,
                stamp,
            ),
        )
        return self.get_generation(resolved)

    def get_generation(self, generation_id: str) -> VectorGenerationRecord:
        row = self._connection.execute(
            "SELECT * FROM vector_generations WHERE id = ?", (generation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("vector generation not found")
        return _generation_from_row(row)

    def all_generation_ids(self) -> tuple[str, ...]:
        """Every generation id of every tenant — the sweep's retained set is
        GLOBAL: the generations root is shared by all tenants, so a
        per-tenant set would delete other tenants' serving directories."""
        rows = self._connection.execute("SELECT id FROM vector_generations ORDER BY id").fetchall()
        return tuple(str(row["id"]) for row in rows)

    def all_pointer_generation_ids(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT generation_id FROM vector_current ORDER BY tenant_id"
        ).fetchall()
        return tuple(str(row["generation_id"]) for row in rows)

    def id_map_invalidate_tombstoned(self, tenant_id: str, *, now_us: int | None = None) -> int:
        """Invalidate id-map rows whose resource carries a tombstone.

        Called at publish time so the map never claims serviceability for a
        resource the generation provably excludes — a late ``vector.apply``
        then sees the row already invalid and records no spurious delta."""
        stamp = now_us if now_us is not None else self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE vector_id_map SET status = 'invalid', invalidated_us = ? "
            "WHERE tenant_id = ? AND status = 'active' AND EXISTS ("
            "SELECT 1 FROM resource_tombstones _rt WHERE _rt.tenant_id = vector_id_map.tenant_id "
            "AND _rt.resource_type = vector_id_map.resource_type "
            "AND _rt.resource_id = vector_id_map.resource_id)",
            (stamp, tenant_id),
        )
        return cursor.rowcount

    def generations_for_tenant(self, tenant_id: str) -> tuple[VectorGenerationRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM vector_generations WHERE tenant_id = ? ORDER BY created_us, id",
            (tenant_id,),
        ).fetchall()
        return tuple(_generation_from_row(row) for row in rows)

    def reactivate_generation(
        self,
        tenant_id: str,
        generation: VectorGenerationRecord,
        *,
        expected_epoch: int,
        source_watermark: int,
        tombstone_watermark: int,
        agent_watermarks: dict[str, int],
    ) -> VectorGenerationRecord:
        """Called only after exact retained-content verification in the same transaction.

        Immutable bytes/digests/space stay unchanged. Lifecycle and proven watermarks
        advance together with the caller's pointer/configuration publication fence.
        """
        current = self.get_generation(generation.id)
        if (
            current != generation
            or current.tenant_id != tenant_id
            or self.current_epoch(tenant_id) != expected_epoch
            or current.status not in {"retired", "verified"}
            or source_watermark < current.source_watermark
            or tombstone_watermark < current.tombstone_watermark
        ):
            raise ConflictError("retained vector generation moved")
        watermarks = current.agent_watermarks()
        for agent, watermark in agent_watermarks.items():
            watermarks[agent] = max(watermarks.get(agent, 0), watermark)
        self._connection.execute(
            "UPDATE vector_generations SET status='verified',retired_us=NULL,verified_us=?,"
            "source_watermark=?,tombstone_watermark=?,agent_watermarks_json=? "
            "WHERE id=? AND tenant_id=?",
            (
                self._clock.now_us(),
                source_watermark,
                tombstone_watermark,
                canonical_json(watermarks),
                generation.id,
                tenant_id,
            ),
        )
        return self.get_generation(generation.id)

    def retire_generation(self, generation_id: str, *, now_us: int | None = None) -> int:
        stamp = now_us if now_us is not None else self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE vector_generations SET status = 'retired', retired_us = ? "
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
        """Remove retired generations beyond the retention window.

        ``older_than_us`` enforces the rollback window: a retired generation
        whose retirement stamp is newer stays on disk until it ages out
        (ADR-0015 §5)."""
        rows = self._connection.execute(
            "SELECT id, retired_us FROM vector_generations WHERE tenant_id = ? "
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
                "DELETE FROM vector_generations WHERE id = ?", (generation_id,)
            )
        return tuple(stale)

    # -- current pointer ---------------------------------------------------------

    def pointer(self, tenant_id: str) -> VectorCurrentPointer | None:
        row = self._connection.execute(
            "SELECT * FROM vector_current WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return _pointer_from_row(row) if row is not None else None

    def current_epoch(self, tenant_id: str) -> int:
        row = self._connection.execute(
            "SELECT switch_epoch FROM vector_current WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return int(row["switch_epoch"]) if row is not None else 0

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: VectorGenerationRecord,
        expected_epoch: int,
        now_us: int | None = None,
    ) -> VectorCurrentPointer:
        """Fenced CAS pointer switch (ADR-0015 §5).

        The epoch must be exactly ``expected_epoch`` (what the publisher
        observed when it started); a concurrent newer switch makes this CAS
        fail loudly instead of silently rolling the pointer back. The flip,
        the retiring of the outgoing generation, the delta clear and the
        ready marker all land in ONE transaction.
        """
        stamp = now_us if now_us is not None else self._clock.now_us()
        previous = self.pointer(tenant_id)
        if previous is not None:
            if previous.switch_epoch != expected_epoch:
                raise ConflictError(
                    "vector pointer epoch advanced during publish (fenced)",
                    details={"expected_epoch": expected_epoch},
                )
            new_epoch = previous.switch_epoch + 1
            if previous.generation_id != generation.id:
                self.retire_generation(previous.generation_id, now_us=stamp)
        else:
            if expected_epoch != 0:
                raise ConflictError(
                    "vector pointer epoch precondition failed",
                    details={"expected_epoch": expected_epoch},
                )
            new_epoch = 1
        self._connection.execute(
            "INSERT INTO vector_current (tenant_id, generation_id, switch_epoch, model, "
            "dimension, metric, normalization, template_version, builder_version, "
            "source_watermark, tombstone_watermark, switched_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET generation_id = excluded.generation_id, "
            "switch_epoch = excluded.switch_epoch, model = excluded.model, "
            "dimension = excluded.dimension, metric = excluded.metric, "
            "normalization = excluded.normalization, "
            "template_version = excluded.template_version, "
            "builder_version = excluded.builder_version, "
            "source_watermark = excluded.source_watermark, "
            "tombstone_watermark = excluded.tombstone_watermark, "
            "switched_us = excluded.switched_us",
            (
                tenant_id,
                generation.id,
                new_epoch,
                generation.space.model,
                generation.space.dimension,
                generation.space.metric,
                generation.space.normalization,
                generation.space.template_version,
                generation.space.builder_version,
                generation.source_watermark,
                generation.tombstone_watermark,
                stamp,
            ),
        )
        self.delta_clear(tenant_id)
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
            "model": pointer.space.model,
            "dimension": pointer.space.dimension,
            "source_watermark": pointer.source_watermark,
            "tombstone_watermark": pointer.tombstone_watermark,
        }


__all__ = [
    "VectorCurrentPointer",
    "VectorGenerationRecord",
    "VectorRepository",
]
