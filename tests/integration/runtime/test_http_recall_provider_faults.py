"""Real loopback embedding transport and failed rebuild continuity over ASGI."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris_memory_core.api.app import create_app
from iris_memory_core.domain.vector import EmbeddingProviderError, VectorSpaceConfig
from iris_memory_core.providers.embedding import EmbeddingProviderLimits, HttpEmbeddingProvider
from iris_memory_core.recall_runtime import RecallAssemblyConfig
from tests.integration.recall.graph_profile_helpers import TENANT, Phase8World
from tests.integration.runtime.test_http_recall_assembly import client_for, request_for
from tests.integration.runtime.test_http_recall_assembly import world as world_fixture

world = world_fixture


@contextmanager
def provider_server() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"mode": "normal", "calls": 0}
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"] += 1
            if state["mode"] == "disconnect":
                self.close_connection = True
                return
            if state["mode"] == "timeout":
                stop.wait(0.3)
                self.close_connection = True
                return
            payload = json.dumps(
                {"data": [{"embedding": [1.0] + [0.0] * 31} for _ in body["input"]]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/embeddings", state
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["timeout", "disconnect"])
@pytest.mark.parametrize("allow_partial", [True, False])
def test_real_embedding_transport_degrades_and_recovers(
    world: Phase8World,
    mode: str,
    allow_partial: bool,
) -> None:
    assert world.speaker_entity is not None
    claim = world.remember("tcp-vector", "Photovoltaic orchard record", world.speaker_entity)
    with provider_server() as (endpoint, state), client_for(world) as base:
        provider = HttpEmbeddingProvider(
            endpoint=endpoint,
            api_key="local-fixture-key",
            space=VectorSpaceConfig(model="tcp-test", dimension=32),
            limits=EmbeddingProviderLimits(timeout_us=50_000, max_qps=1000),
        )
        app = create_app(
            world.store,
            credentials=cast(FastAPI, base.app).state.runtime.credentials,
            recall_config=RecallAssemblyConfig(embedding=provider),
        )
        with TestClient(app, headers=base.headers) as client:
            app.state.runtime.projections.vector.rebuild(TENANT)
            before = state["calls"]
            state["mode"] = mode
            request = request_for(world, "vector")
            request["allow_partial"] = allow_partial
            response = client.post("/v1/recall", json=request)
            assert state["calls"] > before
            if allow_partial:
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["partial"] is True
                assert "vector" not in body["completed_routes"]
                assert any(
                    r["route"] == "vector" and r["reason_code"] == "vector_unavailable"
                    for r in body["degraded_routes"]
                )
                assert any(
                    r["route"] == "vector" and r["outcome"] == "degraded"
                    for r in body["trace"]["routes"]
                )
            else:
                assert response.status_code != 200, response.text
                assert response.json()["error"]["code"] in {"not_ready", "deadline_exceeded"}
            assert "local-fixture-key" not in response.text
            state["mode"] = "normal"
            recovered = request_for(world, "vector")
            recovered["request_id"] += "-recovered"
            body = client.post("/v1/recall", json=recovered).json()
            assert "vector" in body["completed_routes"]
            assert any(
                c["resource_ref"]["resource_id"] == claim.claim_id for c in body["candidates"]
            )


def test_failed_rebuild_keeps_the_old_generation_serving_http(
    world: Phase8World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert world.speaker_entity is not None
    claim = world.remember("old", "Old verified generation content", world.speaker_entity)
    with client_for(world, development=True) as client:
        projection = cast(FastAPI, client.app).state.runtime.projections.vector
        projection.rebuild(TENANT)
        old = projection.pointer_info(TENANT)

        def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise EmbeddingProviderError("transport_error", retryable=True)

        with monkeypatch.context() as patch:
            patch.setattr(projection._provider, "embed_batch", unavailable)
            with pytest.raises(EmbeddingProviderError):
                projection.rebuild(TENANT)
        assert projection.pointer_info(TENANT) == old
        response = client.post("/v1/recall", json=request_for(world, "vector"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert "vector" in body["completed_routes"]
        assert any(c["resource_ref"]["resource_id"] == claim.claim_id for c in body["candidates"])


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("configured", [False, True])
def test_readiness_uses_the_runtime_embedding_probe(
    world: Phase8World,
    required: bool,
    configured: bool,
) -> None:
    with provider_server() as (endpoint, state), client_for(world) as base:
        state["mode"] = "disconnect"
        provider = (
            HttpEmbeddingProvider(
                endpoint=endpoint,
                api_key="probe-fixture",
                space=VectorSpaceConfig(model="tcp-test", dimension=32),
            )
            if configured
            else None
        )
        app = create_app(
            world.store,
            credentials=cast(FastAPI, base.app).state.runtime.credentials,
            recall_config=RecallAssemblyConfig(embedding=provider, vector_required=required),
        )
        with TestClient(app, headers=base.headers) as client:
            response = client.get("/health/ready")
            assert response.status_code == (503 if required else 200), response.text
            assert response.json()["checks"]["vector_capability"] == "unavailable"
            if required:
                assert "vector_capability_unavailable" in response.json()["reasons"]
            assert state["calls"] > 0 if configured else state["calls"] == 0
