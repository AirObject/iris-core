"""Authenticated Console HTTP lifecycle over actual provider HTTP and FAISS work."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from iris_memory_core.api.console.app import create_console_app
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import headers, signed_in
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


@pytest.fixture
def http(activation: dict[str, Any]) -> dict[str, Any]:
    app = create_console_app(
        store=activation["store"],
        embedding_runtime=activation["factory"],
        provider_generations=activation["generations"],
    )
    client, csrf = signed_in(app, activation["token"])
    store = activation["store"]
    worker = OutboxWorker(
        activation["outbox"],
        phase14_handlers(
            store,
            store.clock,
            store.ids,
            embedding_runtime=activation["factory"],
            provider_generations=activation["generations"],
        ),
    )
    return {**activation, "app": app, "client": client, "csrf": csrf, "worker": worker}


def definition(world: dict[str, Any], *, second: bool = False) -> dict[str, Any]:
    with world["store"].read() as tx:
        value = json.loads(
            tx.providers.revision(world["tenant"], world["config_id"], 1).definition.encode()
        )
    if second:
        value["space"]["model"] = "next-model"
        value["space"]["dimension"] = 3
        world["gateway"]["model_dimensions"] = {"fixture": 2, "next-model": 3}
    return cast(dict[str, Any], value)


def create(world: dict[str, Any], *, second: bool = False) -> dict[str, Any]:
    response = world["client"].post(
        "/v1/providers/embedding/configs",
        json={
            "definition": definition(world, second=second),
            "secret": {"mode": "secret_ref", "value": "env:PROBE_KEY"},
            "reason_code": "operator_request",
        },
        headers=headers(world["csrf"]),
    )
    assert response.status_code == 201, response.text
    validate_response("EmbeddingConfigViewEnvelope", response.json())
    return cast(dict[str, Any], response.json()["data"])


def detail(world: dict[str, Any], identifier: str) -> dict[str, Any]:
    response = world["client"].get("/v1/providers/embedding/configs/" + identifier)
    assert response.status_code == 200, response.text
    validate_response("EmbeddingConfigViewEnvelope", response.json())
    return cast(dict[str, Any], response.json()["data"])


def command(world: dict[str, Any], path: str, value: dict[str, Any]) -> Any:
    return world["client"].post(
        "/v1/providers/embedding/" + path,
        json={"reason_code": "operator_request", **value},
        headers=headers(world["csrf"]),
    )


def reauth(world: dict[str, Any]) -> None:
    response = world["client"].post(
        "/v1/auth/reauth", json={"key": world["token"]}, headers=headers(world["csrf"])
    )
    assert response.status_code == 200, response.text


def run_probe(world: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    response = command(
        world, "configs/" + config["id"] + ":test", {"expected_revision": config["revision"]}
    )
    assert response.status_code == 202, response.text
    validate_response("ConsoleOperationEnvelope", response.json())
    assert world["worker"].run_once()["completed"] == 1
    value = detail(world, config["id"])
    assert value["status"] == "probed" and value["probe"]["ok"]
    return value


def activated(world: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    plan = config["activation_plan"]
    response = command(
        world,
        "configs/" + config["id"] + ":activate",
        {"expected_revision": config["revision"], "rebuild_ack": plan["rebuild_plan_hash"]},
    )
    assert response.status_code == 202, response.text
    validate_response("EmbeddingRebuildEnvelope", response.json())
    assert response.json()["data"]["side_effects"] == plan["side_effects"]
    assert world["worker"].run_once()["completed"] == 1
    return detail(world, config["id"])


def test_real_http_configuration_probe_activate_and_retained_rollback(http: dict[str, Any]) -> None:
    for path, schema in [
        ("/v1/providers", "ProvidersViewEnvelope"),
        ("/v1/providers/embedding", "EmbeddingOverviewEnvelope"),
        ("/v1/providers/embedding/adapters", "EmbeddingAdapterPage"),
    ]:
        response = http["client"].get(path)
        assert response.status_code == 200, response.text
        validate_response(schema, response.json())
    assert "providers" in http["client"].get("/v1/bootstrap").json()["data"]["modules"]
    first = create(http)
    response = command(
        http, "configs/" + first["id"] + ":test", {"expected_revision": first["revision"]}
    )
    assert (
        response.status_code == 403
        and response.json()["error"]["details"]["kind"] == "reauth_required"
    )
    assert http["gateway"]["requests"] == []
    reauth(http)
    first = run_probe(http, first)
    bad = command(
        http, "configs/" + first["id"] + ":activate", {"expected_revision": first["revision"]}
    )
    assert (
        bad.status_code == 409
        and bad.json()["error"]["details"]["kind"] == "provider_rebuild_ack_required"
    )
    first = activated(http, first)
    old_generation = first["last_generation_id"]
    assert first["status"] == "active"
    second = activated(http, run_probe(http, create(http, second=True)))
    assert second["last_generation_id"] != old_generation
    retired = detail(http, first["id"])
    assert retired["status"] == "retired"
    assert retired["activation_plan"]["reuse_generation_id"] == old_generation
    response = http["client"].post(
        "/v1/providers/embedding:rollback",
        json={
            "target_config_id": first["id"],
            "expected_revision": retired["revision"],
            "reason_code": "operator_request",
        },
        headers=headers(http["csrf"]),
    )
    assert response.status_code == 202, response.text
    validate_response("EmbeddingRebuildEnvelope", response.json())
    assert response.json()["data"]["side_effects"]["rebuild"] is False
    assert http["worker"].run_once()["completed"] == 1
    assert detail(http, first["id"])["last_generation_id"] == old_generation
    assert detail(http, second["id"])["status"] == "retired"
    rebuilds = http["client"].get("/v1/providers/embedding/rebuilds").json()
    validate_response("EmbeddingRebuildPage", rebuilds)
    assert len(rebuilds["data"]) == 3
    assert all(row["operation"]["status"] == "completed" for row in rebuilds["data"])
    assert all(row["operation"]["progress"]["unit"] == "steps" for row in rebuilds["data"])


def test_patch_invalidates_probe_and_history_is_cursor_bound(http: dict[str, Any]) -> None:
    reauth(http)
    config = run_probe(http, create(http))
    value = definition(http)
    value["label"] = "new immutable revision"
    response = http["client"].patch(
        "/v1/providers/embedding/configs/" + config["id"],
        json={
            "expected_revision": config["revision"],
            "definition": value,
            "reason_code": "operator_request",
        },
        headers=headers(http["csrf"]),
    )
    assert response.status_code == 200, response.text
    patched = detail(http, config["id"])
    assert patched["status"] == "draft" and patched["content_revision"] == 2
    assert patched["probe"] is None and patched["activation_plan"] is None
    path = "/v1/providers/embedding/configs/" + config["id"] + "/revisions"
    first = http["client"].get(path, params={"limit": 1}).json()
    validate_response("EmbeddingRevisionPage", first)
    cursor = first["meta"]["page"]["next_cursor"]
    assert first["data"][0]["content_revision"] == 2 and cursor
    second = http["client"].get(path, params={"limit": 1, "cursor": cursor}).json()
    assert second["data"][0]["content_revision"] == 1
    other = http["client"].get("/v1/providers/embedding", params={"limit": 1, "cursor": cursor})
    assert other.status_code == 400
    assert http["client"].get(path, params={"limit": 101}).status_code == 400
    assert (
        command(
            http, "configs/" + config["id"] + ":discard", {"expected_revision": patched["revision"]}
        ).status_code
        == 200
    )


def test_cancel_rebuild_keeps_old_generation_and_rejects_other_operation_kinds(
    http: dict[str, Any],
) -> None:
    reauth(http)
    first = activated(http, run_probe(http, create(http)))
    second = run_probe(http, create(http, second=True))
    accepted = command(
        http,
        "configs/" + second["id"] + ":activate",
        {
            "expected_revision": second["revision"],
            "rebuild_ack": second["activation_plan"]["rebuild_plan_hash"],
        },
    )
    operation = accepted.json()["data"]["operation"]["id"]
    result = command(http, "rebuilds/" + operation + ":cancel", {})
    assert result.status_code == 200, result.text
    validate_response("EmbeddingRebuildEnvelope", result.json())
    assert result.json()["data"]["operation"]["status"] == "cancelled"
    http["worker"].run_once()
    assert detail(http, first["id"])["status"] == "active"
    assert detail(http, second["id"])["status"] == "draft"
    with http["store"].read() as tx:
        probe_id = tx.providers.probe(http["tenant"], first["probe"]["id"]).operation_id
    assert command(http, "rebuilds/" + probe_id + ":cancel", {}).status_code == 404


@pytest.mark.parametrize("permission", ["system.read", "providers.manage"])
def test_reader_cannot_write_but_provider_manager_can_read_its_plans(
    http: dict[str, Any], permission: str
) -> None:
    grant = OperatorGrant(
        frozenset({permission}),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        data_purposes=frozenset({"console.manage"}),
    )
    _, token = http["security"].issue_offline(
        tenant_id=http["tenant"],
        label="scope",
        description="fixture",
        template="viewer",
        grant=grant,
        expires_us=http["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(http["app"], token)
    response = client.get("/v1/providers/embedding")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["can_manage"] is (permission == "providers.manage")
    response = client.post(
        "/v1/providers/embedding/configs",
        json={
            "definition": definition(http),
            "secret": {"mode": "secret_ref", "value": "env:PROBE_KEY"},
            "reason_code": "operator_request",
        },
        headers=headers(csrf),
    )
    assert response.status_code == (403 if permission == "system.read" else 201), response.text


def test_sealed_without_master_and_arbitrary_transport_inputs_are_rejected(
    http: dict[str, Any],
) -> None:
    payload: dict[str, Any] = {
        "definition": definition(http),
        "secret": {"mode": "sealed", "value": "private-fixture-secret"},
        "reason_code": "operator_request",
    }
    response = http["client"].post(
        "/v1/providers/embedding/configs", json=payload, headers=headers(http["csrf"])
    )
    assert response.status_code == 409, response.text
    assert "private-fixture-secret" not in response.text
    assert response.json()["error"]["details"]["kind"] == "secret_unavailable"
    payload["definition"]["headers"] = {"Authorization": "private-fixture-secret"}
    response = http["client"].post(
        "/v1/providers/embedding/configs", json=payload, headers=headers(http["csrf"])
    )
    assert response.status_code == 400
    assert http["gateway"]["requests"] == []


def another_operator(http: dict[str, Any], *, tenant: str | None = None) -> dict[str, Any]:
    if tenant is not None:
        with http["store"].write() as tx:
            tx.insert_tenant(tenant, status="active")
    _, token = http["security"].issue_offline(
        tenant_id=tenant or http["tenant"],
        label="Separate HTTP operator",
        description="Isolation fixture",
        template="owner",
        grant=http["principal"].key.grant,
        expires_us=http["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(http["app"], token)
    other = {**http, "client": client, "csrf": csrf, "token": token}
    reauth(other)
    return other


def test_other_tenant_cannot_read_history_mutate_or_cancel_provider_work(
    http: dict[str, Any],
) -> None:
    reauth(http)
    config = run_probe(http, create(http))
    accepted = command(
        http,
        "configs/" + config["id"] + ":activate",
        {
            "expected_revision": config["revision"],
            "rebuild_ack": config["activation_plan"]["rebuild_plan_hash"],
        },
    )
    operation_id = accepted.json()["data"]["operation"]["id"]
    other = another_operator(http, tenant="separate-http-provider-tenant")
    for path in [
        "configs/" + config["id"],
        "configs/" + config["id"] + "/revisions",
        "rebuilds/" + operation_id,
    ]:
        response = other["client"].get("/v1/providers/embedding/" + path)
        assert response.status_code == 404, response.text
    for action in ("test", "activate", "discard"):
        response = command(
            other,
            "configs/" + config["id"] + ":" + action,
            {"expected_revision": config["revision"]},
        )
        assert response.status_code == 404, response.text
    assert command(other, "rebuilds/" + operation_id + ":cancel", {}).status_code == 404
    assert other["client"].get("/v1/providers/embedding").json()["data"]["configs"] == []
    assert other["client"].get("/v1/providers/embedding/rebuilds").json()["data"] == []
    assert http["worker"].run_once()["completed"] == 1


def test_same_tenant_other_operator_can_observe_but_not_cancel(http: dict[str, Any]) -> None:
    reauth(http)
    config = run_probe(http, create(http))
    accepted = command(
        http,
        "configs/" + config["id"] + ":activate",
        {
            "expected_revision": config["revision"],
            "rebuild_ack": config["activation_plan"]["rebuild_plan_hash"],
        },
    )
    identifier = accepted.json()["data"]["operation"]["id"]
    other = another_operator(http)
    result = other["client"].get("/v1/providers/embedding/rebuilds/" + identifier)
    validate_response("EmbeddingRebuildEnvelope", result.json())
    assert result.json()["data"]["operation"]["cancellable"] is False
    assert result.json()["data"]["operation"]["available_actions"] == []
    assert command(other, "rebuilds/" + identifier + ":cancel", {}).status_code == 404
    assert http["worker"].run_once()["completed"] == 1


@pytest.mark.parametrize("mode", ["invalid", "redirect", "rate", "failure", "norm", "dimension"])
def test_real_failed_probe_never_returns_secrets_body_or_vectors(
    http: dict[str, Any], mode: str
) -> None:
    reauth(http)
    config = create(http)
    if mode == "dimension":
        http["gateway"]["dimension"] = 3
    else:
        http["gateway"]["mode"] = mode
    accepted = command(
        http, "configs/" + config["id"] + ":test", {"expected_revision": config["revision"]}
    )
    assert accepted.status_code == 202, accepted.text
    http["worker"].run_once()
    result = detail(http, config["id"])
    assert result["status"] == "draft" and result["probe"]["ok"] is False
    assert result["activation_plan"] is None
    operation = http["client"].get("/v1/operations/" + accepted.json()["data"]["id"])
    assert operation.status_code == 200
    validate_response("ConsoleOperationEnvelope", operation.json())
    combined = json.dumps(result) + operation.text
    for forbidden in (
        "isolated-private-provider-token",
        "private-error-response",
        "ciphertext",
        "secret_fingerprint",
        '"embedding":',
        "env:PROBE_KEY",
    ):
        assert forbidden not in combined
    assert len(http["gateway"]["requests"]) == 1


def test_current_secret_change_and_stale_revision_reject_activation_without_outbound(
    http: dict[str, Any],
) -> None:
    reauth(http)
    config = run_probe(http, create(http))
    http["environment"]["PROBE_KEY"] = "rotated-private-provider-token"
    refreshed = detail(http, config["id"])
    assert refreshed["activation_plan"] is None
    assert refreshed["activation_blocked_reason"] == "provider_secret_changed"
    result = command(
        http,
        "configs/" + config["id"] + ":activate",
        {
            "expected_revision": config["revision"],
            "rebuild_ack": config["activation_plan"]["rebuild_plan_hash"],
        },
    )
    assert result.status_code == 409
    validate_response("ErrorEnvelope", result.json())
    assert result.json()["error"]["details"]["kind"] == "provider_secret_changed"
    result = command(
        http, "configs/" + config["id"] + ":discard", {"expected_revision": config["revision"] - 1}
    )
    assert result.status_code == 409
    assert len(http["gateway"]["requests"]) == 1
