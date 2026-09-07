"""Phase 7 recall route integration tests (P7-HYBRID-01, ADR-0015 §6).

The vector route rides the Phase 6 three-stage orchestrator unchanged:
envelope, deadline share, candidate caps, final canonical rehydrate, usage
stages and degradation reporting. Covers hybrid fusion (FTS+vector dedup),
stable degraded reasons (each ≥20 rounds), as_of exclusion, replay
determinism (100 rounds), usage subsetting and the provider-unavailable
boundary.
"""

from __future__ import annotations

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
from iris_memory_core.domain.recall import ROUTE_VECTOR
from iris_memory_core.indexing.fts import FtsProjectionService
from iris_memory_core.indexing.vector import VectorProjectionService
from iris_memory_core.providers.embedding import DeterministicEmbeddingProvider
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for
from tests.integration.recall.vector_helpers import vector_space

DEGRADED_ROUNDS = 20
REPLAY_ROUNDS = 100


class _World:
    _request_counter = 0

    def __init__(self, store: Store, clock: MutableClock) -> None:
        from iris_memory_core.domain.identity import EntityKind, ExternalIdentityKey

        self.store = store
        self.clock = clock
        with store.write() as tx:
            tx.insert_tenant("t1", status="active")
            self.agent = tx.insert_agent("t1", "A", actor="t").id
            self.space = tx.insert_space("t1", "chat_group").id
            self.other_space = tx.insert_space("t1", "direct").id
            self.entity = tx.identities.insert_entity(
                "t1", EntityKind.PERSON, display_name="Bob"
            ).id
            tx.identities.insert_external_identity(
                ExternalIdentityKey(
                    tenant_id="t1", provider="qq", realm="default", external_id="bob-1"
                ),
                entity_id=self.entity,
            )
        self.access = access_for(
            "t1",
            agent_ids=frozenset({self.agent}),
            space_ids=frozenset({self.space, self.other_space}),
        )
        idem = IdempotencyManager(store)
        self.observations = ObservationService(store)
        self.claims = ClaimService(store, clock, idempotency=idem)
        self.fts = FtsProjectionService(store, clock)
        self.vector = VectorProjectionService(
            store,
            clock,
            provider=DeterministicEmbeddingProvider(vector_space()),
            vector_root=Path(store.runtime.database).parent / "vector",
            space=vector_space(),
        )
        orchestrator = StructuredRecallOrchestrator(
            store,
            RecentContextService(store, clock),
            StateService(store, clock, idempotency=idem),
            FocusService(store, clock),
            clock=clock,
            claims_enabled=True,
            relations_enabled=True,
            fts=self.fts,
            vector=self.vector,
            monotonic=SystemMonotonicClock(),
        )
        self.service = RecallService(orchestrator, store, clock)

    def remember(self, key: str, text: str, **overrides: Any) -> Any:
        observation = self.observations.observe_batch(
            self.access,
            [
                {
                    "agent_id": self.agent,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"obs-{key}",
                    "occurred_us": self.clock.now_us(),
                    "committed_us": self.clock.now_us(),
                    "content": f"evidence for {key}",
                    "space_id": self.space,
                }
            ],
        ).accepted_observation_ids[0]
        payload: dict[str, Any] = {
            "agent_id": self.agent,
            "space_id": self.space,
            "subject_entity_id": self.entity,
            "predicate": f"p_{key}",
            "value": {"k": key},
            "canonical_text": text,
            "category": "fact",
            "evidence": [{"source_type": "observation", "source_id": observation}],
            "idempotency_key": f"idem-{key}",
        }
        payload.update(overrides)
        return self.claims.remember(self.access, **payload)

    def rebuild(self) -> None:
        self.fts.rebuild("t1")
        self.vector.rebuild("t1")

    def recall(self, topic: str, **overrides: Any) -> Any:
        type(self)._request_counter += 1
        request = self.service.build_request(
            request_id=overrides.pop(
                "request_id", f"req-{topic[:8]}-{type(self)._request_counter}"
            ),
            agent_id=self.agent,
            space_id=self.space,
            deadline_at_us=self.clock.now_us() + 1_000_000,
            topic=topic,
            purpose="reply",
            token_budget=100_000,
            **overrides,
        )
        return self.service.recall(self.access, request)


@pytest.fixture
def world(clocked_store: Store, mutable_clock: MutableClock) -> _World:
    return _World(clocked_store, mutable_clock)


