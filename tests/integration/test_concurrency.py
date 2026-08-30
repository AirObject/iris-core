"""Concurrency: one winner per expected revision; watermarks stay monotonic."""

from __future__ import annotations

import threading

from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import RevisionMismatchError
from iris_memory_core.storage.uow import Store

WRITERS = 50


def test_fifty_concurrent_writers_same_expected_revision_exactly_one_wins(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    group = provisioning.create_space_group(admin_access, "contended", reason="ops: setup")
    barrier = threading.Barrier(WRITERS)
    successes: list[str] = []
    mismatches: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait()
        try:
            with store.write() as tx:
                tx.update_space_group(
                    group.id,
                    name=f"writer-{index}",
                    description="",
                    expected_revision=1,
                    actor=f"writer-{index}",
                    reason_code="concurrent update",
                )
            with lock:
                successes.append(f"writer-{index}")
        except RevisionMismatchError as error:
            with lock:
                mismatches.append(error.code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(WRITERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(mismatches) == WRITERS - 1
    assert set(mismatches) == {"revision_mismatch"}
    with store.read() as tx:
        final = tx.get_space_group(group.id)
    assert final.revision == 2


def test_concurrent_watermark_advances_never_collide(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    agent = provisioning.create_agent(admin_access, "counter")
    barrier = threading.Barrier(WRITERS)
    results: list[int] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        with store.write() as tx:
            seq = tx.advance_watermark(
                admin_access.tenant_id, agent.id, (("note", f"n-{threading.get_ident()}", 1),)
            )
        with lock:
            results.append(seq)

    threads = [threading.Thread(target=attempt) for _ in range(WRITERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == list(range(results[0], results[0] + WRITERS))
    with store.read() as tx:
        state = tx.watermark(admin_access.tenant_id, agent.id)
    assert state is not None
    assert state.current_seq == max(results)
