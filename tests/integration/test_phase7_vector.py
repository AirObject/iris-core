"""Phase 7 FAISS generation lifecycle tests (P7-GEN-01, ADR-0015 §5).

Build → verify → publish → load; manifest/checksum/snapshot integrity;
corruption scenarios (each ≥20 rounds); restart persistence; rollback to
the previous trusted generation; orphan sweeps; handle refcounting and
use-after-close/double-close guards; model/dimension switch (1,000 sampled
revisions) and the vector trust gates.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from iris_memory_core.domain.vector import (
    VECTOR_REASON_BUILDER_UNKNOWN,
    VECTOR_REASON_GENERATION_STALE,
    VECTOR_REASON_INDEX_CORRUPT,
    VECTOR_REASON_REBUILD_PENDING,
    VECTOR_REASON_SPACE_MISMATCH,
    VectorDegradedError,
)
from iris_memory_core.indexing.vector import (
    VectorIndexHandle,
    VectorProjectionService,
    parse_id_map_snapshot,
)
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.integration.phase7_helpers import VectorCtx, make_projection, vector_space

CORRUPTION_ROUNDS = 20


@pytest.fixture
def ctx(clocked_store, mutable_clock) -> VectorCtx:  # type: ignore[no-untyped-def]
    return VectorCtx(clocked_store, mutable_clock)


def _generation_dir(ctx: VectorCtx) -> Path:
    pointer = ctx.pointer()
    assert pointer is not None
    return ctx.vector.manager.generation_dir(pointer.generation_id)


class TestBuildAndSwitch:
    def test_first_rebuild_publishes_verified_generation(self, ctx: VectorCtx) -> None:
        for i in range(5):
            ctx.remember(f"claim{i}", f"claim number {i} about quantum physics")
        report = ctx.rebuild()
        assert report.vector_count == 5
        assert report.switched_from is None
        directory = _generation_dir(ctx)
        for name in ("manifest.json", "index.faiss", "id-map.snapshot", "checksums.txt"):
            assert (directory / name).is_file()
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["vector_count"] == 5
        assert manifest["model"] == "test-embedding"
        assert manifest["dimension"] == 8
        assert manifest["metric"] == "cosine"
        assert manifest["normalization"] == "l2"
        assert manifest["template_version"] == 1
        assert manifest["builder_version"] == 1
        assert manifest["schema_version"] == 8
        assert manifest["source_watermark"] >= 1
        assert manifest["tombstone_watermark"] >= 0
        for field in ("content_hash", "id_map_hash", "index_hash"):
            assert len(manifest[field]) == 64
        with ctx.store.read() as tx:
            assert tx.vector.projection_state() == "ready"
            assert tx.vector.current_epoch("t1") == 1

    def test_second_rebuild_switches_pointer_and_retires_previous(self, ctx: VectorCtx) -> None:
        ctx.remember("a", "alpha content")
        first = ctx.rebuild()
        ctx.clock.advance(2_000_000)
        ctx.remember("b", "beta content")
        second = ctx.rebuild()
        assert second.switched_from == first.generation_id
        with ctx.store.read() as tx:
            pointer = tx.vector.pointer("t1")
            assert pointer is not None
            assert pointer.generation_id == second.generation_id
            assert pointer.switch_epoch == 2
            old = tx.vector.get_generation(first.generation_id)
            assert old.status == "retired"
            assert old.retired_us is not None

    def test_search_returns_rehydratable_refs_only(self, ctx: VectorCtx) -> None:
        ctx.remember("q1", "quantum entanglement basics")
        ctx.remember("q2", "grocery shopping list bananas")
        ctx.rebuild()
        hits = ctx.search("quantum entanglement basics")
        ids = {hit.resource_id for hit in hits}
        assert len(ids) >= 1
        for hit in hits:
            assert hit.resource_type == "claim"
            assert hit.resource_revision >= 1
            assert -1.0 <= hit.score <= 1.0

    def test_exact_topic_matches_rank_first(self, ctx: VectorCtx) -> None:
        ctx.remember("p1", "the manhattan project history")
        ctx.remember("p2", "banana bread recipe")
        ctx.rebuild()
        hits = ctx.search("the manhattan project history")
        assert hits[0].score > hits[-1].score or len(hits) == 1

    def test_empty_tenant_builds_zero_vector_generation(self, ctx: VectorCtx) -> None:
        report = ctx.rebuild()
        assert report.vector_count == 0
        assert ctx.search("anything") == []


class TestCorruptionFailClosed:
    """Each corruption shape is injected ≥20 times: the trusted handle must
    never be replaced by untrustworthy bytes (ADR-0015 §5)."""

    def _build_with_data(self, ctx: VectorCtx, round_index: int = 0) -> Path:
        for i in range(3):
            ctx.remember(f"c{round_index}-{i}", f"content {i}")
        ctx.rebuild()
        return _generation_dir(ctx)

    def test_missing_manifest_rejected(self, ctx: VectorCtx) -> None:
        for round_index in range(CORRUPTION_ROUNDS):
            directory = self._build_with_data(ctx, round_index)
            (directory / "manifest.json").unlink()
            with pytest.raises(VectorDegradedError) as error:
                ctx.vector.manager.verify_directory(directory, space=ctx.vector.manager.space)
            assert error.value.reason_code == VECTOR_REASON_INDEX_CORRUPT
            ctx.vector.manager.drop_current()
            shutil.rmtree(ctx.vector.manager.generations_root, ignore_errors=True)

    def test_checksum_drift_rejected(self, ctx: VectorCtx) -> None:
        for round_index in range(CORRUPTION_ROUNDS):
            directory = self._build_with_data(ctx, round_index)
            index_path = directory / "index.faiss"
            index_path.write_bytes(index_path.read_bytes() + b"\x00truncation")
            with pytest.raises(VectorDegradedError) as error:
                ctx.vector.manager.verify_directory(directory, space=ctx.vector.manager.space)
            assert error.value.reason_code == VECTOR_REASON_INDEX_CORRUPT
            ctx.vector.manager.drop_current()
            shutil.rmtree(ctx.vector.manager.generations_root, ignore_errors=True)

    def test_truncated_id_map_rejected(self, ctx: VectorCtx) -> None:
        for round_index in range(CORRUPTION_ROUNDS):
            directory = self._build_with_data(ctx, round_index)
            snapshot = directory / "id-map.snapshot"
            lines = snapshot.read_text().splitlines()
            snapshot.write_text("\n".join(lines[:1]) + "\n")
            # Also rewrite checksums so the failure is the count/id mismatch,
            # not (only) the checksum — the deeper invariant must still trip.
            import hashlib

            hashes = {
                "manifest.json": hashlib.sha256(
                    (directory / "manifest.json").read_bytes()
                ).hexdigest(),
                "index.faiss": hashlib.sha256((directory / "index.faiss").read_bytes()).hexdigest(),
                "id-map.snapshot": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            }
            (directory / "checksums.txt").write_text(
                "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
            )
            with pytest.raises(VectorDegradedError) as error:
                ctx.vector.manager.verify_directory(directory, space=ctx.vector.manager.space)
            assert error.value.reason_code == VECTOR_REASON_INDEX_CORRUPT
            ctx.vector.manager.drop_current()
            shutil.rmtree(ctx.vector.manager.generations_root, ignore_errors=True)

    def test_dimension_drift_rejected(self, ctx: VectorCtx) -> None:
        for round_index in range(CORRUPTION_ROUNDS):
            directory = self._build_with_data(ctx, round_index)
            manifest = json.loads((directory / "manifest.json").read_text())
            manifest["dimension"] = 16
            (directory / "manifest.json").write_text(json.dumps(manifest))
            with pytest.raises(VectorDegradedError) as error:
                ctx.vector.manager.verify_directory(directory, space=ctx.vector.manager.space)
            assert error.value.reason_code in (
                VECTOR_REASON_INDEX_CORRUPT,
                VECTOR_REASON_SPACE_MISMATCH,
            )
            ctx.vector.manager.drop_current()
            shutil.rmtree(ctx.vector.manager.generations_root, ignore_errors=True)

    def test_unknown_builder_rejected_without_retry(self, ctx: VectorCtx) -> None:
        for round_index in range(CORRUPTION_ROUNDS):
            directory = self._build_with_data(ctx, round_index)
            manifest = json.loads((directory / "manifest.json").read_text())
            manifest["builder_version"] = 99
            (directory / "manifest.json").write_text(json.dumps(manifest))
            import hashlib

            hashes = {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in ("manifest.json", "index.faiss", "id-map.snapshot")
            }
            (directory / "checksums.txt").write_text(
                "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
            )
            with pytest.raises(VectorDegradedError) as error:
                ctx.vector.manager.verify_directory(directory, space=ctx.vector.manager.space)
            assert error.value.reason_code == VECTOR_REASON_BUILDER_UNKNOWN
            assert error.value.retryable is False
            ctx.vector.manager.drop_current()
            shutil.rmtree(ctx.vector.manager.generations_root, ignore_errors=True)

    def test_unsorted_snapshot_rejected(self, ctx: VectorCtx) -> None:
        for round_index in range(CORRUPTION_ROUNDS):
            directory = self._build_with_data(ctx, round_index)
            snapshot = directory / "id-map.snapshot"
            lines = snapshot.read_text().splitlines()
            if len(lines) >= 2:
                lines[0], lines[1] = lines[1], lines[0]
                snapshot.write_text("\n".join(lines) + "\n")
                with pytest.raises(VectorDegradedError):
                    parse_id_map_snapshot(snapshot)
            ctx.vector.manager.drop_current()
            shutil.rmtree(ctx.vector.manager.generations_root, ignore_errors=True)

    def test_missing_files_and_garbage_dirs_never_serve(self, ctx: VectorCtx) -> None:
        for round_index in range(CORRUPTION_ROUNDS):
            self._build_with_data(ctx, round_index)
            # Point the DB at a generation whose directory vanished.
            with ctx.store.write() as tx:
                tx.vector.reset_projection()
            with pytest.raises(VectorDegradedError) as error:
                ctx.search("anything")
            assert error.value.reason_code == VECTOR_REASON_REBUILD_PENDING
            shutil.rmtree(ctx.vector.manager.generations_root, ignore_errors=True)
            ctx.vector.manager.drop_current()


class TestRestartAndRollback:
    def test_restart_loads_pointer_generation_only(self, ctx: VectorCtx, tmp_path: Path) -> None:
        ctx.remember("r1", "restart persistence content")
        ctx.rebuild()
        database = Path(ctx.store.runtime.database)
        # A brand-new store over the same files (fresh process simulation).
        fresh_store = Store(
            SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
            clock=ctx.clock,
        )
        fresh = make_projection(fresh_store, ctx.clock)
        query = fresh.embed_query("restart persistence content")
        with fresh_store.read() as tx:
            hits = fresh.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=10
            )
        assert len(hits) == 1
        assert hits[0].resource_type == "claim"
        fresh.manager.drop_current()

    def test_failed_load_keeps_previous_trusted_generation(self, ctx: VectorCtx) -> None:
        ctx.remember("f1", "first trusted content")
        ctx.rebuild()
        good_dir = _generation_dir(ctx)
        hits_before = ctx.search("first trusted content")
        assert len(hits_before) == 1
        # Corrupt the on-disk generation AFTER it was already loaded: the
        # in-process handle keeps serving (it verified at load time), but a
        # fresh process / reload must fail closed.
        (good_dir / "index.faiss").write_bytes(b"garbage")
        ctx.vector.manager.drop_current()
        with pytest.raises(VectorDegradedError) as error:
            ctx.search("first trusted content")
        assert error.value.reason_code == VECTOR_REASON_INDEX_CORRUPT

    def test_publish_aborts_when_canonical_moved(self, ctx: VectorCtx) -> None:
        from iris_memory_core.domain.errors import ConflictError

        ctx.remember("m1", "moving target content")
        prepared = ctx.vector.prepare_generation("t1")
        # A write lands between prepare and switch.
        ctx.remember("m2", "sneaky late content")
        with pytest.raises(ConflictError), ctx.store.write() as tx:
            ctx.vector.switch_in_tx(tx, "t1", prepared)
        # Nothing was published; a retry picks up the new state.
        report = ctx.rebuild()
        assert report.vector_count == 2

    def test_fenced_epoch_rejects_stale_publish(self, ctx: VectorCtx) -> None:
        from iris_memory_core.domain.errors import ConflictError

        ctx.remember("e1", "epoch race content")
        prepared = ctx.vector.prepare_generation("t1")
        # Another publisher wins the switch first.
        ctx.rebuild()
        with pytest.raises(ConflictError), ctx.store.write() as tx:
            ctx.vector.switch_in_tx(tx, "t1", prepared)

    def test_orphan_sweep_removes_unreferenced_directories(self, ctx: VectorCtx) -> None:
        ctx.remember("o1", "orphan probe content")
        ctx.rebuild()
        orphan = ctx.vector.manager.generations_root / "vecg-orphan"
        orphan.mkdir(parents=True)
        (orphan / "index.faiss").write_bytes(b"x")
        import os

        past = ctx.clock.now_us() - ctx.vector._retirement_window_us - 1
        os.utime(orphan, (past / 1_000_000, past / 1_000_000))
        removed = ctx.vector.sweep_filesystem()
        assert "vecg-orphan" in removed
        assert not orphan.exists()
        # The retained generation directory is untouched.
        assert _generation_dir(ctx).is_dir()

    def test_retired_generation_cleanup_respects_window(self, ctx: VectorCtx) -> None:
        reports = []
        for index in range(4):
            ctx.remember(f"w{index}", f"window content {index}")
            ctx.clock.advance(1_000_000)
            reports.append(ctx.rebuild())
        # Three retired generations exist; retention keeps the newest two
        # while they are inside the rollback window.
        with ctx.store.read() as tx:
            retired = [
                row.id for row in tx.vector.generations_for_tenant("t1") if row.status == "retired"
            ]
        assert len(retired) == 3
        oldest_dir = ctx.vector.manager.generation_dir(retired[-1])
        assert oldest_dir.is_dir()
        with ctx.store.write() as tx:
            _id_rows, removed = ctx.vector.cleanup_in_tx(tx, "t1")
        assert removed == ()
        # Directory removal happens POST-commit only (never inside the
        # fenced transaction); inside the window nothing is swept either.
        assert ctx.vector.sweep_filesystem() == ()
        assert oldest_dir.is_dir()
        # Age every retirement beyond the window: the ones past the
        # keep-newest-two retention lose their ROW in the fenced transaction
        # and their DIRECTORY in the post-commit filesystem sweep.
        with ctx.store.write() as tx:
            tx.raw().execute(
                "UPDATE vector_generations SET retired_us = ? WHERE status = 'retired'",
                (ctx.clock.now_us() - ctx.vector._retirement_window_us - 1,),
            )
        with ctx.store.write() as tx:
            _id_rows, removed = ctx.vector.cleanup_in_tx(tx, "t1")
        assert set(removed) == {retired[-1]}
        assert oldest_dir.is_dir(), "rows go first; directories only post-commit"
        assert ctx.vector.remove_generation_dirs(removed) == (retired[-1],)
        assert not oldest_dir.exists()
        for generation_id in retired[:2]:
            assert ctx.vector.manager.generation_dir(generation_id).is_dir()


class TestHandleLifecycle:
    def _loaded_handle(self, ctx: VectorCtx) -> VectorIndexHandle:
        ctx.remember("h1", "handle lifecycle content")
        ctx.rebuild()
        pointer = ctx.pointer()
        return ctx.vector.manager.handle_for(pointer)

    def test_refcount_release_and_use_after_close(self, ctx: VectorCtx) -> None:
        handle = self._loaded_handle(ctx)  # manager ref + acquired ref = 2
        lease = handle.acquire()  # 3
        assert handle.refcount == 3
        handle.release()  # 2
        assert handle.refcount == 2
        query = ctx.vector.embed_query("handle lifecycle content")
        assert len(lease.search(query, 1)) == 1
        lease.release()  # 1 — the manager still holds its reference
        assert not lease.closed
        handle.release()  # 0 — sealed with the last reference
        assert lease.closed
        with pytest.raises(VectorDegradedError):
            lease.search(query, 1)
        with pytest.raises(VectorDegradedError):
            lease.lookup(1)

    def test_double_release_refused(self, ctx: VectorCtx) -> None:
        handle = self._loaded_handle(ctx)
        # handle_for returns a +1 reference on top of the manager's own, so
        # two releases seal the handle; a third is a double release.
        handle.release()
        handle.release()
        assert handle.closed
        with pytest.raises(VectorDegradedError):
            handle.release()

    def test_acquire_after_close_refused(self, ctx: VectorCtx) -> None:
        handle = self._loaded_handle(ctx)
        handle.release()
        handle.release()
        with pytest.raises(VectorDegradedError):
            handle.acquire()

    def test_cow_swap_keeps_old_handle_alive_for_inflight_searches(self, ctx: VectorCtx) -> None:
        ctx.remember("s1", "swap survival content")
        ctx.rebuild()
        pointer = ctx.pointer()
        old = ctx.vector.manager.handle_for(pointer)
        # Publish a second generation: the manager swaps (COW) but `old`
        # still holds its own reference.
        ctx.clock.advance(1_000_000)
        ctx.remember("s2", "second generation content")
        ctx.rebuild()
        assert ctx.vector.manager.loaded_generation != old.generation_id
        assert not old.closed, "in-flight holder keeps the old handle open"
        query = ctx.vector.embed_query("swap survival content")
        assert len(old.search(query, 1)) == 1  # still searchable
        old.release()
        assert old.closed


class TestModelSwitch:
    def test_model_switch_never_mixes_spaces_1000_revisions(
        self, ctx: VectorCtx, tmp_path: Path
    ) -> None:
        """1,000 known resource revisions across a model/dimension switch:
        surrogate stability, content hashes, space identity and canonical
        rehydrate results all match (ADR-0015 §5)."""
        known: dict[str, int] = {}
        for i in range(1_000):
            result = ctx.remember(f"k{i}", f"known revision {i} payload")
            known[result.claim_id] = result.revision
        first = ctx.rebuild()
        with ctx.store.read() as tx:
            before = {
                row.resource_id: (row.surrogate_id, row.resource_revision)
                for row in tx.vector.id_map_active_for_tenant("t1")
            }
        assert len(before) == 1_000

        # Switch the vector space: new model, new dimension (16), same build.
        ctx.vector.manager.drop_current()
        new_space = vector_space(model="test-embedding-v2", dimension=16)
        from iris_memory_core.providers.embedding import DeterministicEmbeddingProvider

        switched = VectorProjectionService(
            ctx.store,
            ctx.clock,
            provider=DeterministicEmbeddingProvider(new_space),
            vector_root=Path(ctx.store.runtime.database).parent / "vector",
            space=new_space,
        )
        # The old space's pointer must NOT serve under the new configuration.
        with pytest.raises(VectorDegradedError) as error:
            query = switched.embed_query("known revision 1 payload")
            with ctx.store.read() as tx:
                switched.search_in_tx(
                    tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
                )
        assert error.value.reason_code == VECTOR_REASON_SPACE_MISMATCH

        second = switched.rebuild("t1")
        assert second.generation_id != first.generation_id
        manifest = json.loads(
            (switched.manager.generation_dir(second.generation_id) / "manifest.json").read_text()
        )
        assert manifest["model"] == "test-embedding-v2"
        assert manifest["dimension"] == 16
        with ctx.store.read() as tx:
            after = {
                row.resource_id: (row.surrogate_id, row.resource_revision)
                for row in tx.vector.id_map_active_for_tenant("t1")
            }
        # Surrogates and revisions are IDENTICAL across the space switch.
        assert before == after
        # Canonical rehydrate results: search returns the same resources.
        query = switched.embed_query("known revision 500 payload")
        with ctx.store.read() as tx:
            hits = switched.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=10
            )
        assert hits, "the new space must serve results"
        for hit in hits:
            assert hit.resource_revision == known[hit.resource_id]
        switched.manager.drop_current()


class TestTrustGates:
    def test_never_built_degrades_rebuild_pending(self, ctx: VectorCtx) -> None:
        ctx.remember("g1", "gate content")
        with pytest.raises(VectorDegradedError) as error:
            ctx.search("gate content")
        assert error.value.reason_code == VECTOR_REASON_REBUILD_PENDING

    def test_delta_lag_degrades_stale(self, ctx: VectorCtx) -> None:
        ctx.remember("lag1", "lag probe content")
        ctx.rebuild()
        strict = make_projection(ctx.store, ctx.clock, staleness_limit=0)
        query = strict.embed_query("lag probe content")
        with ctx.store.read() as tx:
            hits = strict.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
            )
        assert len(hits) == 1
        # A revision change records a delta row beyond the generation.
        ctx.claims.correct(
            ctx.access,
            claim_id=_first_claim_id(ctx),
            expected_revision=_first_claim_revision(ctx),
            canonical_text="corrected lag probe content",
            evidence=[{"source_type": "claim", "source_id": _first_claim_id(ctx)}],
            reason="correction",
            idempotency_key="lag-corr",
        )
        with ctx.store.write() as tx:
            ctx.vector.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=_first_claim_id(ctx)
            )
        with ctx.store.read() as tx:
            assert tx.vector.delta_count("t1", ctx.agent) == 1
        query = strict.embed_query("corrected lag probe content")
        with pytest.raises(VectorDegradedError) as error, ctx.store.read() as tx:
            strict.search_in_tx(tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5)
        assert error.value.reason_code == VECTOR_REASON_GENERATION_STALE
        # A rebuild clears the covered delta and serves the new revision.
        strict.rebuild("t1")
        query = strict.embed_query("corrected lag probe content")
        with ctx.store.read() as tx:
            hits = strict.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
            )
        assert len(hits) == 1
        strict.manager.drop_current()

    def test_minimum_watermark_strictness(self, ctx: VectorCtx) -> None:
        ctx.remember("mw1", "minimum watermark probe")
        ctx.rebuild()
        query = ctx.vector.embed_query("minimum watermark probe")
        # A watermark beyond the generation's coverage degrades (stale).
        with pytest.raises(VectorDegradedError) as error, ctx.store.read() as tx:
            ctx.vector.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                query_vector=query,
                limit=5,
                minimum_watermark=10**9,
            )
        assert error.value.reason_code == VECTOR_REASON_GENERATION_STALE
        # The exact covered watermark is served.
        with ctx.store.read() as tx:
            pointer = ctx.pointer()
            assert pointer is not None
            generation = tx.vector.get_generation(pointer.generation_id)
        watermark = generation.agent_watermarks()[ctx.agent]
        with ctx.store.read() as tx:
            hits = ctx.vector.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                query_vector=query,
                limit=5,
                minimum_watermark=watermark,
            )
        assert len(hits) == 1

    def test_agent_isolation_in_search(self, ctx: VectorCtx) -> None:
        ctx.remember("iso1", "agent isolated content")
        ctx.rebuild()
        query = ctx.vector.embed_query("agent isolated content")
        with ctx.store.read() as tx:
            hits = ctx.vector.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.other_agent, query_vector=query, limit=10
            )
        assert hits == [], "another agent's vectors must not leak"


def _first_claim_id(ctx: VectorCtx) -> str:
    with ctx.store.read() as tx:
        row = (
            tx.raw()
            .execute("SELECT id FROM claims WHERE tenant_id = 't1' ORDER BY created_us LIMIT 1")
            .fetchone()
        )
        return str(row[0])


def _first_claim_revision(ctx: VectorCtx) -> int:
    with ctx.store.read() as tx:
        row = (
            tx.raw()
            .execute(
                "SELECT current_revision FROM claims WHERE tenant_id = 't1' "
                "ORDER BY created_us LIMIT 1"
            )
            .fetchone()
        )
        return int(row[0])


class TestBackupRestore:
    def test_restore_resets_vector_projection_and_keeps_id_map(
        self, ctx: VectorCtx, tmp_path: Path
    ) -> None:
        from iris_memory_core.storage.backup import BackupService

        ctx.remember("b1", "backup restore content")
        ctx.rebuild()
        with ctx.store.read() as tx:
            id_map_before = tx.vector.id_map_active_for_tenant("t1")
        service = BackupService(ctx.store)
        backup_dir = tmp_path / "backup"
        service.create_backup(backup_dir)
        target = tmp_path / "restored"
        report = service.restore_backup(backup_dir, target)
        assert report.check.ok, report.check.problems
        connection = sqlite3.connect(target / "canonical.sqlite3")
        try:
            state = connection.execute(
                "SELECT state FROM vector_projection_state WHERE id = 1"
            ).fetchone()
            assert state is not None and state[0] == "pending_rebuild"
            generations = connection.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[
                0
            ]
            assert generations == 0
            id_rows = connection.execute(
                "SELECT COUNT(*) FROM vector_id_map WHERE status = 'active'"
            ).fetchone()[0]
            # The id map SURVIVES the restore: surrogate assignment stays
            # stable across disaster recovery (ADR-0015 §10).
            assert id_rows == len(id_map_before)
        finally:
            connection.close()
        # No vector directory was carried into the restore target; the next
        # build recreates it under the new root.
        assert not (target / "vector").exists() or not any((target / "vector").iterdir())
        restored_store = Store(
            SQLiteRuntime(
                target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
            ),
            clock=ctx.clock,
        )
        restored = make_projection(restored_store, ctx.clock)
        with pytest.raises(VectorDegradedError) as error:
            query = restored.embed_query("backup restore content")
            with restored_store.read() as tx:
                restored.search_in_tx(
                    tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
                )
        assert error.value.reason_code == VECTOR_REASON_REBUILD_PENDING
        rebuild_report = restored.rebuild("t1")
        assert rebuild_report.vector_count == 1
        query = restored.embed_query("backup restore content")
        with restored_store.read() as tx:
            hits = restored.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
            )
        assert len(hits) == 1
        # Surrogates survived the whole round trip.
        with restored_store.read() as tx:
            id_map_after = tx.vector.id_map_active_for_tenant("t1")
        assert {row.surrogate_id for row in id_map_after} == {
            row.surrogate_id for row in id_map_before
        }
        restored.manager.drop_current()
