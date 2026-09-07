"""Managed State writes enforce namespace authority, natural-key CAS and retention."""

from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from iris_memory_core.application.state import StateService
from iris_memory_core.domain.state import DEFAULT_STATE_POLICY
from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import world as read_world_fixture

auth = auth_fixture
world = read_world_fixture


def create_body(world: dict[str, Any], namespace: str = "custom") -> dict[str, Any]:
    return {
        "scope": {"agent_id": world["agent"], "space_id": world["spaces"][0]},
        "fields": {
            "namespace": namespace,
            "key": "current",
            "value": {"text": "private state value"},
        },
        "expected_revision": 0,
        "reason_code": "operator_request",
    }


def test_state_receipt_replay_survives_retention_without_old_value(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        registry = client.get("/v1/memory/resource-types").json()
        validate_response("ResourceTypePage", registry)
        descriptor = next(row for row in registry["data"] if row["collection"] == "states")
        assert descriptor["create"]["initial_revision"] == 0
        Draft202012Validator(descriptor["create_schema"]).validate(create_body(world))
        key = headers(csrf)
        first = client.post("/v1/memory/states", headers=key, json=create_body(world))
        assert first.status_code == 201, first.text
        validate_response("StateCommandReceiptEnvelope", first.json())
        assert "private state value" not in first.text
        identifier = first.json()["data"]["resource_id"]
        path = "/v1/memory/states/" + identifier
        duplicate = client.post("/v1/memory/states", headers=headers(csrf), json=create_body(world))
        assert duplicate.status_code == 409, duplicate.text
        update = {
            "fields": {"value": {"text": "new state"}},
            "expected_revision": 1,
            "reason_code": "operator_request",
        }
        Draft202012Validator(descriptor["update_schema"]).validate(update)
        edited = client.patch(path, headers=headers(csrf), json=update)
        assert edited.status_code == 200, edited.text
        with world["store"].read() as tx:
            assert tx.states.revision_count(identifier) == 1
        replay = client.post("/v1/memory/states", headers=key, json=create_body(world))
        assert replay.json()["data"] == first.json()["data"]
        assert client.get(path).json()["data"]["fields"]["value"] == {"text": "new state"}
        expire_key = headers(csrf)
        expire_payload = {"expected_revision": 2, "reason_code": "operator_request"}
        expired = client.post(path + ":expire", headers=expire_key, json=expire_payload)
        assert expired.status_code == 200, expired.text
        again = client.post(path + ":expire", headers=expire_key, json=expire_payload)
        assert again.json()["data"] == expired.json()["data"]
        detail = client.get(path).json()["data"]
        assert detail["status"] == "expired" and detail["available_actions"] == ["update", "forget"]
        assert detail["fields"]["source_authority"] == "user"
        with world["store"].read() as tx:
            row = tx.states.current_revision(tx.states.get(identifier).current_revision_id)
            assert row.expires_us == row.observed_us
        states = StateService(world["store"], world["store"].clock)
        assert (
            states.get(
                world["access"],
                "custom",
                "current",
                agent_id=world["agent"],
                space_id=world["spaces"][0],
            )
            is None
        )


@pytest.mark.parametrize("namespace", ["runtime", "environment"])
def test_state_cannot_claim_host_authority_even_with_owner_key(
    world: dict[str, Any], namespace: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        response = client.post(
            "/v1/memory/states", headers=headers(csrf), json=create_body(world, namespace)
        )
        assert response.status_code == 403, response.text


def test_changed_namespace_policy_revokes_cached_state_outcome(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        key = headers(csrf)
        result = client.post("/v1/memory/states", headers=key, json=create_body(world))
        assert result.status_code == 201, result.text
        with world["store"].write() as tx:
            tx.states.upsert_policy(
                replace(
                    DEFAULT_STATE_POLICY,
                    namespace="custom",
                    allowed_source_authorities=frozenset({"host"}),
                ),
                tenant_id=world["tenant"],
            )
        replay = client.post("/v1/memory/states", headers=key, json=create_body(world))
        assert replay.status_code == 403, replay.text
        path = "/v1/memory/states/" + result.json()["data"]["resource_id"]
        assert client.get(path).json()["data"]["available_actions"] == []


@pytest.mark.parametrize(
    "invalid",
    ["source_authority", "origin", "coalesce_key", "observed_at", "ttl-and-expiry", "bad-revision"],
)
def test_state_http_rejects_forged_authority_and_internal_fields(
    world: dict[str, Any], invalid: str
) -> None:
    value = create_body(world)
    if invalid == "ttl-and-expiry":
        value["fields"].update(ttl_us="10", expires_at="2030-01-01T00:00:00.000000Z")
    elif invalid == "bad-revision":
        value["expected_revision"] = 1
    else:
        value["fields"][invalid] = "host"
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        response = client.post("/v1/memory/states", headers=headers(csrf), json=value)
        assert response.status_code == 400, response.text


@pytest.mark.parametrize("operation", ["update", "expire"])
def test_limited_operator_cannot_modify_parent_scope_state(
    world: dict[str, Any], operation: str
) -> None:
    from tests.integration.test_console_reads import grant_for

    owner_client, owner_csrf = signed_in(world["app"], world["token"])
    value = create_body(world)
    value["scope"].pop("space_id")
    with owner_client:
        response = owner_client.post("/v1/memory/states", headers=headers(owner_csrf), json=value)
        assert response.status_code == 201, response.text
        identifier = response.json()["data"]["resource_id"]
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="state writer",
        description="limited state",
        template="maintainer",
        grant=grant_for(world, permissions=frozenset({"memory.read", "memory.write"})),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        path = "/v1/memory/states/" + identifier
        assert client.get(path).json()["data"]["available_actions"] == []
        payload = {"expected_revision": 1, "reason_code": "operator_request"}
        if operation == "update":
            payload["fields"] = {"value": {"overwritten": True}}
            denied = client.patch(path, headers=headers(csrf), json=payload)
        else:
            denied = client.post(path + ":expire", headers=headers(csrf), json=payload)
        assert denied.status_code == 403, denied.text
        with world["store"].read() as tx:
            assert tx.states.get(identifier).current_revision == 1


def test_state_ttl_is_capped_by_current_namespace_policy(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    value = create_body(world, "topic")
    value["fields"]["ttl_us"] = "9999999999999999"
    with client:
        result = client.post("/v1/memory/states", headers=headers(csrf), json=value)
        assert result.status_code == 201, result.text
        with world["store"].read() as tx:
            record = tx.states.get(result.json()["data"]["resource_id"])
            revision = tx.states.current_revision(record.current_revision_id)
            assert revision.expires_us - revision.observed_us == 21_600_000_000
