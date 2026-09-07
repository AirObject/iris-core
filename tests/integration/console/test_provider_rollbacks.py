"""Real retained FAISS rollback and rebuild fallback preserve immutable history."""

from __future__ import annotations

import shutil
from typing import Any

import pytest

from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.console.provider_activation_work import ACTIVATION_JOB_KIND
from iris_memory_core.domain.errors import ConflictError, LeaseFencedError
from iris_memory_core.jobs.worker import OutboxWorker
from tests.integration.console.test_provider_activations import (
    accept_activation,
    claimed,
    first_generation,
    next_config,
    old_query,
)
from tests.integration.console.test_provider_activations import activation as activation_fixture
from tests.integration.console.test_provider_activations import auth as auth_fixture
from tests.integration.console.test_provider_activations import gateway as gateway_fixture
from tests.integration.console.test_provider_activations import probe as probe_fixture
from tests.integration.console.test_provider_activations import world as world_fixture
from tests.integration.console.test_provider_probes import accept as accept_probe
from tests.integration.console.test_provider_probes import claim as claim_probe

auth, world, gateway, probe, activation = (
    auth_fixture,
    world_fixture,
    gateway_fixture,
    probe_fixture,
    activation_fixture,
)


def pair(world: dict[str, Any], *, hot: bool = False) -> tuple[Any, Any]:
    old = first_generation(world)
    next_config(world, hot=hot)
    accept_activation(world, "activate-next")
    job = claimed(world)
    assert world["outbox"].execute(job, world["activation"].work, owner="first") == "completed"
    with world["store"].read() as tx:
        current = tx.providers.serving(world["tenant"])
    world["config_id"] = old.config_id
    return old, current


def rollback(world: dict[str, Any], key: str = "rollback-one") -> Any:
    with world["store"].read() as tx:
        revision = tx.providers.get(world["tenant"], world["config_id"]).revision
    return world["activation"].rollback(
        world["principal"],
        world["config_id"],
        expected_revision=revision,
        reason="operator_request",
        idempotency_key=key,
    )


@pytest.mark.parametrize("hot", [False, True])
def test_verified_retained_generation_switches_without_embedding_and_preserves_history(
    activation: dict[str, Any], hot: bool
) -> None:
    old, current = pair(activation, hot=hot)
    with activation["store"].read() as tx:
        target = tx.providers.get(activation["tenant"], old.config_id)
        planned = activation["activation"].planner.plan(
            tx, target, now_us=activation["store"].clock.now_us()
        )
        assert not planned.rebuild and planned.reuse_generation_id == old.generation_id
    before = len(activation["gateway"]["requests"])
    operation = rollback(activation)
    replay = activation["activation"].rollback(
        activation["principal"],
        old.config_id,
        expected_revision=target.revision,
        reason="operator_request",
        idempotency_key="rollback-one",
    )
    assert replay.id == operation.id
    job = claimed(activation)
    commit = activation["activation"].work(job)
    assert len(activation["gateway"]["requests"]) == before
    assert old_query(activation, current)
    activation["outbox"].execute(job, lambda _: commit, owner="first")
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        assert serving.config_id == old.config_id and serving.generation_id == old.generation_id
        assert serving.epoch == current.epoch + 1
        assert tx.vector.get_generation(old.generation_id).status == "verified"
        assert tx.providers.get(activation["tenant"], current.config_id).status == "retired"
        assert tx.providers.get(activation["tenant"], old.config_id).status == "active"
        assert len(tx.providers.history(activation["tenant"], current.config_id)) == 1
        assert (
            tx.console_operations.get(activation["tenant"], operation.id).provider.generation_id
            == old.generation_id
        )
        for row in tx.raw().execute(
            "SELECT model,dimension,incorporated_generation FROM vector_id_map "
            "WHERE tenant_id=? AND status='active'",
            (activation["tenant"],),
        ):
            assert tuple(row) == ("fixture", 2, old.generation_id)
    assert old_query(activation, serving)


