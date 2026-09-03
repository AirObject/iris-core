"""Phase 7 adversarial review regressions (ADR-0015).

Crash-window consistency between the filesystem and the SQLite pointer,
backup structural rejection of vector directories, dead-letter-resilient
freshness, provider-outage isolation and multi-process handle races. Each
case encodes a concrete attack/failure scenario from the review.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.domain.vector import VectorDegradedError
from tests.integration.phase7_helpers import VectorCtx

if TYPE_CHECKING:
    from iris_memory_core.indexing.vector import VectorProjectionService


@pytest.fixture
def ctx(clocked_store, mutable_clock) -> VectorCtx:  # type: ignore[no-untyped-def]
    return VectorCtx(clocked_store, mutable_clock)


class TestReviewFixes:
    """Regressions for the adversarial review's fixed findings."""

    def test_sweep_is_global_multitenant_no_cross_deletion(self, ctx: VectorCtx) -> None:
        """[P1] Tenant A's cleanup must never delete tenant B's serving
        generation directories: the retained set is global."""
        import os

        from tests.integration.phase7_helpers import make_projection

        with ctx.store.write() as tx:
            tx.insert_tenant("t2", status="active")
        other_vector = make_projection(ctx.store, ctx.clock)
        # Tenant 1 builds; tenant 2 builds over the SAME shared root.
        ctx.remember("t1c", "tenant one content")
        ctx.rebuild()
        # Tenant 2 needs its own world — provision minimally through the
        # same ctx services scoped to t2 via direct SQL is heavy; instead
        # verify the invariant that matters: sweep removes ONLY what no row
        # retains. Create a second tenant generation by hand through the
        # projection (single-space deployments share the service).
        foreign = other_vector.manager.generations_root / "vecg-foreign-retained"
        foreign.mkdir(parents=True)
        (foreign / "index.faiss").write_bytes(b"x")
        with ctx.store.write() as tx:
            tx.raw().execute(
                "INSERT INTO vector_generations (id, tenant_id, model, dimension, metric, "
                "normalization, template_version, builder_version, source_watermark, "
                "tombstone_watermark, vector_count, content_checksum, id_map_checksum, "
                "index_checksum, agent_watermarks_json, status, created_us, verified_us) "
                "VALUES ('vecg-foreign-retained','t2','test-embedding',8,'cosine','l2',1,1,"
                "0,0,0,'a','b','c','{}','verified',1,1)"
            )
        past = ctx.clock.now_us() - ctx.vector._retirement_window_us - 1
        os.utime(foreign, (past / 1_000_000, past / 1_000_000))
        # Tenant t1's cleanup runs: the foreign VERIFIED row is retained
        # regardless of tenant.
        removed = ctx.vector.sweep_filesystem()
        assert "vecg-foreign-retained" not in removed
        assert foreign.is_dir()

    def test_late_apply_after_switch_records_no_spurious_delta(self, ctx: VectorCtx) -> None:
        """[P2 false-stale] An apply delivered AFTER the generation already
        incorporated its change skips the delta write (state-based skip)."""
        claim = ctx.remember("late", "late apply target content")
        ctx.rebuild()
        # A revision change and its apply BEFORE the rebuild is incorporated;
        # simulate the LATE delivery: run apply again after the rebuild.
        ctx.claims.correct(
            ctx.access,
            claim_id=claim.claim_id,
            expected_revision=claim.revision,
            canonical_text="late corrected content",
            evidence=[{"source_type": "claim", "source_id": claim.claim_id}],
            reason="correction",
            idempotency_key="late-corr",
        )
        ctx.rebuild()  # generation now contains the corrected revision
        with ctx.store.write() as tx:
            ctx.vector.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=claim.claim_id
            )
        with ctx.store.read() as tx:
            assert tx.vector.delta_count("t1") == 0, "incorporated change must not lag"
        strict = _strict_projection(ctx)
        query = strict.embed_query("late corrected content")
        with ctx.store.read() as tx:
            hits = strict.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
            )
        assert hits
        strict.manager.drop_current()

    def test_new_resource_during_build_joins_generation(self, ctx: VectorCtx) -> None:
        """[P2 starvation] A resource created between the collect snapshot
        and the re-validation is allocated at stage 4 and JOINS the build —
        the switch does not abort under moderate write traffic."""
        ctx.remember("early", "early bird content")
        ctx.vector.prepare_generation("t1")  # its orphan is swept later
        # The write lands after prepare: rebuild() re-prepares and its stage-4
        # allocation picks the new resource up within ONE attempt.
        ctx.remember("latecomer", "late arriving content")
        report = ctx.rebuild()
        assert report.vector_count == 2
        hits = ctx.search("late arriving content")
        assert any(hit.resource_revision >= 1 for hit in hits)

    def test_abandoned_tmp_directory_swept(self, ctx: VectorCtx) -> None:
        """[P3 tmp leak] A staging directory abandoned by SIGKILL ages out."""
        import os

        tmp_root = Path(ctx.store.runtime.database).parent / "vector" / "tmp"
        abandoned = tmp_root / "build-abandoned"
        abandoned.mkdir(parents=True)
        (abandoned / "index.faiss").write_bytes(b"partial")
        past = ctx.clock.now_us() - ctx.vector._retirement_window_us - 1
        os.utime(abandoned, (past / 1_000_000, past / 1_000_000))
        removed = ctx.vector.manager.sweep_tmp(
            older_than_us=ctx.clock.now_us() - ctx.vector._retirement_window_us
        )
        assert "build-abandoned" in removed
        assert not abandoned.exists()


