"""Persona HTTP commands require reviewed revisions and recent real authentication."""

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
        "expected_revision": 1,
        "expected_policy_revision": 1,
        "reason_code": "operator_request",
        "fields": {"core": {"name": "Iris"}, "traits": {"style": "warm"}, "narrative": {}},
    }


def test_http_publish_rollback_and_form_metadata(world: Any) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    base = "/v1/personas/" + world["agent"]
    with client:
        current = client.get(base)
        assert current.status_code == 200, current.text
        validate_response("ResourceViewEnvelope", current.json())
        assert current.json()["data"]["fields"]["policy_revision"] == 1
        assert current.json()["data"]["available_actions"] == ["publish", "rollback"]
        assert client.get(base + "?limit=50").status_code == 400
        form = client.get(base + "/commands")
        assert form.status_code == 200, form.text
        validate_response("PersonaCommandsViewEnvelope", form.json())
        assert form.json()["data"]["expected_revision"] == 1
        assert all(a["high_risk"] for a in form.json()["data"]["actions"])
        assert (
            client.post(base + "/revisions", headers=headers(csrf), json=payload()).status_code
            == 403
        )
        result = client.post("/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]})
        assert result.status_code == 200, result.text
        key = headers(csrf)
        published = client.post(base + "/revisions", headers=key, json=payload())
        assert published.status_code == 201, published.text
        validate_response("PersonaCommandReceiptEnvelope", published.json())
        receipt = published.json()["data"]
        assert receipt["revision"] == 2
        assert (
            client.post(base + "/revisions", headers=key, json=payload()).json()["data"] == receipt
        )
        rolled = client.post(
            base + ":rollback",
            headers=headers(csrf),
            json={
                "expected_revision": 2,
                "expected_policy_revision": 1,
                "target_revision": 1,
                "reason_code": "operator_request",
            },
        )
        assert rolled.status_code == 201, rolled.text
        validate_response("PersonaCommandReceiptEnvelope", rolled.json())
        assert rolled.json()["data"]["revision"] == 3
        assert (
            client.post(base + "/revisions", headers=key, json=payload()).json()["data"] == receipt
        )
        history = client.get(base + "/history?limit=50")
        assert history.status_code == 200, history.text
        assert [row["revision"] for row in history.json()["data"]] == [3, 2, 1]
        assert [row["status"] for row in history.json()["data"]] == [
            "published",
            "superseded",
            "superseded",
        ]


@pytest.mark.parametrize(
    "patch",
    [
        {"expected_revision": True},
        {"expected_revision": "1"},
        {"expected_policy_revision": 0},
        {"fields": {"core": [], "traits": {}, "narrative": {}}},
        {"fields": {"core": {"system_prompt": "forged"}, "traits": {}, "narrative": {}}},
        {"actor": "admin"},
        {"origin": "core"},
        {"admin": True},
        {"target_revision": 1},
        {"source_refs": [{"resource_type": "note", "resource_id": "anything"}]},
        {"source_refs": [{"resource_type": "task", "resource_id": "anything", "admin": True}]},
    ],
)
def test_http_rejects_malformed_persona_and_forged_authority(world: Any, patch: Any) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        response = client.post(
            "/v1/personas/" + world["agent"] + "/revisions",
            headers=headers(csrf),
            json={**payload(), **patch},
        )
        assert response.status_code == 400, response.text
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1
