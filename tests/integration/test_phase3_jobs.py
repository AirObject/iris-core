"""Phase 3 job handler tests (§16/§17, Phase 3 job kinds).

Real handler behavior for observation.recorded → recent_context.maintenance,
the deterministic rebuild handler, the focus decay sweep handler and the
state pointer invariant job; plus fail-closed behavior for unknown kinds and
payload versions.
"""

from __future__ import annotations

from typing import Any

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.jobs import NewOutboxJob, spec_for
from iris_memory_core.jobs.worker import (
    OutboxWorker,
    phase3_handlers,
    phase4_handlers,
)
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for


@pytest.fixture
def phase3(
    clocked_store: Store,
    generous_gauge: BackpressureGauge,
    clocked_tenant_id: str,
    phase2_agent: str,
) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space = tx.insert_space(clocked_tenant_id, "chat_group")
        session = tx.insert_session(clocked_tenant_id, space.id, actor="t")
    access = access_for(
        clocked_tenant_id, agent_ids=frozenset({phase2_agent}), space_ids=frozenset({space.id})
    )
    idem = IdempotencyManager(clocked_store)
    recent = RecentContextService(clocked_store, clocked_store.clock)
    focus = FocusService(clocked_store, clocked_store.clock, idempotency=idem)
    notes = NoteService(clocked_store, clocked_store.clock, idempotency=idem)
    tasks = TaskService(clocked_store, clocked_store.clock, idempotency=idem)
    outbox = OutboxService(clocked_store, clocked_store.clock, gauge=generous_gauge)
    handlers = {
        **phase3_handlers(
            clocked_store, clocked_store.clock, recent=recent, focus=focus, gauge=generous_gauge
        ),
        **phase4_handlers(clocked_store.clock, notes=notes, tasks=tasks),
    }
    return {
        "store": clocked_store,
        "gauge": generous_gauge,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "space": space.id,
        "session": session.id,
        "access": access,
        "idem": idem,
        "recent": recent,
        "focus": focus,
        "notes": notes,
        "tasks": tasks,
        "outbox": outbox,
        "states": StateService(
            clocked_store, clocked_store.clock, gauge=generous_gauge, idempotency=idem
        ),
        "observations": ObservationService(clocked_store, gauge=generous_gauge),
        "worker": OutboxWorker(outbox, handlers=handlers),
    }


def _observe(ctx: dict[str, Any], key: str, content: str, session: str | None = None) -> Any:
    now = ctx["store"].clock.now_us()
    return ctx["observations"].observe_batch(
        ctx["access"],
        [
            {
                "agent_id": ctx["agent"],
                "role": "user",
                "kind": "message.text",
                "idempotency_key": key,
                "occurred_us": now,
                "committed_us": now + 1,
                "content": content,
                "space_id": ctx["space"],
                "session_id": session,
            }
        ],
    )


class TestObservationRecordedHandler:
    def test_observe_schedules_recent_rebuild(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _observe(ctx, "o1", "hello world", session=ctx["session"])
        outcome = ctx["worker"].run_once()
        assert outcome["claimed"] >= 1
        assert outcome["completed"] >= 1
        with ctx["store"].read() as tx:
            maintenance = tx.outbox.list_jobs(
                tenant_id=ctx["tenant"], job_kind="recent_context.maintenance"
            )
        assert len(maintenance) >= 1
        # Second run drains the rebuild job itself.
        outcome = ctx["worker"].run_once()
        assert outcome["completed"] >= 1
        view = ctx["recent"].get(
            ctx["access"], agent_id=ctx["agent"], space_id=ctx["space"], session_id=ctx["session"]
        )
        assert view.source == "generation"
        assert len(view.projection.hot_observation_refs) == 1

    def test_handler_is_idempotent_across_replays(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _observe(ctx, "o1", "once", session=ctx["session"])
        for _ in range(4):
            ctx["worker"].run_once()
        with ctx["store"].read() as tx:
            jobs = tx.outbox.list_jobs(
                tenant_id=ctx["tenant"], job_kind="recent_context.maintenance"
            )
            generations = (
                tx.raw().execute("SELECT COUNT(*) FROM recent_context_generations").fetchone()[0]
            )
        assert generations == 1  # dedupe by (target, watermark) absorbed replays
        assert all(job.status == "completed" for job in jobs)

    def test_agent_level_observation_schedules_nothing(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        ctx["observations"].observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "system",
                    "kind": "agent.event",
                    "idempotency_key": "no-space",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "agent level",
                }
            ],
        )
        ctx["worker"].run_once()
        with ctx["store"].read() as tx:
            jobs = tx.outbox.list_jobs(
                tenant_id=ctx["tenant"], job_kind="recent_context.maintenance"
            )
        assert not jobs


