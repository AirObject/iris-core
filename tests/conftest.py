"""Shared fixtures: an isolated migrated store per test."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from iris_memory_core.application.backpressure import (
    BackpressureConfig,
    BackpressureGauge,
    FixedDiskProbe,
)
from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.health import HealthService
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.scheduler import SchedulerService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.tasks import TaskService
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


class MutableClock:
    """Deterministic wall clock for observation/schedule/lease tests."""

    def __init__(self, start_us: int = 1_700_000_000_000_000) -> None:
        self._us = start_us

    def now_us(self) -> int:
        return self._us

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._us / 1_000_000, tz=UTC)

    def advance(self, delta_us: int) -> None:
        self._us += delta_us

    def set(self, us: int) -> None:
        self._us = us


@pytest.fixture
def mutable_clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "canonical.sqlite3"


@pytest.fixture
def store(database: Path) -> Store:
    MigrationRunner(database).migrate()
    runtime = SQLiteRuntime(database, allowed_versions=local_allowed_versions())
    return Store(runtime)


@pytest.fixture
def clocked_store(database: Path, mutable_clock: MutableClock) -> Store:
    MigrationRunner(database).migrate()
    runtime = SQLiteRuntime(database, allowed_versions=local_allowed_versions())
    return Store(runtime, clock=mutable_clock)


@pytest.fixture
def generous_gauge(database: Path) -> BackpressureGauge:
    """A gauge whose queue/disk limits never trip under normal test volumes."""
    return BackpressureGauge(
        BackpressureConfig(
            soft_disk_free_bytes=10**12,
            hard_disk_free_bytes=10**11,
            retry_base_delay_us=1_000,
            retry_max_delay_us=10_000,
        ),
        probe=FixedDiskProbe(10**13),
        database_path=database,
    )


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
def observations(clocked_store: Store, generous_gauge: BackpressureGauge) -> ObservationService:
    return ObservationService(clocked_store, gauge=generous_gauge)


@pytest.fixture
def outbox_service(clocked_store: Store, generous_gauge: BackpressureGauge) -> OutboxService:
    return OutboxService(clocked_store, clocked_store.clock, gauge=generous_gauge)


@pytest.fixture
def scheduler(clocked_store: Store) -> SchedulerService:
    return SchedulerService(clocked_store, clocked_store.clock)


@pytest.fixture
def phase4_notes(clocked_store: Store, idempotency: IdempotencyManager) -> NoteService:
    return NoteService(clocked_store, clocked_store.clock, idempotency=idempotency)


@pytest.fixture
def phase4_tasks(clocked_store: Store, idempotency: IdempotencyManager) -> TaskService:
    return TaskService(clocked_store, clocked_store.clock, idempotency=idempotency)


@pytest.fixture
def phase4_events(clocked_store: Store, idempotency: IdempotencyManager) -> CognitiveEventService:
    return CognitiveEventService(clocked_store, clocked_store.clock, idempotency=idempotency)


@pytest.fixture
def surface(clocked_store: Store) -> SurfaceCoordinatorService:
    return SurfaceCoordinatorService(clocked_store, clocked_store.clock)


@pytest.fixture
def health(
    clocked_store: Store, generous_gauge: BackpressureGauge, scheduler: SchedulerService
) -> HealthService:
    return HealthService(
        clocked_store,
        clocked_store.clock,
        gauge=generous_gauge,
        scheduler_lag=scheduler.schedule_lag,
    )


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


@pytest.fixture
def clocked_tenant_id(clocked_store: Store) -> str:
    tenant = "tenant-a"
    with clocked_store.write() as tx:
        tx.insert_tenant(tenant, status="active")
    return tenant


@pytest.fixture
def phase2_agent(clocked_store: Store, clocked_tenant_id: str) -> str:
    """An agent provisioned on the clocked store for Phase 2 tests."""
    from iris_memory_core.application.provisioning import ProvisioningService

    provisioning = ProvisioningService(clocked_store)
    admin = AccessContext(
        clocked_tenant_id,
        app_instance_id="bootstrap",
        admin=True,
        capabilities=frozenset({"manage"}),
    )
    agent = provisioning.create_agent(admin, "Phase2 Agent")
    return agent.id


@pytest.fixture
def phase2_access(clocked_tenant_id: str, phase2_agent: str) -> AccessContext:
    return AccessContext(
        tenant_id=clocked_tenant_id,
        app_instance_id="app-1",
        admin=True,
        agent_ids=frozenset({phase2_agent}),
    )


def access_for(
    tenant_id: str,
    *,
    agent_ids: frozenset[str] = frozenset(),
    space_group_ids: frozenset[str] = frozenset(),
    space_ids: frozenset[str] = frozenset(),
    consent_entities: frozenset[str] = frozenset(),
    admin: bool = False,
    app_instance_id: str = "app-1",
) -> AccessContext:
    return AccessContext(
        tenant_id=tenant_id,
        app_instance_id=app_instance_id,
        agent_ids=agent_ids,
        allowed_space_group_ids=space_group_ids,
        allowed_space_ids=space_ids,
        consent_subject_entity_ids=consent_entities,
        admin=admin,
    )