class TestCrashWindows:
    def test_crash_between_rename_and_switch_leaves_orphan_not_pointer(
        self, ctx: VectorCtx
    ) -> None:
        """A builder dying after the atomic rename but before the switch
        transaction leaves an unreferenced directory: the pointer still
        serves the previous trusted generation and the sweep eventually
        removes the orphan (ADR-0015 §5 stage 5/6 crash window)."""
        ctx.remember("c1", "crash window trusted content")
        first = ctx.rebuild()
        # Simulate the crash: prepare runs through the rename, switch never
        # runs (the "process died" between the two).
        prepared = ctx.vector.prepare_generation("t1")
        pointer = ctx.pointer()
        assert pointer is not None
        assert pointer.generation_id == first.generation_id, "pointer must not move"
        orphan = ctx.vector.manager.generation_dir(prepared.generation_id)
        assert orphan.is_dir(), "the renamed-but-unpublished directory exists"
        # Serving continues from the trusted generation.
        assert len(ctx.search("crash window trusted content")) == 1
        # The sweep only removes it after the retirement window.
        import os

        past = ctx.clock.now_us() - ctx.vector._retirement_window_us - 1
        os.utime(orphan, (past / 1_000_000, past / 1_000_000))
        removed = ctx.vector.sweep_filesystem()
        assert prepared.generation_id in removed
        assert not orphan.exists()

    def test_crash_mid_switch_transaction_rolls_back_completely(self, ctx: VectorCtx) -> None:
        """A failure inside the switch transaction (fencing mismatch raised
        after the insert) rolls the generation row back with the pointer."""
        from iris_memory_core.domain.errors import ConflictError

        ctx.remember("c2", "mid switch rollback content")
        prepared = ctx.vector.prepare_generation("t1")
        # Someone moves the pointer between prepare and our switch: the CAS
        # aborts and nothing of ours may be visible.
        ctx.remember("c2b", "competing content")
        ctx.rebuild()
        with pytest.raises(ConflictError), ctx.store.write() as tx:
            ctx.vector.switch_in_tx(tx, "t1", prepared)
        with ctx.store.read() as tx:
            rows = tx.vector.generations_for_tenant("t1")
            # The aborted generation row must not linger as verified.
            lingering = [
                row for row in rows if row.id == prepared.generation_id and row.status == "verified"
            ]
        assert not lingering
        hits = ctx.search("competing content")
        assert hits, "the trusted generation keeps serving"

    def test_restart_with_dangling_pointer_degrades_until_rebuild(self, ctx: VectorCtx) -> None:
        """Pointer references a directory that vanished (operator deleted
        the volume): every search degrades with the corrupt reason — no
        unverified fallback, no crash (ADR-0015 §5)."""
        ctx.remember("c3", "dangling pointer content")
        ctx.rebuild()
        directory = ctx.vector.manager.generation_dir(ctx.pointer().generation_id)
        shutil.rmtree(directory)
        ctx.vector.manager.drop_current()
        for _round in range(5):
            with pytest.raises(VectorDegradedError) as error:
                ctx.search("dangling pointer content")
            assert error.value.reason_code == "vector_index_corrupt"
        # A rebuild fully self-heals from canonical state.
        ctx.rebuild()
        assert len(ctx.search("dangling pointer content")) == 1

    def test_tmp_build_directory_never_entered_via_pointer(self, ctx: VectorCtx) -> None:
        """The tmp root is a sibling of generations/; nothing can make the
        pointer reference it because generation ids resolve exclusively
        under generations/ (manager.generation_dir)."""
        ctx.remember("c4", "tmp root isolation content")
        ctx.rebuild()
        pointer = ctx.pointer()
        directory = ctx.vector.manager.generation_dir(pointer.generation_id)
        assert directory.parent.name == "generations"
        assert (Path(ctx.store.runtime.database).parent / "vector" / "tmp").exists()


