"""Schema 11 upgrade, compatibility, credential and archive evidence."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from iris_memory_core.application.security import CredentialService, token_digest
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ProviderUnavailableError,
    SchemaIncompatibleError,
)
from iris_memory_core.domain.reflection import ProviderKind
from iris_memory_core.providers.cognitive import (
    CognitiveProviderLimits,
    DurableProviderState,
    ProviderGovernance,
)
from iris_memory_core.storage.admin_archives import AdminArchiveService
from iris_memory_core.storage.backup import verify_backup
from iris_memory_core.storage.migrations import MigrationRunner, default_migrations_path
from iris_memory_core.storage.runtime import verify_schema_compatible
from iris_memory_core.storage.uow import Store


def test_empty_database_and_schema10_upgrade_apply_0011(tmp_path: Path) -> None:
    phase10_dir = tmp_path / "schema11"
    phase10_dir.mkdir()
    for migration in sorted(default_migrations_path().glob("*.sql")):
        if int(migration.name[:4]) <= 11:
            shutil.copy2(migration, phase10_dir / migration.name)
    database = tmp_path / "empty.sqlite3"
    applied = MigrationRunner(database, phase10_dir).migrate()
    assert applied[-1].version == 11
    with sqlite3.connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "consolidation_windows",
        "reflection_records",
        "reflection_evidence",
        "cognitive_candidates",
        "provider_outcomes",
        "provider_circuit_states",
        "provider_budget_states",
        "service_credentials",
        "service_events",
        "recall_usage_activations",
    } <= tables

    legacy_dir = tmp_path / "schema10"
    legacy_dir.mkdir()
    for migration in sorted(default_migrations_path().glob("*.sql")):
        if int(migration.name[:4]) <= 10:
            shutil.copy2(migration, legacy_dir / migration.name)
    upgraded = tmp_path / "upgraded.sqlite3"
    MigrationRunner(upgraded, legacy_dir).migrate()
    with sqlite3.connect(upgraded) as connection:
        connection.execute(
            "INSERT INTO tenants(id,status,created_us,created_at) "
            "VALUES ('preserved','active',1,'1970-01-01T00:00:00Z')"
        )
        connection.commit()
    assert [item.version for item in MigrationRunner(upgraded, phase10_dir).migrate()] == [11]
    with sqlite3.connect(upgraded) as connection:
        assert connection.execute("SELECT status FROM tenants WHERE id='preserved'").fetchone() == (
            "active",
        )


def test_schema_compatibility_requires_dependency_lifecycle_schema() -> None:
    verify_schema_compatible(24)
    for schema in (10, 11, 12, 13, 14, 15, 16, 17):
        with pytest.raises(SchemaIncompatibleError):
            verify_schema_compatible(schema)
    with pytest.raises(SchemaIncompatibleError):
        verify_schema_compatible(25)


def test_credentials_are_hashed_rotatable_and_never_exported(
    clocked_store: Store, clocked_tenant_id: str, tmp_path: Path
) -> None:
    token = "secret-bearer-token-that-is-long-enough"
    service = CredentialService(clocked_store, clocked_store.clock)
    first = service.issue(
        token,
        tenant_id=clocked_tenant_id,
        app_instance_id="host-a",
        plane="application",
        expires_us=clocked_store.clock.now_us() + 10_000_000,
    )
    assert first.token_sha256 == token_digest(token)
    with sqlite3.connect(clocked_store.runtime.database) as connection:
        dump = "\n".join(connection.iterdump())
    assert token not in dump
    rotated_token = "replacement-bearer-token-that-is-long"
    service.issue(
        rotated_token,
        tenant_id=clocked_tenant_id,
        app_instance_id="host-a",
        plane="application",
        expires_us=clocked_store.clock.now_us() + 10_000_000,
        rotated_from_id=first.id,
    )
    with pytest.raises(AccessDeniedError):
        service.authenticate(token)
    assert service.authenticate(rotated_token).tenant_id == clocked_tenant_id

    archives = AdminArchiveService(
        clocked_store,
        backup_root=tmp_path / "backups",
        export_root=tmp_path / "exports",
    )
    backup = archives.create_backup("backup-1")
    export = archives.create_export("export-1", tenant_id=clocked_tenant_id)
    assert backup["kind"] == "backup" and export["kind"] == "export"
    assert verify_backup(tmp_path / "backups" / "backup-1").ok
    exported = (tmp_path / "exports" / "export-1" / "export.jsonl").read_text()
    assert token not in exported and rotated_token not in exported
    assert "service_credentials" not in exported
    assert (
        json.loads((tmp_path / "exports" / "export-1" / "manifest.json").read_text())[
            "operation_id"
        ]
        == "export-1"
    )


def test_provider_budget_and_circuit_are_shared_across_workers(
    clocked_store: Store, clocked_tenant_id: str
) -> None:
    limits: dict[ProviderKind, CognitiveProviderLimits] = {
        "extraction": CognitiveProviderLimits(
            max_retries=0,
            daily_budget_microunits=3,
            breaker_failures=1,
            breaker_cooldown_seconds=60,
        )
    }
    governors = [
        ProviderGovernance(
            limits,
            durable_state=DurableProviderState(clocked_store, clocked_store.clock),
        )
        for _ in range(2)
    ]
    governors[0].call(
        "extraction",
        tenant_id=clocked_tenant_id,
        agent_id=None,
        request_material={"request": 1},
        estimated_cost_microunits=2,
        invoke=lambda _timeout: {"ok": True},
    )
    with pytest.raises(ProviderUnavailableError) as budget:
        governors[1].call(
            "extraction",
            tenant_id=clocked_tenant_id,
            agent_id=None,
            request_material={"request": 2},
            estimated_cost_microunits=2,
            invoke=lambda _timeout: {"ok": True},
        )
    assert budget.value.details["reason_code"] == "budget_exhausted"

    failure_limits: dict[ProviderKind, CognitiveProviderLimits] = {
        "summarization": CognitiveProviderLimits(
            max_retries=0,
            breaker_failures=1,
            breaker_cooldown_seconds=60,
        )
    }
    failure_governors = [
        ProviderGovernance(
            failure_limits,
            durable_state=DurableProviderState(clocked_store, clocked_store.clock),
        )
        for _ in range(2)
    ]
    with pytest.raises(ProviderUnavailableError):
        failure_governors[0].call(
            "summarization",
            tenant_id=clocked_tenant_id,
            agent_id=None,
            request_material={"request": 3},
            estimated_cost_microunits=1,
            invoke=lambda _timeout: (_ for _ in ()).throw(RuntimeError("transport")),
        )
    assert failure_governors[1].circuit_state(clocked_tenant_id, "summarization") == "open"
    with pytest.raises(ProviderUnavailableError) as circuit:
        failure_governors[1].call(
            "summarization",
            tenant_id=clocked_tenant_id,
            agent_id=None,
            request_material={"request": 4},
            estimated_cost_microunits=1,
            invoke=lambda _timeout: {"unreachable": True},
        )
    assert circuit.value.details["reason_code"] == "circuit_open"
