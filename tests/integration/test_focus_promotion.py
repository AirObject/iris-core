"""Focus promotion creates canonical targets atomically with original evidence."""

from typing import Any

import pytest

from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.domain.errors import NotFoundError, RevisionMismatchError
from iris_memory_core.domain.memory import EvidenceRequiredError
from iris_memory_core.storage.idempotency import IdempotencyManager
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def source_focus(
    world: dict[str, Any], *, with_evidence: bool = True
) -> tuple[Any, str, str | None]:
    store = world["store"]
    focus = FocusService(store, store.clock, idempotency=IdempotencyManager(store))
    observation = None
    if with_evidence:
        observation = (
            ObservationService(store)
            .observe_batch(
                world["access"],
                [
                    {
                        "agent_id": world["agent"],
                        "kind": "message.text",
                        "role": "user",
                        "content": "Original promotion evidence",
                        "occurred_us": store.clock.now_us(),
                        "committed_us": store.clock.now_us(),
                        "idempotency_key": "focus-proof",
                    }
                ],
            )
            .accepted_observation_ids[0]
        )
    item = focus.create(
        world["access"],
        agent_id=world["agent"],
        kind="goal",
        summary="Follow this issue",
        source_refs=[{"resource_type": "observation", "resource_id": observation, "revision": 1}]
        if observation
        else [],
        idempotency_key="focus-for-promotion",
    )
    return focus, item.item_id, observation


@pytest.mark.parametrize("target", ["note", "task", "episode", "claim"])
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("deleted", ["target", "source"])
def test_promotion_materializes_target_and_replays_once(
    world: dict[str, Any], target: str, managed: bool, deleted: str
) -> None:
    focus, identifier, observation = source_focus(world)
    assert observation is not None
    store = world["store"]
    table = {"note": "notes", "task": "tasks", "episode": "episodes", "claim": "claims"}[target]
    with store.read() as tx:
        before = tx.raw().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        if managed:
            path = "/v1/memory/focus-items/" + identifier + ":transition"
            key = headers(csrf)
            payload = {
                "expected_revision": 1,
                "target_status": "promoted",
                "promotion_target_type": target,
                "reason_code": "operator_request",
            }
            response = client.post(path, headers=key, json=payload)
            assert response.status_code == 200, response.text
            target_id = response.json()["data"]["fields"]["promotion_target_id"]
            assert (
                client.post(path, headers=key, json=payload).json()["data"]
                == response.json()["data"]
            )
        else:
            result = focus.transition(
                world["access"],
                identifier,
                "promoted",
                expected_revision=1,
                promotion_target_type=target,
                reason="explicit promotion",
                idempotency_key="promote-once",
            )
            target_id = result.promotion_target_id
            assert (
                focus.transition(
                    world["access"],
                    identifier,
                    "promoted",
                    expected_revision=1,
                    promotion_target_type=target,
                    reason="explicit promotion",
                    idempotency_key="promote-once",
                )
                == result
            )
        assert target_id
        with store.read() as tx:
            assert tx.focus.get(identifier).current_revision == 2
            assert tx.raw().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == before + 1
            assert (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM resource_links WHERE source_type='focus_item' "
                    "AND source_id=? AND target_id=? AND relation='promoted_to'",
                    (identifier, target_id),
                )
                .fetchone()[0]
                == 1
            )
            row = tx.console_reads.get(table, world["tenant"], target_id)
            assert row is not None and row.revision == 1
            assert row.scope.agent_id == world["agent"]
            assert any(
                ref.resource_type == "focus_item" and ref.resource_id == identifier
                for ref in row.source_refs
            )
            if target == "task":
                assert row.status == "proposed"
            if target == "claim":
                assert row.fields["source_authority"] == "agent_inference"
                evidence = (
                    tx.raw()
                    .execute(
                        "SELECT source_type,source_id FROM claim_evidence WHERE claim_id=?",
                        (target_id,),
                    )
                    .fetchall()
                )
                assert [(entry[0], entry[1]) for entry in evidence] == [
                    ("observation", observation)
                ]
        with store.write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type=target if deleted == "target" else "observation",
                resource_id=target_id if deleted == "target" else observation,
                reason_code="operator_request",
                deleted_by="test",
            )
        if managed:
            assert client.post(path, headers=key, json=payload).status_code == 404
            detail = client.get("/v1/memory/focus-items/" + identifier)
            assert detail.status_code == 200, detail.text
            assert detail.json()["data"]["fields"]["promotion_target_id"] is None
        else:
            visible = focus.get(world["access"], identifier)
            assert visible is not None and visible[1].promotion_target_id is None
            listed = focus.list_items(
                world["access"], agent_id=world["agent"], statuses=("promoted",)
            )
            assert len(listed) == 1 and listed[0][1].promotion_target_id is None
            assert all(
                revision.promotion_target_id is None
                for revision in focus.history(world["access"], identifier)
            )
            with pytest.raises(NotFoundError):
                focus.transition(
                    world["access"],
                    identifier,
                    "promoted",
                    expected_revision=1,
                    promotion_target_type=target,
                    reason="explicit promotion",
                    idempotency_key="promote-once",
                )


