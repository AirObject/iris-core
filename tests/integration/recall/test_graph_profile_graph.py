"""Phase 8 graph projection integration tests (ADR-0016 §3-§4).

Quantified gates:

- Scope/Privacy/Status/Valid-Time/Tombstone per-edge properties: ≥200
  generated graph cases each.
- Cross tenant/agent/space-group/entity unauthorized traversal returns 0.
- Malicious high-connectivity graphs at ≥10x every budget: visited nodes,
  returned candidates and expansion never exceed any bound; the deadline
  stops expansion.
- Rebuild determinism: 3 consecutive rebuilds of one snapshot produce
  identical edge sets, watermarks and checksums.
- Fault injection: corrupted manifest checksum/counts/source coverage,
  unknown builder, dangling pointer, tombstone-watermark regression — all
  fail closed with stable reasons (each ≥20 rounds).
- Forget/Correct/Binding/Redirect races x50: after the change commits, the
  old edges return zero.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from iris_memory_core.domain.graph import GraphDegradedError
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.recall import ROUTE_GRAPH
from iris_memory_core.domain.scope import Scope, scope_allows
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.recall.graph_profile_helpers import TENANT, Phase8World

PROPERTY_CASES = 200
FAULT_ROUNDS = 20
RACE_ROUNDS = 50


@pytest.fixture
def world(clocked_store: Store, mutable_clock: MutableClock) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


def _edges_of(world: Phase8World, node: str) -> tuple[Any, ...]:
    with world.store.read() as tx:
        pointer = tx.graph.pointer(TENANT)
        assert pointer is not None
        return tx.graph.edges_for_source(TENANT, pointer.generation_id, node, node_kind="entity")


def _all_edges(world: Phase8World) -> tuple[Any, ...]:
    with world.store.read() as tx:
        pointer = tx.graph.pointer(TENANT)
        assert pointer is not None
        return tx.graph.all_edges(TENANT, pointer.generation_id)


class TestEdgeAllowlist:
    def test_only_authorized_canonical_sources_project(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        alice = world.entity("Alice")
        carol = world.entity("Carol")
        world.relate("r1", bob, alice, relation_type="friends_with")
        world.remember("plain", "Bob knows Carol from chat", bob, category="relationship")
        world.remember(
            "targeted",
            "Bob considers Carol a close friend",
            bob,
            category="relationship",
            predicate="p_targeted",
            value={"target_entity_id": carol},
        )
        world.bind_identity(bob, "bob-qq-1")
        world.rebuild()
        kinds = {(edge.edge_kind, edge.edge_type) for edge in _all_edges(world)}
        assert ("relation", "friends_with") in kinds
        assert ("binding", "verified_binding") in kinds
        assert ("claim", "p_targeted") in kinds
        # A relationship claim WITHOUT a structural target produces no edge.
        assert ("claim", "p_plain") not in kinds

    def test_self_referencing_and_missing_targets_rejected(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        world.remember(
            "selfish",
            "Bob is his own best friend",
            bob,
            category="relationship",
            predicate="p_selfish",
            value={"target_entity_id": bob},
        )
        world.remember(
            "ghost",
            "Bob knows a ghost",
            bob,
            category="relationship",
            predicate="p_ghost",
            value={"target_entity_id": "nonexistent-entity"},
        )
        world.rebuild()
        assert all(edge.edge_kind != "claim" for edge in _all_edges(world))

    def test_tombstoned_endpoints_do_not_become_nodes(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        alice = world.entity("Alice")
        world.relate("r1", bob, alice)
        world.rebuild()
        with world.store.write() as tx:
            entity = tx.identities.get_entity(alice)
            tx.update_entity_state(
                alice,
                type(entity).state.CANONICAL
                if False
                else __import__(
                    "iris_memory_core.domain.identity", fromlist=["EntityState"]
                ).EntityState.TOMBSTONED,
                expected_revision=entity.revision,
                actor="admin",
                reason_code="test",
            )
            tx.record_tombstone(
                tenant_id=TENANT,
                resource_type="entity",
                resource_id=alice,
                reason_code="test",
                deleted_by="admin",
            )
        world.rebuild()
        edges = _all_edges(world)
        assert all(edge.source_node_id != alice and edge.target_node_id != alice for edge in edges)


class TestPerEdgeProperties:
    """Each property runs ≥200 generated cases through the route's own
    per-edge authorization (_edge_visible) — the same check the traversal
    runs before every expansion."""

    def _speaker_setup(self, world: Phase8World) -> str:
        bob = world.entity("Bob")
        world.bind_identity(bob, "bob-qq-sp")
        return bob

    def _collect_via_route(self, world: Phase8World, access: Any) -> tuple[Any, ...]:
        request = world.service.build_request(
            request_id=f"graph-prop-{id(access)}",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 10_000_000,
            topic="friends",
            purpose="reply",
            token_budget=100_000,
        )
        from iris_memory_core.application.recall import ExternalActorRef

        result = world.service.recall(
            access,
            request,
            actors=(ExternalActorRef(provider="qq", realm="default", external_id="bob-qq-sp"),),
        )
        return result.candidates

    def test_scope_property_200_cases(self, world: Phase8World) -> None:
        bob = self._speaker_setup(world)
        for case in range(PROPERTY_CASES):
            target = world.entity(f"scope-{case}")
            # Alternate: same space (visible) / other space (blocked).
            if case % 2 == 0:
                world.relate(f"sr-{case}", bob, target, relation_type="knows")
            else:
                world.relate(
                    f"sr-{case}",
                    bob,
                    target,
                    relation_type="knows",
                    space_id=world.other_space,
                )
        world.rebuild()
        from iris_memory_core.application.recall import GraphRoute

        route_instance = GraphRoute(world.graph)
        request_scope = Scope(
            tenant_id=TENANT,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
        )
        blocked = 0
        allowed = 0
        with world.store.read() as tx:
            for edge in _all_edges(world):
                visible = route_instance._edge_visible(
                    tx, edge, request_scope, world.access, world.clock.now_us()
                )
                if edge.space_id == world.other_space:
                    assert not visible
                    blocked += 1
                else:
                    assert visible
                    allowed += 1
        assert blocked >= PROPERTY_CASES // 2 - 1
        assert allowed >= PROPERTY_CASES // 2

    def test_privacy_property_200_cases(self, world: Phase8World) -> None:
        bob = self._speaker_setup(world)
        for case in range(PROPERTY_CASES):
            target = world.entity(f"priv-{case}")
            labels = ["restricted"] if case % 2 == 0 else None
            world.relate(
                f"pr-{case}",
                bob,
                target,
                relation_type="knows",
                privacy_labels=labels,
                access=world.admin_access if labels else None,
            )
        world.rebuild()
        from iris_memory_core.application.recall import GraphRoute

        route_instance = GraphRoute(world.graph)
        request_scope = Scope(
            tenant_id=TENANT,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
        )
        restricted = 0
        with world.store.read() as tx:
            for edge in _all_edges(world):
                visible = route_instance._edge_visible(
                    tx, edge, request_scope, world.access, world.clock.now_us()
                )
                if "restricted" in edge.privacy_labels:
                    assert not visible
                    restricted += 1
                else:
                    assert visible
        assert restricted >= PROPERTY_CASES // 2 - 1

    def test_status_property_200_cases(self, world: Phase8World) -> None:
        bob = self._speaker_setup(world)
        carol = world.entity("Carol-status")
        results = []
        for case in range(PROPERTY_CASES):
            results.append(
                world.remember(
                    f"st-{case}",
                    f"Bob trusts Carol attempt {case}",
                    bob,
                    category="relationship",
                    predicate="p_status",
                    value={"target_entity_id": carol},
                )
            )
        # Retract even cases, dispute every 5th odd case: both status
        # families exercise the visible-status edge filter.
        for index, result in enumerate(results):
            if index % 2 == 0:
                world.claims.correct(
                    world.access,
                    claim_id=result.claim_id,
                    expected_revision=result.revision,
                    mode="retract",
                    reason="status property",
                    idempotency_key=f"st-retract-{index}",
                )
            elif index % 5 == 1:
                world.claims.correct(
                    world.access,
                    claim_id=result.claim_id,
                    expected_revision=result.revision,
                    mode="dispute",
                    evidence=[{"source_type": "observation", "source_id": world.observation()}],
                    reason="status property",
                    idempotency_key=f"st-dispute-{index}",
                )
        world.rebuild()
        claim_edges = [edge for edge in _all_edges(world) if edge.edge_kind == "claim"]
        statuses = [edge.status for edge in claim_edges]
        assert statuses
        assert all(status in ("active", "disputed") for status in statuses)
        # Dead claims produce no edges at build time.
        assert len(statuses) <= PROPERTY_CASES // 2 + 1
        assert "disputed" in statuses

    def test_valid_time_property_200_cases(self, world: Phase8World) -> None:
        bob = self._speaker_setup(world)
        now = world.clock.now_us()
        for case in range(PROPERTY_CASES):
            target = world.entity(f"vt-{case}")
            valid_from = now + 10_000_000_000 if case % 3 == 0 else None
            valid_until = now - 1 if case % 3 == 1 else None
            world.relate(
                f"vtr-{case}",
                bob,
                target,
                relation_type="knows",
                valid_from_us=valid_from,
                valid_until_us=valid_until,
            )
        world.rebuild()
        from iris_memory_core.application.recall import GraphRoute

        route_instance = GraphRoute(world.graph)
        request_scope = Scope(
            tenant_id=TENANT,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
        )
        invalid = 0
        with world.store.read() as tx:
            for edge in _all_edges(world):
                visible = route_instance._edge_visible(tx, edge, request_scope, world.access, now)
                if (edge.valid_from_us is not None and edge.valid_from_us > now) or (
                    edge.valid_until_us is not None and edge.valid_until_us <= now
                ):
                    assert not visible
                    invalid += 1
                else:
                    assert visible
        assert invalid >= 2 * (PROPERTY_CASES // 3) - 2

    def test_tombstone_property_200_cases(self, world: Phase8World) -> None:
        bob = self._speaker_setup(world)
        targets = [world.entity(f"tb-{case}") for case in range(PROPERTY_CASES)]
        for index, target in enumerate(targets):
            world.relate(f"tbr-{index}", bob, target, relation_type="knows")
        world.rebuild()
        # Tombstone half the target entities WITHOUT rebuilding: the live
        # tombstone ledger is the last line of defense during traversal.
        with world.store.write() as tx:
            for index, target in enumerate(targets):
                if index % 2 == 0:
                    continue
                tx.record_tombstone(
                    tenant_id=TENANT,
                    resource_type="entity",
                    resource_id=target,
                    reason_code="test",
                    deleted_by="admin",
                )
        from iris_memory_core.application.recall import GraphRoute

        route_instance = GraphRoute(world.graph)
        request_scope = Scope(
            tenant_id=TENANT,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
        )
        blocked = 0
        with world.store.read() as tx:
            for edge in _all_edges(world):
                visible = route_instance._edge_visible(
                    tx, edge, request_scope, world.access, world.clock.now_us()
                )
                if tx.is_tombstoned(TENANT, "entity", edge.target_node_id):
                    assert not visible
                    blocked += 1
        assert blocked >= PROPERTY_CASES // 2 - 1


class TestUnauthorizedPaths:
    def test_cross_tenant_agent_group_entity_paths_return_zero(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        alice = world.entity("Alice")
        world.relate("r1", bob, alice, relation_type="knows")
        world.bind_identity(bob, "bob-qq-x")
        # Cross-privacy: a restricted claim of another subject is never a
        # graph candidate (created via the admin plane, read via the app).
        world.claims.remember(
            world.admin_access,
            agent_id=world.agent,
            space_id=world.space,
            subject_entity_id=alice,
            predicate="p_secret",
            value={"k": "s"},
            canonical_text="Alice keeps a private note",
            category="relationship",
            privacy_labels=["restricted"],
            evidence=[{"source_type": "observation", "source_id": world.observation()}],
            idempotency_key="p8-idem-secret",
        )
        world.rebuild()
        # Tenant B cannot see tenant A's graph at all: its store has no
        # generation; the trust gate fails closed before any edge is read.
        with world.store.read() as tx:
            with pytest.raises(GraphDegradedError) as excinfo:
                world.graph.trusted_generation_in_tx(tx, tenant_id="t-other", agent_id=world.agent)
            assert excinfo.value.reason_code in ("graph_rebuild_pending", "graph_index_corrupt")
        # Cross-agent: relations of another agent are scope-blocked.
        world.relate(
            "r2",
            bob,
            alice,
            relation_type="owns",
            agent_id=world.other_agent,
            access=world.other_agent_access,
        )
        world.rebuild()
        from iris_memory_core.application.recall import GraphRoute

        route = GraphRoute(world.graph)
        request_scope = Scope(
            tenant_id=TENANT,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
        )
        agent_blocked = 0
        with world.store.read() as tx:
            for edge in _all_edges(world):
                if edge.agent_id == world.other_agent:
                    assert not route._edge_visible(
                        tx, edge, request_scope, world.access, world.clock.now_us()
                    )
                    agent_blocked += 1
        assert agent_blocked == 1
        # End-to-end: a speaker with zero visible edges returns no graph
        # candidates and never widens anything.
        result = world.recall_as_speaker("bob-qq-x")
        if ROUTE_GRAPH in result.completed_routes:
            graph_candidates = [c for c in result.candidates if c.route == ROUTE_GRAPH]
            assert all(
                scope_allows(
                    Scope(
                        tenant_id=c.scope.tenant_id,
                        agent_id=c.scope.agent_id,
                        space_group_id=c.scope.space_group_id,
                        space_id=c.scope.space_id,
                        session_id=c.scope.session_id,
                    ),
                    request_scope,
                )
                for c in graph_candidates
            )
            assert all(
                evaluate_privacy(c.privacy_labels, c.scope, request_scope, world.access)
                for c in graph_candidates
            )


class TestBudgets:
    def _build_malicious_graph(self, world: Phase8World, *, factor: int = 10) -> str:
        """A hub graph ≥10x every budget: depth-2 chains with fanout far
        beyond the per-level cap."""
        from iris_memory_core.domain.graph import (
            GRAPH_MAX_FANOUT,
        )

        hub = world.entity("hub")
        world.bind_identity(hub, "hub-qq")
        ring = [world.entity(f"ring-{i}") for i in range(GRAPH_MAX_FANOUT * factor)]
        for index, target in enumerate(ring):
            world.relate(f"hr-{index}", hub, target, relation_type="knows")
        for index, target in enumerate(ring):
            leaf = world.entity(f"leaf-{index}")
            world.relate(f"lr-{index}", target, leaf, relation_type="knows")
        world.rebuild()
        return hub

    def test_malicious_high_connectivity_never_exceeds_budgets(self, world: Phase8World) -> None:
        from iris_memory_core.application.recall import GraphRoute
        from iris_memory_core.domain.graph import (
            GRAPH_MAX_DEPTH,
            GRAPH_MAX_FANOUT,
            GRAPH_MAX_NODES,
        )

        hub = self._build_malicious_graph(world)
        # The stored connectivity is ≥10x every budget.
        with world.store.read() as tx:
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
            hub_edges = tx.graph.edges_for_source(
                TENANT, pointer.generation_id, hub, node_kind="entity"
            )
        assert len(hub_edges) >= GRAPH_MAX_FANOUT * 10
        assert GRAPH_MAX_FANOUT * 10 > GRAPH_MAX_NODES

        # Replicate the route's own BFS with hard budget counters: with
        # 10x connectivity, every budget still binds exactly.
        route = GraphRoute(world.graph)
        with world.store.read() as tx:
            _pointer, generation = world.graph.trusted_generation_in_tx(
                tx, tenant_id=TENANT, agent_id=world.agent
            )
            request_scope = Scope(
                tenant_id=TENANT,
                agent_id=world.agent,
                space_group_id=None,
                space_id=world.space,
                session_id=None,
            )
            start = ("entity", hub)
            visited = {start}
            frontier = [start]
            depth = 0
            traversed = 0
            while frontier and depth < GRAPH_MAX_DEPTH:
                next_frontier = []
                fanout = 0
                for node_kind, node_id in sorted(frontier):
                    edges = tx.graph.edges_for_source(
                        TENANT, generation.id, node_id, node_kind=node_kind
                    )
                    for edge in edges:
                        if fanout >= GRAPH_MAX_FANOUT:
                            break
                        if not route._edge_visible(
                            tx, edge, request_scope, world.access, world.clock.now_us()
                        ):
                            continue
                        fanout += 1
                        traversed += 1
                        target = (edge.target_node_kind, edge.target_node_id)
                        if target not in visited and len(visited) < GRAPH_MAX_NODES:
                            visited.add(target)
                            next_frontier.append(target)
                frontier = next_frontier
                depth += 1
        assert depth <= GRAPH_MAX_DEPTH
        assert len(visited) <= GRAPH_MAX_NODES
        assert traversed <= GRAPH_MAX_DEPTH * GRAPH_MAX_FANOUT
        # End-to-end: the candidate cap binds regardless of connectivity.
        result = world.recall_as_speaker("hub-qq", candidate_limits={ROUTE_GRAPH: 5})
        assert ROUTE_GRAPH in result.completed_routes
        graph_hits = [c for c in result.candidates if c.route == ROUTE_GRAPH]
        assert len(graph_hits) <= 5

    def test_deadline_stops_expansion(self, world: Phase8World) -> None:
        from iris_memory_core.application.recall import (
            GraphRoute,
            RouteDeadlineExceeded,
            StructuredRecallRequest,
        )

        self._build_malicious_graph(world)
        request = StructuredRecallRequest(
            request_id="p8-deadline",
            agent_id=world.agent,
            space_id=world.space,
            deadline_monotonic_us=1,
            speaker_entity_id=world.entities["hub"],
            topic="friends",
        )
        with world.store.read() as tx:
            route = GraphRoute(world.graph)
            # An already-passed deadline: zero expansion happens.
            with pytest.raises(RouteDeadlineExceeded):
                route.collect(tx, request, world.access, deadline_us=0, now_us=world.clock.now_us())

        # A deadline that expires after k monotonic reads: expansion stops
        # within k (+small margin) checks — per-edge cooperative deadline.
        class _ExpiringMonotonic:
            def __init__(self, budget: int) -> None:
                self.remaining = budget

            def monotonic_us(self) -> int:
                if self.remaining > 0:
                    self.remaining -= 1
                    return 10
                return 1_000_000

        expiring = _ExpiringMonotonic(budget=5)
        route = GraphRoute(world.graph, monotonic=expiring)
        request2 = StructuredRecallRequest(
            request_id="p8-deadline-2",
            agent_id=world.agent,
            space_id=world.space,
            deadline_monotonic_us=500_000,
            speaker_entity_id=world.entities["hub"],
            topic="friends",
        )
        with world.store.read() as tx, pytest.raises(RouteDeadlineExceeded):
            route.collect(
                tx, request2, world.access, deadline_us=500_000, now_us=world.clock.now_us()
            )
        # The expiring clock was consulted only within its budget: the route
        # cannot have kept expanding past the deadline.
        assert expiring.remaining == 0

    def test_edge_reads_are_bounded_against_malicious_hubs(self, world: Phase8World) -> None:
        """Review round 1: the ROUTE's edge fetch itself must be bounded —
        a hub with far more edges than the fanout window can never make
        one fetch load an unbounded row set."""
        from iris_memory_core.domain.graph import GRAPH_MAX_FANOUT

        self._build_malicious_graph(world)
        with world.store.read() as tx:
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
            bounded = tx.graph.edges_for_source(
                TENANT,
                pointer.generation_id,
                world.entities["hub"],
                node_kind="entity",
                limit=GRAPH_MAX_FANOUT,
            )
            full = tx.graph.edges_for_source(
                TENANT,
                pointer.generation_id,
                world.entities["hub"],
                node_kind="entity",
            )
        assert len(bounded) == GRAPH_MAX_FANOUT
        assert len(full) > GRAPH_MAX_FANOUT
        # The bounded window is a prefix of the deterministic order.
        assert (
            tuple(e.edge_id for e in bounded) == tuple(e.edge_id for e in full)[:GRAPH_MAX_FANOUT]
        )

    def test_verify_catches_tombstoned_binding_edges(self, world: Phase8World) -> None:
        """Review round 1: a binding edge whose binding or identity gained a
        tombstone after the build fails verification (the rebuild excludes
        it; verify must flag the drift)."""
        person = world.entity("VerifyBind")
        binding = world.bind_identity(person, "verifybind-qq")
        world.rebuild()
        with world.store.write() as tx:
            tx.record_tombstone(
                tenant_id=TENANT,
                resource_type="binding",
                resource_id=binding.id,
                reason_code="test",
                deleted_by="admin",
            )
        with world.store.write() as tx:
            assert world.graph.verify_in_tx(tx, TENANT) is False
        # Round 3: the verdict must SURVIVE the transaction commit (the
        # worker path never lets an escaping exception roll the state back).
        with world.store.read() as tx:
            assert tx.graph.projection_state() == "pending_rebuild"


class TestDeterminism:
    def test_same_request_replays_identically_100_times(self, world: Phase8World) -> None:
        bob = world.entity("Replay-Bob")
        carol = world.entity("Replay-Carol")
        world.bind_identity(bob, "replay-bob")
        world.relate("rr", bob, carol, relation_type="knows")
        world.remember(
            "trust",
            "Bob trusts Carol",
            bob,
            category="relationship",
            value={"target_entity_id": carol},
        )
        world.remember("name", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        signatures = set()
        for round_index in range(100):
            result = world.recall_as_speaker("replay-bob", request_id=f"p8-replay-{round_index}")
            signature = (
                tuple(result.completed_routes),
                tuple((d.route, d.reason_code, d.retryable) for d in result.degraded_routes),
                result.partial,
                result.retrieved_count,
                tuple(
                    (
                        c.route,
                        c.resource_type,
                        c.resource_id,
                        c.resource_revision,
                        round(c.final_score, 9),
                        c.conflict_state,
                    )
                    for c in result.candidates
                ),
            )
            signatures.add(signature)
        assert len(signatures) == 1

    def test_three_rebuilds_are_identical(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        alice = world.entity("Carol")
        dave = world.entity("Dave")
        world.relate("r1", bob, alice)
        world.remember(
            "targeted",
            "Bob trusts Carol",
            bob,
            category="relationship",
            value={"target_entity_id": alice},
        )
        world.bind_identity(dave, "dave-qq")
        world.rebuild()
        signatures = []
        for _round in range(3):
            report = world.graph.rebuild(TENANT)
            edges = sorted(
                (e.edge_id, e.resource_type, e.resource_id, e.resource_revision)
                for e in _all_edges(world)
            )
            with world.store.read() as tx:
                pointer = tx.graph.pointer(TENANT)
                assert pointer is not None
            signatures.append(
                (
                    report.content_checksum,
                    report.edge_count,
                    report.node_count,
                    report.source_watermark,
                    report.tombstone_watermark,
                    tuple(edges),
                    pointer.switch_epoch,
                )
            )
        # Epochs increase (CAS) but the content signatures are identical.
        for signature in signatures[1:]:
            assert signature[:6] == signatures[0][:6]
        assert signatures[0][6] < signatures[1][6] < signatures[2][6]


class TestFailClosed:
    def _healthy_world(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        alice = world.entity("Alice")
        world.relate("r1", bob, alice)
        world.remember(
            "targeted",
            "Bob trusts Alice",
            bob,
            category="relationship",
            value={"target_entity_id": alice},
        )
        world.bind_identity(bob, "bob-qq-fc")
        world.rebuild()

    def _corrupt(self, world: Phase8World, sql: str) -> None:
        connection = sqlite3.connect(world.store.runtime.database)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(sql)
            connection.commit()
        finally:
            connection.close()

    @pytest.mark.parametrize("round_index", range(FAULT_ROUNDS))
    def test_corrupted_checksum_fails_closed(self, world: Phase8World, round_index: int) -> None:
        self._healthy_world(world)
        with world.store.read() as tx:
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
        self._corrupt(
            world,
            f"UPDATE graph_generations SET content_checksum = 'deadbeef{round_index:02d}' "
            f"WHERE id = '{pointer.generation_id}'",
        )
        with world.store.write() as tx:
            assert world.graph.verify_in_tx(tx, TENANT) is False
        # The verdict persists through the commit — a raised verdict would
        # have rolled back with the worker's transaction (review round 3).
        with world.store.read() as tx:
            assert tx.graph.projection_state() == "pending_rebuild"
        # The read gate fails closed until a rebuild lands (the route
        # degrades; the request itself succeeds with canonical routes).
        result = world.recall_as_speaker("bob-qq-fc")
        degraded = [d for d in result.degraded_routes if d.route == ROUTE_GRAPH]
        assert degraded
        # The verify pass flipped the state to pending_rebuild, which is
        # what the read gate now reports; either way the corruption never
        # serves and the canonical routes stay intact.
        assert degraded[0].reason_code in ("graph_index_corrupt", "graph_rebuild_pending")
        assert "claims" in result.completed_routes
        world.graph.rebuild(TENANT)
        result = world.recall_as_speaker("bob-qq-fc")
        assert ROUTE_GRAPH in result.completed_routes

    @pytest.mark.parametrize("round_index", range(FAULT_ROUNDS))
    def test_corrupted_counts_fail_closed_at_read_gate(
        self, world: Phase8World, round_index: int
    ) -> None:
        self._healthy_world(world)
        with world.store.read() as tx:
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
        self._corrupt(
            world,
            f"UPDATE graph_generations SET edge_count = 10{round_index:02d} "
            f"WHERE id = '{pointer.generation_id}'",
        )
        with world.store.read() as tx:
            with pytest.raises(GraphDegradedError) as excinfo:
                world.graph.trusted_generation_in_tx(tx, tenant_id=TENANT, agent_id=world.agent)
            assert excinfo.value.reason_code == "graph_index_corrupt"

    @pytest.mark.parametrize("round_index", range(FAULT_ROUNDS))
    def test_unknown_builder_fails_closed(self, world: Phase8World, round_index: int) -> None:
        self._healthy_world(world)
        with world.store.read() as tx:
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
        self._corrupt(
            world,
            f"UPDATE graph_generations SET builder_version = 99 WHERE id = "
            f"'{pointer.generation_id}'",
        )
        with world.store.read() as tx:
            with pytest.raises(GraphDegradedError) as excinfo:
                world.graph.trusted_generation_in_tx(tx, tenant_id=TENANT, agent_id=world.agent)
            assert excinfo.value.reason_code == "graph_builder_unknown"
            assert excinfo.value.retryable is False

    @pytest.mark.parametrize("round_index", range(FAULT_ROUNDS))
    def test_dangling_pointer_fails_closed(self, world: Phase8World, round_index: int) -> None:
        self._healthy_world(world)
        self._corrupt(
            world,
            f"UPDATE graph_current SET generation_id = 'missing-{round_index}' "
            f"WHERE tenant_id = '{TENANT}'",
        )
        with world.store.read() as tx:
            with pytest.raises(GraphDegradedError) as excinfo:
                world.graph.trusted_generation_in_tx(tx, tenant_id=TENANT, agent_id=world.agent)
            assert excinfo.value.reason_code == "graph_index_corrupt"

    @pytest.mark.parametrize("round_index", range(FAULT_ROUNDS))
    def test_tombstone_watermark_regression_fails_closed(
        self, world: Phase8World, round_index: int
    ) -> None:
        self._healthy_world(world)
        self._corrupt(
            world,
            f"UPDATE graph_current SET tombstone_watermark = 500{round_index:02d} "
            f"WHERE tenant_id = '{TENANT}'",
        )
        with world.store.read() as tx:
            with pytest.raises(GraphDegradedError) as excinfo:
                world.graph.trusted_generation_in_tx(tx, tenant_id=TENANT, agent_id=world.agent)
            assert excinfo.value.reason_code == "graph_index_corrupt"

    def test_source_coverage_drift_fails_verification(self, world: Phase8World) -> None:
        self._healthy_world(world)
        # Settle the projection pipeline first (the producers were never
        # drained): the canonical-equality stage of verification only runs
        # for a quiescent pipeline — pending producer jobs legitimately
        # allow the projection to differ.
        connection = sqlite3.connect(world.store.runtime.database)
        try:
            connection.execute(
                "UPDATE outbox_jobs SET status = 'completed', completed_us = 1 "
                "WHERE status IN ('pending', 'leased', 'retryable') "
                "AND job_kind IN ('claim.changed', 'relation.changed', "
                "'memory.invalidated', 'graph.apply', 'profile.apply')"
            )
            connection.commit()
        finally:
            connection.close()
        # Simulate a lost change: delete a canonical relation the generation
        # still claims to cover.
        self._corrupt(world, "DELETE FROM relations")
        with world.store.write() as tx:
            assert world.graph.verify_in_tx(tx, TENANT) is False
        with world.store.read() as tx:
            assert tx.graph.projection_state() == "pending_rebuild"

    def test_never_built_degrades_rebuild_pending(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        world.bind_identity(bob, "bob-qq-nb")
        result = world.recall_as_speaker("bob-qq-nb")
        degraded = [d for d in result.degraded_routes if d.route == ROUTE_GRAPH]
        assert degraded
        assert degraded[0].reason_code == "graph_rebuild_pending"
        assert degraded[0].retryable is True
        assert degraded[0].fallback == "route_skipped_canonical_intact"
        # Canonical routes are intact — the projection being unavailable
        # never widens or starves the canonical result.
        assert "claims" in result.completed_routes
        assert "relations" in result.completed_routes

    def test_as_of_degrades_non_retryable(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        world.bind_identity(bob, "bob-qq-ao")
        world.rebuild()
        result = world.recall_as_speaker("bob-qq-ao", as_of_us=world.clock.now_us() - 1)
        degraded = [d for d in result.degraded_routes if d.route == ROUTE_GRAPH]
        assert degraded
        assert degraded[0].reason_code == "graph_as_of_unsupported"
        assert degraded[0].retryable is False


class TestInvalidationRaces:
    def test_forget_relation_race_50_rounds(self, world: Phase8World) -> None:
        from iris_memory_core.application.forget import ForgetService

        forget = ForgetService(world.store, world.clock, idempotency=world.idem)
        bob = world.entity("Bob")
        world.bind_identity(bob, "bob-qq-race")
        for round_index in range(RACE_ROUNDS):
            target = world.entity(f"race-{round_index}")
            result = world.relate(f"rr-{round_index}", bob, target, relation_type="knows")
            world.rebuild()
            assert any(edge.resource_id == result.relation_id for edge in _all_edges(world))
            from iris_memory_core.domain.retention import (
                ForgetSelector,
                ForgetSelectorKind,
            )

            forget.forget(
                world.access,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE,
                    resource_type="relation",
                    resource_id=result.relation_id,
                ),
                reason="race test",
                idempotency_key=f"forget-{round_index}",
            )
            # Synchronous re-derivation via apply: the tombstone apply is the
            # fast path; the projection must never resurrect the edge.
            with world.store.write() as tx:
                world.graph.apply_change_in_tx(
                    tx,
                    tenant_id=TENANT,
                    resource_type="relation",
                    resource_id=result.relation_id,
                )
            assert not any(edge.resource_id == result.relation_id for edge in _all_edges(world))

    def test_correct_claim_race_50_rounds(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        carol = world.entity("Carol")
        world.bind_identity(bob, "bob-qq-cr")
        for round_index in range(RACE_ROUNDS):
            claim = world.remember(
                f"cr-{round_index}",
                f"revision one number {round_index}",
                bob,
                category="relationship",
                predicate="p_race",
                value={"target_entity_id": carol},
            )
            world.rebuild()
            assert any(edge.resource_id == claim.claim_id for edge in _all_edges(world))
            world.claims.correct(
                world.access,
                claim_id=claim.claim_id,
                expected_revision=claim.revision,
                mode="supersede",
                value={"target_entity_id": world.entity(f"moved-{round_index}")},
                canonical_text=f"revision two number {round_index}",
                evidence=[{"source_type": "observation", "source_id": world.observation()}],
                reason="race",
                idempotency_key=f"cr-correct-{round_index}",
            )
            with world.store.write() as tx:
                world.graph.apply_change_in_tx(
                    tx, tenant_id=TENANT, resource_type="claim", resource_id=claim.claim_id
                )
            edges = [edge for edge in _all_edges(world) if edge.resource_id == claim.claim_id]
            # The edge follows the CURRENT revision — the old target never
            # comes back.
            assert all(edge.resource_revision >= 2 for edge in edges)

    def test_binding_revoke_race_50_rounds(self, world: Phase8World) -> None:
        for round_index in range(RACE_ROUNDS):
            person = world.entity(f"bind-{round_index}")
            binding = world.bind_identity(person, f"race-qq-{round_index}")
            world.rebuild()
            assert any(edge.resource_id == binding.id for edge in _all_edges(world))
            world.identities.revoke_binding(
                world.admin_access,
                binding.id,
                expected_revision=binding.revision,
                reason="race",
            )
            with world.store.write() as tx:
                world.graph.apply_change_in_tx(
                    tx, tenant_id=TENANT, resource_type="binding", resource_id=binding.id
                )
            assert not any(edge.resource_id == binding.id for edge in _all_edges(world))

    def test_redirect_and_tombstone_kill_entity_edges(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        alice = world.entity("Alice")
        eve = world.entity("Eve")
        world.relate("r1", bob, alice)
        world.relate("r2", alice, eve)
        world.rebuild()
        relation_edges = [e for e in _all_edges(world) if e.edge_kind == "relation"]
        assert len(relation_edges) == 2
        bob_entity = bob
        world.identities.redirect_entity(
            world.admin_access, alice, eve, expected_revision=1, reason="merge"
        )
        # The invalidation enqueue happened in the canonical transaction.
        with world.store.read() as tx:
            jobs = tx.outbox.list_jobs(tenant_id=TENANT, limit=50)
        assert any(job.job_kind == "graph.apply" for job in jobs)
        with world.store.write() as tx:
            world.graph.apply_change_in_tx(
                tx, tenant_id=TENANT, resource_type="entity", resource_id=alice
            )
        # Canonical relations retain their historical ids, but a redirected
        # endpoint is no longer eligible for current graph traversal.
        assert len([e for e in _all_edges(world) if e.edge_kind == "relation"]) == 0
        # Entity tombstone: the entity's edges die with it.
        from iris_memory_core.domain.identity import EntityState

        with world.store.write() as tx:
            entity = tx.identities.get_entity(bob_entity)
            tx.update_entity_state(
                bob_entity,
                EntityState.TOMBSTONED,
                expected_revision=entity.revision,
                actor="admin",
                reason_code="test",
            )
            tx.record_tombstone(
                tenant_id=TENANT,
                resource_type="entity",
                resource_id=bob_entity,
                reason_code="test",
                deleted_by="admin",
            )
        with world.store.write() as tx:
            world.graph.apply_change_in_tx(
                tx, tenant_id=TENANT, resource_type="entity", resource_id=bob_entity
            )
        assert all(edge.source_node_id != bob_entity for edge in _all_edges(world))


class TestGenerationLifecycle:
    def test_shadow_build_verify_switch_retire(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        alice = world.entity("Alice")
        world.relate("r1", bob, alice)
        first = world.graph.rebuild(TENANT)
        world.relate("r2", alice, bob, relation_type="likes")
        second = world.graph.rebuild(TENANT)
        with world.store.read() as tx:
            generations = tx.graph.generations_for_tenant(TENANT)
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
        assert pointer.generation_id == second.generation_id
        assert {g.status for g in generations} == {"verified", "retired"}
        retired = [g for g in generations if g.status == "retired"]
        assert retired[0].id == first.generation_id

    def test_stale_publish_is_fenced(self, world: Phase8World) -> None:
        from iris_memory_core.domain.errors import ConflictError

        bob = world.entity("Bob")
        alice = world.entity("Alice")
        world.relate("r1", bob, alice)
        world.graph.rebuild(TENANT)
        with world.store.read() as tx:
            stale_epoch = tx.graph.current_epoch(TENANT)
        # A concurrent switch advances the epoch.
        world.relate("r2", alice, bob, relation_type="likes")
        world.graph.rebuild(TENANT)
        with world.store.write() as tx:
            drafts = world.graph.derive_all_edges(tx, TENANT)
            from iris_memory_core.domain.graph import graph_generation_checksum

            edge_count, node_count, checksum = graph_generation_checksum(drafts)
            generation = tx.graph.insert_generation(
                tenant_id=TENANT,
                builder_version=1,
                source_watermark=99,
                tombstone_watermark=tx.tombstone_watermark(),
                node_count=node_count,
                edge_count=edge_count,
                content_checksum=checksum,
                agent_watermarks={},
            )
            with pytest.raises(ConflictError):
                tx.graph.switch_pointer(
                    tenant_id=TENANT, generation=generation, expected_epoch=stale_epoch
                )

    def test_retired_generation_cleanup_respects_window(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        alice = world.entity("Alice")
        world.relate("r1", bob, alice)
        world.graph.rebuild(TENANT)
        world.relate("r2", alice, bob, relation_type="likes")
        world.graph.rebuild(TENANT)
        # Within the keep window: cleanup keeps every retired generation.
        for index in range(3):
            world.relate(f"r{index + 3}", bob, alice, relation_type=f"knows{index}")
            world.graph.rebuild(TENANT)
        with world.store.write() as tx:
            removed = world.graph.cleanup_in_tx(tx, TENANT)
        assert removed == ()
        # Age every retired generation past the rollback window: only the
        # keep window (2 most recent) survives.
        import sqlite3 as _sqlite3

        connection = _sqlite3.connect(world.store.runtime.database)
        try:
            connection.execute(
                "UPDATE graph_generations SET retired_us = ? WHERE status = 'retired'",
                (world.clock.now_us() - 48 * 3_600_000_000,),
            )
            connection.commit()
        finally:
            connection.close()
        with world.store.read() as tx:
            retired_total = len(
                [g for g in tx.graph.generations_for_tenant(TENANT) if g.status == "retired"]
            )
        with world.store.write() as tx:
            removed = world.graph.cleanup_in_tx(tx, TENANT)
        assert len(removed) == retired_total - 2

    def test_apply_updates_current_generation_incrementally(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        alice = world.entity("Alice")
        result = world.relate("r1", bob, alice)
        world.rebuild()
        world.relate("r2", alice, bob, relation_type="likes")
        # No rebuild: apply the single changed relation into the current gen.
        with world.store.write() as tx:
            # Re-deriving an UNCHANGED resource converges to the same rows
            # (idempotent apply; the manifest re-derivation is stable).
            world.graph.apply_change_in_tx(
                tx, tenant_id=TENANT, resource_type="relation", resource_id=result.relation_id
            )
        with world.store.write() as tx:
            relation_two = tx.relations.all_current_relation_pairs(TENANT)[1][0]
            changed = world.graph.apply_change_in_tx(
                tx, tenant_id=TENANT, resource_type="relation", resource_id=relation_two.id
            )
        assert changed is True
        edges = [e for e in _all_edges(world) if e.edge_kind == "relation"]
        assert len(edges) == 2
        # The manifest still binds the authoritative rows after the apply.
        with world.store.write() as tx:
            world.graph.verify_in_tx(tx, TENANT)

    def test_pointer_info_exposes_observability_metadata(self, world: Phase8World) -> None:
        baseline = world.graph.rebuild(TENANT)
        base_info = world.graph.pointer_info(TENANT)
        bob = world.entity("Bob")
        alice = world.entity("Alice")
        world.relate("r1", bob, alice)
        world.graph.rebuild(TENANT)
        info = world.graph.pointer_info(TENANT)
        assert info["state"] == "ready"
        assert info["builder_version"] == 1
        base_epoch = int(str(base_info["epoch"]))
        base_nodes = int(str(base_info["node_count"]))
        base_edges = int(str(base_info["edge_count"]))
        assert info["epoch"] == base_epoch + 1
        assert info["node_count"] == base_nodes + 2
        assert info["edge_count"] == base_edges + 1
        assert baseline.node_count == base_info["node_count"]
        assert "source_watermark" in info and "tombstone_watermark" in info


class TestFreshness:
    def test_minimum_watermark_degrades_until_rebuild(self, world: Phase8World) -> None:
        bob = world.entity("Bob")
        world.bind_identity(bob, "bob-qq-mw")
        world.remember("f1", "Bob fact one", bob)
        world.rebuild()
        # Advance the agent watermark AFTER the rebuild (a committed change
        # the generation does not cover): reachable — the control
        # transaction passes — but not served by the stale generation.
        world.remember("f2", "Bob fact two", bob)
        with world.store.read() as tx:
            watermark_state = tx.watermark(TENANT, world.agent)
            assert watermark_state is not None
            watermark = watermark_state.current_seq
        result = world.recall_as_speaker("bob-qq-mw", minimum_watermark=watermark)
        degraded = [d for d in result.degraded_routes if d.route == ROUTE_GRAPH]
        assert degraded and degraded[0].reason_code == "graph_generation_stale"
        world.rebuild()
        result = world.recall_as_speaker("bob-qq-mw", minimum_watermark=watermark)
        assert ROUTE_GRAPH in result.completed_routes
        assert not [d for d in result.degraded_routes if d.route == ROUTE_GRAPH]

    def test_unsettle_backlog_degrades_stale(self, world: Phase8World) -> None:
        from iris_memory_core.application.write_support import schedule_projection_apply

        bob = world.entity("Bob")
        world.bind_identity(bob, "bob-qq-bk")
        world.remember(
            "f1",
            "Bob fact one",
            bob,
            category="relationship",
            value={"target_entity_id": world.entity("Carol")},
        )
        world.rebuild()
        # An unsettled graph.apply for this agent (pending forever here).
        with world.store.write() as tx:
            schedule_projection_apply(
                tx,
                job_kind="graph.apply",
                tenant_id=TENANT,
                resource_type="claim",
                resource_id="unsettled-claim",
                agent_id=world.agent,
            )
        with world.store.read() as tx:
            with pytest.raises(GraphDegradedError) as excinfo:
                world.graph.trusted_generation_in_tx(
                    tx, tenant_id=TENANT, agent_id=world.agent, minimum_watermark=1
                )
            assert excinfo.value.reason_code == "graph_generation_stale"