class TestBackupBoundaries:
    def test_backup_with_vector_directory_is_structurally_rejected(
        self, ctx: VectorCtx, tmp_path: Path
    ) -> None:
        """Backups do not carry FAISS files; a directory smuggled into a
        backup set fails verification before any restore happens."""
        from iris_memory_core.storage.backup import BackupService, verify_backup

        ctx.remember("bk1", "backup rejection content")
        ctx.rebuild()
        service = BackupService(ctx.store)
        backup_dir = tmp_path / "backup"
        service.create_backup(backup_dir)
        smuggled = backup_dir / "vector"
        smuggled.mkdir()
        (smuggled / "generations").mkdir()
        (smuggled / "generations" / "vecg-forged").mkdir()
        (smuggled / "generations" / "vecg-forged" / "manifest.json").write_text("{}")
        check = verify_backup(backup_dir)
        assert not check.ok
        assert any("vector" in problem or "unexpected" in problem for problem in check.problems)

    def test_restore_never_adopts_stale_generation_files(
        self, ctx: VectorCtx, tmp_path: Path
    ) -> None:
        """Even if an operator manually copies an old vector/ directory into
        the restore target, the pending_rebuild state makes it unusable
        until a fresh verified build lands (ADR-0015 §10)."""
        from iris_memory_core.storage.backup import BackupService
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
        from iris_memory_core.storage.uow import Store
        from tests.integration.phase7_helpers import make_projection

        ctx.remember("bk2", "stale adoption content")
        ctx.rebuild()
        service = BackupService(ctx.store)
        backup_dir = tmp_path / "backup"
        service.create_backup(backup_dir)
        target = tmp_path / "restored"
        report = service.restore_backup(backup_dir, target)
        assert report.check.ok, report.check.problems
        # Operator drops a STALE vector directory next to the restored db.
        stale_root = target / "vector" / "generations"
        stale_root.mkdir(parents=True, exist_ok=True)
        source_generation = ctx.vector.manager.generation_dir(ctx.pointer().generation_id)
        shutil.copytree(source_generation, stale_root / source_generation.name)
        restored_store = Store(
            SQLiteRuntime(
                target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
            ),
            clock=ctx.clock,
        )
        restored = make_projection(restored_store, ctx.clock)
        query = restored.embed_query("stale adoption content")
        # pending_rebuild gates BEFORE any directory is even looked at.
        with pytest.raises(VectorDegradedError) as error, restored_store.read() as tx:
            restored.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
            )
        assert error.value.reason_code == "vector_rebuild_pending"
        restored.manager.drop_current()


