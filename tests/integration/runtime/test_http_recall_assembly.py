"""W01 real ASGI proofs for projection assembly and honest capabilities."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris_memory_core.api.app import create_app
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.recall import DEFAULT_ROUTES, VectorRoute
from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.domain.vector import EmbeddingProviderError, VectorSpaceConfig
from iris_memory_core.indexing.vector import VectorProjectionService
from iris_memory_core.providers.embedding import DeterministicEmbeddingProvider
from iris_memory_core.recall_runtime import RecallAssemblyConfig, assemble_recall
from iris_memory_core.runtime import load_config
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.recall.graph_profile_helpers import TENANT, Phase8World


@pytest.fixture
def world(clocked_store: Store, mutable_clock: MutableClock) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


def client_for(world: Phase8World, *, development: bool = False) -> TestClient:
    credentials = CredentialService(world.store, world.clock)
    token = "w01-assembly-test-token-at-least-32-bytes"
    credentials.issue(
        token,
        tenant_id=TENANT,
        app_instance_id="w01-client",
        plane="application",
        expires_us=world.clock.now_us() + 10_000_000_000,
        agent_ids=[world.agent],
        space_ids=[world.space],
        capabilities=json.loads(Path("contracts/source/contracts.json").read_text())[
            "capabilities"
        ],
        data_purposes=["reply"],
    )
    app = create_app(
        world.store,
        credentials=credentials,
        recall_config=RecallAssemblyConfig(development_embedding=development),
    )
    return TestClient(app, headers={"Authorization": "Bearer " + token})


def request_for(world: Phase8World, route: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_id": "w01-" + route,
        "scope": {"agent_id": world.agent, "space_id": world.space},
        "actors": [{"provider": "qq", "realm": "default", "external_id": "default-speaker"}],
        "topic": "unrelated semantic query",
        "purpose": "reply",
        "token_budget": 2000,
        "deadline_at": datetime.fromtimestamp(
            (world.clock.now_us() + 10_000_000) / 1_000_000, UTC
        ).isoformat(),
        "include_trace": True,
        "candidate_limits": {name: 20 if name == route else 0 for name in DEFAULT_ROUTES},
    }


def test_unconfigured_vector_is_not_advertised(world: Phase8World) -> None:
    with client_for(world) as client:
        caps = client.get("/v1/capabilities").json()["capabilities"]
        assert "recall.graph.v1" in caps
        assert "recall.vector.v1" not in caps
        assert "embedding.v1" not in caps


def test_unbuilt_configured_routes_are_traced_and_degraded(world: Phase8World) -> None:
    with client_for(world, development=True) as client:
        caps = client.get("/v1/capabilities").json()["capabilities"]
        assert {"recall.vector.v1", "recall.graph.v1"} <= set(caps)
        response = client.post("/v1/recall", json=request_for(world, "vector"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["partial"] is True
        assert {"vector", "graph"} <= {item["route"] for item in body["trace"]["routes"]}
        assert "vector" in {item["route"] for item in body["degraded_routes"]}


def test_http_returns_vector_source(world: Phase8World) -> None:
    with client_for(world, development=True) as client:
        response = client.post(
            "/v1/notes",
            json={
                "agent_id": world.agent,
                "space_id": world.space,
                "kind": "idea",
                "title": "Orchard",
                "body": "photovoltaic orchard observations",
            },
            headers={"Idempotency-Key": "w01-vector-note"},
        )
        assert response.status_code == 200, response.text
        note = response.json()
        runtime = cast(FastAPI, client.app).state.runtime
        projection = runtime.projections.vector
        assert projection is not None
        projection.rebuild(TENANT)
        runtime.fts.rebuild(TENANT)
        world.rebuild()
        request = request_for(world, "vector")
        del request["candidate_limits"]  # All normal routes remain enabled.
        response = client.post("/v1/recall", json=request)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "vector" in body["completed_routes"]
        assert any(c["resource_ref"]["resource_id"] == note["note_id"] for c in body["candidates"])
        assert any(
            t["route"] == "vector" and t["candidate_count"] > 0 for t in body["trace"]["routes"]
        )
        without_vector = create_app(world.store, credentials=runtime.credentials)
        with TestClient(without_vector, headers=client.headers) as without:
            request["request_id"] += "-without-vector"
            absent = without.post("/v1/recall", json=request)
            assert absent.status_code == 200, absent.text
            assert all(
                c["resource_ref"]["resource_id"] != note["note_id"]
                for c in absent.json()["candidates"]
            )


def test_http_graph_reaches_second_hop(world: Phase8World) -> None:
    assert world.speaker_entity is not None
    middle, end = world.entity("Middle"), world.entity("End")
    world.relate("hop-one", world.speaker_entity, middle)
    second = world.relate("hop-two", middle, end)
    world.rebuild()
    with client_for(world) as client:
        response = client.post("/v1/recall", json=request_for(world, "graph"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert "graph" in body["completed_routes"]
        assert any(
            c["resource_ref"]["resource_id"] == second.relation_id for c in body["candidates"]
        )
        assert any(
            t["route"] == "graph" and t["candidate_count"] >= 2 for t in body["trace"]["routes"]
        )


def test_api_and_worker_config_resolve_same_projection_root(world: Phase8World) -> None:
    config = load_config(
        environ={
            "IRIS_MEMORY_DATABASE": str(world.store.runtime.database),
            "IRIS_MEMORY_DEVELOPMENT_EMBEDDING": "true",
        }
    )
    first = assemble_recall(world.store, world.clock, config.recall_config())
    second = assemble_recall(world.store, world.clock, config.recall_config())
    assert first.vector is not None and second.vector is not None
    assert isinstance(first.vector, VectorProjectionService)
    assert isinstance(second.vector, VectorProjectionService)
    assert first.vector._root == second.vector._root
    assert first.vector._space == second.vector._space
    assert (
        load_config(
            environ={"IRIS_MEMORY_DATABASE": str(world.store.runtime.database)}
        ).development_embedding
        is False
    )


@pytest.mark.parametrize("capability", ["recall.vector.v1", "embedding.v1", "unknown.v99"])
def test_missing_required_capability_fails_closed(world: Phase8World, capability: str) -> None:
    with client_for(world) as client:
        response = client.post(
            "/v1/negotiation",
            json={
                "api_versions": ["v1"],
                "required_capabilities": [capability],
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unsupported_version"


@pytest.mark.parametrize("required", ["recall.v1", [None], [""], ["recall.v1", "recall.v1"]])
def test_required_capability_shape_is_validated(world: Phase8World, required: Any) -> None:
    with client_for(world) as client:
        response = client.post(
            "/v1/negotiation",
            json={
                "api_versions": ["v1"],
                "required_capabilities": required,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


def test_configured_but_unbuilt_capability_negotiates(world: Phase8World) -> None:
    with client_for(world, development=True) as client:
        response = client.post(
            "/v1/negotiation",
            json={
                "api_versions": ["v1"],
                "required_capabilities": ["recall.vector.v1", "recall.graph.v1"],
            },
        )
        assert response.status_code == 200


@pytest.mark.parametrize("granted", [(), ("recall.v1",)])
def test_required_capabilities_cannot_exceed_credential(
    world: Phase8World,
    granted: tuple[str, ...],
) -> None:
    with client_for(world, development=True) as client:
        runtime = cast(FastAPI, client.app).state.runtime
        token = "w01-narrowed-credential-with-32-bytes"
        runtime.credentials.issue(
            token,
            tenant_id=TENANT,
            app_instance_id="w01-narrowed",
            plane="application",
            expires_us=world.clock.now_us() + 100_000_000,
            agent_ids=[world.agent],
            space_ids=[world.space],
            capabilities=granted,
        )
        headers = {"Authorization": "Bearer " + token}
        caps = client.get("/v1/capabilities", headers=headers).json()["capabilities"]
        assert "recall.vector.v1" not in caps
        response = client.post(
            "/v1/negotiation",
            headers=headers,
            json={
                "api_versions": ["v1"],
                "required_capabilities": ["recall.vector.v1"],
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unsupported_version"


@pytest.mark.parametrize("reason", ["timeout", "transport_error"])
def test_provider_failure_degrades_the_http_route(
    world: Phase8World,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    with client_for(world, development=True) as client:
        projection = cast(FastAPI, client.app).state.runtime.projections.vector
        projection.rebuild(TENANT)

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise EmbeddingProviderError(reason, retryable=True)

        monkeypatch.setattr(projection._provider, "embed_batch", fail)
        response = client.post("/v1/recall", json=request_for(world, "vector"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["partial"] is True
        assert "vector" not in body["completed_routes"]
        assert any(
            r["route"] == "vector" and r["reason_code"] == "vector_unavailable"
            for r in body["degraded_routes"]
        )
        assert "recall.vector.v1" in client.get("/v1/capabilities").json()["capabilities"]


def test_mismatched_space_does_not_serve_an_old_generation(world: Phase8World) -> None:
    with client_for(world, development=True) as client:
        cast(FastAPI, client.app).state.runtime.projections.vector.rebuild(TENANT)
        binding = RecallAssemblyConfig(
            development_embedding=True,
            embedding=DeterministicEmbeddingProvider(
                VectorSpaceConfig(model="different", dimension=32)
            ),
        )
        other = create_app(
            world.store,
            credentials=cast(FastAPI, client.app).state.runtime.credentials,
            recall_config=binding,
        )
        with TestClient(other, headers=client.headers) as changed:
            response = changed.post("/v1/recall", json=request_for(world, "vector"))
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["partial"] is True
            assert "vector" not in body["completed_routes"]
            assert any(
                r["route"] == "vector" and r["reason_code"] == "vector_space_mismatch"
                for r in body["degraded_routes"]
            )


def test_deterministic_provider_requires_explicit_development(world: Phase8World) -> None:
    config = RecallAssemblyConfig(
        embedding=DeterministicEmbeddingProvider(VectorSpaceConfig(model="test", dimension=32))
    )
    with pytest.raises(ValueError, match="explicit development"):
        assemble_recall(world.store, world.clock, config)


def test_forget_after_vector_collection_cannot_escape_http_rehydrate(
    world: Phase8World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert world.speaker_entity is not None
    claim = world.remember("deleted", "DELETE-CANARY W01", world.speaker_entity)
    with client_for(world, development=True) as client:
        cast(FastAPI, client.app).state.runtime.projections.vector.rebuild(TENANT)
        original = VectorRoute.collect

        def collect(self: VectorRoute, *args: Any, **kwargs: Any) -> Any:
            candidates = original(self, *args, **kwargs)
            assert any(c.resource_id == claim.claim_id for c in candidates)
            ForgetService(world.store, world.clock, idempotency=world.idem).forget(
                world.access,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE,
                    agent_id=world.agent,
                    resource_type="claim",
                    resource_id=claim.claim_id,
                ),
                reason="W01 race",
                idempotency_key="w01-delete",
            )
            return candidates

        monkeypatch.setattr(VectorRoute, "collect", collect)
        response = client.post("/v1/recall", json=request_for(world, "vector"))
        assert response.status_code == 200, response.text
        assert "DELETE-CANARY" not in response.text
        assert all(
            c["resource_ref"]["resource_id"] != claim.claim_id
            for c in response.json()["candidates"]
        )
