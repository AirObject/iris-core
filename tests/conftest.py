"""Shared fixtures: an isolated migrated store per test."""

from __future__ import annotations

from pathlib import Path

import pytest

from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store


def local_allowed_versions() -> tuple[tuple[int, int, int], ...]:
    """Integration tests pin the local dev runtime explicitly.

    The official production allowlist stays the default in ``SQLiteRuntime``;
    the bundled dev SQLite (3.50.4 here) is deliberately not in it, so tests
    must acknowledge it the same way a deployment pin would.
    """
    return (sqlite_runtime_version(),)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "canonical.sqlite3"


@pytest.fixture
def store(database: Path) -> Store:
    MigrationRunner(database).migrate()
    runtime = SQLiteRuntime(database, allowed_versions=local_allowed_versions())
    return Store(runtime)


@pytest.fixture
def idempotency(store: Store) -> IdempotencyManager:
    return IdempotencyManager(store)


@pytest.fixture
def provisioning(store: Store, idempotency: IdempotencyManager) -> ProvisioningService:
    return ProvisioningService(store, idempotency)


@pytest.fixture
def identities(store: Store) -> IdentityService:
    return IdentityService(store)


@pytest.fixture
def admin_access(tenant_id: str) -> AccessContext:
    return AccessContext(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        admin=True,
        capabilities=frozenset({"manage"}),
    )


@pytest.fixture
def tenant_id(store: Store) -> str:
    tenant = "tenant-a"
    with store.write() as tx:
        tx.insert_tenant(tenant, status="active")
    return tenant


def access_for(
    tenant_id: str,
    *,
    agent_ids: frozenset[str] = frozenset(),
    space_group_ids: frozenset[str] = frozenset(),
    space_ids: frozenset[str] = frozenset(),
    consent_entities: frozenset[str] = frozenset(),
    admin: bool = False,
) -> AccessContext:
    return AccessContext(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        agent_ids=agent_ids,
        allowed_space_group_ids=space_group_ids,
        allowed_space_ids=space_ids,
        consent_subject_entity_ids=consent_entities,
        admin=admin,
    )
