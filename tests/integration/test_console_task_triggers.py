"""Managed trigger specs, child CAS, scheduling and source authorization."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import world as world_fixture
from tests.integration.test_console_task_http import create_body

auth = auth_fixture
world = world_fixture


def test_trigger_create_edit_disable_enable_and_scan(world: dict[str, Any]) -> None:
    from iris_memory_core.application.tasks import TaskService

    client, csrf = signed_in(world["app"], world["token"])
    store = world["store"]
    future = store.clock.now_us() + 10000000
    with client:
        task = client.post(
            "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
        ).json()["data"]
        path = "/v1/memory/tasks/" + task["id"] + "/triggers"
        listing = client.get(path).json()
        validate_response("ResourcePage", listing)
        payload = {
            "expected_revision": 1,
            "fields": {"kind": "at_time", "schedule_spec": {"at_us": future}},
            "reason_code": "operator_request",
        }
        Draft202012Validator(listing["meta"]["descriptor"]["create_schema"]).validate(payload)
        key = headers(csrf)
        created = client.post(path, headers=key, json=payload)
        assert created.status_code == 201, created.text
        validate_response("TaskChildCommandReceiptEnvelope", created.json())
        receipt = created.json()["data"]
        assert receipt["task_revision"] == 2 and receipt["revision"] == 1
        assert client.post(path, headers=key, json=payload).json()["data"] == receipt
        target = path + "/" + receipt["resource_id"]
        rows = client.get(path).json()["data"]
        assert rows[0]["available_actions"] == ["update", "enabled"]
        assert rows[0]["fields"]["misfire_grace_us"] == 86400000000
        assert rows[0]["fields"]["schedule_spec"] is None  # Summary omits structured bodies.
        detail = client.get(target)
        assert detail.status_code == 200, detail.text
        validate_response("ResourceViewEnvelope", detail.json())
        assert detail.json()["data"]["fields"]["schedule_spec"] == {"at_us": future}
        updated = client.patch(
            target,
            headers=headers(csrf),
            json={
                "expected_revision": 2,
                "child_expected_revision": 1,
                "fields": {"schedule_spec": {"at_us": future + 1000}, "max_occurrences_per_run": 3},
                "reason_code": "operator_request",
            },
        )
        assert updated.status_code == 200, updated.text
        with store.read() as tx:
            current = tx.tasks.get_trigger(receipt["resource_id"])
            assert current.current_revision == 2 and current.next_fire_at_us == future + 1000
            assert current.max_occurrences_per_run == 3
        disabled = client.post(
            target + ":enabled",
            headers=headers(csrf),
            json={
                "expected_revision": 3,
                "child_expected_revision": 2,
                "enabled": False,
                "reason_code": "operator_request",
            },
        )
        assert disabled.status_code == 200, disabled.text
        store.clock.set(future + 1000)
        tasks = TaskService(store, store.clock)
        with store.write() as tx:
            assert (
                tasks.trigger_scan(
                    tx, tenant_id=world["tenant"], agent_id=world["agent"]
                ).occurrences_created
                == 0
            )
        enabled = client.post(
            target + ":enabled",
            headers=headers(csrf),
            json={
                "expected_revision": 4,
                "child_expected_revision": 3,
                "enabled": True,
                "reason_code": "operator_request",
            },
        )
        assert enabled.status_code == 200, enabled.text
        with store.write() as tx:
            report = tasks.trigger_scan(tx, tenant_id=world["tenant"], agent_id=world["agent"])
            assert report.occurrences_created == 1
            assert tx.tasks.get_trigger(receipt["resource_id"]).current_revision == 4
            assert tx.tasks.get_task(task["id"]).current_revision == 5
        with store.write() as tx:
            assert (
                tasks.trigger_scan(
                    tx, tenant_id=world["tenant"], agent_id=world["agent"]
                ).occurrences_created
                == 0
            )


@pytest.mark.parametrize("fault", ["parent", "child", "foreign", "terminal", "unknown", "progress"])
def test_trigger_rejections_do_not_change_spec(world: dict[str, Any], fault: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        task = client.post(
            "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
        ).json()["data"]
        parent = "/v1/memory/tasks/" + task["id"]
        created = client.post(
            parent + "/triggers",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"kind": "task_transition", "condition_spec": {"to_status": "completed"}},
                "reason_code": "operator_request",
            },
        )
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["resource_id"]
        target = parent
        revision = 2
        if fault == "foreign":
            other = client.post(
                "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
            ).json()["data"]
            target = "/v1/memory/tasks/" + other["id"]
            revision = 1
        if fault == "terminal":
            assert (
                client.post(
                    parent + ":transition",
                    headers=headers(csrf),
                    json={
                        "expected_revision": 2,
                        "target_status": "cancelled",
                        "reason_code": "operator_request",
                    },
                ).status_code
                == 200
            )
            revision = 3
        value: dict[str, Any] = {
            "expected_revision": 1 if fault == "parent" else revision,
            "child_expected_revision": 2 if fault == "child" else 1,
            "fields": {"timezone": "Asia/Shanghai"},
            "reason_code": "operator_request",
        }
        if fault in {"unknown", "progress"}:
            value["fields"] = (
                {"origin": "console"} if fault == "unknown" else {"next_fire_at_us": 1}
            )
        result = client.patch(target + "/triggers/" + identifier, headers=headers(csrf), json=value)
        assert result.status_code == (
            400 if fault in {"unknown", "progress"} else 404 if fault == "foreign" else 409
        ), result.text
        with world["store"].read() as tx:
            assert tx.tasks.get_trigger(identifier).current_revision == 1
            assert tx.tasks.get_task(task["id"]).current_revision == (
                3 if fault == "terminal" else 2
            )


@pytest.mark.parametrize("source", ["attached", "watched"])
@pytest.mark.parametrize("operation", ["create", "update", "enabled"])
@pytest.mark.parametrize("allowed", [False, True])
def test_trigger_grants_cover_attached_and_watched_steps(
    world: dict[str, Any],
    source: str,
    operation: str,
    allowed: bool,
) -> None:
    from iris_memory_core.application.tasks import TaskService
    from iris_memory_core.storage.idempotency import IdempotencyManager
    from tests.integration.test_console_reads import grant_for

    store = world["store"]
    tasks = TaskService(store, store.clock, idempotency=IdempotencyManager(store))
    task = tasks.create(
        world["access"],
        agent_id=world["agent"],
        space_id=world["spaces"][0],
        title="Private trigger source",
        origin="explicit_tool",
        idempotency_key="trigger-plan",
    )
    step = tasks.create_step(
        world["access"],
        task.task_id,
        stable_key="private",
        title="Private",
        privacy_labels=["restricted"],
        idempotency_key="trigger-step",
    )
    fields: dict[str, Any] = (
        {
            "kind": "at_time",
            "task_step_id": step.step_id,
            "schedule_spec": {"at_us": store.clock.now_us() + 1000000},
        }
        if source == "attached"
        else {
            "kind": "task_transition",
            "condition_spec": {"task_step_id": step.step_id, "to_status": "completed"},
        }
    )
    existing = None
    if operation != "create":
        existing = tasks.create_trigger(
            world["access"], task.task_id, **fields, idempotency_key="trigger-original"
        )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="Trigger editor",
        description="Limited trigger sources",
        template="maintainer",
        grant=grant_for(
            world, permissions=frozenset({"memory.read", "memory.write"}), allow_restricted=allowed
        ),
        expires_us=store.clock.now_us() + 3600000000,
    )
    client, csrf = signed_in(world["app"], token)
    before = 3 if existing else 2
    with client:
        path = "/v1/memory/tasks/" + task.task_id + "/triggers"
        if existing:
            listing = client.get(path)
            assert listing.status_code == 200, listing.text
            assert bool(listing.json()["data"]) == allowed
            path += "/" + existing.trigger_id
            detail = client.get(path)
            assert detail.status_code == (200 if allowed else 404), detail.text
        body = {"expected_revision": before, "reason_code": "operator_request"}
        if existing:
            body["child_expected_revision"] = 1
        if operation == "enabled":
            result = client.post(
                path + ":enabled", headers=headers(csrf), json={**body, "enabled": False}
            )
        elif operation == "update":
            result = client.patch(
                path, headers=headers(csrf), json={**body, "fields": {"max_occurrences_per_run": 2}}
            )
        else:
            result = client.post(path, headers=headers(csrf), json={**body, "fields": fields})
        assert result.status_code == ((201 if not existing else 200) if allowed else 404), (
            result.text
        )
        with store.read() as tx:
            assert tx.tasks.get_task(task.task_id).current_revision == before + int(allowed)


@pytest.mark.parametrize("deleted", ["task_trigger", "task_step"])
def test_worker_skips_deleted_trigger_or_attached_step(world: dict[str, Any], deleted: str) -> None:
    from iris_memory_core.application.tasks import TaskService
    from iris_memory_core.storage.idempotency import IdempotencyManager

    store = world["store"]
    tasks = TaskService(store, store.clock, idempotency=IdempotencyManager(store))
    task = tasks.create(
        world["access"],
        agent_id=world["agent"],
        title="Due plan",
        origin="explicit_tool",
        idempotency_key="deleted-plan",
    )
    step = tasks.create_step(
        world["access"],
        task.task_id,
        stable_key="step",
        title="Due step",
        idempotency_key="deleted-step",
    )
    trigger = tasks.create_trigger(
        world["access"],
        task.task_id,
        kind="at_time",
        task_step_id=step.step_id,
        schedule_spec={"at_us": store.clock.now_us()},
        idempotency_key="deleted-trigger",
    )
    with store.write() as tx:
        tx.record_tombstone(
            tenant_id=world["tenant"],
            resource_type=deleted,
            resource_id=trigger.trigger_id if deleted == "task_trigger" else step.step_id,
            reason_code="operator_request",
            deleted_by="test",
        )
        assert (
            tasks.trigger_scan(
                tx, tenant_id=world["tenant"], agent_id=world["agent"]
            ).occurrences_created
            == 0
        )


def test_trigger_edit_rearms_schedule_and_replay_preserves_scan_progress(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.application.tasks import TaskService

    store = world["store"]
    tasks = TaskService(store, store.clock)
    client, csrf = signed_in(world["app"], world["token"])
    start = store.clock.now_us()
    with client:
        task = client.post(
            "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
        ).json()["data"]
        path = "/v1/memory/tasks/" + task["id"] + "/triggers"
        created = client.post(
            path,
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"kind": "recurrence", "schedule_spec": {"every_seconds": 60}},
                "reason_code": "operator_request",
            },
        )
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["resource_id"]
        target = path + "/" + identifier
        store.clock.set(start + 30_000_000)
        key = headers(csrf)
        payload = {
            "expected_revision": 2,
            "child_expected_revision": 1,
            "fields": {"schedule_spec": {"every_seconds": 120}},
            "reason_code": "operator_request",
        }
        updated = client.patch(target, headers=key, json=payload)
        assert updated.status_code == 200, updated.text
        with store.read() as tx:
            current = tx.tasks.get_trigger(identifier)
            assert current.next_fire_at_us == start + 150_000_000
            assert current.current_revision == 2
        for seconds, count in ((60, 0), (150, 1)):
            store.clock.set(start + seconds * 1_000_000)
            with store.write() as tx:
                assert (
                    tasks.trigger_scan(
                        tx, tenant_id=world["tenant"], agent_id=world["agent"]
                    ).occurrences_created
                    == count
                )
        assert (
            client.patch(target, headers=key, json=payload).json()["data"] == updated.json()["data"]
        )
        with store.read() as tx:
            current = tx.tasks.get_trigger(identifier)
            assert current.next_fire_at_us == start + 270_000_000
            assert current.current_revision == 2
        other = client.post(
            "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
        ).json()["data"]
        assert (
            client.get("/v1/memory/tasks/" + other["id"] + "/triggers/" + identifier).status_code
            == 404
        )
        with store.write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type="task_trigger",
                resource_id=identifier,
                reason_code="operator_request",
                deleted_by="test",
            )
        assert client.get(target).status_code == 404
        assert client.patch(target, headers=key, json=payload).status_code == 404


def test_watched_task_is_not_a_trigger_parent(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        parent, watched = [
            client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world)).json()[
                "data"
            ]
            for _ in range(2)
        ]
        path = "/v1/memory/tasks/" + parent["id"] + "/triggers"
        created = client.post(
            path,
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {
                    "kind": "task_transition",
                    "condition_spec": {"task_id": watched["id"], "to_status": "completed"},
                },
                "reason_code": "operator_request",
            },
        )
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["resource_id"]
        assert client.get(path + "/" + identifier).status_code == 200
        wrong_parent = "/v1/memory/tasks/" + watched["id"] + "/triggers/" + identifier
        assert client.get(wrong_parent).status_code == 404