@pytest.mark.parametrize(
    "invalid", ["collected", "missing-file", "corrupt-index", "canonical-change"]
)
def test_unusable_history_rebuilds_old_model_with_current_content(
    activation: dict[str, Any], invalid: str
) -> None:
    old, current = pair(activation)
    directory = activation["root"] / "generations" / old.generation_id
    if invalid == "collected":
        with activation["store"].write() as tx:
            assert old.generation_id in tx.vector.delete_retired_generations(
                activation["tenant"], keep=0
            )
        shutil.rmtree(directory)
    elif invalid == "missing-file":
        (directory / "index.faiss").unlink()
    elif invalid == "corrupt-index":
        (directory / "index.faiss").write_bytes(b"corrupt fixture index")
    else:
        activation["notes"].create(
            activation["access"],
            agent_id=activation["agent"],
            kind="idea",
            title="new canonical content",
            body="Must survive rollback",
            idempotency_key="rollback-concurrent-content",
        )
    with activation["store"].read() as tx:
        target = tx.providers.get(activation["tenant"], old.config_id)
        planned = activation["activation"].planner.plan(
            tx, target, now_us=activation["store"].clock.now_us()
        )
        assert planned.rebuild and planned.reuse_generation_id is None
    before = len(activation["gateway"]["requests"])
    rollback(activation)
    job = claimed(activation)
    assert (
        activation["outbox"].execute(job, activation["activation"].work, owner="first")
        == "completed"
    )
    assert len(activation["gateway"]["requests"]) > before
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        assert serving.config_id == old.config_id and serving.generation_id not in {
            old.generation_id,
            current.generation_id,
        }
        generation = tx.vector.get_generation(serving.generation_id)
        assert generation.space.model == "fixture" and generation.space.dimension == 2
        assert generation.vector_count == activation["generations"].count_resources(
            tx, activation["tenant"]
        )
        assert tx.providers.get(activation["tenant"], current.config_id).status == "retired"
    assert old_query(activation, serving)


def test_files_removed_after_preparation_retry_then_rebuild_without_switching_early(
    activation: dict[str, Any],
) -> None:
    old, current = pair(activation)
    rollback(activation)
    job = claimed(activation)
    commit = activation["activation"].work(job)
    (activation["root"] / "generations" / old.generation_id / "index.faiss").unlink()
    assert activation["outbox"].execute(job, lambda _: commit, owner="first") == "retryable"
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]) == current
    activation["store"].clock.advance(10_000_000)
    worker = OutboxWorker(
        activation["outbox"], {ACTIVATION_JOB_KIND: activation["activation"].work}, owner="retry"
    )
    assert worker.run_once()["completed"] == 1
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        assert serving.config_id == old.config_id and serving.generation_id != old.generation_id
    assert old_query(activation, serving)


def test_cancel_rollback_preserves_retired_target_and_current_service(
    activation: dict[str, Any],
) -> None:
    old, current = pair(activation)
    operation = rollback(activation)
    job = claimed(activation)
    commit = activation["activation"].work(job)
    ConsoleOperations(activation["security"]).cancel(
        activation["principal"],
        operation.id,
        reason="operator_request",
        idempotency_key="cancel-rollback",
    )
    activation["outbox"].execute(job, lambda _: commit, owner="first")
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]) == current
        assert tx.providers.get(activation["tenant"], old.config_id).status == "retired"
    assert old_query(activation, current)


@pytest.mark.parametrize("mode", ["ok", "failure", "cancel"])
def test_reprobe_retired_target_preserves_history_status(
    activation: dict[str, Any], mode: str
) -> None:
    old, current = pair(activation)
    activation["environment"]["PROBE_KEY"] = "rotated-fixture-secret"
    with pytest.raises(ConflictError):
        rollback(activation)
    operation = accept_probe(activation, key="reprobe-retired")
    if mode == "failure":
        activation["gateway"]["mode"] = "failure"
    job = claim_probe(activation)
    if mode == "cancel":
        ConsoleOperations(activation["security"]).cancel(
            activation["principal"],
            operation.id,
            reason="operator_request",
            idempotency_key="cancel-reprobe",
        )
    activation["outbox"].execute(job, activation["service"].work, owner="first")
    with activation["store"].read() as tx:
        target = tx.providers.get(activation["tenant"], old.config_id)
        assert target.status == "retired" and target.last_generation_id == old.generation_id
        assert tx.providers.serving(activation["tenant"]) == current
    if mode == "ok":
        assert rollback(activation, "after-reprobe").status == "queued"
    else:
        with pytest.raises(ConflictError):
            rollback(activation, "after-failed-reprobe")


def test_second_worker_reclaims_rollback_and_first_is_fenced(activation: dict[str, Any]) -> None:
    old, current = pair(activation)
    rollback(activation)
    first = claimed(activation)
    commit = activation["activation"].work(first)
    activation["store"].clock.advance(31_000_000)
    worker = OutboxWorker(
        activation["outbox"], {ACTIVATION_JOB_KIND: activation["activation"].work}, owner="second"
    )
    assert worker.run_once()["completed"] == 1
    with pytest.raises(LeaseFencedError):
        activation["outbox"].execute(first, lambda _: commit, owner="first")
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        assert serving.generation_id == old.generation_id and serving.epoch == current.epoch + 1
    assert old_query(activation, serving)
