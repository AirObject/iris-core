"""Managed Task deletion through HTTP, authorization and deletion-ledger recovery."""

from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_forget_http import commit, preview, target
from tests.integration.console.test_console_reads import world as world_fixture
from tests.integration.memory.test_task_deletion_storage import seed

auth = auth_fixture
world = world_fixture


@pytest.mark.parametrize("mode", ["soft", "erase"])
@pytest.mark.parametrize("schema18", [False, True])
def test_terminal_task_cascade_and_restore(
    world: dict[str, Any], tmp_path: Path, mode: str, schema18: bool
) -> None:
    task, step, trigger, event = seed(world)
    store = world["store"]
    backups = BackupService(store)
    backup = tmp_path / "before-terminal"
    backups.create_backup(backup)
    if schema18:
        import json
        import re
        import sqlite3

        from iris_memory_core.storage.migrations import default_migrations_path
        from tests.integration.recovery.test_memory_legacy_restore_and_ledger_identity import (
            _refresh_backup_checksums,
        )

        # Schema 19 adds only indexes; retain the exact Schema 18 domain rows.
        with sqlite3.connect(backup / "canonical.sqlite3") as connection:
            migration = (default_migrations_path() / "0019_task_deletion_lookups.sql").read_text()
            for name in re.findall(r"CREATE INDEX (\w+)", migration):
                connection.execute(f"DROP INDEX {name}")
            # A genuine Schema18 snapshot cannot retain tables from later
            # Console/Provider migrations. Only remove empty future tables;
            # nonempty data here would invalidate this historical fixture.
            future_tables = dict.fromkeys(
                name
                for source in sorted(default_migrations_path().glob("*.sql"))
                if int(source.name[:4]) > 18
                for name in re.findall(r"(?m)^CREATE TABLE (\w+)", source.read_text())
            )
            for name in reversed(future_tables):
                assert connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
                connection.execute(f"DROP TABLE {name}")
            connection.execute("DELETE FROM schema_migrations WHERE version>=19")
            connection.execute("DELETE FROM migration_runs WHERE version>=19")
        connection.close()  # Checkpoint the fixture before hashing its immutable snapshot.
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["schema_version"] = 18
        manifest_path.write_text(json.dumps(manifest))
        _refresh_backup_checksums(backup)
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        current = target(client, task, "tasks")
        blocked = preview(client, csrf, [current], mode="soft")
        assert blocked["targets"][0]["status"] == "protected"
        assert not blocked["can_commit"]
        result = client.post(
            "/v1/memory/tasks/" + task + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": current["expected_revision"],
                "target_status": "cancelled",
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == 200, result.text
        if mode == "erase":
            assert (
                client.post(
                    "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
                ).status_code
                == 200
            )
        saved = preview(client, csrf, [target(client, task, "tasks")], mode=mode)
        assert "步骤 1" in saved["impacts"]["notice"]
        assert saved["can_commit"]
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["target_count"] == 1
        for suffix in ("", "/history", "/steps", "/triggers"):
            assert client.get("/v1/memory/tasks/" + task + suffix).status_code == 404
    with store.read() as tx:
        for kind, identifier in (("task", task), ("task_step", step), ("task_trigger", trigger)):
            assert tx.is_tombstoned(world["tenant"], kind, identifier)
        assert tx.events.get(event).status == "cancelled"
        assert tx.events.current_revision_row(event).created_by == "console:" + world["owner"].id
        assert tx.tasks.get_task(task).title == ("<erased>" if mode == "erase" else "Managed task")
        assert tx.tasks.get_task(task).status == "cancelled"
    forget = ForgetService(store, store.clock)
    ledger = forget.export_deletion_ledger(world["access"])
    destination = tmp_path / "restored"
    result = backups.restore_backup(
        backup, destination, deletion_ledger=ledger, forget_service=forget
    )
    assert result.check.ok, result.check.problems
    if schema18:
        from iris_memory_core.storage.migrations import MigrationRunner

        assert [
            m.version
            for m in MigrationRunner(destination / "canonical.sqlite3").migrate(
                allow_offline=True, backup_performed=True
            )
        ] == [19, 20, 21, 22]
    restored = Store(
        SQLiteRuntime(
            destination / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
        )
    )
    with restored.read() as tx:
        for kind, identifier in (("task", task), ("task_step", step), ("task_trigger", trigger)):
            assert tx.is_tombstoned(world["tenant"], kind, identifier)
        assert tx.events.get(event).status == "cancelled"
        assert tx.tasks.get_task(task).title == ("<erased>" if mode == "erase" else "Managed task")
        assert (
            tx.tasks.get_task(task).status == "active"
        )  # Recovery never invents task fulfillment.
    assert forget.replay_deletion_ledger(restored, ledger) == 0
    from iris_memory_core.jobs.handlers import memory_invalidated_handler

    with store.write() as tx:
        jobs = (
            tx.raw()
            .execute("SELECT id FROM outbox_jobs WHERE job_kind='memory.invalidated'")
            .fetchall()
        )
        assert jobs
        for row in jobs:
            memory_invalidated_handler(store.clock)(tx.outbox.get(row[0]))(tx)


def cancel_task(client: Any, csrf: str, task: str) -> None:
    current = target(client, task, "tasks")
    result = client.post(
        "/v1/memory/tasks/" + task + ":transition",
        headers=headers(csrf),
        json={
            "expected_revision": current["expected_revision"],
            "target_status": "cancelled",
            "reason_code": "operator_request",
        },
    )
    assert result.status_code == 200, result.text


def test_cascade_late_failure_restores_event_and_all_child_content(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from iris_memory_core.storage.console import ConsoleRepository

    task, step, trigger, event = seed(world)
    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        cancel_task(client, csrf, task)
        assert (
            client.post(
                "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
            ).status_code
            == 200
        )
        saved = preview(
            client,
            csrf,
            [target(client, world["ids"]["a"]), target(client, task, "tasks")],
            mode="erase",
        )
        key = headers(csrf)

        def fail(*a: Any, **kw: Any) -> None:
            raise RuntimeError("late transaction failure")

        with monkeypatch.context() as patch:
            patch.setattr(ConsoleRepository, "consume_command_preview", fail)
            assert commit(client, csrf, saved, headers=key).status_code == 500
        with store.read() as tx:
            assert tx.events.get(event).status == "pending"
            assert tx.tasks.get_task(task).title == "Managed task"
            assert tx.tasks.get_step(step).title == "Private step"
            assert tx.tasks.current_trigger_revision_row(trigger).schedule_spec is not None
            for kind, identifier in (
                ("task", task),
                ("task_step", step),
                ("task_trigger", trigger),
                ("note", world["ids"]["a"]),
            ):
                assert not tx.is_tombstoned(world["tenant"], kind, identifier)
        assert commit(client, csrf, saved, headers=key).status_code == 200


def test_event_state_change_invalidates_parent_deletion_preview(world: dict[str, Any]) -> None:
    from iris_memory_core.application.events import CognitiveEventService
    from iris_memory_core.storage.idempotency import IdempotencyManager

    task, _, _, event = seed(world)
    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        cancel_task(client, csrf, task)
        saved = preview(client, csrf, [target(client, task, "tasks")])
        CognitiveEventService(store, store.clock, idempotency=IdempotencyManager(store)).cancel(
            world["access"], event, reason="test", idempotency_key="event-changed"
        )
        assert commit(client, csrf, saved).status_code == 409
        fresh = preview(client, csrf, [target(client, task, "tasks")])
        assert "投递事件 0" in fresh["impacts"]["notice"]
        assert commit(client, csrf, fresh).status_code == 200


def test_missing_task_in_precreation_backup_is_sealed(
    world: dict[str, Any], tmp_path: Path
) -> None:
    from iris_memory_core.domain.errors import NotFoundError

    store = world["store"]
    backups = BackupService(store)
    backup = tmp_path / "empty"
    backups.create_backup(backup)
    task, _, _, _ = seed(world)
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        cancel_task(client, csrf, task)
        saved = preview(client, csrf, [target(client, task, "tasks")])
        assert commit(client, csrf, saved).status_code == 200
    forget = ForgetService(store, store.clock)
    ledger = forget.export_deletion_ledger(world["access"])
    destination = tmp_path / "restored"
    report = backups.restore_backup(
        backup, destination, deletion_ledger=ledger, forget_service=forget
    )
    assert report.check.ok, report.check.problems
    restored = Store(
        SQLiteRuntime(
            destination / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
        )
    )
    with restored.read() as tx:
        assert tx.is_tombstoned(world["tenant"], "task", task)
        with pytest.raises(NotFoundError):
            tx.tasks.get_task(task)
    assert forget.replay_deletion_ledger(restored, ledger) == 0


def test_task_dependency_is_deleted_with_parent(world: dict[str, Any]) -> None:
    task, step, _, _ = seed(world)
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        path = "/v1/memory/tasks/" + task
        current = target(client, task, "tasks")
        added = client.post(
            path + "/steps",
            headers=headers(csrf),
            json={
                "expected_revision": current["expected_revision"],
                "fields": {"stable_key": "second", "title": "Second"},
                "reason_code": "operator_request",
            },
        )
        assert added.status_code == 201, added.text
        receipt = added.json()["data"]
        dependency = client.post(
            path + "/dependencies",
            headers=headers(csrf),
            json={
                "expected_revision": receipt["task_revision"],
                "fields": {
                    "predecessor_step_id": step,
                    "successor_step_id": receipt["resource_id"],
                },
                "reason_code": "operator_request",
            },
        )
        assert dependency.status_code == 201, dependency.text
        identifier = dependency.json()["data"]["resource_id"]
        cancel_task(client, csrf, task)
        assert (
            client.post(
                "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
            ).status_code
            == 200
        )
        saved = preview(client, csrf, [target(client, task, "tasks")], mode="erase")
        assert "依赖 1" in saved["impacts"]["notice"]
        assert commit(client, csrf, saved).status_code == 200
        assert client.get(path + "/dependencies/" + identifier).status_code == 404
    with world["store"].read() as tx:
        assert tx.is_tombstoned(world["tenant"], "task_dependency", identifier)
        assert tx.tasks.get_dependency(identifier).status == "active"


def test_private_step_prevents_parent_deletion_with_narrow_grant(world: dict[str, Any]) -> None:
    from tests.integration.console.test_console_reads import grant_for

    task, step, _, _ = seed(world)
    owner, csrf = signed_in(world["app"], world["token"])
    with owner:
        cancel_task(owner, csrf, task)
    # Imported history may contain a child with stricter privacy than its parent.
    with world["store"].write() as tx:
        tx.raw().execute(
            "UPDATE task_step_revisions SET privacy_labels='[\"restricted\"]' WHERE step_id=?",
            (step,),
        )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="Task forgetter",
        description="",
        template="viewer",
        grant=grant_for(world, permissions=frozenset({"memory.read", "memory.forget"})),
        expires_us=world["store"].clock.now_us() + 3600000000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        assert client.get("/v1/memory/tasks/" + task).status_code == 200
        saved = preview(
            client, csrf, [{"resource_type": "task", "id": task, "expected_revision": 4}]
        )
        assert saved["targets"] == [{"input_index": 0, "status": "not_visible"}]
        assert "Private step" not in str(saved) and step not in str(saved)
        assert commit(client, csrf, saved).status_code == 409


def test_fifty_terminal_tasks_commit_as_one_fixed_batch(world: dict[str, Any]) -> None:
    from tests.integration.console.test_console_task_http import create_body

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifiers = []
        for _ in range(50):
            created = client.post(
                "/v1/memory/tasks", headers=headers(csrf), json=create_body(world)
            )
            assert created.status_code == 201, created.text
            identifier = created.json()["data"]["id"]
            cancel_task(client, csrf, identifier)
            identifiers.append(identifier)
        saved = preview(
            client, csrf, [target(client, identifier, "tasks") for identifier in identifiers]
        )
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["target_count"] == 50
    with world["store"].read() as tx:
        assert all(
            tx.is_tombstoned(world["tenant"], "task", identifier) for identifier in identifiers
        )


def test_cancelled_promise_task_stays_protected_until_source_archived(
    world: dict[str, Any],
) -> None:
    from tests.integration.console.test_console_note_http import request_body
    from tests.integration.console.test_console_task_http import create_body

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = request_body(world)
        body["fields"]["kind"] = "promise"
        note = client.post("/v1/memory/notes", headers=headers(csrf), json=body)
        assert note.status_code == 201, note.text
        note_id = note.json()["data"]["id"]
        body = create_body(world)
        body["source_refs"] = [{"resource_type": "note", "resource_id": note_id}]
        result = client.post("/v1/memory/tasks", headers=headers(csrf), json=body)
        assert result.status_code == 201, result.text
        task = result.json()["data"]["id"]
        cancel_task(client, csrf, task)
        saved = preview(client, csrf, [target(client, task, "tasks")])
        assert saved["targets"][0]["status"] == "protected"
        assert commit(client, csrf, saved).status_code == 409
        archived = client.post(
            "/v1/memory/notes/" + note_id + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "target_status": "archived",
                "reason_code": "operator_request",
            },
        )
        assert archived.status_code == 200, archived.text
        fresh = preview(client, csrf, [target(client, task, "tasks")])
        assert fresh["can_commit"]
        assert commit(client, csrf, fresh).status_code == 200


@pytest.mark.parametrize("acknowledged", [False, True])
def test_task_forget_preserves_event_delivery_and_ack_facts(
    world: dict[str, Any], acknowledged: bool
) -> None:
    from iris_memory_core.application.events import CognitiveEventService
    from iris_memory_core.storage.idempotency import IdempotencyManager

    task, step, _, event = seed(world)
    store = world["store"]
    events = CognitiveEventService(store, store.clock, idempotency=IdempotencyManager(store))
    bundle = events.pull(world["access"], agent_id=world["agent"])
    assert event in {row[0].id for row in bundle.events}
    if acknowledged:
        events.ack(world["access"], event, idempotency_key="received-before-forget")
    with store.read() as tx:
        before = tx.events.get(event)
        history_before = tx.events.history(event)
        step_before = tx.tasks.current_step_revision_row(step)
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        cancel_task(client, csrf, task)
        saved = preview(client, csrf, [target(client, task, "tasks")])
        assert f"投递事件 {0 if acknowledged else 1}" in saved["impacts"]["notice"]
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
    with store.read() as tx:
        after = tx.events.get(event)
        assert after.delivery_attempts == before.delivery_attempts == 1
        assert after.ack_id == before.ack_id
        assert after.acknowledged_us == before.acknowledged_us
        assert tx.tasks.current_step_revision_row(step) == step_before
        assert not tx.is_tombstoned(world["tenant"], "cognitive_event", event)
        if acknowledged:
            assert after == before
            assert tx.events.history(event) == history_before
        else:
            assert after.status == "cancelled"
            assert after.ack_id is None and after.acknowledged_us is None
            assert len(tx.events.history(event)) == len(history_before) + 1
    assert events.pull(world["access"], agent_id=world["agent"]).events == ()


def test_new_hold_stops_the_whole_task_cascade(world: dict[str, Any]) -> None:
    from iris_memory_core.application.retention import RetentionService

    task, step, trigger, event = seed(world)
    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        cancel_task(client, csrf, task)
        saved = preview(client, csrf, [target(client, task, "tasks")])
        RetentionService(
            store, store.clock, forget=ForgetService(store, store.clock)
        ).create_legal_hold(world["access"], space_id=world["spaces"][0], reason="preserve_task")
        result = commit(client, csrf, saved)
        assert result.status_code == 409, result.text
        fresh = preview(client, csrf, [target(client, task, "tasks")])
        assert fresh["targets"][0]["status"] == "held"
        assert not fresh["can_commit"]
    with store.read() as tx:
        assert tx.events.get(event).status == "pending"
        for kind, identifier in (("task", task), ("task_step", step), ("task_trigger", trigger)):
            assert not tx.is_tombstoned(world["tenant"], kind, identifier)


def clone_steps(store: Any, step: str, count: int) -> None:
    """Populate a large valid plan without hundreds of unrelated HTTP writes."""
    import uuid

    with store.write() as tx:
        db = tx.raw()
        current = dict(db.execute("SELECT * FROM task_steps WHERE id=?", (step,)).fetchone())
        revision = dict(
            db.execute(
                "SELECT * FROM task_step_revisions WHERE id=?", (current["current_revision_id"],)
            ).fetchone()
        )
        for ordinal in range(count):
            identifier, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
            changes = {"stable_key": identifier, "ordinal": ordinal + 1}
            rows = (
                (
                    "task_steps",
                    dict(current, **changes, id=identifier, current_revision_id=revision_id),
                ),
                (
                    "task_step_revisions",
                    dict(revision, **changes, id=revision_id, step_id=identifier),
                ),
            )
            for table, row in rows:
                db.execute(
                    f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                    tuple(row.values()),
                )


@pytest.mark.parametrize("reverse", [False, True])
def test_selected_task_references_are_deleted_before_their_targets(
    world: dict[str, Any], reverse: bool
) -> None:
    from tests.integration.console.test_console_task_http import create_body

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifiers = []
        for _ in range(2):
            result = client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world))
            assert result.status_code == 201, result.text
            identifiers.append(result.json()["data"]["id"])
        parent, watched = identifiers
        result = client.post(
            "/v1/memory/tasks/" + parent + "/triggers",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {
                    "kind": "task_transition",
                    "condition_spec": {"task_id": watched, "to_status": "completed"},
                },
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == 201, result.text
        trigger = result.json()["data"]["resource_id"]
        for identifier in identifiers:
            cancel_task(client, csrf, identifier)
        saved = preview(
            client,
            csrf,
            [
                target(client, identifier, "tasks")
                for identifier in (identifiers[::-1] if reverse else identifiers)
            ],
        )
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["target_count"] == 2
    with world["store"].read() as tx:
        assert tx.is_tombstoned(world["tenant"], "task_trigger", trigger)
        assert all(
            tx.is_tombstoned(world["tenant"], "task", identifier) for identifier in identifiers
        )


def test_independent_child_plan_must_be_deleted_before_parent(world: dict[str, Any]) -> None:
    from tests.integration.console.test_console_task_http import create_body

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifiers = []
        for _ in range(2):
            result = client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world))
            assert result.status_code == 201, result.text
            identifiers.append(result.json()["data"]["id"])
        parent, child = identifiers
        # The current managed create command does not accept historical parent links.
        with world["store"].write() as tx:
            tx.raw().execute("UPDATE tasks SET parent_task_id=? WHERE id=?", (parent, child))
        for identifier in identifiers:
            cancel_task(client, csrf, identifier)
        saved = preview(client, csrf, [target(client, parent, "tasks")])
        assert saved["targets"][0]["status"] == "protected"
        assert commit(client, csrf, saved).status_code == 409
        saved = preview(client, csrf, [target(client, child, "tasks")])
        assert commit(client, csrf, saved).status_code == 200
        saved = preview(client, csrf, [target(client, parent, "tasks")])
        assert saved["can_commit"]
        assert commit(client, csrf, saved).status_code == 200


@pytest.mark.parametrize("task_count,extra_steps", [(1, 498), (2, 248)])
def test_task_and_batch_child_limits_reject_without_partial_preview(
    world: dict[str, Any], task_count: int, extra_steps: int
) -> None:
    store = world["store"]
    seeds = [seed(world) for _ in range(task_count)]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        for task, step, _, _ in seeds:
            cancel_task(client, csrf, task)
            clone_steps(store, step, extra_steps)
        with store.read() as tx:
            before = tx.raw().execute("SELECT COUNT(*) FROM console_command_previews").fetchone()[0]
        result = client.post(
            "/v1/memory:forget-preview",
            headers=headers(csrf),
            json={
                "targets": [target(client, task, "tasks") for task, _, _, _ in seeds],
                "mode": "soft",
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == 503, result.text
    with store.read() as tx:
        assert (
            tx.raw().execute("SELECT COUNT(*) FROM console_command_previews").fetchone()[0]
            == before
        )
        for task, step, trigger, event in seeds:
            assert tx.events.get(event).status == "pending"
            for kind, identifier in (
                ("task", task),
                ("task_step", step),
                ("task_trigger", trigger),
            ):
                assert not tx.is_tombstoned(world["tenant"], kind, identifier)
