"""Activation plans derive from real probe results and current canonical snapshots."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.console.provider_activation import (
    PROBE_MAX_AGE_US,
    ProviderActivationPlanner,
)
from iris_memory_core.application.console.provider_configs import (
    ProviderConfigCommands,
    ProviderSecretInput,
)
from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.provider_configs import ProviderLimits, ProviderServing
from iris_memory_core.indexing.vector import VectorProjectionService, collect_vector_entries
from tests.integration.console.test_provider_probes import accept, claim
from tests.integration.console.test_provider_probes import auth as auth_fixture
from tests.integration.console.test_provider_probes import gateway as gateway_fixture
from tests.integration.console.test_provider_probes import probe as probe_fixture
from tests.integration.console.test_provider_probes import world as world_fixture

auth, world, gateway, probe = auth_fixture, world_fixture, gateway_fixture, probe_fixture


def planner(probe: dict[str, Any]) -> ProviderActivationPlanner:
    return ProviderActivationPlanner(
        probe["factory"], lambda tx, tenant: len(collect_vector_entries(tx, tenant)[0])
    )


def complete_probe(probe: dict[str, Any], key: str = "probe-one") -> None:
    accept(probe, key=key)
    job = claim(probe)
    assert probe["outbox"].execute(job, probe["service"].work, owner="first") == "completed"


def plan(probe: dict[str, Any]) -> Any:
    with probe["store"].read() as tx:
        return planner(probe).plan(
            tx,
            tx.providers.get(probe["tenant"], probe["config_id"]),
            now_us=probe["store"].clock.now_us(),
        )


def test_unprobed_and_failed_probes_cannot_plan_activation(probe: dict[str, Any]) -> None:
    with pytest.raises(ConflictError) as error:
        plan(probe)
    assert error.value.details["kind"] == "provider_probe_required"
    probe["gateway"]["dimension"] = 3
    complete_probe(probe)
    with pytest.raises(ConflictError):
        plan(probe)


@pytest.mark.parametrize(
    ("age", "accepted"),
    [(0, True), (PROBE_MAX_AGE_US, True), (PROBE_MAX_AGE_US + 1, False), (-1, False)],
)
def test_probe_freshness_exact_boundary_and_clock_rollback(
    probe: dict[str, Any], age: int, accepted: bool
) -> None:
    complete_probe(probe)
    with probe["store"].read() as tx:
        config = tx.providers.get(probe["tenant"], probe["config_id"])
        observed = tx.providers.probe(probe["tenant"], config.latest_probe_id)
        if accepted:
            assert planner(probe).plan(tx, config, now_us=observed.created_us + age).rebuild
        else:
            with pytest.raises(ConflictError) as error:
                planner(probe).plan(tx, config, now_us=observed.created_us + age)
            assert error.value.details["kind"] == "provider_probe_expired"


def test_initial_plan_binds_actual_count_snapshot_and_requires_exact_ack(
    probe: dict[str, Any],
) -> None:
    complete_probe(probe)
    first = plan(probe)
    assert first.rebuild and first.estimated_resources > 0 and first.serving_epoch == 0
    assert first == plan(probe)
    for ack in (None, "", "0" * 64):
        with pytest.raises(ConflictError):
            first.require_ack(ack)
    first.require_ack(first.rebuild_plan_hash)
    # Canonical content change changes the server estimate and therefore the ack.
    probe["notes"].create(
        probe["access"],
        agent_id=probe["agent"],
        kind="idea",
        title="new canonical entry",
        body="new content",
        idempotency_key="plan-count-change",
    )
    changed = plan(probe)
    assert changed.estimated_resources == first.estimated_resources + 1
    assert changed.rebuild_plan_hash != first.rebuild_plan_hash
    with pytest.raises(ConflictError):
        changed.require_ack(first.rebuild_plan_hash)


def test_secret_ref_rotation_invalidates_successful_probe_without_edit(
    probe: dict[str, Any],
) -> None:
    complete_probe(probe)
    plan(probe)
    probe["environment"]["PROBE_KEY"] = "changed-provider-token"
    with pytest.raises(ConflictError) as error:
        plan(probe)
    assert error.value.details["kind"] == "provider_secret_changed"


@pytest.mark.parametrize("change", ["endpoint", "model", "dimension"])
def test_actual_generation_limits_are_hot_but_endpoint_model_or_space_require_rebuild(
    probe: dict[str, Any], tmp_path: Path, change: str
) -> None:
    complete_probe(probe)
    store = probe["store"]
    with store.read() as tx:
        config = tx.providers.get(probe["tenant"], probe["config_id"])
        revision = tx.providers.revision(probe["tenant"], config.id, config.content_revision)
    service = VectorProjectionService(
        store,
        store.clock,
        provider=probe["factory"].resolve(revision),
        vector_root=tmp_path / "vector",
        space=revision.definition.space,
    )
    prepared = service.prepare_generation(probe["tenant"])
    with store.write() as tx:
        service.switch_in_tx(tx, probe["tenant"], prepared)
        current = tx.providers.get(probe["tenant"], config.id)
        tx.providers.advance(
            replace(
                current,
                status="active",
                revision=current.revision + 1,
                last_generation_id=prepared.generation_id,
            ),
            expected_revision=current.revision,
        )
        tx.providers.bind_generation(
            probe["tenant"],
            prepared.generation_id,
            config.id,
            config.content_revision,
            now_us=store.clock.now_us(),
        )
        tx.providers.switch_serving(
            ProviderServing(
                probe["tenant"],
                config.id,
                config.content_revision,
                prepared.generation_id,
                1,
                store.clock.now_us(),
            ),
            expected_epoch=0,
        )
    commands = ProviderConfigCommands(probe["security"], probe["factory"].secrets, probe["factory"])
    definition = replace(revision.definition, limits=ProviderLimits(batch_size=4, max_qps=20))
    created = commands.create(
        probe["principal"],
        definition=definition,
        secret=ProviderSecretInput("secret_ref", "env:PROBE_KEY"),
        reason="operator_request",
        idempotency_key="hot-config",
    )
    probe["config_id"] = str(created["id"])
    complete_probe(probe, key="hot-probe")
    hot = plan(probe)
    assert not hot.rebuild and hot.estimated_resources == 0 and hot.serving_epoch == 1
    hot.require_ack(None)
    # Different endpoint names the same isolated server but is conservatively a new provider.
    if change == "endpoint":
        next_definition = replace(definition, endpoint=definition.endpoint + "/changed")
    elif change == "model":
        next_definition = replace(definition, space=replace(definition.space, model="new-model"))
    else:
        next_definition = replace(definition, space=replace(definition.space, dimension=3))
        probe["gateway"]["dimension"] = 3
    changed = commands.patch(
        probe["principal"],
        probe["config_id"],
        expected_revision=3,
        definition=next_definition,
        reason="operator_request",
        idempotency_key="change-endpoint",
    )
    assert changed["status"] == "draft"
    complete_probe(probe, key="endpoint-probe")
    assert plan(probe).rebuild