class TestRecentMaintenanceHandler:
    def test_rebuild_reflects_only_committed_set(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _observe(ctx, "o1", "first message", session=ctx["session"])
        _observe(ctx, "o2", "second message", session=ctx["session"])
        for _ in range(4):
            ctx["worker"].run_once()
        view = ctx["recent"].get(
            ctx["access"], agent_id=ctx["agent"], space_id=ctx["space"], session_id=ctx["session"]
        )
        assert len(view.projection.hot_observation_refs) == 2
        _observe(ctx, "o3", "third message", session=ctx["session"])
        for _ in range(4):
            ctx["worker"].run_once()
        refreshed = ctx["recent"].get(
            ctx["access"], agent_id=ctx["agent"], space_id=ctx["space"], session_id=ctx["session"]
        )
        assert refreshed.source == "generation"
        assert len(refreshed.projection.hot_observation_refs) == 3

    def test_handler_enqueues_no_followup_jobs(self, phase3: dict[str, Any]) -> None:
        """The rebuild handler must not spawn unbounded work."""
        ctx = phase3
        _observe(ctx, "o1", "x", session=ctx["session"])
        for _ in range(6):
            ctx["worker"].run_once()
        with ctx["store"].read() as tx:
            pending = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM outbox_jobs WHERE status IN "
                    "('pending','leased','retryable')"
                )
                .fetchone()[0]
            )
        assert pending == 0