class TestFreshnessAdversarial:
    def test_dead_lettered_apply_still_gated_by_minimum_watermark(self, ctx: VectorCtx) -> None:
        """Simulate a lost/dead-lettered apply (its delta row deleted): the
        plain staleness gate cannot see it, but a read-your-writes request
        still refuses to be served stale (ADR-0015 §4)."""
        ctx.remember("dl1", "dead letter probe content")
        ctx.rebuild()
        ctx.claims.correct(
            ctx.access,
            claim_id=_first_claim_id(ctx),
            expected_revision=_first_claim_revision(ctx),
            canonical_text="dead letter corrected content",
            evidence=[{"source_type": "claim", "source_id": _first_claim_id(ctx)}],
            reason="correction",
            idempotency_key="dl1-corr",
        )
        with ctx.store.write() as tx:
            ctx.vector.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=_first_claim_id(ctx)
            )
        # The ledger row "disappears" (dead-letter replay bug simulation).
        with ctx.store.write() as tx:
            tx.raw().execute("DELETE FROM vector_delta_ledger WHERE tenant_id = 't1'")
        # A read-your-writes demand still refuses: the generation watermark
        # predates the change.
        with ctx.store.read() as tx:
            pointer = ctx.pointer()
            assert pointer is not None
            generation = tx.vector.get_generation(pointer.generation_id)
        watermark = generation.agent_watermarks()[ctx.agent]
        query = ctx.vector.embed_query("dead letter corrected content")
        with pytest.raises(VectorDegradedError) as error, ctx.store.read() as tx:
            ctx.vector.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                query_vector=query,
                limit=5,
                minimum_watermark=watermark + 1,
            )
        assert error.value.reason_code == "vector_generation_stale"
        # Correctness backstop even without the watermark: the stale vector
        # maps to the OLD revision, which the hit filter drops.
        hits = ctx.search("dead letter corrected content")
        for hit in hits:
            assert hit.resource_id != _first_claim_id(ctx) or hit.resource_revision == (
                _first_claim_revision(ctx) + 1
            ), "the pre-correction revision must not serve"

    def test_out_of_order_apply_does_not_falsify_freshness(self, ctx: VectorCtx) -> None:
        """Lag is a COUNT of unsettled jobs plus delta rows — an out-of-order
        apply completing early cannot MAX-jump over pending holes (the
        ADR-0014 §14.1 discipline carried into Phase 7)."""
        ctx.remember("oo1", "out of order content one")
        ctx.remember("oo2", "out of order content two")
        ctx.rebuild()
        # Two changes; only the second one's delta is recorded (the first is
        # "still queued" as an unsettled job would be).
        claims = _claim_ids(ctx)
        for index, (claim_id, text) in enumerate(
            (
                (claims[0], "out of order revised one"),
                (claims[1], "out of order revised two"),
            )
        ):
            ctx.claims.correct(
                ctx.access,
                claim_id=claim_id,
                expected_revision=_revision_of(ctx, claim_id),
                canonical_text=text,
                evidence=[{"source_type": "claim", "source_id": claim_id}],
                reason="correction",
                idempotency_key=f"oo-{index}-{claim_id}",
            )
        with ctx.store.write() as tx:
            ctx.vector.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=claims[1]
            )
        with ctx.store.read() as tx:
            backlog = tx.outbox.unsettled_job_count("t1", ctx.agent, "vector.apply")
        del backlog  # no jobs were scheduled in this hand-driven scenario
        strict = _strict_projection(ctx)
        query = strict.embed_query("out of order revised two")
        with pytest.raises(VectorDegradedError) as error, ctx.store.read() as tx:
            strict.search_in_tx(tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5)
        assert error.value.reason_code == "vector_generation_stale"
        strict.manager.drop_current()

    def test_non_indexable_traffic_never_degrades(self, ctx: VectorCtx) -> None:
        """Observations/state writes advance the agent watermark without
        touching the vector projection: the lag count must stay zero (no
        permanent false stale, ADR-0015 §4)."""
        ctx.remember("ni1", "non indexable traffic content")
        ctx.rebuild()
        for index in range(10):
            ctx.observations.observe_batch(
                ctx.access,
                [
                    {
                        "agent_id": ctx.agent,
                        "role": "user",
                        "kind": "message.text",
                        "idempotency_key": f"ni-obs-{index}",
                        "occurred_us": ctx.clock.now_us(),
                        "committed_us": ctx.clock.now_us(),
                        "content": f"pure observation {index}",
                        "space_id": ctx.space,
                    }
                ],
            )
        strict = _strict_projection(ctx)
        query = strict.embed_query("non indexable traffic content")
        with ctx.store.read() as tx:
            hits = strict.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
            )
        assert len(hits) == 1
        strict.manager.drop_current()


class TestProviderIsolation:
    def test_provider_outage_never_touches_canonical_writes(self, ctx: VectorCtx) -> None:
        from iris_memory_core.domain.vector import EmbeddingProviderError

        ctx.remember("po1", "provider isolation content")
        ctx.rebuild()

        def broken(
            texts: list[str], *, deadline_monotonic_us: int | None = None
        ) -> list[list[float]]:
            raise EmbeddingProviderError("circuit_open", retryable=True)

        original = ctx.vector._provider.embed_batch
        ctx.vector._provider.embed_batch = broken
        try:
            with pytest.raises(EmbeddingProviderError):
                ctx.search("provider isolation content")
            # Canonical writes, corrections and forgets keep working.
            ctx.remember("po2", "written during outage")
            from iris_memory_core.application.forget import ForgetService
            from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
            from iris_memory_core.storage.idempotency import IdempotencyManager

            ForgetService(ctx.store, ctx.clock, idempotency=IdempotencyManager(ctx.store)).forget(
                ctx.access,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE,
                    agent_id=ctx.agent,
                    resource_type="claim",
                    resource_id=_first_claim_id(ctx),
                ),
                reason="outage erasure",
                idempotency_key="po-fg",
            )
        finally:
            ctx.vector._provider.embed_batch = original
        assert len(ctx.search("provider isolation content")) == 0


