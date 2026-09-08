"""Actual HTTP Recall and ordinary Worker maintain tenant/model/generation binding."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris_memory_core.api.app import create_app
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.ports.provider_generations import ProviderBuildSnapshotMoved
from iris_memory_core.application.recall import DEFAULT_ROUTES
from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.identity import ExternalIdentityKey
from iris_memory_core.domain.jobs import NewOutboxJob
from iris_memory_core.domain.vector import VectorDegradedError
from iris_memory_core.indexing.managed_vector import ManagedVectorProjection
from iris_memory_core.jobs.handlers import vector_rebuild_handler
from iris_memory_core.jobs.worker import OutboxWorker, phase7_handlers
from iris_memory_core.recall_runtime import RecallAssemblyConfig, assemble_recall
from tests.integration.console.test_provider_activations import (
    accept_activation,
    claimed,
    first_generation,
    next_config,
)
from tests.integration.console.test_provider_activations import activation as activation_fixture
from tests.integration.console.test_provider_activations import auth as auth_fixture
from tests.integration.console.test_provider_activations import gateway as gateway_fixture
from tests.integration.console.test_provider_activations import probe as probe_fixture
from tests.integration.console.test_provider_activations import world as world_fixture

auth, world, gateway, probe, activation = (
    auth_fixture,
    world_fixture,
    gateway_fixture,
    probe_fixture,
    activation_fixture,
)


def managed(world: dict[str, Any]) -> ManagedVectorProjection:
    vector = assemble_recall(
        world["store"],
        world["store"].clock,
        RecallAssemblyConfig(embedding_runtime=world["factory"], vector_root=world["root"]),
    ).vector
    assert isinstance(vector, ManagedVectorProjection)
    return vector


def client_for(
    world: dict[str, Any], *, token: str = "w05-managed-recall-fixture-credential-32bytes"
) -> TestClient:
    credentials = CredentialService(world["store"], world["store"].clock)
    credentials.issue(
        token,
        tenant_id=world["tenant"],
        app_instance_id="w05-recall",
        plane="application",
        expires_us=world["store"].clock.now_us() + 10_000_000_000,
        agent_ids=[world["agent"]],
        space_ids=world["spaces"],
        capabilities=json.loads(Path("contracts/source/contracts.json").read_text())[
            "capabilities"
        ],
        data_purposes=["reply"],
    )
    with world["store"].write() as tx:
        identity = tx.insert_external_identity(
            ExternalIdentityKey(world["tenant"], "fixture", "default", "speaker"),
            entity_id=world["entities"][0],
        )
    identities = IdentityService(world["store"])
    proposed = identities.propose_binding(
        world["access"],
        identity.id,
        world["entities"][0],
        proof_digest="w05-fixture-proof",
        reason="test",
    )
    identities.confirm_binding(
        world["access"], proposed.id, expected_revision=proposed.revision, reason="test"
    )
    app = create_app(
        world["store"],
        credentials=credentials,
        recall_config=RecallAssemblyConfig(
            embedding_runtime=world["factory"], vector_root=world["root"], vector_required=True
        ),
    )
    return TestClient(app, headers={"Authorization": "Bearer " + token})


def request_for(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_id": "w05-managed-vector-" + str(uuid4()),
        "scope": {"agent_id": world["agent"], "space_id": world["spaces"][0]},
        "actors": [{"provider": "fixture", "realm": "default", "external_id": "speaker"}],
        "topic": "semantic query",
        "purpose": "reply",
        "token_budget": 2000,
        "deadline_at": datetime.fromtimestamp(
            (world["store"].clock.now_us() + 10_000_000) / 1_000_000, UTC
        ).isoformat(),
        "include_trace": True,
        "candidate_limits": {name: 20 if name == "vector" else 0 for name in DEFAULT_ROUTES},
    }


def recall(client: TestClient, world: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/v1/recall", json=request_for(world))
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def test_managed_initial_enablement_is_explicitly_degraded_without_outbound(
    activation: dict[str, Any],
) -> None:
    with client_for(activation) as client:
        body = recall(client, activation)
        assert body["partial"] and "vector" not in body["completed_routes"]
        assert any(
            r["route"] == "vector" and r["reason_code"] == "vector_rebuild_pending"
            for r in body["degraded_routes"]
        )
        assert "recall.vector.v1" in client.get("/v1/capabilities").json()["capabilities"]
        assert activation["gateway"]["requests"] == []
        assert not managed(activation).capability_available(activation["tenant"])
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["checks"]["vector_capability"] == "unavailable"


def test_actual_http_query_uses_old_model_until_activation_switch_then_new_model(
    activation: dict[str, Any],
) -> None:
    first_generation(activation)
    with client_for(activation) as client:
        first = recall(client, activation)
        assert "vector" in first["completed_routes"]
        assert any(
            c["resource_ref"]["resource_id"] == activation["ids"]["global"]
            for c in first["candidates"]
        )
        assert activation["gateway"]["requests"][-1][0]["model"] == "fixture"
        next_config(activation)
        accept_activation(activation, "managed-new-space")
        job = claimed(activation)
        prepared = activation["activation"].work(job)
        before = recall(client, activation)
        assert "vector" in before["completed_routes"]
        assert activation["gateway"]["requests"][-1][0]["model"] == "fixture"
        activation["outbox"].execute(job, lambda _: prepared, owner="first")
        after = recall(client, activation)
        assert "vector" in after["completed_routes"]
        assert activation["gateway"]["requests"][-1][0]["model"] == "next-model"
        before_health = len(activation["gateway"]["requests"])
        assert managed(activation).capability_available(activation["tenant"])
        assert len(activation["gateway"]["requests"]) == before_health
        ready = client.get("/health/ready")
        assert ready.json()["checks"]["vector_capability"] == "ok"
        assert len(activation["gateway"]["requests"]) == before_health


def test_one_read_snapshot_keeps_its_provider_when_activation_commits(
    activation: dict[str, Any],
) -> None:
    first_generation(activation)
    projection = managed(activation)
    next_config(activation)
    accept_activation(activation, "snapshot-new-space")
    job = claimed(activation)
    prepared = activation["activation"].work(job)
    with activation["store"].read() as tx:
        bound = projection.resolve_in_tx(tx, activation["tenant"])
        activation["outbox"].execute(job, lambda _: prepared, owner="first")
        query = bound.embed_query("concurrent query")
        hits = bound.search_in_tx(
            tx,
            tenant_id=activation["tenant"],
            agent_id=activation["agent"],
            query_vector=query,
            limit=10,
        )
        assert hits and activation["gateway"]["requests"][-1][0]["model"] == "fixture"
        assert projection.resolve_in_tx(tx, activation["tenant"]) is bound
    with activation["store"].read() as tx:
        updated = projection.resolve_in_tx(tx, activation["tenant"])
        assert updated is not bound
        assert len(updated.embed_query("new snapshot")) == 3
        with pytest.raises(VectorDegradedError):
            projection.resolve_in_tx(tx, "another-tenant")


def enqueue_rebuild(world: dict[str, Any], key: str) -> Any:
    with world["store"].write() as tx:
        tx.outbox.enqueue(
            NewOutboxJob(
                tenant_id=world["tenant"],
                job_kind="vector.rebuild",
                aggregate_type="tenant",
                aggregate_id=world["tenant"],
                source_revision=1,
                payload={"version": 2},
                dedupe_key=key,
                available_at_us=world["store"].clock.now_us(),
            )
        )
    return world["outbox"].claim("ordinary", kinds=frozenset({"vector.rebuild"})).jobs[0]


def test_normal_worker_rebuild_updates_both_pointers_and_original_binding(
    activation: dict[str, Any],
) -> None:
    old = first_generation(activation)
    projection = managed(activation)
    job = enqueue_rebuild(activation, "ordinary-rebuild")
    assert (
        activation["outbox"].execute(job, vector_rebuild_handler(projection), owner="ordinary")
        == "completed"
    )
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        assert serving.config_id == old.config_id and serving.generation_id != old.generation_id
        assert serving.epoch == old.epoch + 1
        assert tx.vector.pointer(activation["tenant"]).generation_id == serving.generation_id
        assert (
            tx.providers.get(activation["tenant"], old.config_id).last_generation_id
            == serving.generation_id
        )
        assert tx.providers.generation_binding(activation["tenant"], serving.generation_id) == (
            old.config_id,
            1,
        )
    assert (
        OutboxWorker(activation["outbox"], phase7_handlers(projection=projection)).run_once()[
            "claimed"
        ]
        == 0
    )


def test_hot_activation_fences_an_ordinary_rebuild_prepared_under_old_limits(
    activation: dict[str, Any],
) -> None:
    first_generation(activation)
    projection = managed(activation)
    prepared = projection.prepare_generation(activation["tenant"])
    next_config(activation, hot=True)
    accept_activation(activation, "ordinary-race-hot")
    job = claimed(activation)
    activation["outbox"].execute(job, activation["activation"].work, owner="first")
    with pytest.raises(ProviderBuildSnapshotMoved), activation["store"].write() as tx:
        projection.switch_in_tx(tx, activation["tenant"], prepared)


def test_last_prepared_builder_cannot_leave_wrong_space_id_map_on_publish(
    activation: dict[str, Any],
) -> None:
    first_generation(activation)
    old = managed(activation)
    next_config(activation)
    accept_activation(activation, "new-space-index-map-race")
    job = claimed(activation)
    prepared = activation["activation"].work(job)
    stale = old.prepare_generation(activation["tenant"])
    with activation["store"].read() as tx:
        assert set(row[0] for row in tx.raw().execute("SELECT model FROM vector_id_map")) == {
            "fixture"
        }
    activation["outbox"].execute(job, lambda _: prepared, owner="first")
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        assert set(
            tuple(row)
            for row in tx.raw().execute(
                "SELECT model,dimension,incorporated_generation FROM vector_id_map "
                "WHERE status='active'"
            )
        ) == {("next-model", 3, serving.generation_id)}
    with pytest.raises(ProviderBuildSnapshotMoved), activation["store"].write() as tx:
        old.switch_in_tx(tx, activation["tenant"], stale)


def test_actual_provider_failure_degrades_http_and_breaker_health_without_hidden_probe(
    activation: dict[str, Any],
) -> None:
    first_generation(activation)
    with client_for(activation) as client:
        assert "vector" in recall(client, activation)["completed_routes"]
        projection = cast(FastAPI, client.app).state.runtime.projections.vector
        activation["gateway"]["mode"] = "failure"
        for _ in range(5):
            body = recall(client, activation)
            assert body["partial"] and "vector" not in body["completed_routes"]
            assert any(
                r["route"] == "vector" and r["reason_code"] == "vector_unavailable"
                for r in body["degraded_routes"]
            )
        before = len(activation["gateway"]["requests"])
        assert not projection.capability_available(activation["tenant"])
        assert len(activation["gateway"]["requests"]) == before
        assert client.get("/health/ready").json()["checks"]["vector_capability"] == "unavailable"
        assert "recall.vector.v1" in client.get("/v1/capabilities").json()["capabilities"]


def test_current_credential_rotation_does_not_change_an_inflight_binding(
    activation: dict[str, Any],
) -> None:
    first_generation(activation)
    projection = managed(activation)
    with activation["store"].read() as tx:
        old = projection.resolve_in_tx(tx, activation["tenant"])
        activation["environment"]["PROBE_KEY"] = "rotated-private-provider-token"
        old.embed_query("inflight credential")
        assert activation["gateway"]["requests"][-1][1] == "Bearer isolated-private-provider-token"
        new = projection.resolve_in_tx(tx, activation["tenant"])
        assert new is not old
        new.embed_query("new credential")
        assert activation["gateway"]["requests"][-1][1] == "Bearer rotated-private-provider-token"


def test_cleanup_uses_local_metadata_even_when_current_secret_is_unavailable(
    activation: dict[str, Any],
) -> None:
    old = first_generation(activation)
    activation["store"].clock.advance(1000)
    next_config(activation)
    accept_activation(activation, "cleanup-new-model")
    job = claimed(activation)
    activation["outbox"].execute(job, activation["activation"].work, owner="first")
    # Existing cleanup preserves two retired generations in addition to its
    # age floor; create enough real successors to put the oldest outside both.
    for index in range(2):
        activation["store"].clock.advance(1000)
        maintenance_job = enqueue_rebuild(activation, "cleanup-successor-" + str(index))
        assert (
            activation["outbox"].execute(
                maintenance_job, vector_rebuild_handler(managed(activation)), owner="ordinary"
            )
            == "completed"
        )
    del activation["environment"]["PROBE_KEY"]
    activation["store"].clock.advance(24 * 3_600_000_000 + 1)
    before = len(activation["gateway"]["requests"])
    with activation["store"].write() as tx:
        tx.outbox.enqueue(
            NewOutboxJob(
                tenant_id=activation["tenant"],
                job_kind="vector.cleanup",
                aggregate_type="tenant",
                aggregate_id=activation["tenant"],
                source_revision=1,
                payload={"version": 2},
                dedupe_key="cleanup-unavailable-secret",
                available_at_us=activation["store"].clock.now_us(),
            )
        )
    result = OutboxWorker(
        activation["outbox"], phase7_handlers(projection=managed(activation))
    ).run_once()
    assert result["completed"] == 1
    assert len(activation["gateway"]["requests"]) == before
    with activation["store"].read() as tx:
        assert old.generation_id not in tx.vector.all_generation_ids()
        assert (
            tx.providers.get(activation["tenant"], old.config_id).last_generation_id
            == old.generation_id
        )
    assert not (activation["root"] / "generations" / old.generation_id).exists()


def test_serving_lifetime_does_not_expire_with_activation_probe_window(
    activation: dict[str, Any],
) -> None:
    first_generation(activation)
    with client_for(activation) as client:
        activation["store"].clock.advance(31 * 60 * 1_000_000)
        before = len(activation["gateway"]["requests"])
        body = recall(client, activation)
        assert "vector" in body["completed_routes"]
        assert any(
            c["resource_ref"]["resource_id"] == activation["ids"]["global"]
            for c in body["candidates"]
        )
        assert len(activation["gateway"]["requests"]) == before + 1
        assert client.get("/health/ready").json()["checks"]["vector_capability"] == "ok"
        assert len(activation["gateway"]["requests"]) == before + 1


def test_ordinary_rebuild_stops_after_first_batch_when_second_worker_takes_lease(
    activation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.domain.errors import LeaseFencedError
    from iris_memory_core.providers.configured import ConfiguredEmbedding

    first_generation(activation)
    next_config(activation, hot=True)
    accept_activation(activation, "batch-size-one")
    activation_job = claimed(activation)
    activation["outbox"].execute(activation_job, activation["activation"].work, owner="first")
    projection = managed(activation)
    job = enqueue_rebuild(activation, "batch-lease-race")
    replacements: list[Any] = []
    original = ConfiguredEmbedding.embed_batch
    before = len(activation["gateway"]["requests"])

    def takeover(self: Any, *args: Any, **kwargs: Any) -> Any:
        values = original(self, *args, **kwargs)
        activation["store"].clock.advance(31_000_000)
        replacements.extend(
            activation["outbox"].claim("replacement", kinds=frozenset({"vector.rebuild"})).jobs
        )
        return values

    monkeypatch.setattr(ConfiguredEmbedding, "embed_batch", takeover)
    with pytest.raises(LeaseFencedError):
        vector_rebuild_handler(projection)(job)
    assert len(activation["gateway"]["requests"]) == before + 1
    assert len(replacements) == 1 and replacements[0].lease_generation == job.lease_generation + 1
    monkeypatch.setattr(ConfiguredEmbedding, "embed_batch", original)
    assert (
        activation["outbox"].execute(
            replacements[0], vector_rebuild_handler(projection), owner="replacement"
        )
        == "completed"
    )


def test_ordinary_rebuild_stops_at_batch_boundary_when_secret_rotates(
    activation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.providers.configured import ConfiguredEmbedding

    first_generation(activation)
    next_config(activation, hot=True)
    accept_activation(activation, "secret-batch-size-one")
    job = claimed(activation)
    activation["outbox"].execute(job, activation["activation"].work, owner="first")
    projection = managed(activation)
    original = ConfiguredEmbedding.embed_batch
    before = len(activation["gateway"]["requests"])

    def rotate(self: Any, *args: Any, **kwargs: Any) -> Any:
        values = original(self, *args, **kwargs)
        activation["environment"]["PROBE_KEY"] = "changed-at-batch-boundary"
        return values

    monkeypatch.setattr(ConfiguredEmbedding, "embed_batch", rotate)
    with pytest.raises(ProviderBuildSnapshotMoved):
        projection.prepare_generation(activation["tenant"])
    assert len(activation["gateway"]["requests"]) == before + 1


def test_normal_worker_retries_missing_secret_without_publishing_then_recovers(
    activation: dict[str, Any],
) -> None:
    old = first_generation(activation)
    projection = managed(activation)
    job = enqueue_rebuild(activation, "temporarily-missing-secret")
    value = activation["environment"].pop("PROBE_KEY")
    before = len(activation["gateway"]["requests"])
    assert (
        activation["outbox"].execute(job, vector_rebuild_handler(projection), owner="ordinary")
        == "retryable"
    )
    assert len(activation["gateway"]["requests"]) == before
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]) == old
        retry = tx.outbox.get(job.id)
        assert retry.status == "retryable" and retry.attempt_count == 1
    activation["environment"]["PROBE_KEY"] = value
    activation["store"].clock.set(retry.available_at_us + 1)
    recovered = activation["outbox"].claim("recovered", kinds=frozenset({"vector.rebuild"})).jobs[0]
    assert (
        activation["outbox"].execute(
            recovered, vector_rebuild_handler(projection), owner="recovered"
        )
        == "completed"
    )
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]).generation_id != old.generation_id
