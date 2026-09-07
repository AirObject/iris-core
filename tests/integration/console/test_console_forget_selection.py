"""Filtering fixes the visible set at preview time and never broadens at commit."""

from typing import Any

import pytest

from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_forget_http import commit
from tests.integration.console.test_console_operation_batches import worker
from tests.integration.console.test_console_reads import grant_for
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def add(world: dict[str, Any], number: int, *, space: int = 0) -> str:
    return str(
        world["notes"]
        .create(
            world["access"],
            agent_id=world["agent"],
            space_id=world["spaces"][space],
            kind="idea",
            title=f"Frozen filter member {number}",
            idempotency_key=f"selector-note-{number}",
        )
        .note_id
    )


def payload(**filters: str) -> dict[str, Any]:
    return {
        "selector": {
            "collection": "notes",
            "filters": {"q": "Frozen filter member", **filters},
            "sort": "created_at_desc",
        },
        "mode": "soft",
        "reason_code": "operator_request",
    }


def test_selector_freezes_revisions_and_does_not_capture_new_matches(world: dict[str, Any]) -> None:
    ids = [add(world, index) for index in range(51)]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        key = headers(csrf)
        result = client.post("/v1/memory:forget-preview", json=payload(), headers=key)
        assert result.status_code == 200, result.text
        saved = result.json()["data"]
        assert saved["impacts"]["target_count"] == 51
        later = add(world, 51)
        repeated = client.post("/v1/memory:forget-preview", json=payload(), headers=key)
        assert repeated.json()["data"] == saved
        result = commit(client, csrf, saved)
        assert result.status_code == 202, result.text
        assert worker(world).run_once()["completed"] == 1
        assert worker(world).run_once()["completed"] == 1
        with world["store"].read() as tx:
            assert all(tx.is_tombstoned(world["tenant"], "note", identifier) for identifier in ids)
            assert not tx.is_tombstoned(world["tenant"], "note", later)
        assert client.get("/v1/memory/notes/" + later).status_code == 200


def test_selector_resolves_only_current_visible_scope(world: dict[str, Any]) -> None:
    visible = add(world, 0)
    hidden = add(world, 1, space=1)
    grant = grant_for(world, permissions=frozenset({"memory.read", "memory.forget"}))
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="Scoped forget",
        description="scope test",
        template="owner",
        grant=grant,
        expires_us=world["owner"].expires_us,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        result = client.post("/v1/memory:forget-preview", json=payload(), headers=headers(csrf))
        assert result.status_code == 200, result.text
        saved = result.json()["data"]
        assert saved["impacts"]["target_count"] == 1
        assert hidden not in result.text
        assert commit(client, csrf, saved).status_code == 200
        with world["store"].read() as tx:
            assert tx.is_tombstoned(world["tenant"], "note", visible)
            assert not tx.is_tombstoned(world["tenant"], "note", hidden)


@pytest.mark.parametrize("count", [0, 501])
def test_selector_rejects_empty_or_oversized_set_without_storing_preview(
    world: dict[str, Any], count: int
) -> None:
    for index in range(count):
        add(world, index)
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        result = client.post("/v1/memory:forget-preview", json=payload(), headers=headers(csrf))
        assert result.status_code == 400, result.text
        with world["store"].read() as tx:
            assert (
                tx.raw().execute("SELECT COUNT(*) FROM console_command_previews").fetchone()[0] == 0
            )
