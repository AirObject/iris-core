"""Actual HTTP/FAISS activation, old-generation continuity and atomic lease fences."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.console.provider_activation_work import (
    ACTIVATION_JOB_KIND,
    ProviderActivations,
)
from iris_memory_core.application.console.provider_configs import (
    ProviderConfigCommands,
    ProviderSecretInput,
)
from iris_memory_core.domain.errors import ConflictError, LeaseFencedError
from iris_memory_core.domain.provider_configs import ProviderLimits
from iris_memory_core.indexing.provider_generations import ProviderGenerationRuntime
from iris_memory_core.indexing.vector import VectorProjectionService
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from tests.integration.console.test_console_commands import invalidate
from tests.integration.console.test_provider_activation_plans import complete_probe, plan
from tests.integration.console.test_provider_probes import auth as auth_fixture
from tests.integration.console.test_provider_probes import gateway as gateway_fixture
from tests.integration.console.test_provider_probes import probe as probe_fixture
from tests.integration.console.test_provider_probes import world as world_fixture

auth, world, gateway, probe = auth_fixture, world_fixture, gateway_fixture, probe_fixture


@pytest.fixture
def activation(probe: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    store = probe["store"]
    root = tmp_path / "vector"
    generations = ProviderGenerationRuntime(store, store.clock, probe["factory"], root)
    return {
        **probe,
        "root": root,
        "generations": generations,
        "activation": ProviderActivations(probe["security"], probe["factory"], generations),
    }


def accept_activation(world: dict[str, Any], key: str = "activate-one") -> Any:
    prepared_plan = plan(world)
    return world["activation"].accept(
        world["principal"],
        world["config_id"],
        expected_revision=prepared_plan.expected_revision,
        rebuild_ack=prepared_plan.rebuild_plan_hash,
        reason="operator_request",
        idempotency_key=key,
    )


def claimed(world: dict[str, Any], owner: str = "first") -> Any:
    return world["outbox"].claim(owner, kinds=frozenset({ACTIVATION_JOB_KIND})).jobs[0]


def first_generation(world: dict[str, Any]) -> Any:
    complete_probe(world)
    operation = accept_activation(world)
    store = world["store"]
    worker = OutboxWorker(
        world["outbox"],
        phase14_handlers(
            store,
            store.clock,
            store.ids,
            embedding_runtime=world["factory"],
            provider_generations=world["generations"],
        ),
    )
    assert worker.run_once()["completed"] == 1
    with store.read() as tx:
        serving = tx.providers.serving(world["tenant"])
        assert tx.console_operations.get(world["tenant"], operation.id).status == "completed"
        assert tx.providers.get(world["tenant"], world["config_id"]).status == "active"
        assert (
            serving.config_id == world["config_id"]
            and serving.generation_id == tx.vector.pointer(world["tenant"]).generation_id
        )
        assert tx.providers.generation_binding(world["tenant"], serving.generation_id) == (
            serving.config_id,
            1,
        )
    return serving


def next_config(world: dict[str, Any], *, hot: bool = False) -> None:
    with world["store"].read() as tx:
        previous = tx.providers.revision(world["tenant"], world["config_id"], 1)
    definition = replace(previous.definition, limits=ProviderLimits(batch_size=1, max_qps=100))
    if not hot:
        definition = replace(
            definition, space=replace(definition.space, model="next-model", dimension=3)
        )
        world["gateway"]["model_dimensions"] = {"fixture": 2, "next-model": 3}
    commands = ProviderConfigCommands(world["security"], world["factory"].secrets, world["factory"])
    created = commands.create(
        world["principal"],
        definition=definition,
        secret=ProviderSecretInput("secret_ref", "env:PROBE_KEY"),
        reason="operator_request",
        idempotency_key="next-draft",
    )
    world["config_id"] = str(created["id"])
    complete_probe(world, "next-probe")


def old_query(world: dict[str, Any], serving: Any) -> list[Any]:
    with world["store"].read() as tx:
        revision = tx.providers.revision(
            world["tenant"], serving.config_id, serving.content_revision
        )
        service = VectorProjectionService(
            world["store"],
            world["store"].clock,
            provider=world["factory"].resolve(revision),
            vector_root=world["root"],
            space=revision.definition.space,
        )
        query = service.embed_query("query for old generation")
        assert tx.vector.pointer(world["tenant"]).generation_id == serving.generation_id
        return service.search_in_tx(
            tx, tenant_id=world["tenant"], agent_id=world["agent"], query_vector=query, limit=10
        )


def test_first_actual_worker_activation_has_one_consistent_generation_and_receipt(
    activation: dict[str, Any],
) -> None:
    complete_probe(activation)
    prepared_plan = plan(activation)
    for ack in (None, "0" * 64):
        with pytest.raises(ConflictError):
            activation["activation"].accept(
                activation["principal"],
                activation["config_id"],
                expected_revision=3,
                rebuild_ack=ack,
                reason="operator_request",
                idempotency_key="bad-" + str(ack),
            )
    operation = accept_activation(activation)
    replay = activation["activation"].accept(
        activation["principal"],
        activation["config_id"],
        expected_revision=3,
        rebuild_ack=prepared_plan.rebuild_plan_hash,
        reason="operator_request",
        idempotency_key="activate-one",
    )
    assert replay.id == operation.id
    job = claimed(activation)
    assert (
        activation["outbox"].execute(job, activation["activation"].work, owner="first")
        == "completed"
    )
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        generation = tx.vector.get_generation(serving.generation_id)
        assert generation.status == "verified" and generation.vector_count > 0
        assert tx.providers.get(activation["tenant"], activation["config_id"]).status == "active"
        result = tx.console_operations.get(activation["tenant"], operation.id)
        assert (
            result.status == "completed" and result.provider.generation_id == serving.generation_id
        )
    assert old_query(activation, serving)


def test_prepared_new_space_keeps_old_queries_until_single_atomic_publish(
    activation: dict[str, Any],
) -> None:
    old = first_generation(activation)
    next_config(activation)
    operation = accept_activation(activation, "activate-next")
    job = claimed(activation)
    commit = activation["activation"].work(job)
    assert old_query(activation, old)
    with activation["store"].read() as tx:
        assert tx.providers.get(activation["tenant"], old.config_id).status == "active"
        assert (
            tx.providers.get(activation["tenant"], activation["config_id"]).status == "activating"
        )
        assert tx.providers.serving(activation["tenant"]) == old
    activation["outbox"].execute(job, lambda _: commit, owner="first")
    with activation["store"].read() as tx:
        current = tx.providers.serving(activation["tenant"])
        assert current.epoch == old.epoch + 1 and current.generation_id != old.generation_id
        assert tx.vector.pointer(activation["tenant"]).space.dimension == 3
        assert tx.providers.get(activation["tenant"], old.config_id).status == "retired"
        assert tx.providers.get(activation["tenant"], activation["config_id"]).status == "active"
        assert tx.console_operations.get(activation["tenant"], operation.id).status == "completed"
    assert old_query(activation, current)


def test_hot_limits_switch_config_without_embedding_or_new_generation(
    activation: dict[str, Any],
) -> None:
    old = first_generation(activation)
    next_config(activation, hot=True)
    assert not plan(activation).rebuild
    accept_activation(activation, "activate-hot")
    requests = len(activation["gateway"]["requests"])
    job = claimed(activation)
    activation["outbox"].execute(job, activation["activation"].work, owner="first")
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        assert (
            serving.config_id == activation["config_id"]
            and serving.generation_id == old.generation_id
        )
        assert serving.epoch == old.epoch + 1
        assert tx.providers.generation_binding(activation["tenant"], serving.generation_id) == (
            old.config_id,
            old.content_revision,
        )
    assert len(activation["gateway"]["requests"]) == requests


def test_cancellation_after_prepare_preserves_old_pointer_and_prevents_publish(
    activation: dict[str, Any],
) -> None:
    old = first_generation(activation)
    next_config(activation)
    operation = accept_activation(activation, "activate-next")
    job = claimed(activation)
    commit = activation["activation"].work(job)
    ConsoleOperations(activation["security"]).cancel(
        activation["principal"],
        operation.id,
        reason="operator_request",
        idempotency_key="cancel-build",
    )
    activation["outbox"].execute(job, lambda _: commit, owner="first")
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]) == old
        assert tx.providers.get(activation["tenant"], activation["config_id"]).status == "draft"
        assert tx.console_operations.get(activation["tenant"], operation.id).status == "cancelled"
    assert old_query(activation, old)


@pytest.mark.parametrize("kind", ["permission", "epoch", "revision"])
def test_authority_change_after_build_preserves_old_service(
    activation: dict[str, Any], kind: str
) -> None:
    old = first_generation(activation)
    next_config(activation)
    operation = accept_activation(activation, "activate-next")
    job = claimed(activation)
    commit = activation["activation"].work(job)
    invalidate(activation, activation["principal"], kind)
    activation["outbox"].execute(job, lambda _: commit, owner="first")
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]) == old
        assert (
            tx.console_operations.get(activation["tenant"], operation.id).blocked_reason
            == "authority_changed"
        )
    assert old_query(activation, old)


def test_second_actual_worker_fences_prepared_first_generation(activation: dict[str, Any]) -> None:
    old = first_generation(activation)
    next_config(activation)
    accept_activation(activation, "activate-next")
    first = claimed(activation)
    first_commit = activation["activation"].work(first)
    activation["store"].clock.advance(31_000_000)
    worker = OutboxWorker(
        activation["outbox"], {ACTIVATION_JOB_KIND: activation["activation"].work}, owner="second"
    )
    assert worker.run_once()["completed"] == 1
    with pytest.raises(LeaseFencedError):
        activation["outbox"].execute(first, lambda _: first_commit, owner="first")
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        assert serving.epoch == old.epoch + 1
        assert len(tx.raw().execute("SELECT id FROM vector_generations").fetchall()) == 2
    assert old_query(activation, serving)


def test_cancel_stops_at_next_embedding_batch_boundary(
    activation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    old = first_generation(activation)
    next_config(activation)
    operation = accept_activation(activation, "activate-next")
    before = len(activation["gateway"]["requests"])
    original = activation["generations"].prepare

    def prepare(revision: Any, check: Any) -> Any:
        def boundary() -> None:
            if len(activation["gateway"]["requests"]) > before:
                ConsoleOperations(activation["security"]).cancel(
                    activation["principal"],
                    operation.id,
                    reason="operator_request",
                    idempotency_key="cancel-at-batch",
                )
            check()

        return original(revision, boundary)

    monkeypatch.setattr(activation["generations"], "prepare", prepare)
    job = claimed(activation)
    assert (
        activation["outbox"].execute(job, activation["activation"].work, owner="first")
        == "completed"
    )
    assert len(activation["gateway"]["requests"]) == before + 1
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]) == old
        assert tx.console_operations.get(activation["tenant"], operation.id).status == "cancelled"
    assert old_query(activation, old)


def test_failure_after_vector_publish_rolls_back_generation_config_and_operation(
    activation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.storage.provider_configs import ProviderConfigRepository

    old = first_generation(activation)
    next_config(activation)
    operation = accept_activation(activation, "activate-next")
    job = claimed(activation)
    commit = activation["activation"].work(job)
    original = ProviderConfigRepository.switch_serving

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic failure after vector and config writes")

    monkeypatch.setattr(ProviderConfigRepository, "switch_serving", unavailable)
    assert activation["outbox"].execute(job, lambda _: commit, owner="first") == "retryable"
    with activation["store"].read() as tx:
        assert tx.vector.pointer(activation["tenant"]).generation_id == old.generation_id
        assert tx.providers.serving(activation["tenant"]) == old
        assert tx.providers.get(activation["tenant"], old.config_id).status == "active"
        assert (
            tx.providers.get(activation["tenant"], activation["config_id"]).status == "activating"
        )
        assert tx.console_operations.get(activation["tenant"], operation.id).status == "queued"
        assert tx.raw().execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0] == 1
    assert old_query(activation, old)
    monkeypatch.setattr(ProviderConfigRepository, "switch_serving", original)
    activation["store"].clock.advance(10_000_000)
    worker = OutboxWorker(
        activation["outbox"], {ACTIVATION_JOB_KIND: activation["activation"].work}, owner="retry"
    )
    assert worker.run_once()["completed"] == 1
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]).epoch == old.epoch + 1


def test_credential_change_after_build_prevents_publishing_new_generation(
    activation: dict[str, Any],
) -> None:
    old = first_generation(activation)
    next_config(activation)
    operation = accept_activation(activation, "activate-next")
    job = claimed(activation)
    commit = activation["activation"].work(job)
    activation["environment"]["PROBE_KEY"] = "new-credential-after-build"
    activation["outbox"].execute(job, lambda _: commit, owner="first")
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]) == old
        assert (
            tx.console_operations.get(activation["tenant"], operation.id).blocked_reason
            == "provider_probe_invalid"
        )
        assert tx.providers.get(activation["tenant"], activation["config_id"]).status == "draft"


def test_canonical_change_after_build_retries_without_publishing_incomplete_generation(
    activation: dict[str, Any],
) -> None:
    old = first_generation(activation)
    next_config(activation)
    accept_activation(activation, "activate-next")
    job = claimed(activation)
    commit = activation["activation"].work(job)
    activation["notes"].create(
        activation["access"],
        agent_id=activation["agent"],
        kind="idea",
        title="Concurrent canonical write",
        body="Must be in the next generation",
        idempotency_key="during-build",
    )
    assert activation["outbox"].execute(job, lambda _: commit, owner="first") == "retryable"
    with activation["store"].read() as tx:
        assert tx.providers.serving(activation["tenant"]) == old
    activation["store"].clock.advance(10_000_000)
    worker = OutboxWorker(
        activation["outbox"], {ACTIVATION_JOB_KIND: activation["activation"].work}, owner="retry"
    )
    assert worker.run_once()["completed"] == 1
    with activation["store"].read() as tx:
        serving = tx.providers.serving(activation["tenant"])
        generation = tx.vector.get_generation(serving.generation_id)
        assert generation.vector_count == activation["generations"].count_resources(
            tx, activation["tenant"]
        )


def test_saved_activation_plan_and_actor_target_cannot_be_rewritten(
    activation: dict[str, Any],
) -> None:
    complete_probe(activation)
    operation = accept_activation(activation)
    for column, value in (
        ("plan_json", "{}"),
        ("plan_hash", "0" * 64),
        ("action", "rollback"),
        ("expected_serving_epoch", 4),
    ):
        with (
            activation["store"].write() as tx,
            pytest.raises(sqlite3.IntegrityError, match="provider operation intent is immutable"),
        ):
            tx.raw().execute(
                f"UPDATE console_operation_providers SET {column}=? WHERE operation_id=?",
                (value, operation.id),
            )
    job = claimed(activation)
    assert (
        activation["outbox"].execute(job, activation["activation"].work, owner="first")
        == "completed"
    )
