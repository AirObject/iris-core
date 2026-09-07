"""Schema 16 upgrades existing Schema 15 and bounds durable deletion metadata."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.domain.console import CommandPreview
from iris_memory_core.storage.migrations import MigrationRunner, default_migrations_path
from tests.integration.test_console_authentication import auth as auth_fixture

auth = auth_fixture


def test_schema15_upgrades_without_rewriting_existing_rows(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    for migration in default_migrations_path().glob("*.sql"):
        if int(migration.name[:4]) <= 15:
            shutil.copyfile(migration, old / migration.name)
    database = tmp_path / "canonical.sqlite3"
    MigrationRunner(database, old).migrate()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tenants (id,status,created_us,created_at) "
            "VALUES ('kept','active',1,'1970-01-01T00:00:00.000001Z')"
        )
        checksums = conn.execute(
            "SELECT version,checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    through16 = tmp_path / "through16"
    through16.mkdir()
    for migration in default_migrations_path().glob("*.sql"):
        if int(migration.name[:4]) <= 16:
            shutil.copyfile(migration, through16 / migration.name)
    runner = MigrationRunner(database, through16)
    assert [migration.version for migration in runner.migrate()] == [16]
    assert runner.migrate() == ()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT id,status,created_us FROM tenants").fetchone() == (
            "kept",
            "active",
            1,
        )
        assert (
            conn.execute(
                "SELECT version,checksum FROM schema_migrations WHERE version<=15 ORDER BY version"
            ).fetchall()
            == checksums
        )
        assert conn.execute("SELECT COUNT(*) FROM console_command_previews").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("invalid", ["ttl", "mode", "json", "oversize", "state", "foreign_key"])
def test_preview_storage_rejects_invalid_or_unbounded_state(auth: Any, invalid: str) -> None:
    from dataclasses import replace

    _, security, key, _ = auth
    now = security.clock.now_us()
    record = CommandPreview(
        "p",
        key.tenant_id,
        key.id,
        key.revision,
        key.grant.fingerprint,
        "memory_forget",
        "soft",
        "operator_request",
        "a" * 64,
        "{}",
        now,
        now + 600_000_000,
    )
    if invalid == "ttl":
        record = replace(record, expires_us=now + 1)
    elif invalid == "mode":
        record = replace(record, mode="undo")
    elif invalid == "json":
        record = replace(record, payload_json="{")
    elif invalid == "oversize":
        record = replace(record, payload_json='{"x":"' + "a" * 262144 + '"}')
    elif invalid == "state":
        record = replace(record, status="consumed")
    else:
        record = replace(record, key_id="missing")
    with pytest.raises(sqlite3.IntegrityError), security.uow.write() as tx:
        tx.console.insert_command_preview(record)
