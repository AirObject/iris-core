"""Managed Task CAS, lifecycle and evidence use Canonical domain validation."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import world as read_world_fixture

auth = auth_fixture
world = read_world_fixture


def create_body(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": {"agent_id": world["agent"], "space_id": world["spaces"][0]},
        "fields": {"title": "Managed task", "goal": "Evidence before completion"},
        "reason_code": "operator_request",
    }


def test_task_http_cas_replay_and_lifecycle(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        registry = client.get("/v1/memory/resource-types").json()
        validate_response("ResourceTypePage", registry)
        descriptor = next(row for row in registry["data"] if row["collection"] == "tasks")
        Draft202012Validator(descriptor["create_schema"]).validate(create_body(world))
        key = headers(csrf)
        created = client.post("/v1/memory/tasks", headers=key, json=create_body(world))
        assert created.status_code == 201, created.text
        validate_response("ResourceViewEnvelope", created.json())
        assert created.json()["data"]["status"] == "active"
        path = "/v1/memory/tasks/" + created.json()["data"]["id"]
        update = {
            "expected_revision": 1,
            "fields": {"title": "Corrected task", "goal": ""},
            "reason_code": "operator_request",
        }
        Draft202012Validator(descriptor["update_schema"]).validate(update)
        updated = client.patch(path, headers=headers(csrf), json=update)
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["fields"]["goal"] == ""
        stale = client.patch(path, headers=headers(csrf), json=update)
        assert stale.status_code == 409, stale.text
        replay = client.post("/v1/memory/tasks", headers=key, json=create_body(world))
        assert replay.json()["data"] == created.json()["data"]
        for revision, status in enumerate(["waiting", "active", "cancelled", "archived"], 2):
            result = client.post(
                path + ":transition",
                headers=headers(csrf),
                json={
                    "expected_revision": revision,
                    "target_status": status,
                    "reason_code": "operator_request",
                },
            )
            assert result.status_code == 200, result.text
            assert result.json()["data"]["revision"] == revision + 1
        assert client.get(path).json()["data"]["available_actions"] == ["forget"]


@pytest.mark.parametrize("effect", ["committed", "partial"])
def test_task_completion_requires_committed_evidence(
    world: dict[str, Any], observations: Any, effect: str
) -> None:
    now = world["store"].clock.now_us()
    item = {
        "agent_id": world["agent"],
        "space_id": world["spaces"][0],
        "role": "assistant",
        "kind": "message.sent",
        "idempotency_key": "task-evidence-" + effect,
        "occurred_us": now,
        "committed_us": now + 1,
        "content": "Task result",
    }
    if effect == "partial":
        item.update(effect_state="partial", effect_proof={"confirmed_range": [0, 4]})
    evidence_id = observations.observe_batch(world["access"], [item]).accepted_observation_ids[0]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        created = client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world))
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
        path = "/v1/memory/tasks/" + identifier
        payload = {
            "expected_revision": 1,
            "target_status": "completed",
            "completion_evidence_refs": [
                {"resource_type": "observation", "resource_id": evidence_id}
            ],
            "reason_code": "operator_request",
        }
        key = headers(csrf)
        result = client.post(path + ":transition", headers=key, json=payload)
        if effect == "partial":
            assert result.status_code == 400, result.text
            assert client.get(path).json()["data"]["revision"] == 1
            return
        assert result.status_code == 200, result.text
        assert result.json()["data"]["status"] == "completed"
        assert result.json()["data"]["source_refs"][0]["resource_id"] == evidence_id
        replay = client.post(path + ":transition", headers=key, json=payload)
        assert replay.json()["data"] == result.json()["data"]
        refs = client.get(path + "/references").json()["data"]
        assert any(row["resource"]["resource_id"] == evidence_id for row in refs)


@pytest.mark.parametrize(
    "invalid", ["origin", "priority", "owner", "missing-evidence", "wrong-evidence"]
)
def test_task_rejects_forged_origin_and_invalid_inputs(world: dict[str, Any], invalid: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        payload = create_body(world)
        if invalid in {"origin", "priority", "owner"}:
            payload["fields"].update(
                {"origin": "policy"}
                if invalid == "origin"
                else {"priority": True}
                if invalid == "priority"
                else {"owner_kind": "entity"}
            )
            result = client.post("/v1/memory/tasks", headers=headers(csrf), json=payload)
        else:
            created = client.post("/v1/memory/tasks", headers=headers(csrf), json=payload)
            path = "/v1/memory/tasks/" + created.json()["data"]["id"] + ":transition"
            transition = {
                "expected_revision": 1,
                "target_status": "completed",
                "reason_code": "operator_request",
            }
            if invalid == "wrong-evidence":
                transition["completion_evidence_refs"] = [
                    {"resource_type": "note", "resource_id": world["ids"]["a"]}
                ]
            result = client.post(path, headers=headers(csrf), json=transition)
        assert result.status_code == 400, result.text


@pytest.mark.parametrize("operation", ["update", "transition"])
def test_limited_writer_cannot_modify_parent_task(world: dict[str, Any], operation: str) -> None:
    from tests.integration.test_console_reads import grant_for

    owner, owner_csrf = signed_in(world["app"], world["token"])
    payload = create_body(world)
    payload["scope"].pop("space_id")
    with owner:
        created = owner.post("/v1/memory/tasks", headers=headers(owner_csrf), json=payload)
        assert created.status_code == 201, created.text
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="task writer",
        description="limited task",
        template="maintainer",
        grant=grant_for(world, permissions=frozenset({"memory.read", "memory.write"})),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        path = "/v1/memory/tasks/" + created.json()["data"]["id"]
        assert client.get(path).json()["data"]["available_actions"] == []
        value = {"expected_revision": 1, "reason_code": "operator_request"}
        if operation == "update":
            value["fields"] = {"title": "forbidden"}
            result = client.patch(path, headers=headers(csrf), json=value)
        else:
            value["target_status"] = "cancelled"
            result = client.post(path + ":transition", headers=headers(csrf), json=value)
        assert result.status_code == 403, result.text
        assert client.get(path).json()["data"]["revision"] == 1


def test_deleted_evidence_cannot_publish_cached_completion(
    world: dict[str, Any], observations: Any
) -> None:
    now = world["store"].clock.now_us()
    evidence = observations.observe_batch(
        world["access"],
        [
            {
                "agent_id": world["agent"],
                "space_id": world["spaces"][0],
                "role": "assistant",
                "kind": "message.sent",
                "idempotency_key": "cached-task-evidence",
                "occurred_us": now,
                "committed_us": now + 1,
                "content": "Result to forget",
            }
        ],
    ).accepted_observation_ids[0]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        created = client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world))
        path = "/v1/memory/tasks/" + created.json()["data"]["id"] + ":transition"
        payload = {
            "expected_revision": 1,
            "target_status": "completed",
            "reason_code": "operator_request",
            "completion_evidence_refs": [{"resource_type": "observation", "resource_id": evidence}],
        }
        key = headers(csrf)
        result = client.post(path, headers=key, json=payload)
        assert result.status_code == 200, result.text
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type="observation",
                resource_id=evidence,
                reason_code="operator_request",
                deleted_by="test",
            )
        replay = client.post(path, headers=key, json=payload)
        assert replay.status_code == 404, replay.text
        assert evidence not in replay.text


def test_visible_cross_space_evidence_is_not_task_evidence(
    world: dict[str, Any], observations: Any
) -> None:
    now = world["store"].clock.now_us()
    evidence = observations.observe_batch(
        world["access"],
        [
            {
                "agent_id": world["agent"],
                "space_id": world["spaces"][1],
                "role": "assistant",
                "kind": "message.sent",
                "idempotency_key": "cross-space-task-evidence",
                "occurred_us": now,
                "committed_us": now + 1,
                "content": "Unrelated result",
            }
        ],
    ).accepted_observation_ids[0]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        created = client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world))
        path = "/v1/memory/tasks/" + created.json()["data"]["id"]
        payload = {
            "expected_revision": 1,
            "target_status": "completed",
            "reason_code": "operator_request",
            "completion_evidence_refs": [{"resource_type": "observation", "resource_id": evidence}],
        }
        result = client.post(path + ":transition", headers=headers(csrf), json=payload)
        assert result.status_code == 400, result.text
        assert client.get(path).json()["data"]["revision"] == 1


def test_task_artifact_evidence_and_owner_reference(
    world: dict[str, Any], phase5_artifacts: Any
) -> None:
    artifact = phase5_artifacts.ingest_inline(
        world["access"],
        agent_id=world["agent"],
        content=b"Task effect",
        media_type="text/plain",
        idempotency_key="console-task-artifact",
    )
    client, csrf = signed_in(world["app"], world["token"])
    payload = create_body(world)
    payload["fields"].update(owner_kind="entity", owner_entity_id=world["entities"][0])
    with client:
        created = client.post("/v1/memory/tasks", headers=headers(csrf), json=payload)
        assert created.status_code == 201, created.text
        assert created.json()["data"]["fields"]["owner_entity_id"] == world["entities"][0]
        path = "/v1/memory/tasks/" + created.json()["data"]["id"] + ":transition"
        result = client.post(
            path,
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "target_status": "completed",
                "reason_code": "operator_request",
                "completion_evidence_refs": [
                    {
                        "resource_type": "artifact",
                        "resource_id": artifact.artifact_id,
                        "revision": 1,
                    }
                ],
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["data"]["status"] == "completed"
        assert result.json()["data"]["source_refs"][0]["resource_id"] == artifact.artifact_id


def test_task_missing_owner_cannot_create(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    payload = create_body(world)
    payload["fields"].update(owner_kind="entity", owner_entity_id="missing-entity")
    with client:
        created = client.post("/v1/memory/tasks", headers=headers(csrf), json=payload)
        assert created.status_code == 404, created.text
        assert client.get("/v1/memory/tasks").json()["data"] == []
