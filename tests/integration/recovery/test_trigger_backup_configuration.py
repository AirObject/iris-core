"""Trigger backup configurations must agree while scheduler progress survives."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_reads import world as world_fixture
from tests.integration.console.test_console_task_http import create_body

auth = auth_fixture
world = world_fixture


def test_trigger_backup_roundtrip_and_static_configuration_corruption(
    world: dict[str, Any], tmp_path: Path
) -> None:
    from iris_memory_core.application.tasks import TaskService
    from iris_memory_core.storage.backup import BackupService, verify_database_invariants
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from iris_memory_core.storage.uow import Store
    from tests.conftest import local_allowed_versions

    store = world["store"]
    tasks = TaskService(store, store.clock)
    client, csrf = signed_in(world["app"], world["token"])
    start = store.clock.now_us()
    with client:
        created = client.post("/v1/memory/tasks", headers=headers(csrf), json=create_body(world))
        assert created.status_code == 201, created.text
        task_id = created.json()["data"]["id"]
        path = "/v1/memory/tasks/" + task_id + "/triggers"
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
        updated = client.patch(
            path + "/" + identifier,
            headers=headers(csrf),
            json={
                "expected_revision": 2,
                "child_expected_revision": 1,
                "fields": {"schedule_spec": {"every_seconds": 120}, "max_occurrences_per_run": 7},
                "reason_code": "operator_request",
            },
        )
        assert updated.status_code == 200, updated.text
    store.clock.set(start + 120_000_000)
    with store.write() as tx:
        assert (
            tasks.trigger_scan(
                tx, tenant_id=world["tenant"], agent_id=world["agent"]
            ).occurrences_created
            == 1
        )
    service = BackupService(store)
    source, destination = tmp_path / "backup", tmp_path / "restored"
    service.create_backup(source)
    restored = service.restore_backup(source, destination)
    assert restored.check.ok, restored.check.problems
    database = destination / "canonical.sqlite3"
    restored_store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    with restored_store.read() as tx:
        current = tx.tasks.get_trigger(identifier)
        spec = tx.tasks.current_trigger_revision_row(identifier)
        assert current.current_revision == 2 and current.max_occurrences_per_run == 7
        assert spec.schedule_spec == {"every_seconds": 120}
        assert current.next_fire_at_us == start + 240_000_000
        assert tx.tasks.get_task(task_id).current_revision == 3
    assert verify_database_invariants(database) == ()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for field, forged in (
        ("kind", "at_time"),
        ("task_step_id", "missing-step"),
        ("timezone", "Asia/Shanghai"),
        ("catch_up_policy", "skip"),
        ("misfire_grace_us", 0),
        ("max_occurrences_per_run", 8),
        ("enabled", 0),
        ("agent_id", "missing-agent"),
        ("tenant_id", "missing-tenant"),
    ):
        copy = tmp_path / (field + ".sqlite3")
        shutil.copy2(database, copy)
        with sqlite3.connect(copy) as connection:
            connection.execute(
                f"UPDATE task_triggers SET {field}=? WHERE id=?", (forged, identifier)
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert (
            "task trigger configuration differs from its revision or parent"
            in verify_database_invariants(copy)
        ), field
