"""Phase 9 Persona Current read p95 <= 20 ms."""

from __future__ import annotations

import statistics
import time

from iris_memory_core.application.persona import PERSONA_READ_CAPABILITY, PersonaService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock


def test_persona_current_read_p95_under_20ms(
    clocked_store: Store, mutable_clock: MutableClock
) -> None:
    tenant_id = "persona-latency"
    provisioning = ProvisioningService(clocked_store)
    provisioning.create_tenant(tenant_id)
    provisioner = AccessContext(tenant_id=tenant_id, app_instance_id="provisioner", admin=True)
    agent = provisioning.create_agent(provisioner, "Iris")
    access = AccessContext(
        tenant_id=tenant_id,
        app_instance_id="latency-host",
        agent_ids=frozenset({agent.id}),
        capabilities=frozenset({PERSONA_READ_CAPABILITY}),
    )
    service = PersonaService(clocked_store, mutable_clock)
    service.current(access, agent.id)  # warm filesystem and SQLite page cache
    samples: list[float] = []
    for _ in range(200):
        started = time.perf_counter()
        view = service.current(access, agent.id)
        samples.append((time.perf_counter() - started) * 1_000)
        assert view.revision.revision == 1
    p95 = sorted(samples)[int(len(samples) * 0.95) - 1]
    print(f"\npersona-current p95={p95:.2f}ms median={statistics.median(samples):.2f}ms")
    assert p95 <= 20.0
