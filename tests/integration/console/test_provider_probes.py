"""Real HTTP probes with durable budgets, actual Console identity and Outbox fences."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.console.provider_configs import (
    ProviderConfigCommands,
    ProviderSecretInput,
)
from iris_memory_core.application.console.provider_probes import (
    PROBE_JOB_KIND,
    ProviderProbeBudget,
    ProviderProbes,
)
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.domain.errors import AccessDeniedError, LeaseFencedError
from iris_memory_core.domain.provider_configs import EmbeddingDefinition
from iris_memory_core.domain.vector import VectorSpaceConfig
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from iris_memory_core.providers.configured import ConfiguredEmbeddingFactory
from iris_memory_core.providers.secrets import ProviderSecrets
from iris_memory_core.providers.transport import ProviderOutboundPolicy
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import invalidate, principal_for
from tests.integration.console.test_console_reads import world as world_fixture
from tests.integration.recall.test_configured_embedding import gateway as gateway_fixture

auth = auth_fixture
world = world_fixture
gateway = gateway_fixture


@pytest.fixture
def probe(world: dict[str, Any], gateway: dict[str, Any]) -> dict[str, Any]:
    environment = {"PROBE_KEY": "isolated-private-provider-token"}
    secrets = ProviderSecrets(
        allowed_references={world["tenant"]: frozenset({"env:PROBE_KEY"})}, environment=environment
    )
    factory = ConfiguredEmbeddingFactory(
        secrets, policy=ProviderOutboundPolicy(allow_loopback=True)
    )
    commands = ProviderConfigCommands(world["security"], secrets, factory)
    principal = world["security"].reauth(principal_for(world), world["token"])
    view = commands.create(
        principal,
        definition=EmbeddingDefinition(
            "openai-compatible",
            gateway["endpoint"],
            VectorSpaceConfig(model="fixture", dimension=2),
        ),
        secret=ProviderSecretInput("secret_ref", "env:PROBE_KEY"),
        reason="operator_request",
        idempotency_key="draft-one",
    )
    store = world["store"]
    return {
        **world,
        "gateway": gateway,
        "environment": environment,
        "factory": factory,
        "service": ProviderProbes(world["security"], factory),
        "principal": principal,
        "config_id": str(view["id"]),
        "outbox": OutboxService(store, store.clock),
    }


def accept(probe: dict[str, Any], *, key: str = "probe-one") -> Any:
    with probe["store"].read() as tx:
        revision = tx.providers.get(probe["tenant"], probe["config_id"]).revision
    return probe["service"].accept(
        probe["principal"],
        probe["config_id"],
        expected_revision=revision,
        reason="operator_request",
        idempotency_key=key,
    )


def claim(probe: dict[str, Any], owner: str = "first") -> Any:
    return probe["outbox"].claim(owner, kinds=frozenset({PROBE_JOB_KIND})).jobs[0]


def snapshot(probe: dict[str, Any]) -> Any:
    with probe["store"].read() as tx:
        config = tx.providers.get(probe["tenant"], probe["config_id"])
        result = (
            tx.providers.probe(probe["tenant"], config.latest_probe_id)
            if config.latest_probe_id
            else None
        )
        budget = (
            tx.raw()
            .execute(
                "SELECT attempts,input_chars FROM provider_probe_budgets WHERE tenant_id=?",
                (probe["tenant"],),
            )
            .fetchone()
        )
        return config, result, tuple(budget) if budget else None


def test_actual_registered_worker_publishes_fixed_probe_once(probe: dict[str, Any]) -> None:
    operation = accept(probe)
    replay = probe["service"].accept(
        probe["principal"],
        probe["config_id"],
        expected_revision=1,
        reason="operator_request",
        idempotency_key="probe-one",
    )
    assert replay.id == operation.id and probe["gateway"]["requests"] == []
    assert snapshot(probe)[0].status == "probing"
    store = probe["store"]
    worker = OutboxWorker(
        probe["outbox"],
        phase14_handlers(store, store.clock, store.ids, embedding_runtime=probe["factory"]),
    )
    assert worker.run_once()["completed"] == 1
    config, result, budget = snapshot(probe)
    assert (
        config.status == "probed"
        and result.ok
        and result.dimension_observed == 2
        and result.normalized
    )
    assert budget == (1, 31)
    assert probe["gateway"]["requests"][0][0]["input"] == ["iris probe alpha", "iris probe beta"]
    assert (
        ConsoleOperations(probe["security"]).detail(probe["principal"], operation.id).status
        == "completed"
    )
    assert worker.run_once()["claimed"] == 0


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("failure", "transport_error"),
        ("rate", "rate_limited"),
        ("invalid", "invalid_output"),
        ("redirect", "transport_error"),
    ],
)
def test_provider_failure_records_bounded_result_and_returns_draft(
    probe: dict[str, Any], mode: str, outcome: str
) -> None:
    operation = accept(probe)
    probe["gateway"]["mode"] = mode
    job = claim(probe)
    assert probe["outbox"].execute(job, probe["service"].work, owner="first") == "completed"
    config, result, budget = snapshot(probe)
    assert (
        config.status == "draft"
        and not result.ok
        and result.outcome == outcome
        and budget == (1, 31)
    )
    assert (
        ConsoleOperations(probe["security"]).detail(probe["principal"], operation.id).status
        == "failed"
    )
    assert len(probe["gateway"]["requests"]) == 1
    assert "isolated-private-provider-token" not in repr(result)


def test_dimension_failure_keeps_actual_dimension_and_normalization(probe: dict[str, Any]) -> None:
    accept(probe)
    probe["gateway"]["dimension"] = 3
    job = claim(probe)
    probe["outbox"].execute(job, probe["service"].work, owner="first")
    config, result, _ = snapshot(probe)
    assert (
        config.status == "draft"
        and result.dimension_observed == 3
        and result.normalized
        and not result.ok
    )


def test_budget_is_durable_across_new_service_instances_and_no_extra_outbound(
    probe: dict[str, Any],
) -> None:
    service = ProviderProbes(
        probe["security"], probe["factory"], budget=ProviderProbeBudget(attempts=1)
    )
    accept(probe)
    job = claim(probe)
    probe["outbox"].execute(job, service.work, owner="first")
    second = accept(probe, key="second-probe")
    restarted = ProviderProbes(
        probe["security"], probe["factory"], budget=ProviderProbeBudget(attempts=1)
    )
    job = claim(probe)
    probe["outbox"].execute(job, restarted.work, owner="first")
    config, result, budget = snapshot(probe)
    assert config.status == "draft" and result is None and budget == (1, 31)
    assert (
        ConsoleOperations(probe["security"]).detail(probe["principal"], second.id).blocked_reason
        == "provider_budget_exhausted"
    )
    assert len(probe["gateway"]["requests"]) == 1


@pytest.mark.parametrize("before_request", [True, False])
def test_cancel_at_either_boundary_prevents_late_publication(
    probe: dict[str, Any], before_request: bool
) -> None:
    operation = accept(probe)
    job = claim(probe)
    operations = ConsoleOperations(probe["security"])
    commit = None if before_request else probe["service"].work(job)
    assert (
        operations.cancel(
            probe["principal"],
            operation.id,
            reason="operator_request",
            idempotency_key="cancel-one",
        ).status
        == "cancelled"
    )
    if commit is None:
        commit = probe["service"].work(job)
    assert probe["outbox"].execute(job, lambda _: commit, owner="first") == "completed"
    config, result, budget = snapshot(probe)
    assert config.status == "draft" and config.current_operation_id is None and result is None
    assert len(probe["gateway"]["requests"]) == (0 if before_request else 1)
    assert budget == (None if before_request else (1, 31))


def test_actual_second_worker_reclaims_lease_and_first_result_is_fenced(
    probe: dict[str, Any],
) -> None:
    accept(probe)
    first = claim(probe)
    stale_commit = probe["service"].work(first)
    probe["store"].clock.advance(31_000_000)
    worker = OutboxWorker(probe["outbox"], {PROBE_JOB_KIND: probe["service"].work}, owner="second")
    assert worker.run_once()["completed"] == 1
    with pytest.raises(LeaseFencedError):
        probe["outbox"].execute(first, lambda _: stale_commit, owner="first")
    config, result, budget = snapshot(probe)
    assert config.status == "probed" and result.ok and budget == (2, 62)
    with probe["store"].read() as tx:
        assert tx.raw().execute("SELECT COUNT(*) FROM provider_probes").fetchone()[0] == 1
    assert len(probe["gateway"]["requests"]) == 2


@pytest.mark.parametrize("kind", ["permission", "revoked", "epoch", "revision"])
def test_authority_change_after_http_never_commits_success(
    probe: dict[str, Any], kind: str
) -> None:
    operation = accept(probe)
    job = claim(probe)
    commit = probe["service"].work(job)
    invalidate(probe, probe["principal"], kind)
    probe["outbox"].execute(job, lambda _: commit, owner="first")
    config, result, _ = snapshot(probe)
    assert config.status == "draft" and result is None
    with probe["store"].read() as tx:
        assert (
            tx.console_operations.get(probe["tenant"], operation.id).blocked_reason
            == "authority_changed"
        )


def test_credential_change_after_outbound_requires_fresh_probe(probe: dict[str, Any]) -> None:
    operation = accept(probe)
    job = claim(probe)
    commit = probe["service"].work(job)
    probe["environment"]["PROBE_KEY"] = "replacement-private-key"
    probe["outbox"].execute(job, lambda _: commit, owner="first")
    assert snapshot(probe)[1] is None
    assert (
        ConsoleOperations(probe["security"]).detail(probe["principal"], operation.id).blocked_reason
        == "secret_changed"
    )


def test_probe_time_is_observation_time_not_delayed_commit_time(probe: dict[str, Any]) -> None:
    accept(probe)
    job = claim(probe)
    observed_us = probe["store"].clock.now_us()
    commit = probe["service"].work(job)
    probe["store"].clock.advance(1_000_000)
    probe["outbox"].execute(job, lambda _: commit, owner="first")
    assert snapshot(probe)[1].created_us == observed_us


def test_missing_recent_reauth_and_expired_lease_cannot_send(probe: dict[str, Any]) -> None:
    with probe["store"].write() as tx:
        session = tx.console.session(probe["principal"].session.id)
        tx.console.save_session(replace(session, reauth_until_us=0))
    with pytest.raises(AccessDeniedError):
        accept(probe)
    probe["principal"] = probe["security"].reauth(probe["principal"], probe["token"])
    accept(probe)
    job = claim(probe)
    probe["store"].clock.advance(31_000_000)
    with pytest.raises(LeaseFencedError):
        probe["service"].work(job)
    assert probe["gateway"]["requests"] == [] and snapshot(probe)[2] is None


def test_real_dead_job_releases_the_draft_for_future_probe(probe: dict[str, Any]) -> None:
    operation = accept(probe)
    job = claim(probe)

    class PermanentFailure(ValueError):
        retryable = False

    def broken(_: Any) -> Any:
        raise PermanentFailure("synthetic permanent internal failure")

    assert probe["outbox"].execute(job, broken, owner="first") == "dead"
    assert snapshot(probe)[0].status == "draft"
    assert (
        ConsoleOperations(probe["security"]).detail(probe["principal"], operation.id).status
        == "failed"
    )
    assert accept(probe, key="retry-after-dead").status == "queued"


def test_console_http_operation_and_problem_contracts_cover_real_provider_result(
    probe: dict[str, Any],
) -> None:
    from tests.contract.test_console_contract import validate_response
    from tests.integration.console.test_console_authentication import signed_in

    operation = accept(probe)
    client, _ = signed_in(probe["app"], probe["token"])
    with client:
        response = client.get("/v1/operations/" + operation.id)
        assert response.status_code == 200, response.text
        validate_response("ConsoleOperationEnvelope", response.json())
        assert response.json()["data"]["phase"] == "provider_configuration"
        listing = client.get("/v1/operations?kind=embedding_provider")
        assert listing.status_code == 200, listing.text
        validate_response("ConsoleOperationPage", listing.json())
        assert [row["id"] for row in listing.json()["data"]] == [operation.id]
        probe["gateway"]["mode"] = "failure"
        job = claim(probe)
        probe["outbox"].execute(job, probe["service"].work, owner="first")
        problems = client.get("/v1/operations/" + operation.id + "/problems")
        assert problems.status_code == 200, problems.text
        validate_response("ConsoleOperationProblemPage", problems.json())
        assert problems.json()["data"][0]["code"] == "provider_probe_failed"
        assert "isolated-private-provider-token" not in response.text + listing.text + problems.text
