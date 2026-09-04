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
from typing import TYPE_CHECKING

from iris_memory_core.application.outbox import JobCommit, JobWork
from iris_memory_core.application.ports import Transaction
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.jobs import NewOutboxJob, OutboxJob
from iris_memory_core.storage.uow import Store

if TYPE_CHECKING:
    from iris_memory_core.application.outbox import OutboxService
    from iris_memory_core.application.reflection import ReflectionPipeline


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


class Phase3Spine(Spine):
    """The Phase 3 services on top of the Phase 2 spine."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        from iris_memory_core.application.focus import FocusService
        from iris_memory_core.application.recent import RecentContextService
        from iris_memory_core.application.state import StateService
        from iris_memory_core.storage.idempotency import IdempotencyManager

        self.idem = IdempotencyManager(self.store)
        self.states = StateService(
            self.store, self.store.clock, gauge=self.gauge, idempotency=self.idem
        )
        self.recent = RecentContextService(self.store, self.store.clock)
        self.focus = FocusService(self.store, self.store.clock, idempotency=self.idem)
        with self.store.write() as tx:
            self.space_id = tx.insert_space("t1", "chat_group").id
            self.session_id = tx.insert_session("t1", self.space_id, actor="fault").id
        self.access = AccessContext(
            tenant_id="t1",
            app_instance_id="app-1",
            admin=True,
            agent_ids=frozenset({self.agent_id}),
            allowed_space_ids=frozenset({self.space_id}),
        )


def _die_on(method_owner: type, method_name: str) -> None:
    """Kill right BEFORE a repository method executes inside the open tx."""

    def die_first(*args: object, **kwargs: object) -> None:
        _self_kill()

    setattr(method_owner, method_name, die_first)


def scenario_state_put(db_path: str, crash_at: str) -> None:
    """State PUT at: pre-tx / pre-pointer-CAS / pre-commit / post-commit."""
    from iris_memory_core.storage import cognitive

    ctx = Phase3Spine(db_path)
    if crash_at == "pre_tx":
        _self_kill()
    if crash_at == "pre_pointer":
        # Creation wires its pointer through set_initial_pointer; the CAS
        # path (advance_pointer) only serves updates.
        _die_on(cognitive.StateRepository, "set_initial_pointer")
    if crash_at == "pre_commit":
        _die_before_commit(ctx.store)
    ctx.states.put(
        ctx.access,
        "environment",
        "fault.key",
        agent_id=ctx.agent_id,
        value={"v": 1},
        source_authority="host",
        idempotency_key="state-fault",
        observed_us=1_000,
        ttl_us=0,
    )
    if crash_at == "post_commit":
        _self_kill()


def scenario_focus_transition(db_path: str, crash_at: str) -> None:
    """Focus create+transition at: pre-commit rollback / post-commit."""
    ctx = Phase3Spine(db_path)
    created = ctx.focus.create(
        ctx.access,
        agent_id=ctx.agent_id,
        kind="goal",
        summary="fault item",
        idempotency_key="focus-fault-1",
    )
    if crash_at == "pre_commit":
        _die_before_commit(ctx.store)
    ctx.focus.transition(
        ctx.access,
        created.item_id,
        "dormant",
        expected_revision=1,
        reason="fault",
        idempotency_key="ik-fx-1",
    )
    if crash_at == "post_commit":
        _self_kill()


def scenario_recent_rebuild(db_path: str, crash_at: str) -> None:
    """Projection shadow swap at: pre-generation / pre-swap / post-commit."""
    from iris_memory_core.storage import cognitive

    ctx = Phase3Spine(db_path)
    ctx.observations.observe_batch(
        ctx.access,
        [
            {
                "agent_id": ctx.agent_id,
                "role": "user",
                "kind": "message.text",
                "idempotency_key": "recent-fault",
                "occurred_us": 1,
                "committed_us": 2,
                "content": "window content",
                "space_id": ctx.space_id,
                "session_id": ctx.session_id,
            }
        ],
    )
    if crash_at == "pre_generation":
        _die_on(cognitive.RecentContextRepository, "insert_generation")
    if crash_at == "pre_swap":
        _die_on(cognitive.RecentContextRepository, "swap_pointer")
    if crash_at == "pre_commit":
        _die_before_commit(ctx.store)
    ctx.recent.rebuild(
        ctx.access,
        agent_id=ctx.agent_id,
        space_id=ctx.space_id,
        session_id=ctx.session_id,
        reason="fault",
    )
    if crash_at == "post_commit":
        _self_kill()


class Phase4Spine:
    """Typed bundle of the Phase 4 services each scenario drives."""

    def __init__(self, db_path: str) -> None:
        from iris_memory_core.application.events import CognitiveEventService
        from iris_memory_core.application.notes import NoteService
        from iris_memory_core.application.tasks import TaskService
        from iris_memory_core.storage.idempotency import IdempotencyManager

        base = Phase3Spine(db_path)
        self.store = base.store
        self.agent_id = base.agent_id
        self.space_id = base.space_id
        self.access = base.access
        idem = IdempotencyManager(self.store)
        self.notes = NoteService(self.store, self.store.clock, idempotency=idem)
        self.tasks = TaskService(self.store, self.store.clock, idempotency=idem)
        self.events = CognitiveEventService(self.store, self.store.clock, idempotency=idem)


def scenario_task_transition(db_path: str, crash_at: str) -> None:
    """Task create+activate at: pre-revision / pre-pointer / pre-commit / post."""
    from iris_memory_core.storage import plans

    ctx = Phase4Spine(db_path)
    created = ctx.tasks.create(
        ctx.access,
        agent_id=ctx.agent_id,
        title="fault task",
        origin="conversation",
        idempotency_key="task-fault-1",
    )
    if crash_at == "pre_revision":
        _die_on(plans.TaskRepository, "insert_task_revision")
    if crash_at == "pre_pointer":
        _die_on(plans.TaskRepository, "advance_task_pointer")
    if crash_at == "pre_commit":
        _die_before_commit(ctx.store)
    ctx.tasks.transition(
        ctx.access,
        created.task_id,
        "activate",
        expected_revision=1,
        origin="explicit_tool",
        reason="fault",
        idempotency_key="task-fault-2",
    )
    if crash_at == "post_commit":
        _self_kill()


def scenario_trigger_scan(db_path: str, crash_at: str) -> None:
    """Occurrence + event creation at: pre-occurrence / pre-event / post."""
    from iris_memory_core.storage import plans

    ctx = Phase4Spine(db_path)
    task = ctx.tasks.create(
        ctx.access,
        agent_id=ctx.agent_id,
        title="scan task",
        origin="explicit_tool",
        idempotency_key="scan-fault-1",
    )
    ctx.tasks.create_trigger(
        ctx.access,
        task.task_id,
        kind="at_time",
        schedule_spec={"at_us": ctx.store.clock.now_us() - 1},
        idempotency_key="scan-fault-2",
    )
    if crash_at == "pre_occurrence":
        _die_on(plans.TaskRepository, "insert_occurrence")
    if crash_at == "pre_commit":
        _die_before_commit(ctx.store)
    with ctx.store.write() as tx:
        ctx.tasks.trigger_scan(tx, tenant_id=ctx.access.tenant_id, agent_id=ctx.agent_id)
    if crash_at == "post_commit":
        _self_kill()


def scenario_event_ack(db_path: str, crash_at: str) -> None:
    """Pull + ACK at: pre-commit / post-commit."""
    ctx = Phase4Spine(db_path)
    now = ctx.store.clock.now_us()
    with ctx.store.write() as tx:
        from iris_memory_core.application.events import CognitiveEventService

        event_id = CognitiveEventService.create_internal(
            tx,
            tenant_id=ctx.access.tenant_id,
            agent_id=ctx.agent_id,
            space_group_id=None,
            space_id=None,
            session_id=None,
            kind="task.due",
            object_type="task",
            object_id="kill9",
            occurrence_id=None,
            scheduled_at_us=now,
            deliver_after_us=now,
        )
    ctx.events.pull(ctx.access, agent_id=ctx.agent_id)
    if crash_at == "pre_commit":
        _die_before_commit(ctx.store)
    ctx.events.ack(ctx.access, event_id, idempotency_key="ack-fault-1")
    if crash_at == "post_commit":
        _self_kill()


def scenario_forget(db_path: str, crash_at: str) -> None:
    """Phase 5: forget must be atomic across tombstones, erasure, ledger,
    watermark and the invalidation outbox — a kill at any boundary leaves
    either the full deletion or none of it."""
    from iris_memory_core.application.forget import ForgetService
    from iris_memory_core.application.memory import ClaimService
    from iris_memory_core.domain.identity import EntityKind
    from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
    from iris_memory_core.storage.idempotency import IdempotencyManager

    ctx = setup(db_path)
    idem = IdempotencyManager(ctx.store)
    claims = ClaimService(ctx.store, ctx.store.clock, idempotency=idem)
    forget = ForgetService(ctx.store, ctx.store.clock, idempotency=idem)
    with ctx.store.write() as tx:
        entity = tx.identities.insert_entity("t1", EntityKind.PERSON, display_name="Bob")
    outcome = ctx.observations.observe_batch(
        ctx.access,
        [
            {
                "agent_id": ctx.agent_id,
                "role": "user",
                "kind": "message.text",
                "idempotency_key": "kill9-forget-obs",
                "occurred_us": 1,
                "committed_us": 2,
                "content": "payload",
            }
        ],
    )
    observation_id = outcome.accepted_observation_ids[0]
    created = claims.remember(
        ctx.access,
        agent_id=ctx.agent_id,
        predicate="likes",
        value={"drink": "tea"},
        canonical_text="Bob likes tea",
        subject_entity_id=entity.id,
        evidence=[
            {"source_type": "observation", "source_id": observation_id, "relation": "supports"}
        ],
        idempotency_key="kill9-forget-claim",
    )
    selector = ForgetSelector(
        kind=ForgetSelectorKind.RESOURCE,
        resource_type="claim",
        resource_id=created.claim_id,
    )
    if crash_at == "pre_tx":
        _self_kill()
    if crash_at == "pre_commit":
        _die_before_commit(ctx.store)
    forget.forget(ctx.access, selector, reason="kill9", idempotency_key="kill9-forget-exec")
    if crash_at == "post_commit":
        _self_kill()


def phase10_context(db_path: str) -> tuple[Store, OutboxService, ReflectionPipeline, str]:
    """Reopen a Phase 10 world: store, outbox, pipeline and the space id.

    Both the crashing child and the recovering parent build the pipeline
    the same way, so recovery re-executes the SAME deterministic work the
    doomed process was in the middle of committing.
    """
    from iris_memory_core.application.outbox import OutboxService
    from iris_memory_core.application.reflection import ReflectionPipeline
    from iris_memory_core.providers.cognitive import (
        DeterministicCognitiveProvider,
        ProviderGovernance,
    )
    from iris_memory_core.storage.migrations import MigrationRunner
    from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version

    database = Path(db_path)
    MigrationRunner(database).migrate()
    runtime = SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),))
    store = Store(runtime)
    with store.read() as tx:
        row = tx.raw().execute("SELECT id FROM spaces LIMIT 1").fetchone()
        observation = tx.raw().execute("SELECT id,agent_id FROM observations LIMIT 1").fetchone()
    space = str(row[0])
    candidate = phase10_candidate("t1", str(observation[1]), space, str(observation[0]))
    provider = DeterministicCognitiveProvider(
        [candidate], summary={"title": "Preferences", "summary": "User prefers tea."}
    )
    pipeline = ReflectionPipeline(
        store,
        store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    return store, OutboxService(store, store.clock), pipeline, space


def phase10_candidate(
    tenant: str, agent: str, space: str, observation_id: str
) -> dict[str, object]:
    """The one evidence-bound claim candidate this scenario reconciles."""
    return {
        "type": "claim",
        "payload": {
            "predicate": "preference.drink",
            "value": "tea",
            "canonical_text": "prefers tea",
            "category": "preference",
            "confidence": 0.7,
            "importance": 0.5,
            "source_authority": "extracted",
        },
        "evidence": [
            {
                "observation_id": observation_id,
                "observation_revision": 1,
                "start": 0,
                "end": 8,
            }
        ],
        "scope": {"tenant_id": tenant, "agent_id": agent, "space_id": space},
        "privacy_labels": [],
    }


def phase10_drain(db_path: str, *, owner: str, steps: int = 12) -> None:
    """Drain the Phase 10 outbox — the parent's recovery entry point."""
    from iris_memory_core.domain.jobs import ENABLED_JOB_KINDS
    from iris_memory_core.jobs.worker import phase10_handlers

    store, outbox, pipeline, _ = phase10_context(db_path)
    handlers = phase10_handlers(pipeline=pipeline)
    kinds = frozenset(handlers) & ENABLED_JOB_KINDS
    with store.write() as tx:
        tx.raw().execute("UPDATE outbox_jobs SET lease_expires_us = 0 WHERE status = 'leased'")
    for _ in range(steps):
        batch = outbox.claim(owner, kinds=kinds)
        if not batch.jobs:
            return
        for job in batch.jobs:
            outbox.execute(job, handlers[job.job_kind], owner=owner)


