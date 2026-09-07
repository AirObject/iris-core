"""Explicit event dismissal contract, current authority and lifecycle metadata."""

from typing import Any

import pytest

from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.domain.errors import InvalidTransitionError
from iris_memory_core.storage.idempotency import IdempotencyManager
from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import grant_for
from tests.integration.test_console_reads import world as world_fixture
from tests.integration.test_task_deletion_storage import seed

auth = auth_fixture
world = world_fixture


def test_dismiss_http_replay_metadata_and_no_later_delivery_or_ack(world: dict[str, Any]) -> None:
    task, step, _, identifier = seed(world)
    store = world["store"]
    with store.read() as tx:
        plan, child = tx.tasks.get_task(task), tx.tasks.get_step(step)
    path = "/v1/memory/cognitive-events/" + identifier
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        registry = client.get("/v1/memory/resource-types").json()
        validate_response("ResourceTypePage", registry)
        descriptor = next(r for r in registry["data"] if r["collection"] == "cognitive-events")
        assert descriptor["create_schema"] is None and descriptor["update_schema"] is None
        assert [a["id"] for a in descriptor["actions"]] == ["dismiss"]
        detail = client.get(path).json()
        validate_response("ResourceViewEnvelope", detail)
        assert detail["data"]["available_actions"] == ["dismiss"]
        key = headers(csrf)
        payload = {"expected_revision": 1, "reason_code": "operator_request"}
        response = client.post(path + ":dismiss", headers=key, json=payload)
        assert response.status_code == 200, response.text
        validate_response("EventCommandReceiptEnvelope", response.json())
        assert response.json()["data"] == {
            "resource_type": "cognitive_event",
            "resource_id": identifier,
            "revision": 2,
            "canonical_status": "committed",
        }
        assert (
            client.post(path + ":dismiss", headers=key, json=payload).json()["data"]
            == response.json()["data"]
        )
        detail = client.get(path).json()["data"]
        assert detail["status"] == "cancelled" and detail["available_actions"] == []
        assert {"action": "dismiss", "reason": "permission_or_state"} in detail["blocked_actions"]
        stale = client.post(path + ":dismiss", headers=headers(csrf), json=payload)
        assert stale.status_code == 409
        assert client.post(path + ":ack", headers=headers(csrf), json={}).status_code == 405
        assert client.patch(path, headers=headers(csrf), json={}).status_code == 405
    events = CognitiveEventService(store, store.clock, idempotency=IdempotencyManager(store))
    assert not events.pull(world["access"], agent_id=world["agent"]).events
    with pytest.raises(InvalidTransitionError):
        events.ack(world["access"], identifier, idempotency_key="late-host-ack")
    with store.read() as tx:
        assert tx.tasks.get_task(task) == plan and tx.tasks.get_step(step) == child
        current = tx.events.get(identifier)
        assert current.ack_id is None and current.acknowledged_us is None


@pytest.mark.parametrize(
    "patch",
    [
        {"expected_revision": 0},
        {"expected_revision": True},
        {"expected_revision": "1"},
        {"reason_code": "admin_override"},
        {"origin": "core"},
        {"actor": "system"},
        {"fields": {"status": "acknowledged"}},
        {"scope": {"agent_id": "other"}},
    ],
)
def test_dismiss_rejects_extra_authority_and_malformed_body(
    world: dict[str, Any], patch: dict[str, Any]
) -> None:
    _, _, _, identifier = seed(world)
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        result = client.post(
            "/v1/memory/cognitive-events/" + identifier + ":dismiss",
            headers=headers(csrf),
            json={"expected_revision": 1, "reason_code": "operator_request", **patch},
        )
        assert result.status_code == 400, result.text
    with world["store"].read() as tx:
        assert tx.events.get(identifier).current_revision == 1


def test_read_only_event_descriptor_and_command_rejection(world: dict[str, Any]) -> None:
    _, _, _, identifier = seed(world)
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="reader",
        description="event reader",
        template="viewer",
        grant=grant_for(world),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        registry = client.get("/v1/memory/resource-types").json()["data"]
        descriptor = next(r for r in registry if r["collection"] == "cognitive-events")
        assert descriptor["actions"] == []
        path = "/v1/memory/cognitive-events/" + identifier
        assert client.get(path).json()["data"]["available_actions"] == []
        result = client.post(
            path + ":dismiss",
            headers=headers(csrf),
            json={"expected_revision": 1, "reason_code": "operator_request"},
        )
        assert result.status_code == 403, result.text
