"""Real local HTTP/production Worker evidence, not external-model quality evidence."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from iris_memory_core.api import create_app
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.domain.errors import ProviderUnavailableError
from iris_memory_core.jobs.worker import OutboxWorker, phase10_handlers
from iris_memory_core.providers.cognitive import CognitiveProviderLimits
from iris_memory_core.providers.cognitive_deployment import load_cognitive_deployment
from iris_memory_core.providers.cognitive_http import HttpCognitiveProvider
from iris_memory_core.runtime import ServiceConfig, worker
from iris_memory_core.storage.uow import Store
from tests.integration.cognition.test_reflection_pipeline import (
    _consolidation_job,
    _observation,
    _world,
)


@pytest.fixture
def gateway() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"requests": [], "status": 200, "invalid": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            document = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(document)
            if state.get("request_hook") is not None:
                state["request_hook"]()
            if state.get("delay"):
                time.sleep(state["delay"])
            material = json.loads(document["messages"][1]["content"])
            prompt = document["messages"][0]["content"]
            value: dict[str, Any]
            if "ignored_observation_ids" in prompt:
                value = {
                    "groups": [
                        {
                            "title": "Preferences",
                            "summary": "User prefers tea.",
                            "observation_ids": [row["id"] for row in material],
                        }
                    ],
                    "ignored_observation_ids": [],
                }
            elif "Summarize only" in prompt:
                value = {"title": "Preferences", "summary": "User prefers tea."}
            else:
                source = material[0]
                value = {
                    "candidates": [
                        {
                            "type": "claim",
                            "payload": {
                                "predicate": "preference.drink",
                                "value": "tea",
                                "canonical_text": "prefers tea",
                                "category": "preference",
                                "confidence": 0.7,
                                "importance": 0.5,
                                "source_authority": "extracted",
                            },
                            "evidence": [
                                {
                                    "observation_id": source["id"],
                                    "observation_revision": source["revision"],
                                    "start": 2,
                                    "end": 13,
                                }
                            ],
                        }
                    ]
                }
            if state.get("outside_evidence") and "candidates" in value:
                value["candidates"][0]["evidence"][0]["observation_id"] = "another-tenant-source"
            body = json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": "invalid" if state["invalid"] else json.dumps(value)
                            },
                        }
                    ]
                }
            ).encode()
            statuses = state.get("statuses", [])
            self.send_response(statuses.pop(0) if statuses else state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["endpoint"] = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def configuration(
    tmp_path: Path, gateway: dict[str, Any], tenant: str
) -> tuple[Path, dict[str, Any]]:
    data = {
        "schema_version": 1,
        "outbound": {"allowed_hosts": ["127.0.0.1"], "allow_loopback": True},
        "limits": {
            kind: asdict(CognitiveProviderLimits(max_qps=1000, max_retries=0))
            for kind in ("extraction", "summarization", "reconciliation", "persona_evolution")
        },
        "tenants": {
            tenant: {
                "adapter": "openai-compatible-chat",
                "endpoint": gateway["endpoint"],
                "model": "local-fixture",
                "model_version": "fixture-v1",
                "secret_ref": "env:W06_FIXTURE_KEY",
                "authorization": {
                    "authorization_id": "synthetic-fixture-only",
                    "privacy_labels": [],
                    "max_input_chars": 4000,
                    "max_output_tokens": 512,
                    "request_cost_microunits": 100,
                },
            }
        },
    }
    path = tmp_path / "cognitive.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path, data


def test_real_http_runs_in_production_worker_and_materializes_candidate(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = clocked_store
    tenant, agent, space, access = _world(store)
    observation = _observation(store, access, agent, space, 1)
    path, _ = configuration(tmp_path, gateway, tenant)
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    outbox = OutboxService(store, store.clock)
    outbox.enqueue(_consolidation_job(store, tenant, agent, space))
    config = ServiceConfig(
        database=store.runtime.database,
        allow_local_sqlite=True,
        cognitive_config_file=path,
        development_cognitive=True,
    )
    for _ in range(6):
        assert worker(config, once=True) == 0
    assert len(gateway["requests"]) == 2
    for request in gateway["requests"]:
        assert request["model"] == "local-fixture"
        assert request["max_tokens"] == 512
        submitted = json.loads(request["messages"][1]["content"])
        assert set(submitted[0]) == {"id", "revision", "role", "content"}
        assert submitted[0]["id"] == observation
    with store.read() as tx:
        candidate = (
            tx.raw()
            .execute("SELECT decision,canonical_resource_type FROM cognitive_candidates")
            .fetchone()
        )
        model = tx.raw().execute("SELECT model_id FROM reflection_records").fetchone()[0]
        costs = (
            tx.raw()
            .execute(
                "SELECT provider_kind,cost_microunits FROM provider_outcomes ORDER BY provider_kind"
            )
            .fetchall()
        )
    assert tuple(candidate) == ("accepted", "claim")
    assert json.loads(model) == ["local-fixture", "fixture-v1"]
    assert [tuple(row) for row in costs] == [("extraction", 100), ("summarization", 100)]


@pytest.mark.parametrize(
    "status,reason,retryable",
    [(429, "rate_limited", True), (500, "server_error", True), (401, "server_error", False)],
)
def test_http_failures_keep_classification(
    clocked_store: Store,
    tmp_path: Path,
    gateway: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    reason: str,
    retryable: bool,
) -> None:
    tenant, *_ = _world(clocked_store)
    path, _ = configuration(tmp_path, gateway, tenant)
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
    assert deployment is not None
    gateway["status"] = status
    provider = deployment.providers[tenant]
    with pytest.raises(ProviderUnavailableError) as caught:
        deployment.governance.call(
            "summarization",
            tenant_id=tenant,
            agent_id=None,
            request_material={},
            estimated_cost_microunits=100,
            invoke=lambda timeout: provider.summarize(
                [{"id": "test", "revision": 1, "content": "tea", "privacy_labels": []}],
                prompt_version="summary.v1",
                schema_version="summary.v1",
                timeout_seconds=timeout,
            ),
        )
    assert caught.value.retryable is retryable
    assert caught.value.details["reason_code"] == reason
    assert deployment.governance.budget_spent(tenant, "summarization") == 100


def test_invalid_output_is_dead_and_not_retried(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, *_ = _world(clocked_store)
    path, _ = configuration(tmp_path, gateway, tenant)
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
    assert deployment is not None
    gateway["invalid"] = True
    with pytest.raises(ProviderUnavailableError) as caught:
        deployment.providers[tenant].summarize(
            [{"content": "tea"}],
            prompt_version="summary.v1",
            schema_version="summary.v1",
            timeout_seconds=1,
        )
    assert caught.value.details["reason_code"] == "invalid_output"
    assert not caught.value.retryable
    assert len(gateway["requests"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        "adapter",
        "model_version",
        "identity_bound",
        "authorization",
        "loopback",
        "secret_ref",
        "mode",
        "limits",
        "nan",
    ],
)
def test_deployment_rejects_unauthorized_or_ambiguous_configuration(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], change: str
) -> None:
    tenant, *_ = _world(clocked_store)
    path, data = configuration(tmp_path, gateway, tenant)
    configured = data["tenants"][tenant]
    if change == "adapter":
        configured["adapter"] = "arbitrary"
    elif change == "model_version":
        configured["model_version"] = ""
    elif change == "identity_bound":
        configured["model"] = "模型" * 48
    elif change == "authorization":
        configured["authorization"].pop("authorization_id")
    elif change == "secret_ref":
        configured["secret_ref"] = "raw:plaintext"
    elif change == "mode":
        path.chmod(0o644)
    elif change == "limits":
        data["limits"].pop("extraction")
    elif change == "nan":
        data["limits"]["extraction"]["max_qps"] = float("nan")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="invalid cognitive Provider deployment"):
        load_cognitive_deployment(clocked_store, path, development_cognitive=change != "loopback")
    assert not gateway["requests"]


def test_privacy_and_input_budget_reject_before_outbound(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, *_ = _world(clocked_store)
    path, _ = configuration(tmp_path, gateway, tenant)
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
    assert deployment is not None
    for material, reason in [
        ({"content": "private", "privacy_labels": ["sensitive"]}, "data_authorization_denied"),
        ({"content": "x" * 5000}, "request_too_large"),
    ]:
        with pytest.raises(ProviderUnavailableError) as caught:
            deployment.providers[tenant].summarize(
                [cast(dict[str, object], material)],
                prompt_version="summary.v1",
                schema_version="summary.v1",
                timeout_seconds=1,
            )
        assert caught.value.details["reason_code"] == reason
    assert not gateway["requests"]
    with pytest.raises(ProviderUnavailableError, match="provider is unavailable"):
        deployment.pipeline(clocked_store, "another-tenant")


def test_unconfigured_worker_does_not_fake_complete_and_capability_is_absent(
    clocked_store: Store,
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    _observation(clocked_store, access, agent, space, 1)
    outbox = OutboxService(clocked_store, clocked_store.clock)
    outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    assert (
        worker(
            ServiceConfig(database=clocked_store.runtime.database, allow_local_sqlite=True),
            once=True,
        )
        == 0
    )
    with clocked_store.read() as tx:
        assert tx.raw().execute("SELECT COUNT(*) FROM consolidation_windows").fetchone()[0] == 0
        status = (
            tx.raw()
            .execute("SELECT status FROM outbox_jobs WHERE job_kind='episode.consolidation'")
            .fetchone()[0]
        )
    assert status == "pending"
    app = create_app(clocked_store)
    with TestClient(app):
        runtime = app.state.runtime
        assert "reflection.v1" not in runtime.capabilities(access)["capabilities"]
        assert "consolidation.v1" not in runtime.capabilities(access)["capabilities"]


def test_budget_survives_process_reassembly_and_prevents_second_network_request(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, *_ = _world(clocked_store)
    path, data = configuration(tmp_path, gateway, tenant)
    for item in data["limits"].values():
        item["daily_budget_microunits"] = 150
    path.write_text(json.dumps(data))
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    for attempt in range(2):
        deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
        assert deployment is not None
        provider = deployment.providers[tenant]

        def invoke(timeout: float, provider: HttpCognitiveProvider = provider) -> object:
            return provider.summarize(
                [{"content": "tea"}],
                prompt_version="summary.v1",
                schema_version="summary.v1",
                timeout_seconds=timeout,
            )

        if attempt == 0:
            deployment.governance.call(
                "summarization",
                tenant_id=tenant,
                agent_id=None,
                request_material={},
                estimated_cost_microunits=100,
                invoke=invoke,
            )
        else:
            with pytest.raises(ProviderUnavailableError) as caught:
                deployment.governance.call(
                    "summarization",
                    tenant_id=tenant,
                    agent_id=None,
                    request_material={},
                    estimated_cost_microunits=100,
                    invoke=invoke,
                )
            assert caught.value.details["reason_code"] == "budget_exhausted"
    assert len(gateway["requests"]) == 1


def test_real_http_timeout_is_bounded_and_retryable(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, *_ = _world(clocked_store)
    path, _ = configuration(tmp_path, gateway, tenant)
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
    assert deployment is not None
    gateway["delay"] = 0.4
    started = time.monotonic()
    with pytest.raises(ProviderUnavailableError) as caught:
        deployment.providers[tenant].summarize(
            [{"content": "tea"}],
            prompt_version="summary.v1",
            schema_version="summary.v1",
            timeout_seconds=0.05,
        )
    assert time.monotonic() - started < 0.3
    assert caught.value.retryable
    assert caught.value.details["reason_code"] == "timeout"


def test_real_http_worker_keeps_sqlite_write_available(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    _observation(clocked_store, access, agent, space, 1)
    path, _ = configuration(tmp_path, gateway, tenant)
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
    assert deployment is not None
    writes: list[float] = []

    def concurrent_write() -> None:
        started = time.monotonic()
        with clocked_store.write() as tx:
            tx.raw().execute("UPDATE tenants SET status=status WHERE id=?", (tenant,))
        writes.append(time.monotonic() - started)

    gateway["request_hook"] = concurrent_write
    outbox = OutboxService(clocked_store, clocked_store.clock)
    outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    runtime = OutboxWorker(
        outbox,
        phase10_handlers(pipeline=deployment.pipeline(clocked_store, tenant)),
        owner="w06-real-http",
    )
    assert runtime.run_once()["completed"] == 1
    assert len(writes) == 1 and writes[0] < 0.3


def test_real_http_outside_evidence_is_rejected_by_normal_pipeline(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    _observation(clocked_store, access, agent, space, 1)
    path, _ = configuration(tmp_path, gateway, tenant)
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    gateway["outside_evidence"] = True
    deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
    assert deployment is not None
    outbox = OutboxService(clocked_store, clocked_store.clock)
    outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    runtime = OutboxWorker(
        outbox,
        phase10_handlers(pipeline=deployment.pipeline(clocked_store, tenant)),
        owner="w06-evidence",
    )
    assert runtime.run_once()["completed"] == 1
    assert runtime.run_once()["completed"] == 1
    with clocked_store.read() as tx:
        row = (
            tx.raw()
            .execute("SELECT candidate_count,rejected_count FROM reflection_records")
            .fetchone()
        )
    assert tuple(row) == (0, 1)
    assert len(gateway["requests"]) == 2


def test_failed_attempts_keep_cost_in_persisted_provider_outcome(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    _observation(clocked_store, access, agent, space, 1)
    path, data = configuration(tmp_path, gateway, tenant)
    data["limits"]["summarization"]["max_retries"] = 1
    path.write_text(json.dumps(data))
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    gateway["status"] = 500
    deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
    assert deployment is not None
    outbox = OutboxService(clocked_store, clocked_store.clock)
    outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    runtime = OutboxWorker(
        outbox,
        phase10_handlers(pipeline=deployment.pipeline(clocked_store, tenant)),
        owner="w06-cost",
    )
    runtime.run_once()
    with clocked_store.read() as tx:
        row = tx.raw().execute("SELECT outcome,cost_microunits FROM provider_outcomes").fetchone()
    assert tuple(row) == ("server_error", 200)
    assert len(gateway["requests"]) == 2
    assert deployment.governance.budget_spent(tenant, "summarization") == 200


@pytest.mark.parametrize("daily_budget,expected", [(1000000, "success"), (150, "budget_exhausted")])
def test_retry_success_or_admission_failure_keeps_previous_attempt_cost(
    clocked_store: Store,
    tmp_path: Path,
    gateway: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    daily_budget: int,
    expected: str,
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    _observation(clocked_store, access, agent, space, 1)
    path, data = configuration(tmp_path, gateway, tenant)
    data["limits"]["summarization"].update(max_retries=1, daily_budget_microunits=daily_budget)
    path.write_text(json.dumps(data))
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    gateway["statuses"] = [500, 200]
    deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
    assert deployment is not None
    outbox = OutboxService(clocked_store, clocked_store.clock)
    outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    runtime = OutboxWorker(
        outbox,
        phase10_handlers(pipeline=deployment.pipeline(clocked_store, tenant)),
        owner="w06-retry-cost",
    )
    runtime.run_once()
    with clocked_store.read() as tx:
        row = tx.raw().execute("SELECT outcome,cost_microunits FROM provider_outcomes").fetchone()
    cost = 200 if expected == "success" else 100
    assert tuple(row) == (expected, cost)
    assert len(gateway["requests"]) == cost // 100
    assert deployment.governance.budget_spent(tenant, "summarization") == cost


def test_grouped_http_summary_preserves_thread_and_time_context(
    clocked_store: Store, tmp_path: Path, gateway: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, *_ = _world(clocked_store)
    path, _ = configuration(tmp_path, gateway, tenant)
    monkeypatch.setenv("W06_FIXTURE_KEY", "synthetic-local-only-secret")
    deployment = load_cognitive_deployment(clocked_store, path, development_cognitive=True)
    assert deployment is not None
    result = deployment.providers[tenant].summarize(
        [
            {
                "id": "raw",
                "revision": 1,
                "content": "I prefer tea",
                "privacy_labels": [],
                "context_kind": "background",
                "occurred_us": 42,
                "source_event_id": "platform-2",
                "source_thread_id": "thread",
                "reply_to_source_event_id": "platform-1",
            }
        ],
        prompt_version="summary.groups.v1",
        schema_version="summary.groups.v1",
        timeout_seconds=2,
    )
    assert result["groups"][0]["observation_ids"] == ["raw"]
    submitted = json.loads(gateway["requests"][0]["messages"][1]["content"])
    assert submitted[0]["occurred_us"] == 42
    assert submitted[0]["source_thread_id"] == "thread"
    assert submitted[0]["reply_to_source_event_id"] == "platform-1"