def scenario_reflection(db_path: str, crash_at: str) -> None:
    """Phase 10 fenced canonical commit of a reconciled candidate.

    The consolidation and reflection stages run to completion first; the
    kill is armed only around the reconciliation commit — the transaction
    that turns an accepted candidate into a Canonical claim.
    """
    from iris_memory_core.application.observation import ObservationService
    from iris_memory_core.application.provisioning import ProvisioningService
    from iris_memory_core.domain.jobs import ENABLED_JOB_KINDS, NewOutboxJob
    from iris_memory_core.jobs.worker import phase10_handlers

    ctx = setup(db_path)
    admin = AccessContext("t1", "bootstrap", admin=True, agent_ids=frozenset({ctx.agent_id}))
    space = (
        ProvisioningService(ctx.store)
        .create_space(admin, "direct", agent_id=ctx.agent_id, reason="kill9")
        .id
    )
    access = AccessContext(
        "t1",
        "host",
        agent_ids=frozenset({ctx.agent_id}),
        allowed_space_ids=frozenset({space}),
        admin=True,
    )
    now = ctx.store.clock.now_us()
    ObservationService(ctx.store).observe_batch(
        access,
        [
            {
                "agent_id": ctx.agent_id,
                "space_id": space,
                "role": "user",
                "kind": "message",
                "idempotency_key": "kill9-phase10-observation",
                "occurred_us": now,
                "committed_us": now,
                "content": "I prefer tea number 1",
                "privacy_labels": [],
            }
        ],
    )

    store, outbox, pipeline, _ = phase10_context(db_path)
    handlers = phase10_handlers(pipeline=pipeline)
    with store.read() as tx:
        watermark = tx.watermark("t1", ctx.agent_id)
        occurred = int(tx.raw().execute("SELECT occurred_us FROM observations").fetchone()[0])
    assert watermark is not None
    now = store.clock.now_us()
    outbox.enqueue(
        NewOutboxJob(
            tenant_id="t1",
            agent_id=ctx.agent_id,
            job_kind="episode.consolidation",
            aggregate_type="observation_window",
            aggregate_id="kill9-window",
            source_revision=watermark.current_seq,
            payload={
                "version": 2,
                "job_kind": "episode.consolidation",
                "scope": {"space_id": space},
                "topic_key": "preferences",
                "window_start_us": occurred - 1,
                "window_end_us": occurred + 1_000_000,
            },
            dedupe_key=f"kill9-consolidation:{ctx.agent_id}:{watermark.current_seq}",
            available_at_us=now,
        )
    )
    kinds = frozenset(handlers) & ENABLED_JOB_KINDS
    for _ in range(12):
        batch = outbox.claim("doomed", kinds=kinds)
        assert batch.jobs, "reconciliation job never became claimable"
        for job in batch.jobs:
            if job.job_kind != "memory.reconciliation":
                outbox.execute(job, handlers[job.job_kind], owner="doomed")
                continue
            # The claim transaction has committed; only the reconciliation
            # work and its fenced completion CAS remain in the next commit.
            if crash_at == "pre_commit":
                _die_before_commit(store)
            outbox.execute(job, handlers[job.job_kind], owner="doomed")
            if crash_at == "post_commit":
                _self_kill()
            return
    raise AssertionError("reconciliation job never appeared")


SCENARIOS = {
    "observe": scenario_observe,
    "worker": scenario_worker,
    "tick": scenario_tick,
    "state_put": scenario_state_put,
    "focus_transition": scenario_focus_transition,
    "recent_rebuild": scenario_recent_rebuild,
    "task_transition": scenario_task_transition,
    "trigger_scan": scenario_trigger_scan,
    "event_ack": scenario_event_ack,
    "forget": scenario_forget,
    "reflection": scenario_reflection,
}


def main(argv: list[str]) -> int:
    scenario, db_path, crash_at = argv[1], argv[2], argv[3]
    SCENARIOS[scenario](db_path, crash_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
