"""P13-AUTH-01: append-only 0012, schema 11 compatibility and release metadata."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from iris_memory_core.api.console.app import create_console_app
from iris_memory_core.application.security import CredentialService
from iris_memory_core.storage.migrations import (
    MigrationRunner,
    default_migrations_path,
    discover_migrations,
)
from iris_memory_core.storage.runtime import SQLiteRuntime, current_schema_version
from iris_memory_core.storage.uow import Store
from tests.conftest import local_allowed_versions
from tests.migration_support import migrate_through


def test_0012_upgrades_schema11_preserving_rows_and_legacy_auth(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite3"
    migrate_through(database, 11)
    # Trusted legacy fixture exercises 0012 in isolation, before the current
    # binary's Schema 23 gate permits normal service access.
    store = Store(
        SQLiteRuntime(database, allowed_versions=local_allowed_versions()),
        verify_schema_window=False,
    )
    with store.write() as tx:
        tx.insert_tenant("tenant", status="active")
    service = CredentialService(store, store.clock)
    token = "legacy-application-" + "x" * 32
    record = service.issue(
        token,
        tenant_id="tenant",
        app_instance_id="app",
        plane="application",
        expires_us=store.clock.now_us() + 60_000_000,
    )
    assert service.authenticate(token).tenant_id == "tenant"
    with sqlite3.connect(database) as db:
        before = db.execute("SELECT * FROM service_credentials").fetchone()
        assert current_schema_version(db) == 11
    with pytest.raises(sqlite3.OperationalError):
        create_console_app(store=store)
    migrate_through(database, 12)
    assert service.authenticate(token).tenant_id == "tenant"
    with sqlite3.connect(database) as db:
        after = db.execute("SELECT * FROM service_credentials").fetchone()
        # last_used_us may advance during authentication; metadata stays NULL.
        columns = [v[1] for v in db.execute("PRAGMA table_info(service_credentials)")]
        for i, value in enumerate(before):
            if columns[i] != "last_used_us":
                assert after[i] == value
        assert all(value is None for value in after[len(before) :])
        assert current_schema_version(db) == 12
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        new_columns = {
            row[1]: row[3] for row in db.execute("PRAGMA table_info(service_credentials)")
        }
        assert all(
            new_columns[name] == 0
            for name in (
                "label",
                "description",
                "token_prefix",
                "created_by",
                "revoke_reason",
                "revoke_after_us",
                "console_revision",
            )
        )
    with store.read() as tx:
        loaded = tx.reflection.credential(record.id)
        assert loaded and loaded.console_revision == 1
    assert create_console_app(store=store).state.security is not None
    assert [
        item.version
        for item in MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
    ] == [13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
    assert MigrationRunner(database).migrate() == ()
    current = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    assert CredentialService(current, current.clock).authenticate(token).tenant_id == "tenant"


def test_published_migrations_unchanged_and_new_migration_online_safe() -> None:
    root = Path(__file__).resolve().parents[3]
    for path in default_migrations_path().glob("*.sql"):
        if int(path.name[:4]) <= 11:
            baseline = subprocess.run(
                ["git", "show", f"HEAD:migrations/{path.name}"],
                cwd=root,
                capture_output=True,
                check=True,
            ).stdout
            assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(baseline).digest()
    migration = next(
        item for item in discover_migrations(default_migrations_path()) if item.version == 12
    )
    assert migration.meta and migration.meta.online_safe and migration.meta.min_app == "0.12.0"
    source = json.loads((root / "contracts/source/console.json").read_text())
    manifest = json.loads((root / "schemas/version-manifest.json").read_text())
    assert source["runtime_versions"] == {"package_version": "0.15.0", "schema_version": 23}
    for name, value in source["runtime_versions"].items():
        assert manifest[name] == value
