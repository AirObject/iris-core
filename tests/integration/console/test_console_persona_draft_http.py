"""Actual HTTP draft lifecycle, finite reads, and publication authority."""

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


def payload(revision: int = 0, *, base: int = 1, policy: int = 1) -> dict[str, Any]:
    return {
        "expected_revision": revision,
        "base_revision": base,
        "expected_policy_revision": policy,
        "reason_code": "operator_request",
        "fields": {
            "core": {"name": "Human draft"},
            "traits": {"style": "careful"},
            "narrative": {},
        },
        "source_refs": [],
    }


def test_actual_http_draft_save_replace_conflict_publish_and_discard(world: Any) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    path = "/v1/personas/" + world["agent"]
    with client:
        original = client.get(path).json()["data"]
        context = client.get(path + "/drafts/commands")
        assert context.status_code == 200, context.text
        validate_response("PersonaDraftContextEnvelope", context.json())
        assert context.json()["data"]["available_actions"] == ["create"]
        create_headers = headers(csrf)
        first = client.post(path + "/drafts", headers=create_headers, json=payload())
        assert first.status_code == 201, first.text
        validate_response("PersonaDraftCommandReceiptEnvelope", first.json())
        draft = first.json()["data"]
        target = path + "/drafts/" + draft["resource_id"]
        assert (
            client.post(path + "/drafts", headers=create_headers, json=payload()).json()["data"]
            == draft
        )
        assert client.get(path).json()["data"]["id"] == original["id"]
        detail = client.get(target)
        validate_response("PersonaDraftContextEnvelope", detail.json())
        assert detail.json()["data"]["available_actions"] == ["update", "publish", "discard"]
        edited = payload(1)
        edited["fields"]["traits"] = {"style": "edited"}
        result = client.put(target, headers=headers(csrf), json=edited)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["revision"] == 2
        assert client.put(target, headers=headers(csrf), json=edited).status_code == 409
        closed = {
            "expected_revision": 2,
            "base_revision": 1,
            "expected_policy_revision": 1,
            "reason_code": "operator_request",
        }
        publish_headers = headers(csrf)
        assert (
            client.post(target + ":publish", headers=publish_headers, json=closed).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
            ).status_code
            == 200
        )
        published = client.post(target + ":publish", headers=publish_headers, json=closed)
        assert published.status_code == 200, published.text
        validate_response("PersonaDraftCommandReceiptEnvelope", published.json())
        assert published.json()["data"]["status"] == "published"
        current = client.get(path).json()["data"]
        assert current["revision"] == 2
        assert current["id"] == published.json()["data"]["published_revision_id"]
        assert client.get(target).json()["data"]["available_actions"] == []
        assert (
            client.post(
                target + ":discard",
                headers=headers(csrf),
                json={"expected_revision": 3, "reason_code": "operator_request"},
            ).status_code
            == 409
        )
        second = client.post(path + "/drafts", headers=headers(csrf), json=payload(base=2)).json()[
            "data"
        ]
        second_path = path + "/drafts/" + second["resource_id"]
        discarded = client.post(
            second_path + ":discard",
            headers=headers(csrf),
            json={"expected_revision": 1, "reason_code": "operator_request"},
        )
        assert discarded.status_code == 200, discarded.text
        assert discarded.json()["data"]["status"] == "discarded"
        assert client.get(second_path).status_code == 404
        listed = client.get(path + "/drafts?limit=1")
        validate_response("ResourcePage", listed.json())
        assert [row["id"] for row in listed.json()["data"]] == [draft["resource_id"]]
        assert "Human draft" not in listed.text  # List never embeds structured draft body.
        assert client.get(path).json()["data"]["id"] == current["id"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_revision": True},
        {"base_revision": 0},
        {"expected_policy_revision": True},
        {"admin": True},
        {"fields": {"core": {"execute": "bad"}, "traits": {}, "narrative": {}}},
        {"fields": {"core": {}, "traits": [], "narrative": {}}},
        {"source_refs": [{"resource_type": "observation", "resource_id": "unknown"}]},
        {"expected_revision": 9007199254740992},
        {"reason_code": "automatic"},
    ],
)
def test_http_draft_rejects_unknown_authority_invalid_shape_and_revision(
    world: Any, mutation: Any
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        response = client.post(
            "/v1/personas/" + world["agent"] + "/drafts",
            headers=headers(csrf),
            json={**payload(), **mutation},
        )
        assert response.status_code == 400, response.text
    with world["store"].read() as tx:
        assert tx.raw().execute("SELECT COUNT(*) FROM persona_drafts").fetchone()[0] == 0


def test_http_draft_readonly_and_parent_scope_do_not_publish_actions(world: Any) -> None:
    grant = replace(
        grant_for(world, permissions=frozenset({"memory.read"})), space_selector=Selector("all")
    )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="draft-reader",
        description="read-only draft authority",
        template="viewer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    path = "/v1/personas/" + world["agent"] + "/drafts"
    with client:
        data = client.get(path + "/commands").json()["data"]
        assert data["actions"] == [] and data["available_actions"] == []
        assert client.post(path, headers=headers(csrf), json=payload()).status_code == 403
        assert client.get(path + "?limit=201").status_code == 400
        assert client.get(path + "?status=draft").status_code == 400
        assert client.get(path + "/commands?limit=1").status_code == 400


def test_http_draft_pagination_cursor_is_bound_to_parent_and_query(world: Any) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    path = "/v1/personas/" + world["agent"] + "/drafts"
    with client:
        for _ in range(3):
            response = client.post(path, headers=headers(csrf), json=payload())
            assert response.status_code == 201, response.text
        first = client.get(path + "?limit=1").json()
        cursor = first["meta"]["page"]["next_cursor"]
        assert cursor and first["meta"]["page"]["has_more"]
        second = client.get(path, params={"limit": 1, "cursor": cursor})
        assert second.status_code == 200, second.text
        assert second.json()["data"][0]["id"] != first["data"][0]["id"]
        assert (
            client.get(
                "/v1/personas/unknown/drafts", params={"limit": 1, "cursor": cursor}
            ).status_code
            == 400
        )
