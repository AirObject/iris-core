"""Policy HTTP preserves integer precision and requires real sensitive-action authority."""

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.domain.console import Selector
from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_reads import grant_for
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def test_policy_http_reauth_precision_cas_and_immutable_persona(world: Any) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    path = "/v1/personas/" + world["agent"] + "/policy"
    with client:
        initial = client.get(path)
        assert initial.status_code == 200, initial.text
        validate_response("PersonaPolicyViewEnvelope", initial.json())
        value = initial.json()["data"]
        assert value["revision"] == 1 and value["available_actions"] == ["replace"]
        assert value["actions"][0]["high_risk"] is True
        form = {field["key"]: field for field in value["actions"][0]["fields"]}
        assert set(form) == set(value["config"])
        assert form["mode"]["options"] == ["locked", "manual", "bounded_auto"]
        assert form["cumulative_window_us"]["type"] == "duration_us"
        assert form["min_evidence"]["type"] == "string"
        assert client.get(path + "?limit=50").status_code == 400
        config = {
            **value["config"],
            "mode": "manual",
            "allowed_fields": ["traits.style"],
            "max_single_delta": 1,
            "max_cumulative_delta": 1,
            "cumulative_window_us": "9007199254740993",
        }
        payload = {"expected_revision": 1, "reason_code": "operator_request", "config": config}
        key = headers(csrf)
        assert client.put(path, headers=key, json=payload).status_code == 403
        assert (
            client.post(
                "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
            ).status_code
            == 200
        )
        updated = client.put(path, headers=key, json=payload)
        assert updated.status_code == 200, updated.text
        validate_response("PersonaPolicyCommandReceiptEnvelope", updated.json())
        receipt = updated.json()["data"]
        assert receipt["revision"] == 2
        assert client.put(path, headers=key, json=payload).json()["data"] == receipt
        assert client.put(path, headers=headers(csrf), json=payload).status_code == 409
        current = client.get(path)
        validate_response("PersonaPolicyViewEnvelope", current.json())
        assert current.json()["data"]["config"]["cumulative_window_us"] == "9007199254740993"
        assert current.json()["data"]["content_hash"] == receipt["content_hash"]
    with world["store"].read() as tx:
        assert (
            tx.personas.current_policy(world["agent"]).cumulative_window_us == 9_007_199_254_740_993
        )
        assert tx.personas.current(world["agent"]).revision == 1


@pytest.mark.parametrize(
    "value",
    [True, 1, -1, "-1", "01", "1e3", "1.0", " 1", "9223372036854775808", "9999999999999999999"],
)
def test_policy_http_rejects_noncanonical_or_out_of_range_integers(world: Any, value: Any) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    path = "/v1/personas/" + world["agent"] + "/policy"
    with client:
        config = client.get(path).json()["data"]["config"]
        config["cooldown_us"] = value
        result = client.put(
            path,
            headers=headers(csrf),
            json={"expected_revision": 1, "reason_code": "operator_request", "config": config},
        )
        assert result.status_code == 400, result.text
    with world["store"].read() as tx:
        assert tx.personas.current_policy(world["agent"]).revision == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"mode": "locked", "allowed_fields": ["traits.style"]},
        {"mode": "manual", "allowed_fields": ["core.name"]},
        {"max_single_delta": True},
        {"max_cumulative_delta": 1.1},
        {"admin": True},
        {"generator": "trusted"},
    ],
)
def test_policy_http_rejects_invalid_fields_and_authority(world: Any, patch: Any) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    path = "/v1/personas/" + world["agent"] + "/policy"
    with client:
        config = client.get(path).json()["data"]["config"]
        response = client.put(
            path,
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "reason_code": "operator_request",
                "config": {**config, **patch},
            },
        )
        assert response.status_code == 400, response.text


def test_policy_http_readonly_has_no_write_actions(world: Any) -> None:
    grant = replace(
        grant_for(world, permissions=frozenset({"memory.read"})), space_selector=Selector("all")
    )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="policy-reader",
        description="read-only policy test",
        template="viewer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    path = "/v1/personas/" + world["agent"] + "/policy"
    with client:
        current = client.get(path)
        assert current.status_code == 200, current.text
        validate_response("PersonaPolicyViewEnvelope", current.json())
        assert current.json()["data"]["actions"] == []
        assert current.json()["data"]["available_actions"] == []
        response = client.put(
            path,
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "reason_code": "operator_request",
                "config": current.json()["data"]["config"],
            },
        )
        assert response.status_code == 403, response.text
