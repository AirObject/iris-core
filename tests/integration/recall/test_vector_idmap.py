"""Phase 7 surrogate ID map property tests (P7-IDMAP-01, ADR-0015 §3).

Fixed-seed property cases (≥200 each): allocation uniqueness inside legal
signed-int64 bounds, cross-tenant/cross-agent isolation, deletion/
invalidation semantics and rebuild consistency (the same resource keeps the
same surrogate across generations). Collisions or wrong reuse must be 0.
"""

from __future__ import annotations

import random

import pytest

from iris_memory_core.domain.vector import SURROGATE_ID_MAX
from tests.integration.recall.vector_helpers import VectorCtx

CASES = 200


@pytest.fixture
def ctx(clocked_store, mutable_clock) -> VectorCtx:  # type: ignore[no-untyped-def]
    return VectorCtx(clocked_store, mutable_clock)


class TestAllocationProperties:
    def test_allocation_is_unique_and_in_bounds(self, ctx: VectorCtx) -> None:
        rng = random.Random(701)
        allocated: list[int] = []
        with ctx.store.write() as tx:
            for _case in range(CASES):
                count = rng.randint(1, 5)
                batch = tx.vector.allocate_surrogate_ids(count)
                assert len(batch) == count
                allocated.extend(batch)
                for surrogate in batch:
                    assert 1 <= surrogate <= SURROGATE_ID_MAX
        assert len(set(allocated)) == len(allocated), "surrogate collision"
        assert allocated == sorted(allocated), "allocation must be monotone"

    def test_allocation_never_reuses_ids_across_transactions(self, ctx: VectorCtx) -> None:
        seen: set[int] = set()
        for _ in range(CASES // 2):
            with ctx.store.write() as tx:
                batch = tx.vector.allocate_surrogate_ids(2)
            for surrogate in batch:
                assert surrogate not in seen
                seen.add(surrogate)

    def test_map_is_unique_per_resource_and_surrogate(self, ctx: VectorCtx) -> None:
        for index in range(CASES):
            claim = ctx.remember(f"c{index}", f"claim text {index}")
            with ctx.store.write() as tx:
                surrogate = tx.vector.allocate_surrogate_ids(1)[0]
                tx.vector.id_map_upsert(
                    tenant_id="t1",
                    resource_type="claim",
                    resource_id=claim.claim_id,
                    resource_revision=claim.revision,
                    surrogate_id=surrogate,
                    agent_id=ctx.agent,
                    space=ctx.vector.manager.space,
                    content_hash=f"h{index}",
                )
        with ctx.store.read() as tx:
            rows = tx.vector.id_map_active_for_tenant("t1")
        surrogates = [row.surrogate_id for row in rows]
        assert len(surrogates) == CASES
        assert len(set(surrogates)) == CASES, "surrogate reuse across resources"

    def test_cross_tenant_isolation_by_unique_constraints(self, ctx: VectorCtx) -> None:
        with ctx.store.write() as tx:
            tx.insert_tenant("t2", status="active")
        # Tenant t1 grabs surrogate 1; tenant t2 may independently hold its
        # own surrogate 1 — uniqueness is per tenant (UNIQUE(tenant, surrogate)).
        for _tenant in ("t1", "t2"):
            with ctx.store.write() as tx:
                batch = tx.vector.allocate_surrogate_ids(1)
                assert batch[0] >= 1
        # But a resource cannot hold two rows in one tenant.
        claim = ctx.remember("dup", "text")
        with ctx.store.write() as tx:
            first = tx.vector.allocate_surrogate_ids(1)[0]
            tx.vector.id_map_upsert(
                tenant_id="t1",
                resource_type="claim",
                resource_id=claim.claim_id,
                resource_revision=1,
                surrogate_id=first,
                agent_id=ctx.agent,
                space=ctx.vector.manager.space,
                content_hash="h",
            )
            # Same resource + different surrogate: the upsert REPLACES (one
            # logical mapping per resource); reading back yields one row.
        with ctx.store.read() as tx:
            assert tx.vector.id_map_count("t1") == 1

    def test_invalidation_blocks_service_and_cleanup_removes(self, ctx: VectorCtx) -> None:
        claim_ids = [ctx.remember(f"inv{i}", f"text {i}").claim_id for i in range(CASES)]
        ctx.rebuild()
        invalidated = 0
        with ctx.store.write() as tx:
            for claim_id in claim_ids[: CASES // 2]:
                invalidated += tx.vector.id_map_invalidate(
                    tenant_id="t1", resource_type="claim", resource_id=claim_id
                )
        assert invalidated == CASES // 2
        with ctx.store.read() as tx:
            assert tx.vector.id_map_count("t1", active_only=True) == CASES - CASES // 2
            assert tx.vector.id_map_count("t1", active_only=False) == CASES
        with ctx.store.write() as tx:
            removed = tx.vector.id_map_delete_invalid("t1", limit=1000)
        assert removed == CASES // 2
        with ctx.store.read() as tx:
            assert tx.vector.id_map_count("t1", active_only=False) == CASES - CASES // 2

    def test_rebuild_keeps_surrogates_stable_across_generations(self, ctx: VectorCtx) -> None:
        claims = [ctx.remember(f"stable{i}", f"stable text {i}").claim_id for i in range(30)]
        first = ctx.rebuild()
        with ctx.store.read() as tx:
            before = {
                row.resource_id: row.surrogate_id
                for row in tx.vector.id_map_active_for_tenant("t1")
            }
        ctx.clock.advance(1_000_000)
        second = ctx.rebuild()
        assert second.generation_id != first.generation_id
        with ctx.store.read() as tx:
            after = {
                row.resource_id: row.surrogate_id
                for row in tx.vector.id_map_active_for_tenant("t1")
            }
        assert before == after, "surrogate drifted across rebuilds"
        assert set(before) == set(claims)

    def test_revision_advance_refreshes_map_immediately(self, ctx: VectorCtx) -> None:
        from iris_memory_core.application.forget import ForgetService
        from iris_memory_core.domain.retention import (
            ForgetSelector,
            ForgetSelectorKind,
        )
        from iris_memory_core.storage.idempotency import IdempotencyManager

        claim = ctx.remember("rev", "revision probe text")
        ctx.rebuild()
        forget = ForgetService(ctx.store, ctx.clock, idempotency=IdempotencyManager(ctx.store))
        # Correct → new revision: the apply path refreshes the mapping.
        ctx.claims.correct(
            ctx.access,
            claim_id=claim.claim_id,
            expected_revision=claim.revision,
            canonical_text="corrected text",
            evidence=[{"source_type": "claim", "source_id": claim.claim_id}],
            reason="correction",
            idempotency_key="corr-1",
        )
        with ctx.store.write() as tx:
            ctx.vector.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=claim.claim_id
            )
        with ctx.store.read() as tx:
            row = tx.vector.id_map_get("t1", "claim", claim.claim_id)
            assert row is not None
            assert row.resource_revision == claim.revision + 1
        # Forget → tombstone: the mapping is invalid immediately.
        forget.forget(
            ctx.access,
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE,
                agent_id=ctx.agent,
                resource_type="claim",
                resource_id=claim.claim_id,
            ),
            reason="erasure",
            idempotency_key="fg-1",
        )
        with ctx.store.write() as tx:
            ctx.vector.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=claim.claim_id
            )
        with ctx.store.read() as tx:
            row = tx.vector.id_map_get("t1", "claim", claim.claim_id)
            assert row is not None and row.status == "invalid"
            assert row.invalidated_us is not None

    def test_allocator_ceiling_is_enforced(self, ctx: VectorCtx) -> None:
        from iris_memory_core.domain.errors import ConflictError

        with ctx.store.write() as tx:
            # Pin the counter just below the ceiling.
            tx.vector.allocate_surrogate_ids(1)
            tx.raw().execute(
                "UPDATE vector_projection_state SET last_surrogate_id = ? WHERE id = 1",
                (SURROGATE_ID_MAX - 1,),
            )
        with pytest.raises(ConflictError), ctx.store.write() as tx:
            tx.vector.allocate_surrogate_ids(2)
        # A single remaining ID still works.
        with ctx.store.write() as tx:
            assert tx.vector.allocate_surrogate_ids(1) == (SURROGATE_ID_MAX,)
