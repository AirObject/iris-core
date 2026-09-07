"""Real Console note writes, schema rejection and operator authorization."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_reads import grant_for
from tests.integration.console.test_console_reads import world as read_world_fixture

auth = auth_fixture
world = read_world_fixture


def request_body(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": {"agent_id": world["agent"], "space_id": world["spaces"][0]},
        "fields": {"kind": "idea", "title": "Console command", "body": "created by operator"},
        "reason_code": "operator_request",
    }


def test_http_note_create_replay_and_self_contained_descriptor(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        registry = client.get("/v1/memory/resource-types").json()
        validate_response("ResourceTypePage", registry)
        descriptor = next(row for row in registry["data"] if row["collection"] == "notes")
        Draft202012Validator(descriptor["create_schema"]).validate(request_body(world))
        auth_headers = headers(csrf)
        first = client.post("/v1/memory/notes", headers=auth_headers, json=request_body(world))
        assert first.status_code == 201, first.text
        validate_response("ResourceViewEnvelope", first.json())
        replay = client.post("/v1/memory/notes", headers=auth_headers, json=request_body(world))
        assert replay.status_code == 201, replay.text
        assert replay.json()["data"] == first.json()["data"]
        identifier = first.json()["data"]["id"]
        detail = client.get("/v1/memory/notes/" + identifier)
        assert detail.json()["data"]["fields"]["body"] == "created by operator"
        world["store"].clock.advance(1_000_000)
        world["notes"].update(
            world["access"],
            identifier,
            expected_revision=1,
            title="later edit",
            idempotency_key="later-edit",
        )
        after_edit = client.post("/v1/memory/notes", headers=auth_headers, json=request_body(world))
        assert after_edit.status_code == 201, after_edit.text
        assert after_edit.json()["data"] == first.json()["data"]


@pytest.mark.parametrize("invalid", ["origin", "tenant", "sql", "lease", "oversize", "source-kind"])
def test_note_http_rejects_internal_inputs(world: dict[str, Any], invalid: str) -> None:
    value = request_body(world)
    if invalid == "origin":
        value["origin"] = "console"
    elif invalid == "tenant":
        value["scope"]["tenant_id"] = "different-tenant"
    elif invalid == "sql":
        value["fields"]["raw_sql"] = "SELECT 1"
    elif invalid == "lease":
        value["lease_id"] = "pretend-host-proof"
    elif invalid == "oversize":
        value["fields"]["body"] = "x" * 20001
    else:
        value["source_refs"] = [{"resource_type": "service_credential", "resource_id": "secret"}]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        result = client.post("/v1/memory/notes", headers=headers(csrf), json=value)
        assert result.status_code == 400, result.text
        assert "SELECT 1" not in result.text and "pretend-host-proof" not in result.text


def test_read_only_operator_cannot_create_note(world: dict[str, Any]) -> None:
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="reader",
        description="read only",
        template="viewer",
        grant=grant_for(world),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        response = client.post("/v1/memory/notes", headers=headers(csrf), json=request_body(world))
        assert response.status_code == 403


def test_note_edit_cas_replay_and_state_machine(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    identifier = world["ids"]["a"]
    path = "/v1/memory/notes/" + identifier
    with client:
        assert client.get(path).json()["data"]["available_actions"] == [
            "update",
            "transition",
            "forget",
        ]
        payload = {
            "expected_revision": 1,
            "reason_code": "operator_request",
            "fields": {"title": "edited", "body": "new text"},
        }
        key = headers(csrf)
        result = client.patch(path, headers=key, json=payload)
        assert result.status_code == 200, result.text
        validate_response("ResourceViewEnvelope", result.json())
        assert result.json()["data"]["revision"] == 2
        stale = client.patch(path, headers=headers(csrf), json=payload)
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "revision_mismatch"
        pin = client.post(
            path + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": 2,
                "target_status": "pinned",
                "reason_code": "operator_request",
            },
        )
        assert pin.status_code == 200, pin.text
        assert pin.json()["data"]["revision"] == 3
        replay = client.patch(path, headers=key, json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["data"] == result.json()["data"]
        archive = client.post(
            path + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": 3,
                "target_status": "archived",
                "reason_code": "operator_request",
            },
        )
        assert archive.status_code == 200, archive.text
        assert client.get(path).json()["data"]["available_actions"] == ["transition", "forget"]
        blocked = client.patch(
            path, headers=headers(csrf), json={**payload, "expected_revision": 4}
        )
        assert blocked.status_code == 409, blocked.text
        with world["store"].read() as tx:
            assert tx.notes.get(identifier).current_revision == 4
            revision = tx.notes.current_revision_row(identifier)
            assert revision.created_by == "console:" + world["owner"].id


@pytest.mark.parametrize("name", ["global", "b", "restricted", "custom", "private"])
@pytest.mark.parametrize("operation", ["update", "transition"])
def test_limited_writer_cannot_edit_visible_parent_or_hidden_note(
    world: dict[str, Any], name: str, operation: str
) -> None:
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="limited writer",
        description="write test",
        template="maintainer",
        grant=grant_for(world, permissions=frozenset({"memory.read", "memory.write"})),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    path = "/v1/memory/notes/" + world["ids"][name]
    with client:
        if name == "global":
            assert client.get(path).json()["data"]["available_actions"] == []
        value = {"expected_revision": 1, "reason_code": "operator_request"}
        if operation == "update":
            value["fields"] = {"title": "forbidden"}
            response = client.patch(path, headers=headers(csrf), json=value)
        else:
            value["target_status"] = "pinned"
            response = client.post(path + ":transition", headers=headers(csrf), json=value)
        assert response.status_code in {403, 404}, response.text
        with world["store"].read() as tx:
            assert tx.notes.get(world["ids"][name]).current_revision == 1


@pytest.mark.parametrize("target", ["task", "claim", "episode"])
def test_note_promotion_materializes_once_with_operator_provenance(
    world: dict[str, Any], target: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    path = "/v1/memory/notes/" + world["ids"]["a"]
    value = {
        "expected_revision": 1,
        "reason_code": "operator_request",
        "target_status": "promoted",
        "promotion_target_type": target,
    }
    with client:
        key = headers(csrf)
        result = client.post(path + ":transition", headers=key, json=value)
        assert result.status_code == 200, result.text
        with world["store"].read() as tx:
            revision = tx.notes.current_revision_row(world["ids"]["a"])
            target_id = revision.promotion_target_id
            assert target_id and revision.promotion_target_type == target
            assert revision.created_by == "console:" + world["owner"].id
        # The new domain target is exposed through authorized references, not raw fields.
        references = client.get(path + "/references").json()["data"]
        assert any(ref["resource"]["resource_id"] == target_id for ref in references)
        replay = client.post(path + ":transition", headers=key, json=value)
        assert replay.json()["data"] == result.json()["data"]
        assert client.get(path).json()["data"]["available_actions"] == ["forget"]


def test_restricted_note_edit_requires_explicit_grant_and_accepts_it(world: dict[str, Any]) -> None:
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="restricted writer",
        description="explicit grant",
        template="maintainer",
        grant=grant_for(
            world, permissions=frozenset({"memory.read", "memory.write"}), allow_restricted=True
        ),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        response = client.patch(
            "/v1/memory/notes/" + world["ids"]["restricted"],
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"title": "authorized restricted edit"},
                "reason_code": "operator_request",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["privacy_labels"] == ["restricted"]