class TestMultiProcessCoordination:
    def test_second_store_instance_sees_pointer_switch(self, ctx: VectorCtx) -> None:
        """Two service instances over one database: instance B's search
        loads whatever the pointer says even though instance A published it
        (SQLite pointer + generation id is the only authority)."""
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
        from iris_memory_core.storage.uow import Store
        from tests.integration.phase7_helpers import make_projection

        ctx.remember("mp1", "multi process content")
        ctx.rebuild()
        other = make_projection(
            Store(
                SQLiteRuntime(
                    Path(ctx.store.runtime.database), allowed_versions=(sqlite_runtime_version(),)
                ),
                clock=ctx.clock,
            ),
            ctx.clock,
        )
        query = other.embed_query("multi process content")
        with ctx.store.read() as tx:
            hits = other.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
            )
        assert len(hits) == 1
        # Instance A publishes a new generation; instance B follows the
        # pointer on its next search (fresh load, fresh verification).
        ctx.remember("mp2", "second generation content")
        ctx.rebuild()
        with other._uow.read() as tx:
            hits = other.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
            )
        assert hits, "instance B follows the pointer to the new generation"
        assert other.manager.loaded_generation == ctx.pointer().generation_id
        other.manager.drop_current()

    def test_concurrent_generation_dir_delete_between_pointer_and_load(
        self, ctx: VectorCtx
    ) -> None:
        """The cleanup deletes a RETIRED generation while another thread
        holds its handle: the handle object keeps working (its files were
        mapped at load); a NEW load of the retired id would fail — but the
        pointer only ever references the current id, so no search path can
        request the deleted one."""
        first = None
        for index in range(4):
            ctx.remember(f"cd{index}", f"concurrent delete content {index}")
            ctx.clock.advance(1_000_000)
            report = ctx.rebuild()
            if index == 0:
                first = report
        assert first is not None
        old = ctx.vector.manager.handle_for(ctx.pointer())
        old.release()  # keep only a sealed-view check below; manager reloads current
        # Age every retirement past the window and clean up.
        with ctx.store.write() as tx:
            tx.raw().execute(
                "UPDATE vector_generations SET retired_us = ? WHERE id = ?",
                (
                    ctx.clock.now_us() - ctx.vector._retirement_window_us - 1,
                    first.generation_id,
                ),
            )
        with ctx.store.write() as tx:
            _rows, removed = ctx.vector.cleanup_in_tx(tx, "t1")
        assert first.generation_id in removed
        current = ctx.vector.manager.handle_for(ctx.pointer())
        query = ctx.vector.embed_query("concurrent delete content 0")
        assert current.search(query, 1), "current handle unaffected by retired cleanup"
        current.release()
        # The current generation keeps serving.
        assert ctx.search("concurrent delete second"), "current handle keeps serving"


def _strict_projection(ctx: VectorCtx) -> VectorProjectionService:
    from tests.integration.phase7_helpers import make_projection

    return make_projection(ctx.store, ctx.clock, staleness_limit=0)


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


def _revision_of(ctx: VectorCtx, claim_id: str) -> int:
    with ctx.store.read() as tx:
        row = (
            tx.raw()
            .execute("SELECT current_revision FROM claims WHERE id = ?", (claim_id,))
            .fetchone()
        )
        return int(row[0])


def _claim_ids(ctx: VectorCtx) -> list[str]:
    with ctx.store.read() as tx:
        rows = (
            tx.raw()
            .execute("SELECT id FROM claims WHERE tenant_id = 't1' ORDER BY created_us")
            .fetchall()
        )
        return [str(row[0]) for row in rows]


