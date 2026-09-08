"""Schema22 retained typed payloads and Provider foreign keys survive W07 migration."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from iris_memory_core.api.console.composition import assemble
from iris_memory_core.application.console.provider_configs import (
    ProviderConfigCommands,
    ProviderSecretInput,
)
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.domain.console_operations import (
    ConsoleOperation,
    ProviderOperationPayload,
    TrustedBackupPayload,
)
from iris_memory_core.domain.provider_configs import EmbeddingDefinition
from iris_memory_core.domain.vector import VectorSpaceConfig
from iris_memory_core.providers.configured import ConfiguredEmbeddingFactory
from iris_memory_core.providers.secrets import ProviderSecrets
from iris_memory_core.storage.migrations import MigrationRunner, default_migrations_path
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store


def test_schema22_populated_operations_provider_refs_and_recall_null_history(
    tmp_path: Path,
) -> None:
    old = tmp_path / "schema22"
    old.mkdir()
    for path in default_migrations_path().glob("*.sql"):
        if int(path.name[:4]) <= 22:
            shutil.copy2(path, old / path.name)
    database = tmp_path / "canonical.sqlite3"
    MigrationRunner(database, old).migrate()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
        verify_schema_window=False,
    )
    tenant = str(uuid4())
    with store.write() as tx:
        tx.insert_tenant(tenant, status="active")
    security, _ = assemble(store)
    grant = OperatorGrant(
        security.permissions,
        Selector("all"),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        allow_restricted=True,
        data_purposes=frozenset({"console.manage"}),
    )
    _, token = security.issue_offline(
        tenant_id=tenant,
        label="migration",
        description="",
        template="owner",
        grant=grant,
        expires_us=store.clock.now_us() + 3_600_000_000,
    )
    principal, _ = security.login(token, client_digest="0" * 64)
    factory = ConfiguredEmbeddingFactory(
        ProviderSecrets(
            allowed_references={tenant: frozenset({"env:W07_MIGRATION_KEY"})},
            environment={"W07_MIGRATION_KEY": "private-migration-fixture"},
        )
    )
    config = ProviderConfigCommands(security, factory.secrets, factory).create(
        principal,
        definition=EmbeddingDefinition(
            "openai-compatible",
            "https://migration.example/v1/embeddings",
            VectorSpaceConfig(model="migration", dimension=2),
        ),
        reason="operator_request",
        secret=ProviderSecretInput("secret_ref", "env:W07_MIGRATION_KEY"),
        idempotency_key="migration-provider",
    )
    records = []
    for kind in ("trusted_backup", "embedding_provider"):
        now = store.clock.now_us()
        identifier = str(uuid4())
        records.append(
            ConsoleOperation(
                id=identifier,
                tenant_id=tenant,
                key_id=principal.key.id,
                key_revision=principal.key.revision,
                grant_fingerprint=principal.key.grant.fingerprint,
                session_id=principal.session.id,
                session_epoch=principal.session.epoch,
                kind=kind,
                reason_code="operator_request",
                status="queued",
                revision=1,
                processed=0,
                total=2,
                current_job_id=None,
                blocked_reason=None,
                created_us=now,
                updated_us=now,
                started_us=None,
                finished_us=None,
                backup=TrustedBackupPayload() if kind == "trusted_backup" else None,
                provider=ProviderOperationPayload(str(config["id"]), 1, "probe", 0)
                if kind == "embedding_provider"
                else None,
            )
        )
    with store.write() as tx:
        for record in records:
            tx.console_operations.insert(record)
        current = tx.providers.get(tenant, str(config["id"]))
        assert current is not None
        tx.providers.advance(
            replace(
                current,
                status="probing",
                revision=current.revision + 1,
                current_operation_id=records[1].id,
            ),
            expected_revision=current.revision,
        )
    MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
    upgraded = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
    with upgraded.read() as tx:
        assert [tx.console_operations.get(tenant, record.id) for record in records] == records
        retained = tx.providers.get(tenant, str(config["id"]))
        assert retained is not None and retained.current_operation_id == records[1].id
        assert tx.raw().execute("PRAGMA foreign_key_check").fetchall() == []
        assert {row[1] for row in tx.raw().execute("PRAGMA table_info(recall_requests)")} >= {
            "duration_us",
            "statistics_json",
        }
        assert tx.statistics.coverage_from_us() > 0


def test_schema23_populated_draft_and_discard_ledger_survive_statistics_upgrade(
    tmp_path: Path,
) -> None:
    from iris_memory_core.application.forget import ForgetService
    from iris_memory_core.application.persona_draft_deletion import discard_draft_in_tx
    from iris_memory_core.application.provisioning import ProvisioningService
    from iris_memory_core.domain.access import AccessContext
    from iris_memory_core.storage.backup import BackupService
    from tests.integration.persona.test_persona_draft_storage import arguments
    from tests.migration_support import migrate_through

    database = tmp_path / "schema23.sqlite3"
    migrate_through(database, 23)
    old = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
        verify_schema_window=False,
    )
    service = ProvisioningService(old)
    service.create_tenant("draft-tenant")
    access = AccessContext("draft-tenant", "trusted-migration", admin=True)
    agent = service.create_agent(access, "Draft predecessor")
    with old.write() as tx:
        live = tx.persona_drafts.create(**arguments({"agent": agent.id}))
        deleted = tx.persona_drafts.create(**arguments({"agent": agent.id}))
        discard_draft_in_tx(
            tx,
            tenant_id="draft-tenant",
            agent_id=agent.id,
            draft_id=deleted.id,
            expected_revision=1,
            actor="console:migration",
            request_key="discard-before-upgrade",
        )
        before = tx.persona_drafts.get("draft-tenant", agent.id, deleted.id)
    ledger = ForgetService(old, old.clock).export_deletion_ledger(access)
    backup = BackupService(old)
    snapshot = tmp_path / "verified23"
    backup.create_backup(snapshot)
    assert backup.verify_backup(snapshot).ok
    assert [
        migration.version
        for migration in MigrationRunner(database).migrate(
            allow_offline=True,
            backup_performed=True,
        )
    ] == [24, 25]
    upgraded = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
    with upgraded.read() as tx:
        assert tx.persona_drafts.get("draft-tenant", agent.id, live.id) == live
        assert tx.persona_drafts.get("draft-tenant", agent.id, deleted.id) == before
        assert tx.is_tombstoned("draft-tenant", "persona_draft", deleted.id)
        assert tx.personas.current(agent.id).revision == 1
        assert tx.raw().execute("PRAGMA foreign_key_check").fetchall() == []
    assert ForgetService(upgraded, upgraded.clock).export_deletion_ledger(access) == ledger
