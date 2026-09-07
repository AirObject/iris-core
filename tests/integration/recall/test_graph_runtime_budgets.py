"""W02: >=10x independent graph pressures measured inside the real route."""

from __future__ import annotations

from typing import Any

import pytest

from iris_memory_core.application.recall import (
    GraphRoute,
    RouteDeadlineExceeded,
    StructuredRecallRequest,
)
from iris_memory_core.indexing.graph import GraphProjectionService
from iris_memory_core.storage.projection import GraphRepository
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.recall.graph_execution_probe import GraphExecutionProbe
from tests.integration.recall.graph_profile_helpers import TENANT, Phase8World
from tests.integration.runtime.test_http_recall_assembly import client_for, request_for


@pytest.fixture
def world(clocked_store: Store, mutable_clock: MutableClock) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


class ControlledMonotonic:
    value = 0

    def monotonic_us(self) -> int:
        return self.value


@pytest.mark.parametrize("pressure", ["depth", "fanout", "nodes"])
def test_real_route_obeys_each_budget_under_tenfold_pressure(
    world: Phase8World, monkeypatch: pytest.MonkeyPatch, pressure: str
) -> None:
    root = world.speaker_entity
    assert root is not None
    depth, fanout, nodes = (2, 16, 64) if pressure != "nodes" else (2, 16, 8)
    levels = {root: 0}
    count = {"depth": depth * 10, "fanout": fanout * 10, "nodes": nodes * 10}[pressure]
    source = root
    for index in range(count):
        target = world.entity(f"{pressure}-{index}")
        levels[target] = index + 1 if pressure == "depth" else 1
        world.relate(f"{pressure}-{index}", source, target)
        if pressure == "depth":
            source = target
    world.graph.rebuild(TENANT)
    if pressure == "depth":
        assert max(levels.values()) >= depth * 10
    elif pressure == "fanout":
        with world.store.read() as tx:
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
            assert (
                len(
                    tx.graph.edges_for_source(
                        TENANT, pointer.generation_id, root, node_kind="entity"
                    )
                )
                >= fanout * 10
            )
    else:
        assert len(levels) >= nodes * 10
    probe = GraphExecutionProbe()
    probe.install(monkeypatch)
    route = GraphRoute(
        world.graph,
        max_depth=depth,
        max_fanout=fanout,
        max_nodes=nodes,
        monotonic=ControlledMonotonic(),
    )
    request = StructuredRecallRequest(
        request_id=f"w02-{pressure}",
        agent_id=world.agent,
        space_id=world.space,
        speaker_entity_id=root,
        candidate_limits={"graph": 100},
        deadline_monotonic_us=100,
        topic="friends",
    )
    with world.store.read() as tx:
        candidates = route.collect(
            tx, request, world.access, deadline_us=100, now_us=world.clock.now_us()
        )
    assert probe.calls == 1 and probe.reads and candidates
    assert len(probe.visited) <= nodes
    assert probe.max_depth <= depth
    assert max(probe.fanout_by_depth.values()) <= fanout
    assert probe.candidate_count == len(candidates) <= 100
    assert len(probe.reads) <= nodes
    assert all(limit == fanout * 4 and returned <= limit for _, limit, returned in probe.reads)
    assert all(levels[node] < depth for node, _, _ in probe.reads)
    # Each separate graph must actually reach the pressured boundary.
    if pressure == "depth":
        assert probe.max_depth == depth and len(candidates) == depth
    elif pressure == "fanout":
        assert max(probe.fanout_by_depth.values()) == fanout and len(candidates) == fanout
    else:
        assert len(probe.visited) == nodes and len(candidates) == nodes - 1
    print(
        f"W02 {pressure}: input={count}, visited={len(probe.visited)}, "
        f"reads={len(probe.reads)}, returned={len(candidates)}, depth={probe.max_depth}"
    )


def test_http_candidate_cap_and_repository_reads_are_observed(
    world: Phase8World, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert world.speaker_entity is not None
    for index in range(160):
        world.relate(f"http-{index}", world.speaker_entity, world.entity(f"target-{index}"))
    world.rebuild()
    probe = GraphExecutionProbe()
    probe.install(monkeypatch)
    with client_for(world) as client:
        request = request_for(world, "graph")
        request["candidate_limits"]["graph"] = 5
        response = client.post("/v1/recall", json=request)
        assert response.status_code == 200, response.text
        body = response.json()
    assert "graph" in body["completed_routes"]
    trace = next(row for row in body["trace"]["routes"] if row["route"] == "graph")
    assert trace["candidate_count"] == probe.candidate_count == 5
    assert len(body["candidates"]) == 5
    assert probe.calls == 1 and len(probe.visited) == 6
    assert probe.reads and all(limit == 64 and count <= 64 for _, limit, count in probe.reads)


@pytest.mark.parametrize("stage", ["read", "visibility", "admissibility", "candidate"])
def test_deadline_expiring_during_real_work_stops_before_next_expansion(
    world: Phase8World, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    assert world.speaker_entity is not None
    world.relate("deadline", world.speaker_entity, world.entity("deadline-target"))
    world.graph.rebuild(TENANT)
    clock = ControlledMonotonic()
    target_class, name = {
        "read": (GraphRepository, "edges_for_source"),
        "visibility": (GraphRoute, "_edge_visible"),
        "admissibility": (GraphProjectionService, "resource_still_admissible"),
        "candidate": (GraphRoute, "_candidate_for_edge"),
    }[stage]
    original = getattr(target_class, name)
    calls = 0

    def expire_after_actual_work(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        assert clock.value < 100, "work continued after the deadline"
        result = original(*args, **kwargs)
        calls += 1
        clock.value = 100
        return result

    monkeypatch.setattr(
        target_class,
        name,
        staticmethod(expire_after_actual_work)
        if stage == "admissibility"
        else expire_after_actual_work,
    )
    probe = GraphExecutionProbe()
    probe.install(monkeypatch)
    route = GraphRoute(world.graph, monotonic=clock, max_depth=1)
    request = StructuredRecallRequest(
        request_id=f"deadline-{stage}",
        agent_id=world.agent,
        space_id=world.space,
        speaker_entity_id=world.speaker_entity,
        candidate_limits={"graph": 1},
        deadline_monotonic_us=100,
    )
    with world.store.read() as tx, pytest.raises(RouteDeadlineExceeded):
        route.collect(tx, request, world.access, deadline_us=100, now_us=world.clock.now_us())
    assert calls == 1
    assert len(probe.visited) == (2 if stage == "candidate" else 1)
    assert probe.candidate_count == 0