class TestVectorRoute:
    def test_vector_completes_and_returns_rehydrated_candidates(self, world: _World) -> None:
        world.remember("vr1", "solar panel installation notes")
        world.rebuild()
        result = world.recall("solar panel installation notes")
        assert ROUTE_VECTOR in result.completed_routes
        # The v3 ranker deduplicates by canonical resource: a claim hit by
        # claims/FTS/vector surfaces exactly ONCE (the best-scored route's
        # instance) — never as one candidate per route.
        hits = [c for c in result.candidates if c.resource_type == "claim"]
        assert hits, "vector-visible claim should surface"
        assert len({c.resource_id for c in hits}) == len(hits), "duplicate resources returned"
        for candidate in hits:
            assert candidate.content_hash
            # The surviving instance keeps its own route's score set
            # (missing ≠ 0, ADR-0014 §5) and a real final score.
            assert 0.0 <= candidate.final_score <= 1.0
            assert isinstance(candidate.missing_components, tuple)

    def test_vector_and_fts_hits_deduplicate_to_one_budget_slot(self, world: _World) -> None:
        """ADR-0015 §6: the same resource surfacing through several routes
        occupies exactly ONE budget slot — the v3 ranker keeps the
        best-scored instance (stable order) and drops the duplicates before
        budgeting; the conflict pass still marks DISTINCT disputing
        resources, but same-resource duplicates never coexist."""
        world.remember("dup1", "duplicate detection target text")
        world.rebuild()
        result = world.recall("duplicate detection target text")
        by_resource: dict[str, list[str]] = {}
        for candidate in result.candidates:
            by_resource.setdefault(candidate.resource_id, []).append(candidate.route)
        for resource_id, routes in by_resource.items():
            assert len(routes) == 1, (
                f"resource {resource_id} occupies {len(routes)} budget slots: {routes}"
            )

    def test_as_of_degrades_vector_with_stable_reason(self, world: _World) -> None:
        world.remember("asof1", "as of exclusion probe")
        world.rebuild()
        for round_index in range(DEGRADED_ROUNDS):
            result = world.recall("as of exclusion probe", as_of_us=1_699_000_000_000_000)
            vector_degraded = [d for d in result.degraded_routes if d.route == ROUTE_VECTOR]
            assert vector_degraded, round_index
            assert vector_degraded[0].reason_code == "vector_as_of_unsupported"
            assert vector_degraded[0].retryable is False
            assert vector_degraded[0].fallback == "route_skipped_canonical_intact"
            # The claim stays reachable through the structured claims route.
            assert "claims" in result.completed_routes

    def test_never_built_degrades_rebuild_pending(self, world: _World) -> None:
        world.remember("nb1", "never built probe")
        for round_index in range(DEGRADED_ROUNDS):
            result = world.recall("never built probe")
            vector_degraded = [d for d in result.degraded_routes if d.route == ROUTE_VECTOR]
            assert vector_degraded, round_index
            assert vector_degraded[0].reason_code == "vector_rebuild_pending"
            assert vector_degraded[0].retryable is True
            assert result.partial is True
            assert "claims" in result.completed_routes

    def test_provider_unavailable_degrades_without_blocking_others(self, world: _World) -> None:
        from iris_memory_core.domain.vector import EmbeddingProviderError

        world.remember("pu1", "provider outage probe")
        world.rebuild()

        def broken(
            texts: list[str], *, deadline_monotonic_us: int | None = None
        ) -> list[list[float]]:
            raise EmbeddingProviderError("timeout", retryable=True)

        original = world.vector._provider.embed_batch
        world.vector._provider.embed_batch = broken
        try:
            for round_index in range(DEGRADED_ROUNDS):
                result = world.recall("provider outage probe")
                vector_degraded = [d for d in result.degraded_routes if d.route == ROUTE_VECTOR]
                assert vector_degraded, round_index
                assert vector_degraded[0].reason_code == "vector_unavailable"
                assert vector_degraded[0].retryable is True
                assert "fts" in result.completed_routes
                assert "claims" in result.completed_routes
        finally:
            world.vector._provider.embed_batch = original

    def test_stale_generation_degrades_with_stable_reason(self, world: _World) -> None:
        world.remember("st1", "stale generation probe")
        world.rebuild()
        # Force unincorporated lag with a zero-tolerance projection bound to
        # the same root — the route reads the same pointer, so the gate trips.
        strict = VectorProjectionService(
            world.store,
            world.clock,
            provider=DeterministicEmbeddingProvider(vector_space()),
            vector_root=Path(world.store.runtime.database).parent / "vector",
            space=vector_space(),
            staleness_limit=0,
        )
        world.vector = strict
        # Re-wire the orchestrator's route through the strict projection.
        orchestrator = StructuredRecallOrchestrator(
            world.store,
            RecentContextService(world.store, world.clock),
            StateService(world.store, world.clock, idempotency=IdempotencyManager(world.store)),
            FocusService(world.store, world.clock),
            clock=world.clock,
            claims_enabled=True,
            relations_enabled=True,
            fts=world.fts,
            vector=strict,
            monotonic=SystemMonotonicClock(),
        )
        world.service = RecallService(orchestrator, world.store, world.clock)
        first = world.remember("st2", "lag inducing claim")
        with world.store.write() as tx:
            strict.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=first.claim_id
            )
        for round_index in range(DEGRADED_ROUNDS):
            result = world.recall("stale generation probe")
            vector_degraded = [d for d in result.degraded_routes if d.route == ROUTE_VECTOR]
            assert vector_degraded, round_index
            assert vector_degraded[0].reason_code == "vector_generation_stale"
            assert vector_degraded[0].retryable is True
        strict.manager.drop_current()

    def test_resource_type_filter_excludes_vector(self, world: _World) -> None:
        world.remember("rt1", "resource type filter probe")
        world.rebuild()
        result = world.recall("resource type filter probe", resource_types=frozenset({"claim"}))
        assert ROUTE_VECTOR in result.completed_routes
        result = world.recall("resource type filter probe", resource_types=frozenset({"task"}))
        # Nothing indexable for tasks: both search routes complete empty.
        assert ROUTE_VECTOR in result.completed_routes
        assert not [c for c in result.candidates if c.route == ROUTE_VECTOR]

    def test_replay_determinism_100_rounds(self, world: _World) -> None:
        world.remember("rp1", "replay determinism target alpha")
        world.remember("rp2", "replay determinism target beta")
        world.rebuild()

        def signature(result: Any) -> tuple[Any, ...]:
            return (
                tuple(c.candidate_id for c in result.candidates),
                tuple(
                    (c.candidate_id, round(c.final_score, 6), c.conflict_state)
                    for c in result.candidates
                ),
                tuple(result.completed_routes),
                tuple(
                    (d.route, d.reason_code, d.retryable, d.fallback)
                    for d in result.degraded_routes
                ),
                result.partial,
                result.dropped_by_rehydrate,
            )

        seen = {signature(world.recall("replay determinism target", request_id="replay-req-1"))}
        for _round in range(REPLAY_ROUNDS - 1):
            seen.add(
                signature(world.recall("replay determinism target", request_id="replay-req-1"))
            )
        assert len(seen) == 1, "replays must be byte-identical"

    def test_usage_subset_property_holds_with_vector(self, world: _World) -> None:
        from iris_memory_core.application.recall import (
            RecallUsageReportInput,
            RecallUsageService,
        )

        world.remember("us1", "usage subset target")
        world.rebuild()
        result = world.recall("usage subset target", request_id="usage-req-1")
        assert result.candidates
        returned = [c.candidate_id for c in result.candidates]
        usage = RecallUsageService(world.store, world.clock)
        host_selected = returned[:2]
        model_visible = returned[:1]
        outcome = usage.report(
            world.access,
            RecallUsageReportInput(
                request_id="usage-req-1",
                host_cycle_id="cycle-1",
                returned_candidate_ids=tuple(returned),
                host_selected_candidate_ids=tuple(host_selected),
                model_visible_candidate_ids=tuple(model_visible),
                persona_revision=result.persona_revision,
                reported_at_us=world.clock.now_us(),
            ),
        )
        assert outcome.created is True
        assert outcome.model_visible_count == 1
        # A forged chain (visible ⊄ selected) is rejected.
        from iris_memory_core.domain.errors import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            usage.report(
                world.access,
                RecallUsageReportInput(
                    request_id="usage-req-1",
                    host_cycle_id="cycle-2",
                    returned_candidate_ids=tuple(returned),
                    host_selected_candidate_ids=tuple(model_visible),
                    model_visible_candidate_ids=tuple(host_selected),
                    persona_revision=result.persona_revision,
                    reported_at_us=world.clock.now_us(),
                ),
            )

    def test_trace_reports_vector_route_without_content(self, world: _World) -> None:
        world.remember("tr1", "TRACE-CANARY-CONTENT probe")
        world.rebuild()
        result = world.recall("TRACE-CANARY-CONTENT probe", include_trace=True)
        assert result.trace is not None
        vector_traces = [t for t in result.trace.routes if t.route == ROUTE_VECTOR]
        assert vector_traces and vector_traces[0].outcome == "completed"
        rendered = repr(result.trace)
        assert "TRACE-CANARY-CONTENT" not in rendered

    def test_vector_candidates_rehydrate_against_current_revision(self, world: _World) -> None:
        """A candidate whose revision moved between build and recall must be
        dropped by the final rehydrate — stale vectors never serve."""
        result_remember = world.remember("rh1", "rehydrate revision guard probe")
        world.rebuild()
        # Supersede the claim AFTER the generation was built.
        world.claims.correct(
            world.access,
            claim_id=result_remember.claim_id,
            expected_revision=result_remember.revision,
            canonical_text="corrected rehydrate guard probe",
            evidence=[{"source_type": "claim", "source_id": result_remember.claim_id}],
            reason="correction",
            idempotency_key="rh1-corr",
        )
        outcome = world.recall("rehydrate revision guard probe")
        for candidate in outcome.candidates:
            if candidate.resource_id == result_remember.claim_id:
                assert candidate.resource_revision == result_remember.revision + 1, (
                    "the old revision must never be returned"
                )
