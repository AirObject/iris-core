"""Exact Graph failure reasons at HTTP, with canonical fallback kept usable."""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI

from iris_memory_core.application.recall import GraphRoute, RouteDeadlineExceeded
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.recall.graph_profile_helpers import TENANT, Phase8World
from tests.integration.runtime.test_http_recall_assembly import client_for, request_for


@pytest.mark.parametrize(
    ("fault", "reason", "retryable"),
    [
        ("pending", "graph_rebuild_pending", True),
        ("deadline", "route_deadline_exceeded", True),
        ("failure", "route_failed", True),
    ],
)
def test_http_graph_failure_preserves_canonical_recall_and_exact_reason(
    clocked_store: Store,
    mutable_clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    reason: str,
    retryable: bool,
) -> None:
    world = Phase8World(clocked_store, mutable_clock)
    assert world.speaker_entity is not None
    claim = world.remember(
        "fallback",
        "current authorized fallback",
        world.speaker_entity,
        category="relationship",
        value={"target_entity_id": world.entity("target")},
    )
    world.rebuild()
    with client_for(world) as client:
        cast(FastAPI, client.app).state.runtime.fts.rebuild(TENANT)
        if fault == "pending":
            with world.store.write() as tx:
                tx.graph.set_projection_state("pending_rebuild")
        else:

            def fail(*args: Any, **kwargs: Any) -> Any:
                if fault == "deadline":
                    raise RouteDeadlineExceeded
                raise RuntimeError("injected graph storage failure")

            monkeypatch.setattr(GraphRoute, "collect", fail)
        request = request_for(world, "graph")
        request["candidate_limits"]["claims"] = 20
        response = client.post("/v1/recall", json=request)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["partial"] is True
        assert body["degraded_routes"] == [
            {
                "route": "graph",
                "reason_code": reason,
                "retryable": retryable,
                "fallback": "route_skipped_canonical_intact",
            }
        ]
        assert "graph" not in body["completed_routes"]
        assert "claims" in body["completed_routes"]
        assert [c["resource_ref"]["resource_id"] for c in body["candidates"]] == [claim.claim_id]
        trace = next(row for row in body["trace"]["routes"] if row["route"] == "graph")
        assert trace["candidate_count"] == 0 and trace["outcome"] == "degraded"
        assert trace["fallback"] == reason
