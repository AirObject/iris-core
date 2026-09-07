"""Bounded Task deletion storage, recovery scans and transactional erasure."""

from typing import Any

import pytest

from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.domain.errors import NotReadyError
from iris_memory_core.storage.backup import verify_database_invariants
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import world as world_fixture
from tests.integration.test_console_task_http import create_body

auth = auth_fixture
world = world_fixture


def seed(world: dict[str, Any]) -> tuple[str, str, str, str]:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        result = client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world))
        assert result.status_code == 201, result.text
        task = result.json()["data"]["id"]
        path = "/v1/memory/tasks/" + task
        result = client.post(
            path + "/steps",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {
                    "stable_key": "private-key",
                    "title": "Private step",
                    "description": "private description",
                    "expected_effect": "private effect",
                },
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == 201, result.text
        step = result.json()["data"]["resource_id"]
        result = client.post(
            path + "/triggers",
            headers=headers(csrf),
            json={
                "expected_revision": 2,
                "fields": {
                    "kind": "at_time",
                    "schedule_spec": {"at_us": world["store"].clock.now_us() + 1000000},
                },
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == 201, result.text
        trigger = result.json()["data"]["resource_id"]
    with world["store"].write() as tx:
        event = CognitiveEventService.create_internal(
            tx,
            tenant_id=world["tenant"],
            agent_id=world["agent"],
            space_group_id=None,
            space_id=world["spaces"][0],
            session_id=None,
            kind="task.due",
            object_type="task_step",
            object_id=step,
            occurrence_id=None,
            scheduled_at_us=world["store"].clock.now_us(),
            deliver_after_us=world["store"].clock.now_us(),
            reason_code="test",
            now_us=world["store"].clock.now_us(),
        )
    return task, step, trigger, event


def test_inventory_and_erasure_preserve_identity(world: dict[str, Any]) -> None:
    task, step, trigger, event = seed(world)
    store = world["store"]
    with store.write() as tx:
        before = tx.tasks.get_task(task)
        assert set(tx.tasks.deletion_children(world["tenant"], task)) == {
            ("task_step", step),
            ("task_trigger", trigger),
            ("cognitive_event", event),
        }
        assert tx.tasks.deletion_children("other-tenant", task) == ()
        assert len(tx.tasks.deletion_children(world["tenant"], task, limit=2)) == 2
        tx.tasks.erase_task_content(task, now_us=store.clock.now_us())
        after = tx.tasks.get_task(task)
        assert after.current_revision == before.current_revision and after.status == before.status
        assert after.title == "<erased>"
        assert all(row.title == "<erased>" and not row.goal for row in tx.tasks.task_history(task))
        child = tx.tasks.get_step(step)
        assert child.title == "<erased>" and child.stable_key == "private-key"
        child_rev = tx.tasks.current_step_revision_row(step)
        assert child_rev.description is None and child_rev.expected_effect is None
        trigger_rev = tx.tasks.current_trigger_revision_row(trigger)
        assert trigger_rev.schedule_spec is None and trigger_rev.condition_spec is None
        assert (
            tx.events.get(event).status == "pending"
        )  # Cancellation belongs to application layer.
    assert verify_database_invariants(store.runtime.database) == ()


def test_task_erase_deadline_rolls_back_parent_and_child(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from types import SimpleNamespace

    from iris_memory_core.storage import plans

    task, step, _, _ = seed(world)
    store = world["store"]
    with store.write() as tx:
        db = tx.raw()
        columns = [r[1] for r in db.execute("PRAGMA table_info(task_step_revisions)")]
        first = dict(
            zip(
                columns,
                db.execute("SELECT * FROM task_step_revisions WHERE step_id=?", (step,)).fetchone(),
                strict=True,
            )
        )
        highest = db.execute(
            "SELECT MAX(revision) FROM task_step_revisions WHERE step_id=?", (step,)
        ).fetchone()[0]
        for n in range(highest + 1, highest + 1001):
            row = dict(first, id=f"test-revision-{n}", revision=n)
            db.execute(
                "INSERT INTO task_step_revisions ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                [row[c] for c in columns],
            )
    calls = 0

    def deadline() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 1.0

    with monkeypatch.context() as patch:
        patch.setattr(plans, "time", SimpleNamespace(monotonic=deadline))
        with pytest.raises(NotReadyError), store.write() as tx:
            tx.tasks.erase_task_content(task, now_us=store.clock.now_us())
    with store.read() as tx:
        assert tx.tasks.get_task(task).title == "Managed task"
        assert tx.tasks.get_step(step).title == "Private step"


def test_restored_active_deleted_task_does_not_starve_recall_or_trigger_scan(
    world: dict[str, Any],
) -> None:
    deleted, _, _, _ = seed(world)
    live, _, trigger, _ = seed(world)
    store = world["store"]
    now = store.clock.now_us()
    with store.write() as tx:
        for identifier, due in ((deleted, now - 2), (live, now - 1)):
            tx.raw().execute("UPDATE tasks SET due_at_us=? WHERE id=?", (due, identifier))
            tx.raw().execute(
                "UPDATE task_revisions SET due_at_us=? WHERE task_id=?", (due, identifier)
            )
            tx.raw().execute(
                "UPDATE task_triggers SET next_fire_at_us=? WHERE task_id=?", (due, identifier)
            )
        # Ledger replay can restore an active historical state with a deletion marker.
        tx.record_tombstone(
            tenant_id=world["tenant"],
            resource_type="task",
            resource_id=deleted,
            reason_code="restored_deletion",
            deleted_by="restore:deletion_ledger",
        )
        assert tx.tasks.get_task(deleted).status == "active"
    with store.read() as tx:
        assert [
            row.id for row in tx.tasks.list_tasks(world["tenant"], world["agent"], limit=1)
        ] == [live]
        assert [
            row.id
            for row in tx.tasks.due_tasks(world["tenant"], world["agent"], now_us=now, limit=1)
        ] == [live]
        assert [
            row.id
            for row in tx.tasks.triggers_for_scan(
                world["tenant"], world["agent"], now_us=now, limit=1
            )
        ] == [trigger]
        assert (
            tx.raw()
            .execute("SELECT COUNT(*) FROM task_step_revisions WHERE title='<erased>'")
            .fetchone()[0]
            == 0
        )
