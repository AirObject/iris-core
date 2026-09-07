"""Restored destructive jobs never replay uncommitted fixed targets."""

from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_operation_batches import accepted, worker
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


@pytest.mark.parametrize("batches", [0, 1, 2])
@pytest.mark.parametrize("mode", ["soft", "erase"])
def test_restore_reconciles_latest_ledger_without_resuming_pending_work(
    world: dict[str, Any], tmp_path: Path, batches: int, mode: str
) -> None:
    _, operation_id, identifiers = accepted(world, mode=mode)
    store = world["store"]
    backups = BackupService(store)
    snapshot = tmp_path / "queued-snapshot"
    backups.create_backup(snapshot)
    for _ in range(batches):
        assert worker(world).run_once()["completed"] == 1
    forget = ForgetService(store, store.clock)
    ledger = forget.export_deletion_ledger(world["access"])
    expected = min(batches * 50, 51)
    for round_number in range(2):
        destination = tmp_path / f"restored-{round_number}"
        result = backups.restore_backup(
            snapshot, destination, deletion_ledger=ledger, forget_service=forget
        )
        assert result.check.ok, result.check.problems
        restored = Store(
            SQLiteRuntime(
                destination / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
            )
        )
        with restored.read() as tx:
            operation = tx.console_operations.get(world["tenant"], operation_id)
            assert operation is not None
            assert operation.processed == expected
            assert operation.status == ("completed" if expected == 51 else "blocked")
            assert operation.blocked_reason == (
                None if expected == 51 else "restore_requires_review"
            )
            assert operation.payload_json == "{}" and operation.current_job_id is None
            assert operation.problems_count == int(expected < 51)
        # The backup still has the original real session and queued job. It
        # must become a harmless no-op even when no auth reset was requested.
        outcome = worker({**world, "store": restored}).run_once()
        assert outcome["completed"] == int(round_number == 0), outcome
        with restored.read() as tx:
            assert (
                sum(
                    tx.is_tombstoned(world["tenant"], "note", identifier)
                    for identifier in identifiers
                )
                == expected
            )
            assert tx.notes.get(identifiers[-1]).title == (
                "<erased>" if expected == 51 and mode == "erase" else "Fixed deletion member 50"
            )
            assert tx.console_operations.get(world["tenant"], operation_id) == operation
        backups = BackupService(restored)
        snapshot = tmp_path / f"safe-snapshot-{round_number}"
        backups.create_backup(snapshot)


@pytest.mark.parametrize("corruption", ["payload", "progress"])
def test_inconsistent_operation_aborts_restore_before_replacing_destination(
    world: dict[str, Any], tmp_path: Path, corruption: str
) -> None:
    import sqlite3

    from tests.integration.test_phase5_review_round4 import _refresh_backup_checksums

    _, operation_id, _ = accepted(world)
    store = world["store"]
    backups = BackupService(store)
    snapshot = tmp_path / "invalid-snapshot"
    backups.create_backup(snapshot)
    with sqlite3.connect(snapshot / "canonical.sqlite3") as connection:
        if corruption == "payload":
            connection.execute(
                "UPDATE console_operations SET payload_json='[]' WHERE id=?", (operation_id,)
            )
        else:
            connection.execute(
                "UPDATE console_operations SET processed=1 WHERE id=?", (operation_id,)
            )
    connection.close()
    _refresh_backup_checksums(snapshot)
    destination = tmp_path / "existing-target"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("existing target must survive")
    forget = ForgetService(store, store.clock)
    result = backups.restore_backup(
        snapshot,
        destination,
        deletion_ledger=forget.export_deletion_ledger(world["access"]),
        forget_service=forget,
    )
    assert not result.check.ok
    assert any("restored operation" in problem for problem in result.check.problems), (
        result.check.problems
    )
    assert sentinel.read_text() == "existing target must survive"
    assert sorted(item.name for item in destination.iterdir()) == ["keep.txt"]
