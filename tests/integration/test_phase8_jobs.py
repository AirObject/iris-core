"""Phase 8 jobs/outbox integration tests (ADR-0016 §7): the six new kinds
have real idempotent handlers; change/invalidation handlers schedule the
graph/profile applies inside the canonical transaction; binding/redirect/
tombstone/space-group operations enqueue invalidations atomically; the
worker claims and completes them under fencing."""

from __future__ import annotations

import json
from typing import Any

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.jobs.handlers import (
    graph_apply_handler,
    graph_rebuild_handler,
    profile_apply_handler,
    profile_rebuild_handler,
)
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.phase8_helpers import TENANT, Phase8World


@pytest.fixture
def world(
    clocked_store: Store, mutable_clock: MutableClock, generous_gauge: BackpressureGauge
) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


def _outbox(world: Phase8World, gauge: BackpressureGauge) -> OutboxService:
    return OutboxService(world.store, world.clock, gauge=gauge)


def _run_kind(world: Phase8World, gauge: BackpressureGauge, kind: str, handler: Any) -> int:
    """Claim and execute every runnable job of one kind until drained."""
    from iris_memory_core.jobs.worker import OutboxWorker

    service = _outbox(world, gauge)
    worker = OutboxWorker(service, {kind: handler}, owner="p8-test-worker")
    executed = 0
    for _ in range(20):
        outcome = worker.run_once()
        executed += outcome.get("completed", 0)
        if outcome.get("claimed", 0) == 0 and outcome.get("completed", 0) == 0:
            break
    return executed


def observation_recorded_noop() -> Any:
    """A typed no-op observation.recorded handler for drain loops."""

    def work(job: Any) -> Any:
        del job

        def commit(tx: Any) -> None:
            del tx

        return commit

    return work


class TestHandlerWiring:
    def test_claim_changed_schedules_graph_and_profile_applies(
        self, world: Phase8World, generous_gauge: BackpressureGauge
    ) -> None:
        from iris_memory_core.jobs.handlers import claim_changed_handler
        from iris_memory_core.jobs.worker import OutboxWorker

        bob = world.entity("Jobs-Bob")
        world.remember("j1", "Bob is a pilot", bob, category="identity")
        service = _outbox(world, generous_gauge)
        worker = OutboxWorker(
            service,
            {
                "claim.changed": claim_changed_handler(world.clock, gauge=generous_gauge),
                "observation.recorded": observation_recorded_noop(),
            },
        )
        # Drain the observation.recorded + claim.changed jobs.
        for _ in range(30):
            outcome = worker.run_once()
            if not any(outcome.get(key, 0) for key in ("completed", "claimed")):
                break
        with world.store.read() as tx:
            jobs = tx.outbox.list_jobs(tenant_id=TENANT, limit=200)
        kinds = {job.job_kind for job in jobs}
        assert "graph.apply" in kinds
        assert "profile.apply" in kinds
        graph_jobs = [j for j in jobs if j.job_kind == "graph.apply"]
        assert any(j.payload.get("resource_type") == "claim" for j in graph_jobs)
        assert any(
            j.payload.get("resource_type") == "claim" for j in jobs if j.job_kind == "profile.apply"
        )
        assert all("pilot" not in str(j.payload) for j in graph_jobs)

    def test_binding_change_enqueues_graph_apply_atomically(self, world: Phase8World) -> None:
        person = world.entity("Bind-Jobs")
        world.bind_identity(person, "bind-qq-jobs")
        with world.store.read() as tx:
            jobs = tx.outbox.list_jobs(tenant_id=TENANT, limit=100)
        graph_jobs = [j for j in jobs if j.job_kind == "graph.apply"]
        assert any(j.aggregate_type == "binding" for j in graph_jobs)

    def test_space_group_bind_unbind_enqueues_invalidations(self, world: Phase8World) -> None:
        from iris_memory_core.application.provisioning import ProvisioningService
        from iris_memory_core.storage.idempotency import IdempotencyManager

        provisioning = ProvisioningService(world.store, IdempotencyManager(world.store))
        with world.store.write() as tx:
            group = tx.insert_space_group(TENANT, "G", "d", actor="t", reason_code="t")
            space = tx.insert_space(TENANT, "chat_group", actor="t")
        provisioning.bind_space_to_group(
            world.admin_access, space.id, group.id, expected_revision=1, reason="test"
        )
        with world.store.read() as tx:
            jobs = tx.outbox.list_jobs(tenant_id=TENANT, limit=100)
        after_bind = [j for j in jobs if j.job_kind in ("graph.apply", "profile.apply")]
        assert any(j.aggregate_id == group.id for j in after_bind)
        provisioning.unbind_space_from_group(
            world.admin_access, space.id, expected_revision=2, reason="test"
        )
        with world.store.read() as tx:
            jobs = tx.outbox.list_jobs(tenant_id=TENANT, limit=100)
        after_unbind = [j for j in jobs if j.job_kind in ("graph.apply", "profile.apply")]
        assert len(after_unbind) >= len(after_bind)


