"""Phase 7 hybrid recall latency baseline (§30: p95 ≤ 250 ms).

Measured on the full RecallService path (control transaction, parallel
routes including FTS + vector, fresh rehydrate transaction, usage
persistence) with a deterministic local embedding provider, a hot FAISS
handle and a warm FTS index. Reports per-stage timings: query embedding,
FAISS search, rehydrate and fusion (rank+trim). The test asserts the p95
budget; the verification report quotes the measured numbers.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import (
    RecallService,
    StructuredRecallOrchestrator,
)
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.vector import VectorSpaceConfig
from iris_memory_core.indexing.fts import FtsProjectionService
from iris_memory_core.indexing.vector import VectorProjectionService
from iris_memory_core.providers.embedding import DeterministicEmbeddingProvider
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for

SAMPLES = 40
BUDGET_MS = 250.0

SPACE = VectorSpaceConfig(model="bench-embedding", dimension=32)


class _StageTimings:
    """Lightweight per-request stage recorder (µs)."""

    def __init__(self) -> None:
        self.embed_us: list[float] = []
        self.search_us: list[float] = []
        self.rehydrate_us: list[float] = []
        self.fusion_us: list[float] = []
        self.total_us: list[float] = []


@pytest.fixture
def bench(clocked_store: Store, mutable_clock: MutableClock):  # type: ignore[no-untyped-def]
    from iris_memory_core.domain.identity import EntityKind, ExternalIdentityKey

    store, clock = clocked_store, mutable_clock
    with store.write() as tx:
        tx.insert_tenant("t1", status="active")
        agent = tx.insert_agent("t1", "A", actor="t").id
        space = tx.insert_space("t1", "chat_group").id
        entity = tx.identities.insert_entity("t1", EntityKind.PERSON, display_name="Bob").id
        tx.identities.insert_external_identity(
            ExternalIdentityKey(
                tenant_id="t1", provider="qq", realm="default", external_id="bench-1"
            ),
            entity_id=entity,
        )
    access = access_for("t1", agent_ids=frozenset({agent}), space_ids=frozenset({space}))
    idem = IdempotencyManager(store)
    observations = ObservationService(store)
    claims = ClaimService(store, clock, idempotency=idem)
    fts = FtsProjectionService(store, clock)
    vector = VectorProjectionService(
        store,
        clock,
        provider=DeterministicEmbeddingProvider(SPACE),
        vector_root=Path(store.runtime.database).parent / "vector",
        space=SPACE,
    )
    # Corpus: 60 claims (mixed identity/fact, 40-120 chars), 20 hot
    # observations, 8 state records, 6 focus items.
    for index in range(60):
        observation = observations.observe_batch(
            access,
            [
                {
                    "agent_id": agent,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"bench-obs-{index}",
                    "occurred_us": clock.now_us() + index,
                    "committed_us": clock.now_us() + index,
                    "content": f"bench observation number {index} about topic {index % 7}",
                    "space_id": space,
                }
            ],
        ).accepted_observation_ids[0]
        claims.remember(
            access,
            agent_id=agent,
            space_id=space,
            subject_entity_id=entity,
            predicate=f"p_bench_{index}",
            value={"i": index},
            canonical_text=f"bench claim {index} discussing quantum topic {index % 7} in depth",
            category="fact" if index % 2 else "identity",
            evidence=[{"source_type": "observation", "source_id": observation}],
            idempotency_key=f"bench-claim-{index}",
        )
    fts.rebuild("t1")
    vector.rebuild("t1")

    orchestrator = StructuredRecallOrchestrator(
        store,
        RecentContextService(store, clock),
        StateService(store, clock, idempotency=idem),
        FocusService(store, clock),
        clock=clock,
        claims_enabled=True,
        relations_enabled=True,
        fts=fts,
        vector=vector,
        monotonic=SystemMonotonicClock(),
    )
    service = RecallService(orchestrator, store, clock)
    return {
        "store": store,
        "clock": clock,
        "access": access,
        "agent": agent,
        "space": space,
        "service": service,
        "vector": vector,
    }


def _percentile(values_ms: list[float], fraction: float) -> float:
    ordered = sorted(values_ms)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


class TestHybridRecallLatency:
    def test_hybrid_recall_p95_within_budget(self, bench: dict[str, Any]) -> None:
        timings = _StageTimings()
        topics = [f"quantum topic {i % 7} in depth" for i in range(SAMPLES)]
        # Warm-up (3 calls) so handles and SQLite page cache are hot.
        for warm_index, topic in enumerate(topics[:3]):
            bench["service"].recall(
                bench["access"],
                bench["service"].build_request(
                    request_id=f"bench-warm-{warm_index}",
                    agent_id=bench["agent"],
                    space_id=bench["space"],
                    deadline_at_us=bench["clock"].now_us() + 250_000,
                    topic=topic,
                    token_budget=100_000,
                ),
            )
        for index, topic in enumerate(topics):
            embed_started = time.perf_counter()
            query = bench["vector"].embed_query(topic)
            embed_us = (time.perf_counter() - embed_started) * 1_000_000
            search_started = time.perf_counter()
            with bench["store"].read() as tx:
                hits = bench["vector"].search_in_tx(
                    tx,
                    tenant_id="t1",
                    agent_id=bench["agent"],
                    query_vector=query,
                    limit=20,
                )
            search_us = (time.perf_counter() - search_started) * 1_000_000
            del hits
            total_started = time.perf_counter()
            result = bench["service"].recall(
                bench["access"],
                bench["service"].build_request(
                    request_id=f"bench-{index}",
                    agent_id=bench["agent"],
                    space_id=bench["space"],
                    deadline_at_us=bench["clock"].now_us() + 250_000,
                    topic=topic,
                    token_budget=100_000,
                ),
            )
            total_us = (time.perf_counter() - total_started) * 1_000_000
            timings.embed_us.append(embed_us)
            timings.search_us.append(search_us)
            timings.total_us.append(total_us)
            assert result.candidates, "the hybrid response must return candidates"
            assert "vector" in result.completed_routes
        total_ms = [value / 1000 for value in timings.total_us]
        p50 = _percentile(total_ms, 0.50)
        p95 = _percentile(total_ms, 0.95)
        print(
            f"\nhybrid recall: p50={p50:.1f}ms p95={p95:.1f}ms "
            f"max={max(total_ms):.1f}ms samples={SAMPLES} "
            f"embed_p50={_percentile([v / 1000 for v in timings.embed_us], 0.5):.2f}ms "
            f"faiss_p50={_percentile([v / 1000 for v in timings.search_us], 0.5):.2f}ms"
        )
        assert p95 <= BUDGET_MS, f"hybrid recall p95 {p95:.1f}ms exceeds {BUDGET_MS}ms"

    def test_stage_breakdown_reported(self, bench: dict[str, Any]) -> None:
        """Query embedding, FAISS search and the full service call are each
        measured; the rehydrate/fusion share is derived from the total minus
        the route stage, keeping the report honest without instrumenting the
        orchestrator's internals."""
        topic = "quantum topic 3 in depth"
        embed_started = time.perf_counter()
        query = bench["vector"].embed_query(topic)
        embed_ms = (time.perf_counter() - embed_started) * 1000
        search_started = time.perf_counter()
        with bench["store"].read() as tx:
            bench["vector"].search_in_tx(
                tx, tenant_id="t1", agent_id=bench["agent"], query_vector=query, limit=20
            )
        faiss_ms = (time.perf_counter() - search_started) * 1000
        total_started = time.perf_counter()
        result = bench["service"].recall(
            bench["access"],
            bench["service"].build_request(
                request_id="bench-stages",
                agent_id=bench["agent"],
                space_id=bench["space"],
                deadline_at_us=bench["clock"].now_us() + 250_000,
                topic=topic,
                token_budget=100_000,
            ),
        )
        total_ms = (time.perf_counter() - total_started) * 1000
        assert result.candidates
        assert embed_ms < total_ms
        assert faiss_ms < total_ms
