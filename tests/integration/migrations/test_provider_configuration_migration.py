"""Real Schema21 history upgrade and Schema22 immutable configuration constraints."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.api.console.composition import assemble
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.domain.console_operations import TrustedBackupPayload
from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.provider_configs import (
    EmbeddingDefinition,
    ProviderConfig,
    ProviderConfigRevision,
    ProviderLimits,
)
from iris_memory_core.domain.vector import VectorSpaceConfig
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.console_operations import ConsoleOperationRepository
from iris_memory_core.storage.migrations import (
    MigrationNotOnlineSafe,
    MigrationRunner,
    default_migrations_path,
)
from iris_memory_core.storage.provider_configs import ProviderConfigRepository
from iris_memory_core.storage.runtime import SQLiteRuntime
from iris_memory_core.storage.uow import Store
from tests.conftest import local_allowed_versions
from tests.integration.migrations.test_typed_operations_migration import (
    assert_history,
    legacy_store,
)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    database = tmp_path / "configs.sqlite3"
    MigrationRunner(database).migrate()
    store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    with store.write() as tx:
        tx.insert_tenant("provider-tenant", status="active")
        tx.insert_tenant("other-tenant", status="active")
    security, _ = assemble(store)
    key, _ = security.issue_offline(
        tenant_id="provider-tenant",
        label="Provider fixture",
        description="Isolated provider migration test",
        template="owner",
        grant=OperatorGrant(
            security.permissions,
            Selector("all"),
            Selector("all"),
            Selector("all"),
            Selector("all"),
            data_purposes=frozenset({"console.manage"}),
        ),
        expires_us=store.clock.now_us() + 3_600_000_000,
    )
    return {"store": store, "key": key, "tenant": "provider-tenant"}


def draft(
    world: dict[str, Any], identifier: str = "config-one"
) -> tuple[ProviderConfig, ProviderConfigRevision]:
    now = world["store"].clock.now_us()
    config = ProviderConfig(
        id=identifier,
        tenant_id=world["tenant"],
        status="draft",
        revision=1,
        content_revision=1,
        created_us=now,
        updated_us=now,
        created_by=world["key"].id,
    )
    revision = ProviderConfigRevision(
        tenant_id=world["tenant"],
        config_id=identifier,
        content_revision=1,
        definition=EmbeddingDefinition(
            adapter="deterministic",
            endpoint="",
            space=VectorSpaceConfig(model="fixture", dimension=2),
        ),
        secret=None,
        created_us=now,
        created_by=world["key"].id,
    )
    with world["store"].write() as tx:
        ProviderConfigRepository(tx.raw()).insert(config, revision)
    return config, revision


def test_schema21_backup_upgrade_preserves_forget_backup_and_problem_history(
    tmp_path: Path,
) -> None:
    store, history = legacy_store(tmp_path)
    prefix = tmp_path / "through21"
    prefix.mkdir()
    for source in default_migrations_path().glob("*.sql"):
        if int(source.name[:4]) <= 21:
            shutil.copyfile(source, prefix / source.name)
    backup = BackupService(store)
    before20 = tmp_path / "verified20"
    backup.create_backup(before20)
    assert backup.verify_backup(before20).ok
    assert [
        m.version
        for m in MigrationRunner(store.runtime.database, prefix).migrate(
            allow_offline=True, backup_performed=True
        )
    ] == [21]
    queued = replace(
        history[0],
        id="historical-backup",
        kind="trusted_backup",
        status="queued",
        revision=1,
        processed=0,
        total=2,
        current_job_id=None,
        blocked_reason=None,
        started_us=None,
        finished_us=None,
        forget=None,
        backup=TrustedBackupPayload(),
    )
    with store.write() as tx:
        tx.console_operations.insert(queued)
    before21 = tmp_path / "verified21"
    backup.create_backup(before21)
    assert backup.verify_backup(before21).ok
    runner = MigrationRunner(store.runtime.database)
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate()
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate(allow_offline=True)
    assert runner.current_version() == 21
    assert [m.version for m in runner.migrate(allow_offline=True, backup_performed=True)] == [
        22,
        23,
    ]
    assert_history(store.runtime.database, history)
    with store.read() as tx:
        assert tx.console_operations.get(queued.tenant_id, queued.id) == queued
        assert ProviderConfigRepository(tx.raw()).list_configs(queued.tenant_id) == ()
        assert tx.raw().execute("PRAGMA foreign_key_check").fetchall() == []
    restored = tmp_path / "restore21"
    assert backup.restore_backup(before21, restored).check.ok
    restored_db = restored / "canonical.sqlite3"
    # Upgrade the actual isolated predecessor restore; its queued backup is
    # correctly invalidated by the existing restore policy before migration.
    assert [
        m.version
        for m in MigrationRunner(restored_db).migrate(allow_offline=True, backup_performed=True)
    ] == [22, 23]
    assert_history(restored_db, history)
    with sqlite3.connect(restored_db) as connection:
        connection.row_factory = sqlite3.Row
        restored_operation = ConsoleOperationRepository(connection).get(queued.tenant_id, queued.id)
        assert restored_operation is not None
        assert restored_operation.status == "blocked"
        assert restored_operation.blocked_reason == "restore_requires_review"
    for source in sorted(default_migrations_path().glob("*.sql"))[:21]:
        assert (
            hashlib.sha256(source.read_bytes()).digest()
            == hashlib.sha256(
                subprocess.check_output(["git", "show", f"HEAD:migrations/{source.name}"])
            ).digest()
        )


def test_draft_patch_preserves_immutable_content_and_resets_probe_state(
    world: dict[str, Any],
) -> None:
    config, first = draft(world)
    with world["store"].write() as tx:
        repository = ProviderConfigRepository(tx.raw())
        probed = replace(config, status="probed", revision=2)
        repository.advance(probed, expected_revision=1)
        second = replace(
            first,
            content_revision=2,
            definition=replace(
                first.definition, label="Revised", limits=ProviderLimits(max_qps=25)
            ),
        )
        changed = repository.append_revision(second, expected_revision=2, now_us=config.updated_us)
        assert changed.status == "draft" and changed.revision == 3
        assert changed.content_revision == 2 and changed.latest_probe_id is None
        assert repository.history(config.tenant_id, config.id) == (second, first)
        for statement in (
            "UPDATE provider_config_revisions SET definition_json='{}'",
            "DELETE FROM provider_config_revisions",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                tx.raw().execute(statement)
        assert repository.revision(config.tenant_id, config.id, 1) == first


def test_actual_concurrent_draft_writers_commit_exactly_one_revision(world: dict[str, Any]) -> None:
    config, first = draft(world)

    def patch(index: int) -> str:
        try:
            with world["store"].write() as tx:
                repository = ProviderConfigRepository(tx.raw())
                revision = replace(
                    first,
                    content_revision=2,
                    definition=replace(first.definition, label=f"Writer {index}"),
                )
                repository.append_revision(revision, expected_revision=1, now_us=config.updated_us)
            return "committed"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(patch, range(6)))
    assert outcomes.count("committed") == 1 and outcomes.count("conflict") == 5
    with world["store"].read() as tx:
        repository = ProviderConfigRepository(tx.raw())
        assert [
            row.content_revision for row in repository.history(config.tenant_id, config.id)
        ] == [2, 1]


def test_single_active_and_single_activation_enforced_in_database(world: dict[str, Any]) -> None:
    first, _ = draft(world, "first")
    second, _ = draft(world, "second")
    with world["store"].write() as tx:
        repository = ProviderConfigRepository(tx.raw())
        repository.advance(replace(first, status="active", revision=2), expected_revision=1)
        with pytest.raises(sqlite3.IntegrityError):
            repository.advance(replace(second, status="active", revision=2), expected_revision=1)
        repository.advance(replace(second, status="activating", revision=2), expected_revision=1)
        with pytest.raises(sqlite3.IntegrityError):
            repository.advance(replace(first, status="activating", revision=3), expected_revision=2)
        first_current = repository.get(first.tenant_id, first.id)
        second_current = repository.get(second.tenant_id, second.id)
        assert first_current is not None and first_current.status == "active"
        assert second_current is not None and second_current.status == "activating"


def test_tenant_and_deep_page_history_boundaries(world: dict[str, Any]) -> None:
    for index in range(65):
        draft(world, f"config-{index:03}")
    with world["store"].read() as tx:
        repository = ProviderConfigRepository(tx.raw())
        seen: list[str] = []
        after = None
        for _ in range(23):
            rows = repository.list_configs(world["tenant"], limit=3, after=after)
            seen.extend(row.id for row in rows)
            if rows:
                after = rows[-1].created_us, rows[-1].id
        assert len(seen) == len(set(seen)) == 65
        assert repository.list_configs("other-tenant") == ()
        assert repository.get("other-tenant", seen[0]) is None
        assert repository.revision("other-tenant", seen[0], 1) is None
        with pytest.raises(ValueError):
            repository.list_configs(world["tenant"], limit=10000)


def test_probe_budget_survives_transactions_and_clock_rollback(world: dict[str, Any]) -> None:
    def reserve(now: int) -> None:
        with world["store"].write() as tx:
            ProviderConfigRepository(tx.raw()).reserve_probe_budget(
                world["tenant"],
                now_us=now,
                input_chars=20,
                max_attempts=2,
                max_input_chars=40,
                window_us=100,
            )

    reserve(1000)
    reserve(1001)
    for now in (999, 1099):
        with pytest.raises(ConflictError) as error:
            reserve(now)
        assert error.value.details["kind"] == "provider_budget_exhausted"
    reserve(1100)


def test_hot_limits_preserve_space_but_endpoint_and_model_require_rebuild() -> None:
    first = EmbeddingDefinition(
        adapter="openai-compatible",
        endpoint="https://first.example/v1/embeddings",
        space=VectorSpaceConfig(model="model", dimension=32),
    )
    assert replace(first, limits=ProviderLimits(max_qps=3)).identity_hash() == first.identity_hash()
    assert (
        replace(first, endpoint="https://second.example/v1/embeddings").identity_hash()
        != first.identity_hash()
    )
    assert (
        replace(first, space=replace(first.space, model="other")).identity_hash()
        != first.identity_hash()
    )
    assert (
        replace(first, space=replace(first.space, dimension=64)).identity_hash()
        != first.identity_hash()
    )
