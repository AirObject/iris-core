"""Schema 15 distinguishes empty installation from trusted offline upgrade."""

import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import (
    MigrationNotOnlineSafe,
    MigrationRunner,
    default_migrations_path,
)
from iris_memory_core.storage.runtime import SQLiteRuntime
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for, local_allowed_versions


def legacy_store(tmp_path: Path) -> tuple[Path, str]:
    migrations = tmp_path / "old-migrations"
    migrations.mkdir()
    for source in default_migrations_path().glob("*.sql"):
        if int(source.name[:4]) <= 14:
            shutil.copyfile(source, migrations / source.name)
    database = tmp_path / "legacy.sqlite3"
    MigrationRunner(database, migrations).migrate(app_version="0.12.0")
    # A trusted fixture prepares legacy bytes before upgrading; normal runtime
    # access remains gated to Schema 25 and never uses this bypass.
    store = Store(
        SQLiteRuntime(database, allowed_versions=local_allowed_versions()),
        verify_schema_window=False,
    )
    with store.write() as tx:
        tx.insert_tenant("legacy", status="active")
    access = access_for("legacy", admin=True)
    agent = ProvisioningService(store).create_agent(access, "Legacy agent")
    access = access_for("legacy", agent_ids=frozenset({agent.id}), admin=True)
    tasks = TaskService(store, store.clock, idempotency=IdempotencyManager(store))
    task = tasks.create(
        access,
        agent_id=agent.id,
        title="Legacy task",
        origin="explicit_tool",
        idempotency_key="legacy-task",
    )
    with store.write() as tx:
        steps = []
        for key, status in [("a", "ready"), ("b", "pending")]:
            step = tx.tasks.insert_step(
                task_id=task.task_id,
                tenant_id="legacy",
                stable_key=key,
                title=key,
                ordinal=0,
                status=status,
            )
            revision = tx.tasks.insert_step_revision(
                step_id=step,
                task_id=task.task_id,
                tenant_id="legacy",
                revision=1,
                stable_key=key,
                title=key,
                description=None,
                privacy_labels=(),
                status=status,
                ordinal=0,
                expected_effect=None,
                completion_evidence_refs=(),
                started_us=None,
                completed_us=None,
                created_by="fixture",
            )
            tx.tasks.set_initial_step_pointer(step, revision)
            tx.advance_watermark("legacy", agent.id, [("task_step", step, 1)])
            steps.append(step)
        edge = tx.tasks.insert_dependency(
            task_id=task.task_id,
            tenant_id="legacy",
            predecessor_step_id=steps[0],
            successor_step_id=steps[1],
            condition="completed",
        )
        revision = str(uuid4())
        tx.raw().execute(
            "INSERT INTO task_dependency_revisions (id, dependency_id, task_id, tenant_id, "
            "revision, predecessor_step_id, successor_step_id, condition, created_us, created_by) "
            "VALUES (?,?,?,?,1,?,?,?,?,?)",
            (
                revision,
                edge,
                task.task_id,
                "legacy",
                steps[0],
                steps[1],
                "completed",
                store.clock.now_us(),
                "fixture",
            ),
        )
        tx.tasks.set_initial_dependency_pointer(edge, revision)
        tx.advance_watermark("legacy", agent.id, [("task_dependency", edge, 1)])
    return database, edge


def test_existing_database_requires_offline_ack_and_verified_backup(tmp_path: Path) -> None:
    database, edge = legacy_store(tmp_path)
    runner = MigrationRunner(database)
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate()
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate(allow_offline=True)
    assert runner.current_version() == 14
    with sqlite3.connect(database) as connection:
        assert "status" not in {
            row[1] for row in connection.execute("PRAGMA table_info(task_dependencies)")
        }
    assert [m.version for m in runner.migrate(allow_offline=True, backup_performed=True)] == [
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status, current_revision FROM task_dependencies WHERE id=?", (edge,)
        ).fetchone() == ("active", 1)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE task_dependencies SET status='unknown' WHERE id=?", (edge,))
    assert runner.migrate() == ()


def test_only_a_genuinely_empty_database_can_bootstrap(tmp_path: Path) -> None:
    runner = MigrationRunner(tmp_path / "empty.sqlite3")
    assert len(runner.migrate()) == 25
    database = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE application_data (value TEXT)")
        connection.execute("INSERT INTO application_data VALUES ('existing')")
    with pytest.raises(MigrationNotOnlineSafe):
        MigrationRunner(database).migrate()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM application_data").fetchone()[0] == "existing"


def test_bootstrap_exception_does_not_apply_to_unmarked_migrations(tmp_path: Path) -> None:
    path = tmp_path / "migrations"
    path.mkdir()
    (path / "0001_probe.sql").write_text("CREATE TABLE probe (id INTEGER);")
    (path / "0002_offline.sql").write_text(
        "-- iris: online_safe=false lock_ms=200 min_app=0.1.0 max_app= recovery=backup\n"
        "ALTER TABLE probe ADD COLUMN name TEXT;"
    )
    with pytest.raises(MigrationNotOnlineSafe):
        MigrationRunner(tmp_path / "db.sqlite3", path).migrate()


def test_cli_upgrades_only_after_verifying_a_real_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from iris_memory_core.cli import main
    from iris_memory_core.storage.backup import verify_backup
    from iris_memory_core.storage.migrations import MigrationAppVersionError

    database, edge = legacy_store(tmp_path)
    with pytest.raises(MigrationAppVersionError):
        MigrationRunner(database).migrate(
            app_version="0.12.0",
            allow_offline=True,
            backup_performed=True,
        )
    assert main(["migrate", str(database), "--allow-offline"]) == 1
    assert MigrationRunner(database).current_version() == 14
    backup = tmp_path / "verified-backup"
    assert (
        main(
            [
                "migrate",
                str(database),
                "--allow-offline",
                "--with-backup",
                str(backup),
            ]
        )
        == 0
    )
    assert "schema_version=25 applied=11" in capsys.readouterr().out
    check = verify_backup(backup)
    assert check.ok, check.problems
    with sqlite3.connect(backup / "canonical.sqlite3") as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 14
        assert (
            connection.execute(
                "SELECT current_revision FROM task_dependencies WHERE id=?", (edge,)
            ).fetchone()[0]
            == 1
        )
