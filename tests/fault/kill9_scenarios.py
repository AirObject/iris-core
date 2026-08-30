"""Kill -9 scenario runner: executed as a subprocess by test_kill9.

Each scenario drives the real Phase 2 spine against a migrated database and
kills itself with a genuine SIGKILL at a named transaction boundary. The
parent process then reopens the database and asserts the joint invariants:
either the whole logical effect landed, or none of it did.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
from pathlib import Path

from iris_memory_core.application.outbox import JobCommit, JobWork
from iris_memory_core.application.ports import Transaction
from iris_memory_core.domain.jobs import NewOutboxJob, OutboxJob
from iris_memory_core.storage.uow import Store


def _self_kill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _die_before_commit(store: Store) -> None:
    def die(connection: sqlite3.Connection) -> None:
        _self_kill()

    store._commit_with_retry = die  # type: ignore[method-assign]


class Spine:
    """Typed bundle of the services each scenario drives."""

    def __init__(self, db_path: str) -> None:
        from iris_memory_core.application.backpressure import (
            BackpressureConfig,
            BackpressureGauge,
            FixedDiskProbe,
        )
        from iris_memory_core.application.observation import ObservationService
        from iris_memory_core.application.outbox import OutboxService
        from iris_memory_core.application.provisioning import ProvisioningService
        from iris_memory_core.application.scheduler import SchedulerService
        from iris_memory_core.domain.access import AccessContext
        from iris_memory_core.storage.migrations import MigrationRunner
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version

        database = Path(db_path)
        MigrationRunner(database).migrate()
        runtime = SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),))
        self.store = Store(runtime)
        self.gauge = BackpressureGauge(
            BackpressureConfig(soft_disk_free_bytes=10**12, hard_disk_free_bytes=10**11),
            probe=FixedDiskProbe(10**13),
            database_path=database,
        )
        with self.store.write() as tx:
            tx.insert_tenant("t1", status="active")
        admin = AccessContext("t1", app_instance_id="bootstrap", admin=True)
        agent = ProvisioningService(self.store).create_agent(admin, "A")
        self.access = AccessContext(
            "t1", app_instance_id="app-1", admin=True, agent_ids=frozenset({agent.id})
        )
        self.agent_id = agent.id
        self.observations = ObservationService(self.store, gauge=self.gauge)
        self.outbox = OutboxService(self.store, self.store.clock, gauge=self.gauge)
        self.scheduler = SchedulerService(self.store, self.store.clock)


def setup(db_path: str) -> Spine:
    return Spine(db_path)


def business_result_work(owner: str) -> JobWork:
    """The ONE business closure every run of the worker scenario executes.

    The crash run and the recovery run share it on purpose: the invariant
    under test is that re-execution after recovery converges to EXACTLY ONE
    business effect — the audit row is the observable logical effect.
    """

    def work(job: OutboxJob) -> JobCommit:
        def commit(tx: Transaction) -> None:
            tx.audit(
                tenant_id="t1",
                actor=f"worker:{owner}",
                action="kill9.business_result",
                resource_type="probe",
                resource_id="probe-1",
                reason_code="fault",
            )

        return commit

    return work


def scenario_observe(db_path: str, crash_at: str) -> None:
    ctx = setup(db_path)
    record = {
        "agent_id": ctx.agent_id,
        "role": "user",
        "kind": "message.text",
        "idempotency_key": "kill9-1",
        "occurred_us": 1,
        "committed_us": 2,
        "content": "payload",
        "source_stream": "s1",
        "source_cursor": "1",
    }
    if crash_at == "pre_tx":
        _self_kill()
    if crash_at == "post_write_pre_commit":
        _die_before_commit(ctx.store)
    ctx.observations.observe_batch(ctx.access, [record])
    if crash_at == "post_commit":
        # Transaction committed; the response never made it out.
        _self_kill()


def scenario_worker(db_path: str, crash_at: str) -> None:
    ctx = setup(db_path)
    outbox = ctx.outbox
    outbox.enqueue(
        NewOutboxJob(
            tenant_id="t1",
            job_kind="maintenance.selfcheck",
            aggregate_type="probe",
            aggregate_id="probe-1",
            source_revision=1,
            payload={"version": 1},
            dedupe_key="kill9-job",
        )
    )
    if crash_at == "post_claim":
        batch = outbox.claim("doomed")
        assert len(batch.jobs) == 1
        _self_kill()

    work = business_result_work("doomed")

    batch = outbox.claim("doomed")
    assert len(batch.jobs) == 1
    if crash_at == "post_work_pre_commit":
        # Arm the kill AFTER the claim transaction committed: the business
        # handler's write and the fenced completion CAS are staged in the
        # open transaction, and the process dies before THAT commit — the
        # boundary the atomicity claim is about.
        _die_before_commit(ctx.store)
        outbox.execute(batch.jobs[0], work, owner="doomed")
        return
    for job in batch.jobs:
        outbox.execute(job, work, owner="doomed")


def scenario_tick(db_path: str, crash_at: str) -> None:
    ctx = setup(db_path)
    scheduler = ctx.scheduler
    schedule = scheduler.create_schedule(
        ctx.access,
        agent_id=ctx.agent_id,
        job_kind="maintenance.selfcheck",
        spec={"kind": "interval", "every_seconds": 60},
        reason="fault",
    )
    if crash_at == "post_tick_pre_commit":
        _die_before_commit(ctx.store)
    scheduler.advance(now_us=schedule.next_tick_at_us + 1)
    if crash_at == "post_advance":
        _self_kill()


SCENARIOS = {
    "observe": scenario_observe,
    "worker": scenario_worker,
    "tick": scenario_tick,
}


def main(argv: list[str]) -> int:
    scenario, db_path, crash_at = argv[1], argv[2], argv[3]
    SCENARIOS[scenario](db_path, crash_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