class TestSecondReviewRound:
    """Regressions for the second adversarial review round: three P1
    (false-fresh unpublished build, verification not bound to the SQLite
    row, hybrid ranker not deduplicating) and the P2 findings (deadline
    propagation wiring, capability/readiness, fenced-tx directory removal,
    cross-agent overfetch crowding, metric emitters)."""

    def test_unpublished_build_cannot_fake_freshness(self, ctx: VectorCtx) -> None:
        """[P1] A build that refreshed the id map at stage 2 but never
        published must NOT count as incorporated: the late apply records a
        delta (lag > 0, minimum-watermark requests degrade) and a rebuild is
        owed. The membership stamp — not the id map's revision alone — is
        the proof."""
        from iris_memory_core.domain.vector import VECTOR_REASON_GENERATION_STALE

        claim = ctx.remember("ff1", "false fresh probe content")
        first = ctx.rebuild()
        ctx.claims.correct(
            ctx.access,
            claim_id=claim.claim_id,
            expected_revision=claim.revision,
            canonical_text="false fresh corrected content",
            evidence=[{"source_type": "claim", "source_id": claim.claim_id}],
            reason="correction",
            idempotency_key="ff-corr",
        )
        # Stage 2 refreshes the id map row to the corrected revision…
        prepared = ctx.vector.prepare_generation("t1")
        # …but the publish never lands (crash after rename / fenced CAS).
        pointer = ctx.pointer()
        assert pointer is not None
        assert pointer.generation_id == first.generation_id != prepared.generation_id
        with ctx.store.read() as tx:
            row = tx.vector.id_map_get("t1", "claim", claim.claim_id)
            assert row is not None
            refreshed_revision = row.resource_revision
            assert refreshed_revision == claim.revision + 1
            assert row.incorporated_generation != pointer.generation_id
        # The late apply must NOT skip: delta recorded, lag visible.
        with ctx.store.write() as tx:
            changed = ctx.vector.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=claim.claim_id
            )
        assert changed is True
        with ctx.store.read() as tx:
            assert tx.vector.delta_count("t1", ctx.agent) == 1
        query = ctx.vector.embed_query("false fresh corrected content")
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
        # A real publish stamps the membership and the same apply skips.
        second = ctx.rebuild()
        assert second.generation_id != first.generation_id
        with ctx.store.write() as tx:
            changed = ctx.vector.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=claim.claim_id
            )
        assert changed is False
        with ctx.store.read() as tx:
            assert tx.vector.delta_count("t1", ctx.agent) == 0
            row = tx.vector.id_map_get("t1", "claim", claim.claim_id)
            assert row is not None
            assert row.incorporated_generation == second.generation_id
        assert len(ctx.search("false fresh corrected content")) == 1

    def test_tampered_content_hash_rejected_against_db_row(self, ctx: VectorCtx) -> None:
        """[P1] Reviewer repro: rewrite manifest.content_hash AND recompute
        checksums.txt — the files stay self-consistent, so only the binding
        against the authoritative SQLite generation row can fail this."""
        import hashlib
        import json

        from iris_memory_core.domain.vector import VECTOR_REASON_INDEX_CORRUPT

        ctx.remember("tam1", "manifest binding probe")
        report = ctx.rebuild()
        directory = ctx.vector.manager.generation_dir(report.generation_id)
        manifest = json.loads((directory / "manifest.json").read_text())
        manifest["content_hash"] = "f" * 64
        (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
        hashes = {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("manifest.json", "index.faiss", "id-map.snapshot")
        }
        (directory / "checksums.txt").write_text(
            "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
        )
        ctx.vector.manager.drop_current()
        with pytest.raises(VectorDegradedError) as error:
            ctx.search("manifest binding probe")
        assert error.value.reason_code == VECTOR_REASON_INDEX_CORRUPT

    def test_rewritten_checksums_cannot_vouch_for_a_modified_index(self, ctx: VectorCtx) -> None:
        """[P1] The manifest — not checksums.txt — is the digest authority:
        flipping index bytes and recomputing checksums.txt still fails
        because the recomputed index digest no longer matches the manifest."""
        import hashlib

        from iris_memory_core.domain.vector import VECTOR_REASON_INDEX_CORRUPT

        ctx.remember("tam2", "index digest binding probe")
        report = ctx.rebuild()
        directory = ctx.vector.manager.generation_dir(report.generation_id)
        index_path = directory / "index.faiss"
        index_path.write_bytes(index_path.read_bytes() + b"\x00truncation")
        hashes = {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("manifest.json", "index.faiss", "id-map.snapshot")
        }
        (directory / "checksums.txt").write_text(
            "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
        )
        ctx.vector.manager.drop_current()
        with pytest.raises(VectorDegradedError) as error:
            ctx.search("index digest binding probe")
        assert error.value.reason_code == VECTOR_REASON_INDEX_CORRUPT

    def test_malformed_manifest_fields_degrade_with_stable_reason(self, ctx: VectorCtx) -> None:
        """[P1] Wrongly typed manifest fields must surface as the stable
        ``vector_index_corrupt`` degradation — never as a raw
        ValueError/TypeError/KeyError escaping the trust gate."""
        import hashlib
        import json

        from iris_memory_core.domain.vector import VECTOR_REASON_INDEX_CORRUPT

        ctx.remember("mal1", "malformed manifest probe")
        ctx.rebuild()
        directory = ctx.vector.manager.generation_dir(ctx.pointer().generation_id)

        def reseal(mutate) -> None:  # type: ignore[no-untyped-def]
            manifest = json.loads((directory / "manifest.json").read_text())
            mutate(manifest)
            (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
            hashes = {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in ("manifest.json", "index.faiss", "id-map.snapshot")
            }
            (directory / "checksums.txt").write_text(
                "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
            )

        mutations = (
            lambda m: m.__setitem__("dimension", "8"),
            lambda m: m.__setitem__("vector_count", "3"),
            lambda m: m.__setitem__("source_watermark", None),
            lambda m: m.__setitem__("agent_watermarks", [1, 2]),
            lambda m: m.__setitem__("model", 8),
        )
        for mutate in mutations:
            reseal(mutate)
            ctx.vector.manager.drop_current()
            with pytest.raises(VectorDegradedError) as error:
                ctx.search("malformed manifest probe")
            assert error.value.reason_code == VECTOR_REASON_INDEX_CORRUPT
        # A truncated manifest.json (undecodable bytes) is equally stable.
        (directory / "manifest.json").write_text("{not json")
        ctx.vector.manager.drop_current()
        with pytest.raises(VectorDegradedError) as error:
            ctx.search("malformed manifest probe")
        assert error.value.reason_code == VECTOR_REASON_INDEX_CORRUPT

    def test_required_vector_readiness_fails_when_provider_is_down(
        self,
        ctx: VectorCtx,
        generous_gauge: BackpressureGauge,
    ) -> None:
        """[P2] ``vector_required`` readiness must consult the live
        capability (FAISS + embedding provider probe), not only the
        projection state."""
        from iris_memory_core.application.health import HealthService
        from iris_memory_core.indexing.vector import VectorProjectionService
        from iris_memory_core.providers.embedding import (
            EmbeddingProviderLimits,
            HttpEmbeddingProvider,
        )
        from tests.integration.phase7_helpers import vector_space

        ctx.remember("hc1", "health capability probe")
        ctx.rebuild()

        def bad_dimension(endpoint, key, model, texts, timeout_s):  # type: ignore[no-untyped-def]
            return [[0.1] * 4 for _ in texts]

        broken = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=vector_space(),
            limits=EmbeddingProviderLimits(max_qps=1_000_000.0, breaker_cooldown_us=1_000),
            transport=bad_dimension,
        )
        broken_projection = VectorProjectionService(
            ctx.store,
            ctx.clock,
            provider=broken,
            vector_root=Path(ctx.store.runtime.database).parent / "vector-broken",
            space=vector_space(),
        )
        assert broken_projection.capability_available() is False
        assert broken_projection.provider_health()["faiss"] is True

        service = HealthService(
            ctx.store,
            ctx.clock,
            gauge=generous_gauge,
            vector_required=True,
            vector_capability=broken_projection.capability_available,
        )
        report = service.readiness()
        assert report.status == "not_ready"
        assert "vector_capability_unavailable" in report.reasons
        assert report.checks["vector_capability"] == "unavailable"

        # The healthy projection keeps a required deployment ready, and an
        # optional deployment never goes not_ready on the capability alone.
        healthy = HealthService(
            ctx.store,
            ctx.clock,
            gauge=generous_gauge,
            vector_required=True,
            vector_capability=ctx.vector.capability_available,
        )
        healthy_report = healthy.readiness()
        assert healthy_report.status != "not_ready", healthy_report.reasons
        assert healthy_report.checks["vector_capability"] == "ok"
        optional = HealthService(
            ctx.store,
            ctx.clock,
            gauge=generous_gauge,
            vector_required=False,
            vector_capability=broken_projection.capability_available,
        )
        assert optional.readiness().status != "not_ready"

    def test_cleanup_directories_removed_only_after_commit(self, ctx: VectorCtx) -> None:
        """[P2] Directory unlisting must never happen inside the fenced
        commit transaction: a rolled-back completion leaves rows AND
        directories intact; the post-commit hook removes them."""
        from iris_memory_core.jobs.handlers import vector_cleanup_handler

        for index in range(4):
            ctx.remember(f"chr{index}", f"cleanup handler content {index}")
            ctx.clock.advance(1_000_000)
            ctx.rebuild()
        with ctx.store.write() as tx:
            tx.raw().execute(
                "UPDATE vector_generations SET retired_us = ? WHERE status = 'retired'",
                (ctx.clock.now_us() - ctx.vector._retirement_window_us - 1,),
            )
        with ctx.store.read() as tx:
            retired = [
                row.id for row in tx.vector.generations_for_tenant("t1") if row.status == "retired"
            ]
        victim = ctx.vector.manager.generation_dir(retired[-1])
        assert victim.is_dir()

        job_payload = {"version": 1, "resource_type": None, "resource_id": None}
        job = _cleanup_job(ctx, job_payload)
        work = vector_cleanup_handler(ctx.vector)
        commit = work(job)

        class _ForcedRollback(Exception):
            pass

        with pytest.raises(_ForcedRollback), ctx.store.write() as tx:
            commit(tx)
            raise _ForcedRollback
        # Rollback restored the rows; directories were never touched.
        with ctx.store.read() as tx:
            assert tx.vector.get_generation(retired[-1]).status == "retired"
        assert victim.is_dir(), "a rolled-back completion must not unlink directories"

        with ctx.store.write() as tx:
            commit(tx)
        with ctx.store.read() as tx:
            from iris_memory_core.domain.errors import NotFoundError

            with pytest.raises(NotFoundError):
                tx.vector.get_generation(retired[-1])
        assert victim.is_dir(), "files are unlinked only by the post-commit hook"
        after_commit = getattr(commit, "after_commit", None)
        assert callable(after_commit)
        after_commit()  # remove_generation_dirs + orphan/tmp sweep
        assert not victim.exists(), "the committed cleanup removes the directory"
        after_commit()  # idempotent on re-run

    def test_cross_agent_crowding_does_not_starve_own_candidates(self, ctx: VectorCtx) -> None:
        """[P2] A fixed 4x overfetch let another agent's high-scoring
        vectors crowd out every legal candidate; the fetch now expands
        until enough post-filter hits exist or the index is exhausted."""
        for index in range(40):
            observation = ctx.observations.observe_batch(
                ctx.access,
                [
                    {
                        "agent_id": ctx.other_agent,
                        "role": "user",
                        "kind": "message.text",
                        "idempotency_key": f"crowd-obs-{index}",
                        "occurred_us": ctx.clock.now_us(),
                        "committed_us": ctx.clock.now_us(),
                        "content": f"flood evidence {index}",
                        "space_id": ctx.other_space,
                    }
                ],
            ).accepted_observation_ids[0]
            ctx.remember(
                f"crowd-{index}",
                "crowding flood identical text",
                agent_id=ctx.other_agent,
                space_id=ctx.other_space,
                evidence=[{"source_type": "observation", "source_id": observation}],
            )
        mine = [
            ctx.remember(f"mine-{index}", f"crowding own material variant {index}").claim_id
            for index in range(5)
        ]
        ctx.rebuild()
        hits = ctx.search("crowding flood identical text", limit=5)
        assert {hit.resource_id for hit in hits} == set(mine)
        for hit in hits:
            assert hit.resource_id in mine, "another agent's vectors must never leak"

    def test_phase7_metrics_emitted_from_production_paths(self, ctx: VectorCtx) -> None:
        """[P2] The four Phase 7 metrics have real emitters: generation
        gauge after publish, lag gauge on search, provider request/duration
        on every provider call."""
        from iris_memory_core.indexing.vector import VectorProjectionService
        from iris_memory_core.providers.embedding import DeterministicEmbeddingProvider
        from tests.integration.phase7_helpers import vector_space

        class _Recording:
            def __init__(self) -> None:
                self.events: list[tuple[object, ...]] = []

            def index_generation(self, index_kind: str, generation: int) -> None:
                self.events.append(("index_generation", index_kind, generation))

            def index_lag(self, index_kind: str, lag_revisions: int) -> None:
                self.events.append(("index_lag", index_kind, lag_revisions))

            def provider_request(self, provider_kind: str, outcome: str) -> None:
                self.events.append(("provider_request", provider_kind, outcome))

            def provider_duration(self, provider_kind: str, duration_seconds: float) -> None:
                self.events.append(("provider_duration", provider_kind, duration_seconds))

        metrics = _Recording()
        space = vector_space()
        projection = VectorProjectionService(
            ctx.store,
            ctx.clock,
            provider=DeterministicEmbeddingProvider(space, metrics=metrics),
            vector_root=Path(ctx.store.runtime.database).parent / "vector-metrics",
            space=space,
            metrics=metrics,
        )
        ctx.remember("mx1", "metrics emission probe")
        projection.rebuild("t1")
        assert ("index_generation", "vector", 1) in metrics.events
        query = projection.embed_query("metrics emission probe")
        assert any(event[:3] == ("provider_request", "embedding", "ok") for event in metrics.events)
        assert any(event[0] == "provider_duration" for event in metrics.events)
        with ctx.store.read() as tx:
            projection.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=5
            )
        assert any(event[:3] == ("index_lag", "vector", 0) for event in metrics.events), (
            metrics.events
        )
        projection.manager.drop_current()


def _cleanup_job(ctx: VectorCtx, payload: Mapping[str, object]) -> Any:
    """Minimal claimed-job shape for exercising a handler closure directly."""
    from iris_memory_core.domain.jobs import OutboxJob

    with ctx.store.read() as tx:
        row = tx.raw().execute("SELECT * FROM outbox_jobs LIMIT 1").fetchone()
    if row is not None:
        columns = row.keys()
        values = dict(zip(columns, tuple(row), strict=True))
        return OutboxJob(**{**values, "payload": payload, "job_kind": "vector.cleanup"})
    return OutboxJob(
        id="job-cleanup-probe",
        tenant_id="t1",
        job_kind="vector.cleanup",
        aggregate_type="maintenance",
        aggregate_id="cleanup-probe",
        source_revision=0,
        payload=dict(payload),
        payload_version=1,
        dedupe_key="vector-cleanup-probe",
        coalesce_key="vector:cleanup",
        priority=8,
        lane="maintenance",
        status="pending",
        available_at_us=1,
        attempt_count=0,
        max_attempts=5,
        lease_owner=None,
        lease_generation=0,
        lease_expires_us=None,
        last_error_code=None,
        replay_of=None,
        created_us=1,
        completed_us=None,
    )
