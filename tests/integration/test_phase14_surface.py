"""Required-mode HTTP proofs, replay and publication fencing for Recall/Focus."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.surface import SurfaceMode
from tests.contract.test_phase10_asgi import asgi_world as asgi_world_fixture

asgi_world = asgi_world_fixture


@pytest.fixture
def online(asgi_world: dict[str, Any]) -> dict[str, Any]:
    world = asgi_world
    store, credentials = world["store"], world["credentials"]
    admin = credentials.authenticate(world["admin_token"])
    access = credentials.authenticate(world["app_token"])
    identities = IdentityService(store)
    identity = identities.register_external_identity(
        admin,
        "test",
        "default",
        "speaker",
        entity_id=world["entity"],
    )
    binding = identities.propose_binding(
        admin,
        identity.id,
        world["entity"],
        proof_digest="trusted-test-bootstrap",
        reason="test",
    )
    identities.confirm_binding(admin, binding.id, expected_revision=1, reason="test")
    headers = {"Authorization": "Bearer " + world["app_token"], "Idempotency-Key": "seed-focus"}
    with TestClient(world["app"]) as client:
        response = client.post(
            "/v1/focus-items",
            headers=headers,
            json={
                "agent_id": world["agent"],
                "space_id": world["space"],
                "kind": "goal",
                "summary": "lease gated goal",
            },
        )
        assert response.status_code == 200, response.text
        item_id = response.json()["focus_item_id"]
    coordinator = SurfaceCoordinatorService(store, store.clock)
    coordinator.set_mode(admin, world["agent"], SurfaceMode.REQUIRED, reason="test")
    lease = coordinator.acquire(access, world["agent"], ttl_us=60_000_000).lease
    return {
        **world,
        "admin": admin,
        "access": access,
        "coordinator": coordinator,
        "lease": lease,
        "item_id": item_id,
    }


def request_for(world: dict[str, Any], operation: str) -> tuple[str, dict[str, Any]]:
    if operation == "recall":
        return "/v1/recall", {
            "schema_version": 1,
            "request_id": "release-recall-proof",
            "scope": {"agent_id": world["agent"], "space_id": world["space"]},
            "actors": [{"provider": "test", "external_id": "speaker"}],
            "topic": "lease gated goal",
            "purpose": "reply",
            "token_budget": 2000,
            "deadline_at": (
                datetime.fromtimestamp(
                    world["store"].clock.now_us() / 1_000_000,
                    UTC,
                )
                + timedelta(seconds=5)
            ).isoformat(),
        }
    if operation == "create":
        return "/v1/focus-items", {
            "agent_id": world["agent"],
            "space_id": world["space"],
            "kind": "goal",
            "summary": "another goal",
        }
    return "/v1/focus-items/" + world["item_id"] + ":" + operation, {
        "expected_revision": 1,
        "reason": "test",
        **({"promotion_target_type": "note"} if operation == "promote" else {}),
    }


@pytest.mark.parametrize(
    "operation", ["recall", "create", "activate", "dormant", "dismiss", "expire", "promote"]
)
@pytest.mark.parametrize("proof", ["missing", "expired", "old_epoch", "non_holder", "valid"])
def test_online_http_lease_matrix(online: dict[str, Any], operation: str, proof: str) -> None:
    lease = online["lease"]
    path, body = request_for(online, operation)
    headers = {"Authorization": "Bearer " + online["app_token"], "Idempotency-Key": "proof-write"}
    if proof != "missing":
        body.update(lease_id=lease.lease_id, lease_epoch=lease.lease_epoch)
    if proof == "old_epoch":
        body["lease_epoch"] = lease.lease_epoch - 1
    if proof == "non_holder":
        headers["Authorization"] = "Bearer " + online["admin_token"]
    if proof == "expired":
        online["store"].clock.advance(61_000_000)
    with TestClient(online["app"]) as client:
        response = client.post(path, json=body, headers=headers)
        if proof == "valid":
            assert response.status_code == 200, response.text
            replay = client.post(path, json=body, headers=headers)
            assert replay.status_code == 200, replay.text
            online["store"].clock.advance(61_000_000)
            expired_replay = client.post(path, json=body, headers=headers)
            assert expired_replay.json()["error"]["code"] == "lease_expired"
            renewed = (
                online["coordinator"]
                .acquire(
                    online["access"],
                    online["agent"],
                    ttl_us=60_000_000,
                )
                .lease
            )
            body.update(lease_id=renewed.lease_id, lease_epoch=renewed.lease_epoch)
            assert client.post(path, json=body, headers=headers).status_code == 200
        else:
            assert response.status_code == 409, response.text
            expected = "lease_expired" if proof in {"missing", "expired"} else "lease_fenced"
            assert response.json()["error"]["code"] == expected


@pytest.mark.parametrize(
    "operation", ["recall", "create", "activate", "dormant", "dismiss", "expire", "promote"]
)
def test_preemption_between_preflight_and_commit_is_fenced(
    online: dict[str, Any],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = online["app"].state.runtime
    target = runtime.recall._orchestrator if operation == "recall" else runtime.idempotency
    method = "recall" if operation == "recall" else "run"
    original = getattr(target, method)

    def preempt() -> None:
        online["coordinator"].acquire(
            online["admin"],
            online["agent"],
            ttl_us=60_000_000,
            priority=1,
            allow_preempt=True,
            reason="race-test",
        )

    def intervening(*args: Any, **kwargs: Any) -> Any:
        if operation == "recall":
            result = original(*args, **kwargs)
            preempt()
            return result
        preempt()
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method, intervening)
    path, body = request_for(online, operation)
    body.update(lease_id=online["lease"].lease_id, lease_epoch=online["lease"].lease_epoch)
    with TestClient(online["app"]) as client:
        response = client.post(
            path,
            json=body,
            headers={
                "Authorization": "Bearer " + online["app_token"],
                "Idempotency-Key": "race-write",
            },
        )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "lease_fenced"
    with online["store"].read() as tx:
        assert tx.focus.get(online["item_id"]).current_revision == 1
        assert tx.usage.get_request(online["tenant"], "release-recall-proof") is None
    assert (
        len(
            runtime.focus.list_items(
                online["access"],
                agent_id=online["agent"],
                space_id=online["space"],
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "operation", ["create", "activate", "dormant", "dismiss", "expire", "promote"]
)
def test_focus_cached_replay_rechecks_a_preempted_proof(
    online: dict[str, Any],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = online["app"].state.runtime
    path, body = request_for(online, operation)
    body.update(lease_id=online["lease"].lease_id, lease_epoch=online["lease"].lease_epoch)
    headers = {"Authorization": "Bearer " + online["app_token"], "Idempotency-Key": "replay-race"}
    with TestClient(online["app"]) as client:
        assert client.post(path, json=body, headers=headers).status_code == 200
        original = runtime.idempotency.run

        def intervene(**kwargs: Any) -> Any:
            online["coordinator"].acquire(
                online["admin"],
                online["agent"],
                ttl_us=60_000_000,
                priority=1,
                allow_preempt=True,
                reason="cached-replay-race",
            )
            return original(**kwargs)

        monkeypatch.setattr(runtime.idempotency, "run", intervene)
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "lease_fenced"
