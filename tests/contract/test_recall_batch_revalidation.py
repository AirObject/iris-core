"""Public batch revalidation checks original records without returning their bodies."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris_memory_core.api.app import create_app
from iris_memory_core.application.security import CredentialService
from tests.integration.test_phase6_recall import World
from tests.integration.test_phase6_review_round2 import _remember


@pytest.fixture
def batch_world(world: World) -> tuple[World, TestClient, dict[str, Any], str]:
    credentials = CredentialService(world.store, world.clock)
    token = "revalidation-application-token-12345678"
    for value, caps in [
        (token, ["recall.v1", "recall.revalidate.v1"]),
        (token + "-old", ["recall.v1"]),
    ]:
        credentials.issue(
            value,
            tenant_id=world.tenant,
            app_instance_id="batch-app",
            plane="application",
            expires_us=world.clock.now_us() + 1_000_000_000,
            agent_ids=[world.agent],
            space_ids=[world.space],
            entity_ids=[world.entity],
            capabilities=caps,
            data_purposes=["reply"],
        )
    client = TestClient(create_app(world.store, credentials=credentials))
    client.headers["Authorization"] = f"Bearer {token}"
    deadline = datetime.fromtimestamp(
        (world.clock.now_us() + 60_000_000) / 1_000_000, UTC
    ).isoformat()
    original = {
        "schema_version": 1,
        "request_id": "batch-original",
        "scope": {"agent_id": world.agent, "space_id": world.space},
        "actors": [{"provider": "qq", "external_id": "bob-1"}],
        "topic": "odyssey",
        "purpose": "reply",
        "token_budget": 16000,
        "deadline_at": deadline,
        "allow_partial": True,
    }
    return world, client, original, token


def batch(client: TestClient, original: dict[str, Any], requests: list[Any]) -> Any:
    return client.post(
        "/v1/recall:revalidate",
        json={
            "schema_version": 1,
            "deadline_at": original["deadline_at"],
            "requests": requests,
        },
    )


def test_original_fingerprint_and_current_resources_are_checked_without_new_recall(
    batch_world: tuple[World, TestClient, dict[str, Any], str],
) -> None:
    world, client, original, _ = batch_world
    claim = _remember(world, "batch-claim", "Private original odyssey text")
    first = client.post("/v1/recall", json=original)
    assert first.status_code == 200, first.text
    assert any(
        c["resource_ref"]["resource_id"] == claim.claim_id for c in first.json()["candidates"]
    )
    old_deadline = {**original, "deadline_at": "2000-01-01T00:00:00Z"}
    assert batch(client, original, [old_deadline]).json()["results"] == [
        {"request_id": original["request_id"], "status": "valid"}
    ]
    assert (
        batch(client, original, [{**original, "topic": "wrong"}]).json()["results"][0]["status"]
        == "unavailable"
    )
    unknown = {**original, "request_id": "never-called"}
    assert batch(client, original, [unknown]).json()["results"][0]["status"] == "unavailable"
    with world.store.read() as tx:
        saved = tx.usage.get_request(world.tenant, original["request_id"])
        assert saved is not None
        before = saved["response_json"]
        assert tx.usage.get_request(world.tenant, "never-called") is None
    world.claims.correct(
        world.access,
        claim.claim_id,
        expected_revision=1,
        mode="retract",
        idempotency_key="batch-retract",
    )
    response = batch(client, original, [original, unknown])
    assert response.status_code == 200
    assert [r["status"] for r in response.json()["results"]] == ["unavailable", "unavailable"]
    assert "Private original" not in response.text and claim.claim_id not in response.text
    with world.store.read() as tx:
        saved = tx.usage.get_request(world.tenant, original["request_id"])
        assert saved is not None
        assert saved["response_json"] == before


@pytest.mark.parametrize("change", ["correction", "expiry"])
def test_batch_uses_one_snapshot_and_evaluation_time(
    batch_world: tuple[World, TestClient, dict[str, Any], str],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    world, client, original, _ = batch_world
    claim = _remember(
        world,
        "snapshot-claim",
        "Snapshot odyssey fact",
        valid_until_us=world.clock.now_us() + 1_000_000 if change == "expiry" else None,
    )
    second = {**original, "request_id": "batch-second"}
    for request in [original, second]:
        assert client.post("/v1/recall", json=request).status_code == 200
    orchestrator = cast(FastAPI, client.app).state.runtime.recall._orchestrator
    original_check = orchestrator.require_result_current_in_tx
    calls = 0

    def check(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        original_check(*args, **kwargs)
        calls += 1
        if calls == 1:
            if change == "expiry":
                world.clock.advance(1_000_001)
                return
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(
                    world.claims.correct,
                    world.access,
                    claim.claim_id,
                    expected_revision=1,
                    mode="retract",
                    idempotency_key="concurrent-batch",
                ).result(timeout=5)

    monkeypatch.setattr(orchestrator, "require_result_current_in_tx", check)
    response = batch(client, original, [original, second])
    assert response.status_code == 200, response.text
    assert [r["status"] for r in response.json()["results"]] == ["valid", "valid"]
    monkeypatch.setattr(orchestrator, "require_result_current_in_tx", original_check)
    assert [r["status"] for r in batch(client, original, [original, second]).json()["results"]] == [
        "unavailable",
        "unavailable",
    ]


def test_capability_scope_deadline_and_batch_bounds_fail_closed(
    batch_world: tuple[World, TestClient, dict[str, Any], str],
) -> None:
    _, client, original, token = batch_world
    client.headers["Authorization"] = f"Bearer {token}-old"
    denied = batch(client, original, [original])
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "access_denied"
    client.headers["Authorization"] = f"Bearer {token}"
    for requests in [[], [original] * 17, [original, original]]:
        assert batch(client, original, requests).status_code == 400
    foreign = {**original, "scope": {**original["scope"], "space_id": "not-authorized"}}
    assert batch(client, original, [foreign]).status_code in {403, 404}
    assert (
        batch(client, {**original, "deadline_at": "2000-01-01T00:00:00Z"}, [original]).status_code
        == 504
    )
    assert client.post("/v1/recall:revalidate", content=b" " * (1024 * 1024 + 1)).status_code == 400
    assert client.post("/v1/recall:revalidate", content=b"\xff").status_code == 400


@pytest.mark.parametrize("oversized", ["body", "candidates"])
def test_saved_response_bounds_reject_without_truncating_a_verdict(
    batch_world: tuple[World, TestClient, dict[str, Any], str], oversized: str
) -> None:
    world, client, original, _ = batch_world
    _remember(world, "bounded", "Bounded odyssey fact")
    assert client.post("/v1/recall", json=original).status_code == 200
    with world.store.write() as tx:
        row = tx.usage.get_request(world.tenant, original["request_id"])
        assert row is not None
        if oversized == "body":
            content = " " * (1024 * 1024 + 1)
        else:
            value = json.loads(row["response_json"])
            value["candidates"] = [value["candidates"][0]] * 513
            content = json.dumps(value)
        tx.raw().execute(
            "UPDATE recall_requests SET response_json=? WHERE id=?",
            (content, original["request_id"]),
        )
    response = batch(client, original, [original])
    assert response.status_code == 400
    assert "results" not in response.json()