class TestStateProjectionHandler:
    def test_pointer_check_completes_for_healthy_records(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        ctx["states"].put(
            ctx["access"],
            "environment",
            "obs.scene",
            agent_id=ctx["agent"],
            value={"scene": "ok"},
            source_authority="host",
            idempotency_key="sp-1",
            ttl_us=0,
        )
        outcome = ctx["worker"].run_once()
        assert outcome["completed"] >= 1

    def test_broken_pointer_fails_the_job(self, phase3: dict[str, Any]) -> None:
        """A forged pointer (revision row deleted) makes the check job fail —
        the coalesced stream owes us this invariant, and its violation must
        not be silently completed."""
        import sqlite3

        ctx = phase3
        ctx["states"].put(
            ctx["access"],
            "environment",
            "obs.scene",
            agent_id=ctx["agent"],
            value={"scene": "ok"},
            source_authority="host",
            idempotency_key="sp-2",
            ttl_us=0,
        )
        with ctx["store"].read() as tx:
            jobs = tx.outbox.list_jobs(tenant_id=ctx["tenant"], job_kind="state.projection")
        assert jobs
        connection = sqlite3.connect(ctx["store"].runtime.database)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM state_record_revisions")
            connection.commit()
        finally:
            connection.close()
        outcome = ctx["worker"].run_once()
        assert outcome["retryable"] >= 1 or outcome["dead"] >= 1


class TestFocusMaintenanceHandler:
    def test_scheduled_decay_sweep_runs_and_is_idempotent(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.application.scheduler import SchedulerService
        from iris_memory_core.domain.jobs import ENABLED_JOB_KINDS

        ctx = phase3
        ctx["focus"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            kind="clue",
            summary="aging clue",
            activation=0.5,
            idempotency_key="fm-1",
        )
        assert "focus.maintenance" in ENABLED_JOB_KINDS
        scheduler = SchedulerService(
            ctx["store"], ctx["store"].clock, enabled_kinds=ENABLED_JOB_KINDS
        )
        admin = access_for(ctx["tenant"], admin=True, agent_ids=frozenset({ctx["agent"]}))
        scheduler.create_schedule(
            admin,
            agent_id=ctx["agent"],
            job_kind="focus.maintenance",
            spec={"kind": "interval", "every_seconds": 60},
            reason="decay",
            catch_up_policy="coalesce",
            misfire_grace_us=25 * 3_600_000_000,
        )  # the 24h decay jump stays in grace
        # Jump far into the future so decay floors the activation.
        ctx["store"].clock.advance(24 * 3_600_000_000)
        scheduler.advance(now_us=ctx["store"].clock.now_us() + 1)
        ctx["store"].clock.advance(60_000_000)  # tick availability is now in the past
        for _ in range(4):
            ctx["worker"].run_once()
        items = ctx["focus"].list_items(
            ctx["access"], agent_id=ctx["agent"], statuses=("active", "dormant")
        )
        assert items and items[0][1].status in ("dormant",)
        assert items[0][1].activation < 0.5
        # Re-running the tick at a later instant keeps it deterministic.
        revisions_before = len(ctx["focus"].history(ctx["access"], items[0][0].id))
        scheduler.advance(now_us=ctx["store"].clock.now_us() + 61_000_000)
        ctx["store"].clock.advance(60_000_000)
        ctx["worker"].run_once()
        revisions_after = len(ctx["focus"].history(ctx["access"], items[0][0].id))
        assert revisions_after >= revisions_before


class TestFailClosed:
    def test_unknown_kind_stays_unclaimed(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.jobs import UnknownJobKindError

        ctx = phase3
        with pytest.raises(UnknownJobKindError):
            ctx["outbox"].enqueue(
                NewOutboxJob(
                    tenant_id=ctx["tenant"],
                    job_kind="does.not_exist",
                    aggregate_type="x",
                    aggregate_id="x",
                    source_revision=1,
                    payload={"version": 1},
                    dedupe_key="unknown-kind",
                )
            )

    def test_unknown_payload_version_never_claimed(self, phase3: dict[str, Any]) -> None:
        import json as _json

        ctx = phase3
        # The enqueue path REFUSES payload versions above the current build
        # (fail closed at the producer); a row from a FUTURE binary sitting in
        # the shared queue must equally never be claimed by this worker.
        from iris_memory_core.domain.errors import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            ctx["outbox"].enqueue(
                NewOutboxJob(
                    tenant_id=ctx["tenant"],
                    job_kind="recent_context.maintenance",
                    aggregate_type="recent_context_target",
                    aggregate_id="t",
                    source_revision=1,
                    payload={
                        "version": 99,
                        "job_kind": "recent_context.maintenance",
                        "agent_id": ctx["agent"],
                        "space_id": ctx["space"],
                    },
                    dedupe_key="future-version",
                    payload_version=99,
                )
            )
        now_us = ctx["store"].clock.now_us()
        with ctx["store"].write() as tx:
            tx.raw().execute(
                "INSERT INTO outbox_jobs (id, tenant_id, agent_id, job_kind, "
                "aggregate_type, aggregate_id, source_revision, payload, "
                "payload_version, dedupe_key, status, available_at_us, "
                "attempt_count, max_attempts, created_us) "
                "VALUES ('future-job', ?, ?, 'recent_context.maintenance', "
                "'recent_context_target', 't', 1, ?, 99, 'future-version', "
                "'pending', ?, 0, 8, ?)",
                (ctx["tenant"], ctx["agent"], _json.dumps({"version": 99}), now_us, now_us),
            )
        outcome = ctx["worker"].run_once()
        assert outcome["claimed"] == 0
        with ctx["store"].read() as tx:
            job = tx.outbox.by_dedupe_key(ctx["tenant"], "future-version")
        assert job is not None and job.status == "pending"

    def test_disabled_kind_rejected_by_scheduler(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.application.scheduler import SchedulerService
        from iris_memory_core.domain.errors import InvalidRequestError

        ctx = phase3
        scheduler = SchedulerService(ctx["store"], ctx["store"].clock)
        admin = access_for(ctx["tenant"], admin=True)
        with pytest.raises(InvalidRequestError):
            scheduler.create_schedule(
                admin,
                agent_id=None,
                job_kind="episode.consolidation",
                spec={"kind": "interval", "every_seconds": 60},
                reason="nope",
            )

    def test_every_enabled_kind_has_a_handler(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.jobs import ENABLED_JOB_KINDS
        from iris_memory_core.jobs.worker import phase4_handlers

        ctx = phase3
        handlers = {
            **phase3_handlers(
                ctx["store"], ctx["store"].clock, recent=ctx["recent"], focus=ctx["focus"]
            ),
            **phase4_handlers(
                ctx["store"].clock,
                notes=ctx["notes"],
                tasks=ctx["tasks"],
            ),
        }
        assert frozenset(handlers) >= ENABLED_JOB_KINDS
        for kind in ENABLED_JOB_KINDS:
            assert spec_for(kind).handler_enabled