@pytest.mark.parametrize("target", ["claim", "episode"])
def test_focus_summary_without_original_evidence_cannot_become_evidence(
    world: dict[str, Any], target: str
) -> None:
    focus, identifier, _ = source_focus(world, with_evidence=False)
    with pytest.raises(EvidenceRequiredError):
        focus.transition(
            world["access"],
            identifier,
            "promote",
            expected_revision=1,
            promotion_target_type=target,
            reason="promotion",
            idempotency_key="no-evidence",
        )
    with world["store"].read() as tx:
        assert tx.focus.get(identifier).status == "active"
        assert (
            tx.raw()
            .execute(
                "SELECT COUNT(*) FROM resource_links WHERE source_type='focus_item' "
                "AND source_id=?",
                (identifier,),
            )
            .fetchone()[0]
            == 0
        )


def test_promotion_cas_and_deleted_source_leave_no_target(world: dict[str, Any]) -> None:
    focus, identifier, observation = source_focus(world)
    assert observation is not None
    with pytest.raises(RevisionMismatchError):
        focus.transition(
            world["access"],
            identifier,
            "promote",
            expected_revision=2,
            promotion_target_type="task",
            reason="promotion",
            idempotency_key="stale",
        )
    with world["store"].write() as tx:
        tx.record_tombstone(
            tenant_id=world["tenant"],
            resource_type="observation",
            resource_id=observation,
            reason_code="operator_request",
            deleted_by="test",
        )
    with pytest.raises(NotFoundError):
        focus.transition(
            world["access"],
            identifier,
            "promote",
            expected_revision=1,
            promotion_target_type="task",
            reason="promotion",
            idempotency_key="deleted-source",
        )
    with world["store"].read() as tx:
        assert tx.focus.get(identifier).status == "active"
        assert tx.raw().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("target", ["note", "task", "claim", "episode"])
def test_managed_promotion_needs_restricted_grant_and_inherits_source_labels(
    world: dict[str, Any], target: str, allowed: bool
) -> None:
    from iris_memory_core.domain.console import Selector
    from tests.integration.test_console_reads import grant_for

    _, identifier, observation = source_focus(world)
    with world["store"].write() as tx:
        tx.raw().execute(
            "UPDATE observations SET privacy_labels='[\"restricted\"]' WHERE id=?", (observation,)
        )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="Focus promotion",
        description="Explicit source grant",
        template="maintainer",
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
        grant=grant_for(
            world,
            space_selector=Selector("all"),
            permissions=frozenset({"memory.read", "memory.write"}),
            allow_restricted=allowed,
        ),
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        path = "/v1/memory/focus-items/" + identifier + ":transition"
        key = headers(csrf)
        payload = {
            "expected_revision": 1,
            "target_status": "promoted",
            "promotion_target_type": target,
            "reason_code": "operator_request",
        }
        response = client.post(path, headers=key, json=payload)
        assert response.status_code == (200 if allowed else 404), response.text
        with world["store"].read() as tx:
            assert tx.focus.get(identifier).current_revision == 1 + int(allowed)
            if allowed:
                target_id = response.json()["data"]["fields"]["promotion_target_id"]
                collection = {
                    "note": "notes",
                    "task": "tasks",
                    "claim": "claims",
                    "episode": "episodes",
                }[target]
                row = tx.console_reads.get(collection, world["tenant"], target_id)
                assert row is not None and "restricted" in row.privacy_labels
