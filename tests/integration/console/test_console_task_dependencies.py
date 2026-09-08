"""Dependency lifecycle preserves history, graph correctness and parent/child CAS."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_reads import world as read_world_fixture
from tests.integration.console.test_console_task_http import create_body

auth = auth_fixture
world = read_world_fixture


def test_dependency_create_remove_restore_and_replay(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        task = client.post(
            "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
        ).json()["data"]
        path = "/v1/memory/tasks/" + task["id"]
        steps = []
        for revision, name in enumerate(["a", "b"], 1):
            created = client.post(
                path + "/steps",
                headers=headers(csrf),
                json={
                    "expected_revision": revision,
                    "fields": {"stable_key": name, "title": name},
                    "reason_code": "operator_request",
                },
            )
            assert created.status_code == 201, created.text
            steps.append(created.json()["data"]["resource_id"])
        payload = {
            "expected_revision": 3,
            "fields": {"predecessor_step_id": steps[0], "successor_step_id": steps[1]},
            "reason_code": "operator_request",
        }
        listing = client.get(path + "/dependencies").json()
        validate_response("ResourcePage", listing)
        descriptor = listing["meta"]["descriptor"]
        Draft202012Validator(descriptor["create_schema"]).validate(payload)
        assert descriptor["create"]["fields"][0]["lookup_parent_id"] == task["id"]
        created = client.post(path + "/dependencies", headers=headers(csrf), json=payload)
        assert created.status_code == 201, created.text
        validate_response("TaskChildCommandReceiptEnvelope", created.json())
        identifier = created.json()["data"]["resource_id"]
        assert created.json()["data"]["task_revision"] == 4
        with world["store"].read() as tx:
            assert tx.tasks.get_step(steps[1]).status == "pending"
        reverse = {
            **payload,
            "expected_revision": 4,
            "fields": {"predecessor_step_id": steps[1], "successor_step_id": steps[0]},
        }
        cycle = client.post(path + "/dependencies", headers=headers(csrf), json=reverse)
        assert cycle.status_code == 409, cycle.text
        assert client.get(path).json()["data"]["revision"] == 4
        key = headers(csrf)
        removal = {
            "expected_revision": 4,
            "child_expected_revision": 1,
            "reason_code": "operator_request",
        }
        removed = client.post(
            path + "/dependencies/" + identifier + ":remove", headers=key, json=removal
        )
        assert removed.status_code == 200, removed.text
        assert (
            removed.json()["data"]["revision"] == 2 and removed.json()["data"]["task_revision"] == 5
        )
        rows = client.get(path + "/dependencies").json()["data"]
        assert rows[0]["status"] == "removed" and rows[0]["available_actions"] == []
        with world["store"].read() as tx:
            assert tx.tasks.dependencies_for_task(task["id"]) == []
            assert tx.tasks.get_step(steps[1]).status == "ready"
        restored = client.post(
            path + "/dependencies", headers=headers(csrf), json={**payload, "expected_revision": 5}
        )
        assert restored.status_code == 201, restored.text
        assert (
            restored.json()["data"]["resource_id"] == identifier
            and restored.json()["data"]["revision"] == 3
        )
        replay = client.post(
            path + "/dependencies/" + identifier + ":remove", headers=key, json=removal
        )
        assert replay.json()["data"] == removed.json()["data"]
        with world["store"].read() as tx:
            edge = tx.tasks.get_dependency(identifier)
            assert edge.status == "active" and edge.current_revision == 3
            assert tx.tasks.get_task(task["id"]).current_revision == 6
            assert tx.tasks.dependency_at_revision(identifier, 2).status == "removed"
            assert tx.tasks.get_step(steps[1]).status == "pending"


def plan(world: dict[str, Any], tasks: Any, *, private: str = "") -> tuple[Any, list[str]]:
    task = tasks.create(
        world["access"],
        agent_id=world["agent"],
        space_id=world["spaces"][0],
        title="Dependency plan",
        origin="explicit_tool",
        idempotency_key="dependency-plan",
    )
    steps = [
        tasks.create_step(
            world["access"],
            task.task_id,
            stable_key=name,
            title=name,
            privacy_labels=["restricted"] if name == private else [],
            idempotency_key="dependency-step-" + name,
        ).step_id
        for name in ("a", "b")
    ]
    return task, steps


def current_revision(world: dict[str, Any], task_id: str) -> int:
    with world["store"].read() as tx:
        return int(tx.tasks.get_task(task_id).current_revision)


def removal(parent_revision: int, child_revision: int = 1) -> dict[str, Any]:
    return {
        "expected_revision": parent_revision,
        "child_expected_revision": child_revision,
        "reason_code": "operator_request",
    }


@pytest.fixture
def phase4_tasks(world: dict[str, Any]) -> Any:
    from iris_memory_core.application.tasks import TaskService
    from iris_memory_core.storage.idempotency import IdempotencyManager

    store = world["store"]
    return TaskService(store, store.clock, idempotency=IdempotencyManager(store))


@pytest.mark.parametrize("conflict", ["parent", "child", "foreign-child", "terminal-parent"])
def test_dependency_removal_fences_parent_and_child(
    world: dict[str, Any],
    phase4_tasks: Any,
    conflict: str,
) -> None:
    task, steps = plan(world, phase4_tasks)
    edge = phase4_tasks.add_dependency(
        world["access"],
        task.task_id,
        predecessor_step_id=steps[0],
        successor_step_id=steps[1],
        idempotency_key="cas-edge",
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        path = "/v1/memory/tasks/" + task.task_id
        if conflict == "foreign-child":
            other = client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world))
            path = "/v1/memory/tasks/" + other.json()["data"]["id"]
            revision = 1
        else:
            revision = 4
        if conflict == "terminal-parent":
            cancelled = client.post(
                path + ":transition",
                headers=headers(csrf),
                json={
                    "expected_revision": 4,
                    "target_status": "cancelled",
                    "reason_code": "operator_request",
                },
            )
            assert cancelled.status_code == 200, cancelled.text
            revision = 5
        before = client.get(path).json()["data"]["revision"]
        result = client.post(
            path + "/dependencies/" + edge.dependency_id + ":remove",
            headers=headers(csrf),
            json=removal(1 if conflict == "parent" else revision, 2 if conflict == "child" else 1),
        )
        assert result.status_code == (404 if conflict == "foreign-child" else 409), result.text
        assert client.get(path).json()["data"]["revision"] == before
    with world["store"].read() as tx:
        assert tx.tasks.get_dependency(edge.dependency_id).current_revision == 1
        assert tx.tasks.get_dependency(edge.dependency_id).status == "active"
        assert tx.tasks.get_step(steps[1]).status == "pending"


@pytest.mark.parametrize("private", ["a", "b"])
@pytest.mark.parametrize("operation", ["create", "remove"])
@pytest.mark.parametrize("allow_restricted", [False, True])
def test_both_dependency_endpoints_require_step_privacy_grants(
    world: dict[str, Any],
    phase4_tasks: Any,
    private: str,
    operation: str,
    allow_restricted: bool,
) -> None:
    from tests.integration.console.test_console_reads import grant_for

    task, steps = plan(world, phase4_tasks, private=private)
    edge = None
    if operation == "remove":
        edge = phase4_tasks.add_dependency(
            world["access"],
            task.task_id,
            predecessor_step_id=steps[0],
            successor_step_id=steps[1],
            idempotency_key="private-edge",
        )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="dependency writer",
        description="Scoped dependency writer",
        template="maintainer",
        grant=grant_for(
            world,
            permissions=frozenset({"memory.read", "memory.write"}),
            allow_restricted=allow_restricted,
        ),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    before = current_revision(world, task.task_id)
    client, csrf = signed_in(world["app"], token)
    with client:
        path = "/v1/memory/tasks/" + task.task_id + "/dependencies"
        if edge:
            rows = client.get(path).json()["data"]
            assert bool(rows) == allow_restricted
            path += "/" + edge.dependency_id + ":remove"
            payload = removal(before)
        else:
            payload = {
                "expected_revision": before,
                "fields": {
                    "predecessor_step_id": steps[0],
                    "successor_step_id": steps[1],
                },
                "reason_code": "operator_request",
            }
        result = client.post(path, headers=headers(csrf), json=payload)
        assert result.status_code == ((200 if edge else 201) if allow_restricted else 404), (
            result.text
        )
    assert current_revision(world, task.task_id) == before + int(allow_restricted)
    with world["store"].read() as tx:
        active = bool(tx.tasks.dependencies_for_task(task.task_id))
        assert active == (allow_restricted if edge is None else not allow_restricted)
        assert tx.tasks.get_step(steps[1]).status == ("pending" if active else "ready")


def test_online_replay_keeps_original_revision_and_restoration_rechecks_cycle(
    world: dict[str, Any],
    phase4_tasks: Any,
) -> None:
    task, steps = plan(world, phase4_tasks)
    fields = {"predecessor_step_id": steps[0], "successor_step_id": steps[1]}
    original = phase4_tasks.add_dependency(
        world["access"],
        task.task_id,
        **fields,
        idempotency_key="original-edge",
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        path = "/v1/memory/tasks/" + task.task_id + "/dependencies"
        target = path + "/" + original.dependency_id + ":remove"
        result = client.post(target, headers=headers(csrf), json=removal(4))
        assert result.status_code == 200, result.text
        restored = phase4_tasks.add_dependency(
            world["access"],
            task.task_id,
            **fields,
            condition="completed_or_skipped",
            idempotency_key="restored-edge",
        )
        assert restored.dependency_id == original.dependency_id and restored.current_revision == 3
        replay = phase4_tasks.add_dependency(
            world["access"],
            task.task_id,
            **fields,
            idempotency_key="original-edge",
        )
        assert replay == original
        assert current_revision(world, task.task_id) == 6
        result = client.post(target, headers=headers(csrf), json=removal(6, 3))
        assert result.status_code == 200, result.text
        # Removed edges no longer participate in cycles; the reverse edge is valid.
        reverse = phase4_tasks.add_dependency(
            world["access"],
            task.task_id,
            predecessor_step_id=steps[1],
            successor_step_id=steps[0],
            idempotency_key="reverse-edge",
        )
        assert reverse.current_revision == 1
        failed = client.post(
            path,
            headers=headers(csrf),
            json={
                "expected_revision": 8,
                "fields": fields,
                "reason_code": "operator_request",
            },
        )
        assert failed.status_code == 409, failed.text
        assert current_revision(world, task.task_id) == 8
        with world["store"].read() as tx:
            assert tx.tasks.get_dependency(original.dependency_id).status == "removed"
            assert tx.tasks.get_dependency(original.dependency_id).current_revision == 4


def test_removed_dependency_survives_backup_restore_and_mismatch_is_rejected(
    world: dict[str, Any],
    phase4_tasks: Any,
    tmp_path: Any,
) -> None:
    import sqlite3

    from iris_memory_core.storage.backup import BackupService, verify_database_invariants
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from iris_memory_core.storage.uow import Store
    from tests.conftest import local_allowed_versions

    task, steps = plan(world, phase4_tasks)
    edge = phase4_tasks.add_dependency(
        world["access"],
        task.task_id,
        predecessor_step_id=steps[0],
        successor_step_id=steps[1],
        idempotency_key="backup-edge",
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        target = (
            "/v1/memory/tasks/" + task.task_id + "/dependencies/" + edge.dependency_id + ":remove"
        )
        result = client.post(target, headers=headers(csrf), json=removal(4))
        assert result.status_code == 200, result.text
    service = BackupService(world["store"])
    source, destination = tmp_path / "backup", tmp_path / "restored"
    report = service.create_backup(source)
    assert report.schema_version == 25
    restored = service.restore_backup(source, destination)
    assert restored.check.ok, restored.check.problems
    database = destination / "canonical.sqlite3"
    store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    with store.read() as tx:
        assert tx.tasks.dependencies_for_task(task.task_id) == []
        current = tx.tasks.get_dependency(edge.dependency_id)
        assert current.status == "removed" and current.current_revision == 2
        assert tx.tasks.dependency_at_revision(edge.dependency_id, 1).status == "active"
        assert tx.tasks.get_step(steps[1]).status == "ready"
        assert tx.tasks.get_task(task.task_id).current_revision == 5
    assert verify_database_invariants(database) == ()
    for statement in (
        "UPDATE task_dependencies SET status='active' WHERE id=?",
        "UPDATE task_dependencies SET condition='completed_or_skipped' WHERE id=?",
    ):
        with sqlite3.connect(database) as connection:
            connection.execute(statement, (edge.dependency_id,))
            connection.commit()
            # Restore verification inspects immutable snapshot bytes, not a live WAL.
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            assert (
                "task dependency current lifecycle differs from its revision"
                in verify_database_invariants(database)
            )
            connection.execute(
                "UPDATE task_dependencies SET status='removed', condition='completed' WHERE id=?",
                (edge.dependency_id,),
            )


@pytest.mark.parametrize("attempt", ["online-replay", "online-restore", "console-restore"])
def test_deleted_dependency_cannot_replay_or_reactivate(
    world: dict[str, Any],
    phase4_tasks: Any,
    attempt: str,
) -> None:
    from iris_memory_core.domain.errors import NotFoundError

    task, steps = plan(world, phase4_tasks)
    fields = {"predecessor_step_id": steps[0], "successor_step_id": steps[1]}
    edge = phase4_tasks.add_dependency(
        world["access"],
        task.task_id,
        **fields,
        idempotency_key="deleted-edge-original",
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        path = "/v1/memory/tasks/" + task.task_id + "/dependencies"
        removed = client.post(
            path + "/" + edge.dependency_id + ":remove", headers=headers(csrf), json=removal(4)
        )
        assert removed.status_code == 200, removed.text
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type="task_dependency",
                resource_id=edge.dependency_id,
                reason_code="operator_request",
                deleted_by="test",
            )
        if attempt == "console-restore":
            result = client.post(
                path,
                headers=headers(csrf),
                json={
                    "expected_revision": 5,
                    "fields": fields,
                    "reason_code": "operator_request",
                },
            )
            assert result.status_code == 404, result.text
        else:
            with pytest.raises(NotFoundError):
                phase4_tasks.add_dependency(
                    world["access"],
                    task.task_id,
                    **fields,
                    idempotency_key="deleted-edge-original"
                    if attempt == "online-replay"
                    else "deleted-edge-restore",
                )
        with world["store"].read() as tx:
            current = tx.tasks.get_dependency(edge.dependency_id)
            assert current.status == "removed" and current.current_revision == 2
            assert tx.tasks.get_task(task.task_id).current_revision == 5
            assert tx.tasks.get_step(steps[1]).status == "ready"
