"""Real Focus HTTP schema, CAS and managed lifecycle."""

from typing import Any

from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import world as read_world_fixture

auth = auth_fixture
world = read_world_fixture


def test_focus_http_descriptor_and_lifecycle(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    payload = {
        "scope": {"agent_id": world["agent"]},
        "fields": {"kind": "goal", "summary": "Focus HTTP"},
        "reason_code": "operator_request",
    }
    with client:
        registry = client.get("/v1/memory/resource-types").json()
        validate_response("ResourceTypePage", registry)
        descriptor = next(row for row in registry["data"] if row["collection"] == "focus-items")
        Draft202012Validator(descriptor["create_schema"]).validate(payload)
        created = client.post("/v1/memory/focus-items", headers=headers(csrf), json=payload)
        assert created.status_code == 201, created.text
        validate_response("ResourceViewEnvelope", created.json())
        path = "/v1/memory/focus-items/" + created.json()["data"]["id"]
        update = {
            "expected_revision": 1,
            "fields": {"summary": "corrected HTTP"},
            "reason_code": "operator_request",
        }
        Draft202012Validator(descriptor["update_schema"]).validate(update)
        response = client.patch(path, headers=headers(csrf), json=update)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["fields"]["summary"] == "corrected HTTP"
        response = client.post(
            path + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": 2,
                "target_status": "dormant",
                "reason_code": "operator_request",
            },
        )
        assert response.status_code == 200, response.text
        response = client.post(
            path + ":activate",
            headers=headers(csrf),
            json={"expected_revision": 3, "reason_code": "operator_request"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["revision"] == 4
        rejected = client.post(
            path + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": 4,
                "target_status": "promoted",
                "reason_code": "operator_request",
            },
        )
        assert rejected.status_code == 400, rejected.text
