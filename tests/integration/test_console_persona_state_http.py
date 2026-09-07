"""Real Console State requests use independent CAS and strict management DTOs."""

from typing import Any

import pytest

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def payload() -> dict[str, Any]:
    return {
        "expected_revision": 0,
        "reason_code": "operator_request",
        "fields": {"state": {"energy": 0.8}, "baseline": {"energy": 0.2}, "ttl_us": 1000},
    }


def test_state_http_update_expired_clear_and_replay(world: Any) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    base = "/v1/personas/" + world["agent"]
    with client:
        initial = client.get(base + "/state")
        assert initial.status_code == 200, initial.text
        validate_response("PersonaStateViewEnvelope", initial.json())
        assert initial.json()["data"]["current"] is None
        assert initial.json()["data"]["expected_revision"] == 0
        assert initial.json()["data"]["available_actions"] == ["update"]
        assert client.get(base + "/state?limit=50").status_code == 400
        key = headers(csrf)
        updated = client.patch(base + "/state", headers=key, json=payload())
        assert updated.status_code == 200, updated.text
        validate_response("PersonaStateCommandReceiptEnvelope", updated.json())
        first = updated.json()["data"]
        assert first["revision"] == 1
        current = client.get(base + "/state")
        validate_response("PersonaStateViewEnvelope", current.json())
        assert current.json()["data"]["current"]["fields"]["state"] == {"energy": 0.8}
        assert (
            client.patch(base + "/state", headers=headers(csrf), json=payload()).status_code == 409
        )
        clear = {"expected_revision": 1, "reason_code": "operator_request"}
        assert (
            client.post(base + "/state:clear", headers=headers(csrf), json=clear).status_code == 409
        )
        world["store"].clock.advance(1001)
        expired = client.get(base + "/state")
        validate_response("PersonaStateViewEnvelope", expired.json())
        assert expired.json()["data"]["available_actions"] == ["update", "clear"]
        cleared = client.post(base + "/state:clear", headers=headers(csrf), json=clear)
        assert cleared.status_code == 200, cleared.text
        validate_response("PersonaStateCommandReceiptEnvelope", cleared.json())
        assert cleared.json()["data"]["revision"] == 2
        assert client.patch(base + "/state", headers=key, json=payload()).json()["data"] == first
        assert client.get(base).json()["data"]["revision"] == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"expected_revision": True},
        {"expected_revision": -1},
        {"expected_revision": "0"},
        {"fields": {"state": {}, "ttl_us": True}},
        {"fields": {"state": {}, "ttl_us": 604_800_000_001}},
        {"fields": {"state": {"energy": 1.1}, "ttl_us": 1000}},
        {"fields": {"state": {"focus": ["x"] * 9}, "ttl_us": 1000}},
        {"fields": {"state": {"system_prompt": "forged"}, "ttl_us": 1000}},
        {"fields": {"state": {}, "baseline": [], "ttl_us": 1000}},
        {"actor": "admin"},
        {"origin": "core"},
        {"source_refs": [{"resource_type": "task", "resource_id": "x", "admin": True}]},
    ],
)
def test_state_http_rejects_invalid_content_and_authority(world: Any, patch: Any) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        response = client.patch(
            "/v1/personas/" + world["agent"] + "/state",
            headers=headers(csrf),
            json={**payload(), **patch},
        )
        assert response.status_code == 400, response.text
    with world["store"].read() as tx:
        assert tx.personas.current_state(world["agent"]) is None


def test_state_http_readonly_grant_has_no_actions_and_cannot_write(world: Any) -> None:
    from dataclasses import replace

    from iris_memory_core.domain.console import Selector
    from tests.integration.test_console_reads import grant_for

    grant = replace(
        grant_for(world, permissions=frozenset({"memory.read"})), space_selector=Selector("all")
    )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="state-reader",
        description="read-only state test",
        template="maintainer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        base = "/v1/personas/" + world["agent"] + "/state"
        current = client.get(base)
        assert current.status_code == 200, current.text
        validate_response("PersonaStateViewEnvelope", current.json())
        assert current.json()["data"]["actions"] == []
        assert current.json()["data"]["available_actions"] == []
        assert client.patch(base, headers=headers(csrf), json=payload()).status_code == 403
