"""Task child commands atomically fence parent/child revisions and side effects."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import world as read_world_fixture
from tests.integration.test_console_task_http import create_body

auth = auth_fixture
world = read_world_fixture


def test_step_creation_and_transition_advance_parent_once(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        created = client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world))
        task_id = created.json()["data"]["id"]
        path = "/v1/memory/tasks/" + task_id
        listing = client.get(path + "/steps")
        validate_response("ResourcePage", listing.json())
        payload = {
            "expected_revision": 1,
            "fields": {"stable_key": "first", "title": "First step"},
            "reason_code": "operator_request",
        }
        Draft202012Validator(listing.json()["meta"]["descriptor"]["create_schema"]).validate(
            payload
        )
        key = headers(csrf)
        result = client.post(path + "/steps", headers=key, json=payload)
        assert result.status_code == 201, result.text
        validate_response("TaskChildCommandReceiptEnvelope", result.json())
        receipt = result.json()["data"]
        assert receipt["task_revision"] == 2 and receipt["revision"] == 2
        assert client.get(path).json()["data"]["revision"] == 2
        replay = client.post(path + "/steps", headers=key, json=payload)
        assert replay.json()["data"] == receipt
        assert client.get(path).json()["data"]["revision"] == 2
        stale = client.post(
            path + "/steps",
            headers=headers(csrf),
            json={**payload, "fields": {"stable_key": "other", "title": "Other"}},
        )
        assert stale.status_code == 409, stale.text
        assert len(client.get(path + "/steps").json()["data"]) == 1
        step_path = path + "/steps/" + receipt["resource_id"] + ":transition"
        for parent_revision, status in [(2, "in_progress"), (3, "completed")]:
            value = {
                "expected_revision": parent_revision,
                "child_expected_revision": parent_revision,
                "target_status": status,
                "reason_code": "operator_request",
            }
            transition = client.post(step_path, headers=headers(csrf), json=value)
            assert transition.status_code == 200, transition.text
            assert transition.json()["data"]["task_revision"] == parent_revision + 1
            assert client.get(path).json()["data"]["revision"] == parent_revision + 1
        assert client.get(path + "/steps").json()["data"][0]["available_actions"] == []


@pytest.mark.parametrize("conflict", ["parent", "child", "foreign-child", "terminal-parent"])
def test_step_cas_parentage_and_terminal_parent(world: dict[str, Any], conflict: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        task = client.post(
            "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
        ).json()["data"]
        path = "/v1/memory/tasks/" + task["id"]
        step = client.post(
            path + "/steps",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"stable_key": "s", "title": "S"},
                "reason_code": "operator_request",
            },
        ).json()["data"]
        if conflict == "foreign-child":
            other = client.post(
                "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
            ).json()["data"]
            target = "/v1/memory/tasks/" + other["id"]
            parent_revision = 1
        else:
            target, parent_revision = path, 2
        if conflict == "terminal-parent":
            cancelled = client.post(
                path + ":transition",
                headers=headers(csrf),
                json={
                    "expected_revision": 2,
                    "target_status": "cancelled",
                    "reason_code": "operator_request",
                },
            )
            assert cancelled.status_code == 200, cancelled.text
            parent_revision = 3
        result = client.post(
            target + "/steps/" + step["resource_id"] + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": 1 if conflict == "parent" else parent_revision,
                "child_expected_revision": 1 if conflict == "child" else 2,
                "target_status": "in_progress",
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == (404 if conflict == "foreign-child" else 409), result.text
        assert client.get(path + "/steps").json()["data"][0]["revision"] == 2
        assert client.get(target).json()["data"]["revision"] == parent_revision


@pytest.mark.parametrize("allow_restricted", [False, True])
def test_successor_readiness_requires_own_grant(
    world: dict[str, Any], phase4_tasks: Any, allow_restricted: bool
) -> None:
    from tests.integration.test_console_reads import grant_for

    task = phase4_tasks.create(
        world["access"],
        agent_id=world["agent"],
        space_id=world["spaces"][0],
        title="Dependency plan",
        origin="explicit_tool",
        idempotency_key="step-plan",
    )
    first = phase4_tasks.create_step(
        world["access"],
        task.task_id,
        stable_key="first",
        title="First",
        idempotency_key="step-first",
    )
    second = phase4_tasks.create_step(
        world["access"],
        task.task_id,
        stable_key="private",
        title="Private successor",
        privacy_labels=["restricted"],
        idempotency_key="step-second",
    )
    phase4_tasks.add_dependency(
        world["access"],
        task.task_id,
        predecessor_step_id=first.step_id,
        successor_step_id=second.step_id,
        idempotency_key="step-edge",
    )
    phase4_tasks.transition_step(
        world["access"],
        task.task_id,
        first.step_id,
        "start",
        expected_revision=2,
        reason="start",
        idempotency_key="step-start",
    )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="step writer",
        description="limited steps",
        template="maintainer",
        grant=grant_for(
            world,
            permissions=frozenset({"memory.read", "memory.write"}),
            allow_restricted=allow_restricted,
        ),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        path = "/v1/memory/tasks/" + task.task_id + "/steps/" + first.step_id + ":transition"
        result = client.post(
            path,
            headers=headers(csrf),
            json={
                "expected_revision": 5,
                "child_expected_revision": 3,
                "target_status": "completed",
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == (200 if allow_restricted else 404), result.text
    with world["store"].read() as tx:
        assert tx.tasks.get_task(task.task_id).current_revision == (6 if allow_restricted else 5)
        assert tx.tasks.get_step(first.step_id).status == (
            "completed" if allow_restricted else "in_progress"
        )
        assert tx.tasks.get_step(second.step_id).status == (
            "ready" if allow_restricted else "pending"
        )


@pytest.mark.parametrize("effect", ["committed", "partial"])
def test_external_step_requires_valid_evidence(
    world: dict[str, Any], observations: Any, effect: str
) -> None:
    now = world["store"].clock.now_us()
    item = {
        "agent_id": world["agent"],
        "space_id": world["spaces"][0],
        "role": "assistant",
        "kind": "message.sent",
        "idempotency_key": "step-evidence-" + effect,
        "occurred_us": now,
        "committed_us": now + 1,
        "content": "Delivered result",
    }
    if effect == "partial":
        item.update(effect_state="partial", effect_proof={"confirmed_range": [0, 4]})
    evidence = observations.observe_batch(world["access"], [item]).accepted_observation_ids[0]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        task = client.post(
            "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
        ).json()["data"]
        path = "/v1/memory/tasks/" + task["id"]
        step = client.post(
            path + "/steps",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {
                    "stable_key": "send",
                    "title": "Send result",
                    "expected_effect": "Delivered",
                },
                "reason_code": "operator_request",
            },
        ).json()["data"]
        step_path = path + "/steps/" + step["resource_id"] + ":transition"
        started = client.post(
            step_path,
            headers=headers(csrf),
            json={
                "expected_revision": 2,
                "child_expected_revision": 2,
                "target_status": "in_progress",
                "reason_code": "operator_request",
            },
        )
        assert started.status_code == 200, started.text
        value = {
            "expected_revision": 3,
            "child_expected_revision": 3,
            "target_status": "completed",
            "reason_code": "operator_request",
        }
        missing = client.post(step_path, headers=headers(csrf), json=value)
        assert missing.status_code == 400, missing.text
        value["completion_evidence_refs"] = [
            {"resource_type": "observation", "resource_id": evidence}
        ]
        key = headers(csrf)
        result = client.post(step_path, headers=key, json=value)
        assert result.status_code == (200 if effect == "committed" else 400), result.text
        assert client.get(path).json()["data"]["revision"] == (4 if effect == "committed" else 3)
        if effect == "committed":
            replay = client.post(step_path, headers=key, json=value)
            assert replay.json()["data"] == result.json()["data"]
            with world["store"].write() as tx:
                tx.record_tombstone(
                    tenant_id=world["tenant"],
                    resource_type="observation",
                    resource_id=evidence,
                    reason_code="operator_request",
                    deleted_by="test",
                )
            assert client.post(step_path, headers=key, json=value).status_code == 404


def test_concurrent_distinct_steps_share_parent_revision_fence(
    world: dict[str, Any], phase4_tasks: Any
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from iris_memory_core.application.console.tasks import ConsoleTaskCommands
    from iris_memory_core.domain.errors import RevisionMismatchError
    from tests.integration.test_console_commands import principal_for

    task = phase4_tasks.create(
        world["access"],
        agent_id=world["agent"],
        space_id=world["spaces"][0],
        title="Concurrent plan",
        origin="explicit_tool",
        idempotency_key="concurrent-plan",
    )
    principal = principal_for(world)
    service = ConsoleTaskCommands(world["security"])

    def create(index: int) -> bool:
        try:
            service.step(
                principal,
                task.task_id,
                operation="task.step.create",
                expected_revision=1,
                fields={"stable_key": str(index), "title": str(index)},
                reason="operator_request",
                idempotency_key="concurrent-child-" + str(index),
            )
            return True
        except RevisionMismatchError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, range(2))) == [False, True]
    with world["store"].read() as tx:
        assert tx.tasks.get_task(task.task_id).current_revision == 2
        assert len(tx.tasks.steps_for_task(task.task_id)) == 1


def test_step_creation_cannot_introduce_ungranted_privacy(
    world: dict[str, Any], phase4_tasks: Any
) -> None:
    from iris_memory_core.application.console.tasks import ConsoleTaskCommands
    from iris_memory_core.domain.errors import AccessDeniedError
    from tests.integration.test_console_commands import principal_for

    task = phase4_tasks.create(
        world["access"],
        agent_id=world["agent"],
        space_id=world["spaces"][0],
        title="Limited plan",
        origin="explicit_tool",
        idempotency_key="limited-plan",
    )
    principal = principal_for(world, limited=True)
    with pytest.raises(AccessDeniedError):
        ConsoleTaskCommands(world["security"]).step(
            principal,
            task.task_id,
            operation="task.step.create",
            expected_revision=1,
            fields={
                "stable_key": "secret",
                "title": "Hidden step",
                "privacy_labels": ["restricted"],
            },
            reason="operator_request",
            idempotency_key="ungranted-step",
        )
    with world["store"].read() as tx:
        assert tx.tasks.get_task(task.task_id).current_revision == 1
        assert tx.tasks.steps_for_task(task.task_id) == []


def test_online_child_write_invalidates_console_parent_revision(
    world: dict[str, Any], phase4_tasks: Any
) -> None:
    from iris_memory_core.application.console.tasks import ConsoleTaskCommands
    from iris_memory_core.domain.errors import RevisionMismatchError
    from tests.integration.test_console_commands import principal_for

    task = phase4_tasks.create(
        world["access"],
        agent_id=world["agent"],
        space_id=world["spaces"][0],
        title="Mixed plan",
        origin="explicit_tool",
        idempotency_key="mixed-plan",
    )
    principal = principal_for(world)
    service = ConsoleTaskCommands(world["security"])
    online = phase4_tasks.create_step(
        world["access"],
        task.task_id,
        stable_key="online",
        title="Online step",
        idempotency_key="online-child",
    )
    with pytest.raises(RevisionMismatchError):
        service.step(
            principal,
            task.task_id,
            operation="task.step.create",
            expected_revision=1,
            fields={"stable_key": "console", "title": "Console step"},
            reason="operator_request",
            idempotency_key="mixed-stale",
        )
    outcome = service.step(
        principal,
        task.task_id,
        operation="task.step.create",
        expected_revision=2,
        fields={"stable_key": "console", "title": "Console step"},
        reason="operator_request",
        idempotency_key="mixed-fresh",
    )
    assert outcome["task_revision"] == 3
    replay = phase4_tasks.create_step(
        world["access"],
        task.task_id,
        stable_key="online",
        title="Online step",
        idempotency_key="online-child",
    )
    assert replay.step_id == online.step_id and replay.replayed
    with world["store"].read() as tx:
        assert tx.tasks.get_task(task.task_id).current_revision == 3
        assert len(tx.tasks.steps_for_task(task.task_id)) == 2
