"""Phase 9 Schema 9 -> 10 expansion and Persona integrity tests."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from iris_memory_core.storage.backup import verify_database_invariants
from iris_memory_core.storage.migrations import (
    MigrationRunner,
    default_migrations_path,
    discover_migrations,
)


def _schema9_database(tmp_path: Path) -> Path:
    legacy = tmp_path / "schema9-migrations"
    legacy.mkdir()
    for migration in discover_migrations(default_migrations_path()):
        if migration.version <= 9:
            shutil.copy2(migration.path, legacy / migration.path.name)
    database = tmp_path / "canonical.sqlite3"
    MigrationRunner(database, legacy).migrate()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO tenants (id, status, created_us, created_at) "
            "VALUES ('t1','active',100,'frozen')"
        )
        connection.execute(
            "INSERT INTO agents (id, tenant_id, display_name, status, created_us, created_at) "
            "VALUES ('a1','t1','Iris','active',101,'frozen')"
        )
        connection.execute(
            "INSERT INTO persona_revisions (id, tenant_id, agent_id, revision, core, traits, "
            "narrative, content_hash, status, source, created_us) VALUES "
            "('persona-frozen','t1','a1',1,'{\"language\":\"zh\"}','[]','',"
            "'frozen-bootstrap-hash','published','bootstrap',102)"
        )
        connection.execute(
            "UPDATE agents SET persona_current_revision_id='persona-frozen' WHERE id='a1'"
        )
        connection.commit()
    finally:
        connection.close()
    return database


def test_phase9_metadata_declares_online_expansion() -> None:
    migration = {item.version: item for item in discover_migrations(default_migrations_path())}[10]
    assert migration.meta is not None
    assert migration.meta.online_safe is True
    assert migration.meta.lock_ms == 200
    assert migration.meta.min_app == "0.10.0"
    assert migration.meta.recovery == "none"


def test_schema9_upgrade_preserves_bootstrap_bytes_pointer_and_hash(tmp_path: Path) -> None:
    database = _schema9_database(tmp_path)
    before = (
        sqlite3.connect(database)
        .execute(
            "SELECT p.id, p.revision, p.core, p.traits, p.narrative, p.content_hash, "
            "a.persona_current_revision_id FROM persona_revisions p "
            "JOIN agents a ON a.id=p.agent_id"
        )
        .fetchone()
    )

    applied = MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
    assert [item.version for item in applied] == [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
    connection = sqlite3.connect(database)
    try:
        after = connection.execute(
            "SELECT p.id, p.revision, p.core, p.traits, p.narrative, p.content_hash, "
            "a.persona_current_revision_id FROM persona_revisions p "
            "JOIN agents a ON a.id=p.agent_id"
        ).fetchone()
        metadata = connection.execute(
            "SELECT change_reason, lifecycle_status FROM persona_revision_metadata "
            "WHERE revision_id='persona-frozen'"
        ).fetchone()
        policy = connection.execute(
            "SELECT mode, status FROM persona_policies WHERE agent_id='a1'"
        ).fetchone()
        strict_tables = {
            row[0]: row[5]
            for row in connection.execute("PRAGMA table_list")
            if str(row[0]).startswith("persona_")
        }
    finally:
        connection.close()
    assert after == before
    assert metadata == ("bootstrap", "published")
    assert policy == ("locked", "current")
    assert all(strict_tables[name] == 1 for name in strict_tables if name != "persona_revisions")
    assert verify_database_invariants(database) == ()


def test_backup_integrity_rejects_missing_current_persona_metadata(tmp_path: Path) -> None:
    database = _schema9_database(tmp_path)
    MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DELETE FROM persona_revision_metadata WHERE revision_id='persona-frozen'"
        )
        connection.commit()
    finally:
        connection.close()
    assert "persona current metadata or policy is incomplete" in verify_database_invariants(
        database
    )
