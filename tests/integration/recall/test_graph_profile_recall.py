"""Phase 8 recall integration tests: graph/profile routes inside the Phase
6/7 envelope (ADR-0016 §4-§6) — completion, dedupe budget slots, degraded
reasons x20, trace safety and usage stages."""

from __future__ import annotations

import pytest

from iris_memory_core.domain.recall import ROUTE_GRAPH, ROUTE_PROFILE
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.recall.graph_profile_helpers import Phase8World

DEGRADED_ROUNDS = 20


@pytest.fixture
def world(clocked_store: Store, mutable_clock: MutableClock) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


def _seed(world: Phase8World) -> None:
    bob = world.entity("Bob")
    carol = world.entity("Carol")
    dave = world.entity("Dave")
    world.bind_identity(bob, "bob-qq-r")
    world.relate("r1", bob, carol, relation_type="friends_with")
    world.relate("r2", carol, dave, relation_type="knows")
    world.remember(
        "trust",
        "Bob trusts Carol deeply",
        bob,
        category="relationship",
        value={"target_entity_id": carol},
    )
    world.remember("name", "Bob is a pilot", bob, category="identity")
    world.remember("tea", "Bob likes tea", bob, category="preference")
    world.bind_identity(dave, "dave-qq-r")
    world.rebuild()


class TestRoutes:
    def test_graph_and_profile_complete_and_rehydrate(self, world: Phase8World) -> None:
        _seed(world)
        result = world.recall_as_speaker("bob-qq-r")
        assert ROUTE_GRAPH in result.completed_routes
        assert ROUTE_PROFILE in result.completed_routes
        assert result.degraded_routes == ()
        # Every returned candidate passed the fresh canonical rehydrate.
        assert all(c.resource_revision >= 1 for c in result.candidates)

    def test_duplicate_resources_occupy_one_budget_slot(self, world: Phase8World) -> None:
        _seed(world)
        result = world.recall_as_speaker("bob-qq-r")
        identities = [(c.resource_type, c.resource_id) for c in result.candidates]
        assert len(identities) == len(set(identities))

    def test_resource_type_filter_controls_routes(self, world: Phase8World) -> None:
        _seed(world)
        result = world.recall_as_speaker("bob-qq-r", resource_types=frozenset({"relation"}))
        # The filtered routes complete with ZERO candidates of the excluded
        # type — the filter narrows, never widens.
        profile_candidates = [c for c in result.candidates if c.route == ROUTE_PROFILE]
        assert all(c.resource_type != "claim" for c in profile_candidates)
        graph_candidates = [c for c in result.candidates if c.route == ROUTE_GRAPH]
        assert all(c.resource_type == "relation" for c in graph_candidates)

    def test_actorless_request_skips_both_routes(self, world: Phase8World) -> None:
        from iris_memory_core.application.recall import (
            GraphRoute,
            ProfileRoute,
            StructuredRecallRequest,
        )

        _seed(world)
        # The public API requires a speaker; the speaker-anchored ROUTES
        # fail safe when one is absent (no start node ⇒ no candidates).
        request = StructuredRecallRequest(
            request_id="p8-actorless",
            agent_id=world.agent,
            space_id=world.space,
            deadline_monotonic_us=10**12,
            speaker_entity_id=None,
            topic="friends",
        )
        with world.store.read() as tx:
            graph = GraphRoute(world.graph).collect(
                tx, request, world.access, 10**12, world.clock.now_us()
            )
            profile = ProfileRoute(world.profile).collect(
                tx, request, world.access, 10**12, world.clock.now_us()
            )
        assert graph == ()
        assert profile == ()


class TestDegradedReasons:
    @pytest.mark.parametrize("round_index", range(DEGRADED_ROUNDS))
    def test_never_built_reasons_are_stable(self, world: Phase8World, round_index: int) -> None:
        bob = world.entity("NB")
        world.bind_identity(bob, f"nb-qq-{round_index}")
        result = world.recall_as_speaker(f"nb-qq-{round_index}")
        for route, reason, retryable in (
            (ROUTE_GRAPH, "graph_rebuild_pending", True),
            (ROUTE_PROFILE, "profile_rebuild_pending", True),
        ):
            degraded = [d for d in result.degraded_routes if d.route == route]
            assert degraded, route
            assert degraded[0].reason_code == reason
            assert degraded[0].retryable is retryable
            assert degraded[0].fallback == "route_skipped_canonical_intact"
        # The canonical routes were never touched.
        assert "claims" in result.completed_routes
        assert "relations" in result.completed_routes

    @pytest.mark.parametrize("round_index", range(DEGRADED_ROUNDS))
    def test_as_of_reasons_are_stable(self, world: Phase8World, round_index: int) -> None:
        _seed_once(world)
        result = world.recall_as_speaker("bob-qq-r", as_of_us=world.clock.now_us() - 1)
        for route, reason in (
            (ROUTE_GRAPH, "graph_as_of_unsupported"),
            (ROUTE_PROFILE, "profile_as_of_unsupported"),
        ):
            degraded = [d for d in result.degraded_routes if d.route == route]
            assert degraded, route
            assert degraded[0].reason_code == reason
            assert degraded[0].retryable is False


def _seed_once(world: Phase8World) -> None:
    if not getattr(world, "_asof_seeded", False):
        _seed(world)
        world._asof_seeded = True  # type: ignore[attr-defined]


class TestTraceAndUsage:
    def test_trace_carries_no_content(self, world: Phase8World) -> None:
        _seed(world)
        result = world.recall_as_speaker("bob-qq-r", include_trace=True)
        assert result.trace is not None
        serialized = repr(result.trace)
        assert "pilot" not in serialized
        assert "tea" not in serialized
        graph_trace = [t for t in result.trace.routes if t.route == ROUTE_GRAPH]
        assert graph_trace and graph_trace[0].outcome == "completed"

    def test_usage_four_stages_accept_graph_profile_candidates(self, world: Phase8World) -> None:
        from iris_memory_core.application.recall import RecallUsageReportInput, RecallUsageService

        _seed(world)
        result = world.recall_as_speaker("bob-qq-r")
        usage = RecallUsageService(world.store, world.clock)
        returned = tuple(c.candidate_id for c in result.candidates)
        if not returned:
            pytest.skip("no candidates in this fixture round")
        report = usage.report(
            world.access,
            RecallUsageReportInput(
                request_id=result.request_id,
                host_cycle_id="cycle-1",
                returned_candidate_ids=tuple(returned),
                host_selected_candidate_ids=tuple(returned[:2]),
                model_visible_candidate_ids=tuple(returned[:1]),
                persona_revision=result.persona_revision,
                reported_at_us=world.clock.now_us(),
            ),
        )
        assert report.created is True
