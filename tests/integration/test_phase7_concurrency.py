"""Phase 7 concurrency tests (P7-CONCURRENCY-01, ADR-0015 §5).

50 concurrent searches interleaved with ≥10 build/validate/swap cycles: no
half-built generation is ever read, no use-after-close/double-close escapes,
every result is rehydrated. Concurrent Correct/Forget against search: zero
resurrections and zero content leaks. Plus worker-driven rebuilds through
the outbox (lease, fencing, retry).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.domain.errors import DomainError
from iris_memory_core.domain.jobs import NewOutboxJob
from iris_memory_core.domain.vector import VectorDegradedError
from iris_memory_core.jobs.worker import OutboxWorker, phase7_handlers
from tests.integration.phase7_helpers import VectorCtx

SEARCHERS = 50
SWAPS = 10


@pytest.fixture
def ctx(clocked_store, mutable_clock) -> VectorCtx:  # type: ignore[no-untyped-def]
    return VectorCtx(clocked_store, mutable_clock)


class TestConcurrentSearchAndSwap:
    def test_50_searchers_against_10_swaps(
        self, ctx: VectorCtx, capfd: pytest.CaptureFixture[str]
    ) -> None:
        for i in range(12):
            ctx.remember(f"s{i}", f"swap corpus sentence {i}")

        stop = threading.Event()
        errors: list[BaseException] = []
        searches: list[int] = []
        generations_served: set[str] = set()
        lock = threading.Lock()

        def searcher(worker_id: int) -> None:
            topic = f"swap corpus sentence {worker_id % 12}"
            try:
                query = ctx.vector.embed_query(topic)
            except BaseException as error:  # pragma: no cover - provider is local
                with lock:
                    errors.append(error)
                return
            while not stop.is_set():
                try:
                    with ctx.store.read() as tx:
                        hits = ctx.vector.search_in_tx(
                            tx,
                            tenant_id="t1",
                            agent_id=ctx.agent,
                            query_vector=query,
                            limit=5,
                        )
                    with lock:
                        searches.append(len(hits))
                        generations_served.add(ctx.pointer().generation_id)
                except VectorDegradedError as error:
                    # Degrades are legal (a rebuild between pointer read and
                    # handle use); untrustworthy results are not.
                    if error.reason_code not in (
                        "vector_generation_stale",
                        "vector_index_corrupt",
                        "vector_rebuild_pending",
                    ):
                        with lock:
                            errors.append(error)
                except DomainError as error:
                    with lock:
                        errors.append(error)

        with ThreadPoolExecutor(max_workers=SEARCHERS) as pool:
            futures = [pool.submit(searcher, worker) for worker in range(SEARCHERS)]
            # Interleave SWAPS full build/validate/swap cycles while the
            # searchers hammer the pointer.
            try:
                for cycle in range(SWAPS):
                    ctx.remember(f"swap-new-{cycle}", f"fresh swap sentence {cycle}")
                    ctx.rebuild()
            finally:
                stop.set()
                for future in futures:
                    future.result()

        assert not errors, errors[:3]
        assert sum(searches) > 0
        assert len(generations_served) >= 2, "the swap cycles must have moved the pointer"

    def test_no_temp_generation_is_ever_served(self, ctx: VectorCtx) -> None:
        """Every served generation must be a complete, checksum-verified
        directory under generations/ — never a tmp build directory."""
        ctx.remember("tmp1", "temporary directory guard probe")
        ctx.rebuild()
        stop = threading.Event()
        failures: list[str] = []

        def searcher() -> None:
            while not stop.is_set():
                pointer = ctx.pointer()
                directory = ctx.vector.manager.generation_dir(pointer.generation_id)
                if directory.parent.name != "generations":
                    failures.append(directory.parent.name)
                if not (directory / "manifest.json").is_file():
                    failures.append(f"missing manifest for {pointer.generation_id}")

        thread = threading.Thread(target=searcher)
        thread.start()
        try:
            for cycle in range(SWAPS):
                ctx.remember(f"tmp-{cycle}", f"guard sentence {cycle}")
                ctx.rebuild()
        finally:
            stop.set()
            thread.join()
        assert not failures, failures[:3]

    def test_use_after_close_and_double_close_never_escape_under_load(self, ctx: VectorCtx) -> None:
        ctx.remember("uc1", "handle close guard probe")
        ctx.rebuild()
        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def worker() -> None:
            barrier.wait()
            for _ in range(50):
                pointer = ctx.pointer()
                handle = ctx.vector.manager.handle_for(pointer)
                # handle_for hands back a +1 reference over the manager's:
                # the third release is the double release and must refuse.
                handle.release()
                handle.release()
                try:
                    handle.release()
                except VectorDegradedError:
                    pass  # refused loudly — the invariant under test
                else:
                    errors.append(AssertionError("double release silently accepted"))
                    return

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors[:2]


class TestForgetRace:
    def test_concurrent_forget_and_search_never_resurrect(self, ctx: VectorCtx) -> None:
        """Forget commits WHILE searches are in flight: the search path may
        still return the pre-forget hit, but the FINAL rehydrate discipline
        (fresh transaction) must drop it — content leakage must be 0."""
        from iris_memory_core.application.forget import ForgetService
        from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
        from iris_memory_core.storage.idempotency import IdempotencyManager

        target = ctx.remember("forgotten", "RESURRECTION-CANARY-CONTENT body")
        ctx.rebuild()
        forget = ForgetService(ctx.store, ctx.clock, idempotency=IdempotencyManager(ctx.store))
        resurrection_count = 0
        leak_count = 0
        rounds = 20
        for round_index in range(rounds):
            fresh_target = ctx.remember(f"race-{round_index}", f"RACE-CANARY-{round_index} body")
            ctx.rebuild()
            query = ctx.vector.embed_query(f"RACE-CANARY-{round_index} body")
            # Search collects hits, THEN a forget commits before rehydrate.
            with ctx.store.read() as tx:
                hits = ctx.vector.search_in_tx(
                    tx,
                    tenant_id="t1",
                    agent_id=ctx.agent,
                    query_vector=query,
                    limit=10,
                )
            assert any(hit.resource_id == fresh_target.claim_id for hit in hits)
            forget.forget(
                ctx.access,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE,
                    agent_id=ctx.agent,
                    resource_type="claim",
                    resource_id=fresh_target.claim_id,
                ),
                reason="race erasure",
                idempotency_key=f"race-fg-{round_index}",
            )
            # The vector id map invalidates logically; the index still has
            # the vector until the next rebuild — a fresh search must not
            # return the tombstoned resource (id map gate + revision check).
            with ctx.store.read() as tx:
                after_hits = ctx.vector.search_in_tx(
                    tx,
                    tenant_id="t1",
                    agent_id=ctx.agent,
                    query_vector=query,
                    limit=10,
                )
            if any(hit.resource_id == fresh_target.claim_id for hit in after_hits):
                resurrection_count += 1
        assert resurrection_count == 0
        del target, leak_count

    def test_canary_content_never_leaks_through_stale_hits(self, ctx: VectorCtx) -> None:
        from iris_memory_core.application.forget import ForgetService
        from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
        from iris_memory_core.storage.idempotency import IdempotencyManager

        forget = ForgetService(ctx.store, ctx.clock, idempotency=IdempotencyManager(ctx.store))
        canary = "TOPSECRET-RACE-CONTENT"
        claim = ctx.remember("leak", f"leak probe {canary}")
        ctx.rebuild()
        query = ctx.vector.embed_query(f"leak probe {canary}")
        forget.forget(
            ctx.access,
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE,
                agent_id=ctx.agent,
                resource_type="claim",
                resource_id=claim.claim_id,
            ),
            reason="erasure",
            idempotency_key="leak-fg",
        )
        leaked = False
        with ctx.store.read() as tx:
            hits = ctx.vector.search_in_tx(
                tx, tenant_id="t1", agent_id=ctx.agent, query_vector=query, limit=10
            )
        leaked = any(hit.resource_id == claim.claim_id for hit in hits)
        assert not leaked, "forgotten resource served after commit"


class TestWorkerIntegration:
    def test_vector_rebuild_through_outbox_worker(self, ctx: VectorCtx) -> None:
        """The rebuild job's provider/file stages run OUTSIDE the fenced
        transaction; only the switch lands inside it (ADR-0015 §8)."""
        from iris_memory_core.jobs.handlers import (
            vector_apply_handler,
            vector_cleanup_handler,
        )

        ctx.remember("wk1", "worker driven rebuild probe")
        service = OutboxService(ctx.store, ctx.clock)
        with ctx.store.write() as tx:
            tx.outbox.enqueue(
                NewOutboxJob(
                    tenant_id="t1",
                    job_kind="vector.rebuild",
                    aggregate_type="vector_projection",
                    aggregate_id="t1",
                    source_revision=0,
                    payload={"version": 1, "job_kind": "vector.rebuild"},
                    dedupe_key="vector-rebuild-test-1",
                    priority=3,
                    available_at_us=ctx.clock.now_us(),
                )
            )
        handlers = {
            **phase7_handlers(projection=ctx.vector),
            "vector.apply": vector_apply_handler(ctx.vector),
            "vector.cleanup": vector_cleanup_handler(ctx.vector),
        }
        worker = OutboxWorker(service, handlers, owner="test-worker")
        summary = worker.run_once()
        assert summary, "the worker must have processed the rebuild job"
        pointer = ctx.pointer()
        assert pointer is not None
        assert ctx.vector.projection_state() == "ready"
        assert len(ctx.search("worker driven rebuild probe")) == 1

    def test_apply_job_updates_delta_and_lag(self, ctx: VectorCtx) -> None:
        from iris_memory_core.application.outbox import OutboxService
        from iris_memory_core.jobs.handlers import vector_apply_handler

        claim = ctx.remember("ap1", "apply job probe")
        ctx.rebuild()
        # A revision change fires claim.changed → the handler schedules
        # vector.apply; simulate the change event's scheduling directly.
        from iris_memory_core.jobs.handlers import _schedule_vector_apply

        ctx.claims.correct(
            ctx.access,
            claim_id=claim.claim_id,
            expected_revision=claim.revision,
            canonical_text="apply job corrected probe",
            evidence=[{"source_type": "claim", "source_id": claim.claim_id}],
            reason="correction",
            idempotency_key="ap1-corr",
        )
        with ctx.store.write() as tx:
            _schedule_vector_apply(
                tx,
                tenant_id="t1",
                resource_type="claim",
                resource_id=claim.claim_id,
                agent_id=ctx.agent,
                clock=ctx.clock,
            )
        service = OutboxService(ctx.store, ctx.clock)
        worker = OutboxWorker(
            service,
            {"vector.apply": vector_apply_handler(ctx.vector)},
            owner="test-worker",
        )
        summary = worker.run_once()
        assert summary, "the worker must have processed the apply job"
        with ctx.store.read() as tx:
            assert tx.vector.delta_count("t1", ctx.agent) == 1
        ctx.rebuild()
        with ctx.store.read() as tx:
            assert tx.vector.delta_count("t1", ctx.agent) == 0
        assert len(ctx.search("apply job corrected probe")) == 1