class TestWorkerExecution:
    def test_graph_apply_handler_executes_and_drains(
        self, world: Phase8World, generous_gauge: BackpressureGauge
    ) -> None:
        bob = world.entity("Apply-Bob")
        carol = world.entity("Apply-Carol")
        world.relate("ar1", bob, carol)
        world.rebuild()
        # A new relation whose graph.apply is pending (scheduled by the
        # relation.changed handler chain).
        world.relate("ar2", carol, bob, relation_type="likes")
        from iris_memory_core.jobs.handlers import relation_changed_handler
        from iris_memory_core.jobs.worker import OutboxWorker

        service = _outbox(world, generous_gauge)
        # First drain relation.changed so it schedules graph.apply.
        worker = OutboxWorker(
            service,
            {"relation.changed": relation_changed_handler(world.clock, gauge=generous_gauge)},
        )
        for _ in range(10):
            outcome = worker.run_once()
            if not outcome.get("completed", 0):
                break
        executed = _run_kind(
            world,
            generous_gauge,
            "graph.apply",
            graph_apply_handler(world.graph),
        )
        assert executed >= 1
        with world.store.read() as tx:
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
            edges = tx.graph.all_edges(TENANT, pointer.generation_id)
        assert any(edge.resource_id == "ar2" or edge.resource_type == "relation" for edge in edges)
        assert len([e for e in edges if e.edge_type == "likes"]) == 1

    def test_graph_rebuild_handler_builds_and_switches(
        self, world: Phase8World, generous_gauge: BackpressureGauge
    ) -> None:
        from iris_memory_core.application.outbox import enqueue_with_pressure
        from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, NewOutboxJob

        bob = world.entity("Rebuild-Bob")
        carol = world.entity("Rebuild-Carol")
        world.relate("rr1", bob, carol)
        with world.store.write() as tx:
            enqueue_with_pressure(
                tx,
                NewOutboxJob(
                    tenant_id=TENANT,
                    job_kind="graph.rebuild",
                    aggregate_type="projection",
                    aggregate_id="graph",
                    source_revision=1,
                    payload={"version": JOB_PAYLOAD_VERSION},
                    dedupe_key=f"p8-graph-rebuild-{world.clock.now_us()}",
                    priority=3,
                ),
                generous_gauge,
            )
        executed = _run_kind(
            world, generous_gauge, "graph.rebuild", graph_rebuild_handler(world.graph)
        )
        assert executed >= 1
        with world.store.read() as tx:
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
            assert tx.graph.projection_state() == "ready"

    def test_profile_apply_handler_rederives_subject(
        self, world: Phase8World, generous_gauge: BackpressureGauge
    ) -> None:
        bob = world.entity("PApply-Bob")
        world.remember("pa1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        world.remember("pa2", "Bob likes tea", bob, category="preference")
        # Drain claim.changed first: it is the producer of profile.apply.
        from iris_memory_core.jobs.handlers import claim_changed_handler
        from iris_memory_core.jobs.worker import OutboxWorker

        service = _outbox(world, generous_gauge)
        worker = OutboxWorker(
            service,
            {"claim.changed": claim_changed_handler(world.clock, gauge=generous_gauge)},
        )
        for _ in range(10):
            outcome = worker.run_once()
            if not outcome.get("completed", 0):
                break
        executed = _run_kind(
            world,
            generous_gauge,
            "profile.apply",
            profile_apply_handler(world.profile),
        )
        assert executed >= 1
        from iris_memory_core.domain.profile import ProfileSubjectKey

        with world.store.read() as tx:
            pointer = tx.profile.pointer(TENANT)
            assert pointer is not None
            fields = tx.profile.fields_for_subject(
                TENANT, pointer.generation_id, ProfileSubjectKey("entity", bob)
            )
        assert {f.field for f in fields} >= {"p_pa1", "p_pa2"}

    def test_profile_rebuild_handler_builds_and_switches(
        self, world: Phase8World, generous_gauge: BackpressureGauge
    ) -> None:
        from iris_memory_core.application.outbox import enqueue_with_pressure
        from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, NewOutboxJob

        bob = world.entity("PRebuild-Bob")
        world.remember("pr1", "Bob is a pilot", bob, category="identity")
        with world.store.write() as tx:
            enqueue_with_pressure(
                tx,
                NewOutboxJob(
                    tenant_id=TENANT,
                    job_kind="profile.rebuild",
                    aggregate_type="projection",
                    aggregate_id="profile",
                    source_revision=1,
                    payload={"version": JOB_PAYLOAD_VERSION},
                    dedupe_key=f"p8-profile-rebuild-{world.clock.now_us()}",
                    priority=3,
                ),
                generous_gauge,
            )
        executed = _run_kind(
            world,
            generous_gauge,
            "profile.rebuild",
            profile_rebuild_handler(world.profile),
        )
        assert executed >= 1
        with world.store.read() as tx:
            assert tx.profile.projection_state() == "ready"


class TestStaleWorkerDiscipline:
    def test_old_worker_does_not_claim_unknown_payload_versions(
        self, world: Phase8World, generous_gauge: BackpressureGauge
    ) -> None:
        # A future-version job sits pending forever: this build never claims
        # it (the enqueue path refuses >current versions, so seed the row
        # directly — a NEWER deployment wrote it).
        import sqlite3

        connection = sqlite3.connect(world.store.runtime.database)
        try:
            connection.execute(
                "INSERT INTO outbox_jobs (id, tenant_id, agent_id, job_kind, "
                "aggregate_type, aggregate_id, source_revision, payload, "
                "payload_version, dedupe_key, coalesce_key, priority, status, "
                "available_at_us, attempt_count, max_attempts, lease_generation, "
                "created_us) VALUES ('future-job-1', ?, ?, 'graph.apply', 'claim', "
                "'future-1', 1, ?, 2, 'future-graph-apply-1', "
                "'graph:claim:future-1', 5, 'pending', 0, 0, 8, 0, 1)",
                (TENANT, world.agent, json.dumps({"version": 2})),
            )
            connection.commit()
        finally:
            connection.close()
        _run_kind(world, generous_gauge, "graph.apply", graph_apply_handler(world.graph))
        with world.store.read() as tx:
            jobs = tx.outbox.list_jobs(tenant_id=TENANT, status="pending", job_kind="graph.apply")
        future = [job for job in jobs if job.aggregate_id == "future-1"]
        assert future, "the future-version job must stay pending (never claimed)"
        assert future[0].attempt_count == 0

    def test_stale_source_revision_cannot_complete(
        self, world: Phase8World, generous_gauge: BackpressureGauge
    ) -> None:
        from iris_memory_core.jobs.handlers import graph_apply_handler

        bob = world.entity("Stale-Bob")
        world.remember("st1", "Bob is a pilot", bob, category="identity")
        handler = graph_apply_handler(world.graph)
        service = _outbox(world, generous_gauge)
        batch = service.claim("stale-worker", kinds=frozenset({"graph.apply"}))
        # Bump the row's source_revision (a coalesce merge): the stale
        # worker's fenced completion must be rejected.
        if batch.jobs:
            import sqlite3

            connection = sqlite3.connect(world.store.runtime.database)
            try:
                connection.execute(
                    "UPDATE outbox_jobs SET source_revision = source_revision + 1 WHERE id = ?",
                    (batch.jobs[0].id,),
                )
                connection.commit()
            finally:
                connection.close()
            work = handler(batch.jobs[0])
            fenced = {}

            def commit_that_fences(tx: Any) -> None:
                work(tx)
                # The four-fold CAS (source revision bumped ⇒ rowcount 0);
                # the SERVICE turns this into LeaseFencedError.
                fenced["rowcount"] = tx.outbox.complete(
                    batch.jobs[0].id,
                    owner="stale-worker",
                    generation=batch.jobs[0].lease_generation,
                    now_us=world.clock.now_us(),
                    source_revision=batch.jobs[0].source_revision,
                )

            with world.store.write() as tx:
                commit_that_fences(tx)
            assert fenced["rowcount"] == 0
            with world.store.read() as tx:
                job = next(
                    candidate
                    for candidate in tx.outbox.list_jobs(tenant_id=TENANT, job_kind="graph.apply")
                    if candidate.id == batch.jobs[0].id
                )
            assert job.status != "completed"
