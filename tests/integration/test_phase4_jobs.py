"""Phase 4 job handler and Recall integration tests.

note.review and task.trigger_scan run through the real worker loop with
fenced commits; unknown payload versions fail closed; the structured recall
skeleton gains the due-tasks route and pending_event_ids.
"""

from __future__ import annotations

from typing import Any

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.recall import (
    ROUTE_TASKS,
    StructuredRecallOrchestrator,
    StructuredRecallRequest,
)
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.jobs import NewOutboxJob, spec_for
from iris_memory_core.jobs.worker import OutboxWorker, phase3_handlers, phase4_handlers
from iris_memory_core.storage.idempotency import IdempotencyManager
from tests.conftest import access_for


@pytest.fixture
def jobs_ctx(
    clocked_store: Any,
    generous_gauge: BackpressureGauge,
    clocked_tenant_id: str,
    phase2_agent: str,
) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space = tx.insert_space(clocked_tenant_id, "chat_group")
        session = tx.insert_session(clocked_tenant_id, space.id, actor="t")
    access = access_for(
        clocked_tenant_id,
        agent_ids=frozenset({phase2_agent}),
        space_ids=frozenset({space.id}),
        admin=True,
    )
    idem = IdempotencyManager(clocked_store)
    notes = NoteService(clocked_store, clocked_store.clock, idempotency=idem)
    tasks = TaskService(clocked_store, clocked_store.clock, idempotency=idem)
    outbox = OutboxService(clocked_store, clocked_store.clock, gauge=generous_gauge)
    handlers = {
        **phase3_handlers(
            clocked_store,
            clocked_store.clock,
            recent=RecentContextService(clocked_store, clocked_store.clock),
            focus=FocusService(clocked_store, clocked_store.clock, idempotency=idem),
            gauge=generous_gauge,
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
        "notes": notes,
        "tasks": tasks,
        "events": CognitiveEventService(clocked_store, clocked_store.clock, idempotency=idem),
        "observations": ObservationService(clocked_store, gauge=generous_gauge),
        "states": StateService(
            clocked_store, clocked_store.clock, gauge=generous_gauge, idempotency=idem
        ),
        "worker": OutboxWorker(outbox, handlers=handlers),
        "outbox": outbox,
    }


class TestReviewJob:
    def test_note_review_job_runs_through_worker_idempotently(
        self, jobs_ctx: dict[str, Any]
    ) -> None:
        ctx = jobs_ctx
        clock = ctx["store"].clock
        note = ctx["notes"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            kind="promise",
            title="promise note",
            review_after_us=clock.now_us() - 1,
            idempotency_key="jr-1",
        )
        ctx["outbox"].enqueue(
            NewOutboxJob(
                tenant_id=ctx["tenant"],
                job_kind="note.review",
                aggregate_type="agent",
                aggregate_id=ctx["agent"],
                source_revision=1,
                payload={"version": 1, "job_kind": "note.review"},
                dedupe_key="jr-note-1",
                agent_id=ctx["agent"],
                priority=4,
            )
        )
        for _ in range(4):
            outcomes = ctx["worker"].run_once()
            if outcomes["claimed"] == 0:
                break
        with ctx["store"].read() as tx:
            promoted = tx.notes.get(note.note_id)
            promoted_revision = tx.notes.current_revision_row(note.note_id)
        assert promoted.status == "promoted"
        assert promoted_revision.promotion_target_id is not None
        # Second job run (replay) changes nothing.
        ctx["outbox"].enqueue(
            NewOutboxJob(
                tenant_id=ctx["tenant"],
                job_kind="note.review",
                aggregate_type="agent",
                aggregate_id=ctx["agent"],
                source_revision=2,
                payload={"version": 1, "job_kind": "note.review"},
                dedupe_key="jr-note-2",
                agent_id=ctx["agent"],
                priority=4,
            )
        )
        for _ in range(4):
            outcomes = ctx["worker"].run_once()
            if outcomes["claimed"] == 0:
                break
        with ctx["store"].read() as tx:
            assert tx.notes.get(note.note_id).current_revision == promoted.current_revision


class TestTriggerScanJob:
    def test_trigger_scan_job_creates_events_via_worker(self, jobs_ctx: dict[str, Any]) -> None:
        ctx = jobs_ctx
        clock = ctx["store"].clock
        task = ctx["tasks"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            title="scan job",
            origin="explicit_tool",
            idempotency_key="ts-1",
        )
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="at_time",
            schedule_spec={"at_us": clock.now_us() + 1_000},
            idempotency_key="ts-2",
        )
        clock.advance(2_000)
        ctx["outbox"].enqueue(
            NewOutboxJob(
                tenant_id=ctx["tenant"],
                job_kind="task.trigger_scan",
                aggregate_type="agent",
                aggregate_id=ctx["agent"],
                source_revision=1,
                payload={"version": 1, "job_kind": "task.trigger_scan"},
                dedupe_key="ts-scan-1",
                agent_id=ctx["agent"],
                priority=4,
            )
        )
        dead_before_scan = 0
        for _ in range(6):
            outcomes = ctx["worker"].run_once()
            dead_before_scan += outcomes["dead"]
            if outcomes["claimed"] == 0:
                break
        # The trigger-scan job must actually run to completion — never
        # dead-letter — and the event it plans must be visible.
        assert dead_before_scan == 0
        listed = ctx["events"].list_events(ctx["access"], agent_id=ctx["agent"])
        assert len(listed) == 1
        assert listed[0][0].kind == "task.due"
        # The task.changed jobs enqueued by writes also complete (pointer checks).
        for _ in range(8):
            outcomes = ctx["worker"].run_once()
            if outcomes["claimed"] == 0:
                break
            assert outcomes["dead"] == 0


class TestFailClosed:
    def test_unknown_payload_version_rejected_at_enqueue(self, jobs_ctx: dict[str, Any]) -> None:
        """A payload version above this build never even lands in the queue —
        the enqueue itself fails closed."""
        from iris_memory_core.domain.errors import InvalidRequestError

        ctx = jobs_ctx
        with pytest.raises(InvalidRequestError):
            ctx["outbox"].enqueue(
                NewOutboxJob(
                    tenant_id=ctx["tenant"],
                    job_kind="task.trigger_scan",
                    aggregate_type="agent",
                    aggregate_id=ctx["agent"],
                    source_revision=1,
                    payload={"version": 99, "job_kind": "task.trigger_scan"},
                    dedupe_key="fc-1",
                    agent_id=ctx["agent"],
                    payload_version=99,
                )
            )

    def test_enabled_kinds_have_handlers(self, jobs_ctx: dict[str, Any]) -> None:
        from iris_memory_core.application.forget import ForgetService
        from iris_memory_core.application.retention import RetentionService
        from iris_memory_core.domain.jobs import ENABLED_JOB_KINDS
        from iris_memory_core.jobs.worker import phase5_handlers

        retention = RetentionService(
            jobs_ctx["store"],
            jobs_ctx["store"].clock,
            forget=ForgetService(jobs_ctx["store"], jobs_ctx["store"].clock),
        )
        handlers = {
            **phase3_handlers(
                jobs_ctx["store"],
                jobs_ctx["store"].clock,
                recent=RecentContextService(jobs_ctx["store"], jobs_ctx["store"].clock),
                focus=FocusService(jobs_ctx["store"], jobs_ctx["store"].clock),
            ),
            **phase4_handlers(
                jobs_ctx["store"].clock, notes=jobs_ctx["notes"], tasks=jobs_ctx["tasks"]
            ),
            **phase5_handlers(jobs_ctx["store"].clock, retention=retention),
        }
        assert frozenset(handlers) >= ENABLED_JOB_KINDS
        for kind in (
            "note.review",
            "task.trigger_scan",
            "note.changed",
            "task.changed",
            "cognitive_event.changed",
            "claim.changed",
            "episode.changed",
            "relation.changed",
            "memory.invalidated",
            "retention.compaction",
        ):
            assert spec_for(kind).handler_enabled

    def test_coalescing_stays_forbidden_for_phase4_kinds(self) -> None:
        from iris_memory_core.domain.jobs import CoalescingNotAllowedError, require_coalesce_key

        for kind in (
            "note.review",
            "task.trigger_scan",
            "note.changed",
            "task.changed",
            "cognitive_event.changed",
        ):
            with pytest.raises(CoalescingNotAllowedError):
                require_coalesce_key(kind, "any-key")


class TestRecallIntegration:
    def test_due_tasks_route_and_pending_event_ids(self, jobs_ctx: dict[str, Any]) -> None:
        ctx = jobs_ctx
        clock = ctx["store"].clock
        # A due, active task and a not-yet-due one.
        due = ctx["tasks"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            title="due task",
            origin="explicit_tool",
            due_at_us=clock.now_us() - 1,
            idempotency_key="rc-1",
        )
        ctx["tasks"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            title="later task",
            origin="explicit_tool",
            due_at_us=clock.now_us() + 86_400_000_000,
            idempotency_key="rc-2",
        )
        # A pending event for the same agent, plus a SPACE-SCOPED one: the
        # pending_event_ids listing follows the access envelope, so events of
        # registered spaces are included (P1 round-2 gate).
        with ctx["store"].write() as tx:
            event_id = CognitiveEventService.create_internal(
                tx,
                tenant_id=ctx["tenant"],
                agent_id=ctx["agent"],
                space_group_id=None,
                space_id=None,
                session_id=None,
                kind="task.due",
                object_type="task",
                object_id=due.task_id,
                occurrence_id=None,
                scheduled_at_us=clock.now_us(),
                deliver_after_us=clock.now_us(),
            )
            space_event_id = CognitiveEventService.create_internal(
                tx,
                tenant_id=ctx["tenant"],
                agent_id=ctx["agent"],
                space_group_id=None,
                space_id=ctx["space"],
                session_id=None,
                kind="task.due",
                object_type="task",
                object_id=due.task_id,
                occurrence_id=None,
                scheduled_at_us=clock.now_us() + 1,
                deliver_after_us=clock.now_us(),
            )
        orchestrator = StructuredRecallOrchestrator(
            ctx["store"],
            RecentContextService(ctx["store"], ctx["store"].clock),
            StateService(
                ctx["store"], ctx["store"].clock, gauge=ctx["gauge"], idempotency=ctx["idem"]
            ),
            FocusService(ctx["store"], ctx["store"].clock, idempotency=ctx["idem"]),
            clock=ctx["store"].clock,
            tasks=ctx["tasks"],
            events=ctx["events"],
        )
        result = orchestrator.recall(
            ctx["access"],
            StructuredRecallRequest(
                request_id="req-1",
                agent_id=ctx["agent"],
                space_id=ctx["space"],
                deadline_monotonic_us=10**15,
            ),
        )
        assert ROUTE_TASKS in result.completed_routes
        task_candidates = [c for c in result.candidates if c.route == ROUTE_TASKS]
        assert [c.resource_id for c in task_candidates] == [due.task_id]
        # Tasks outrank every other category in the stable sort.
        assert result.candidates[0].route == ROUTE_TASKS
        assert result.pending_event_ids == (event_id, space_event_id)

    def test_completed_task_dropped_by_rehydrate(self, jobs_ctx: dict[str, Any]) -> None:
        ctx = jobs_ctx
        clock = ctx["store"].clock
        task = ctx["tasks"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            title="finished",
            origin="explicit_tool",
            due_at_us=clock.now_us() - 1,
            idempotency_key="rh-1",
        )
        ctx["tasks"].transition(
            ctx["access"],
            task.task_id,
            "complete",
            expected_revision=1,
            origin="admin",
            reason="done",
            idempotency_key="rh-2",
        )
        orchestrator = StructuredRecallOrchestrator(
            ctx["store"],
            RecentContextService(ctx["store"], ctx["store"].clock),
            StateService(
                ctx["store"], ctx["store"].clock, gauge=ctx["gauge"], idempotency=ctx["idem"]
            ),
            FocusService(ctx["store"], ctx["store"].clock, idempotency=ctx["idem"]),
            clock=ctx["store"].clock,
            tasks=ctx["tasks"],
            events=ctx["events"],
        )
        result = orchestrator.recall(
            ctx["access"],
            StructuredRecallRequest(
                request_id="req-2",
                agent_id=ctx["agent"],
                space_id=ctx["space"],
                deadline_monotonic_us=10**15,
            ),
        )
        assert all(candidate.route != ROUTE_TASKS for candidate in result.candidates)

    def test_phase6_boundary_no_recall_contract_paths(self) -> None:
        """The /v1/recall protocol stays unpublished (Phase 6 boundary)."""
        import json
        from pathlib import Path

        openapi = json.loads(
            (
                Path(__file__).resolve().parents[2] / "schemas" / "openapi" / "openapi.json"
            ).read_text(encoding="utf-8")
        )
        recall_paths = [path for path in openapi["paths"] if "recall" in path]
        assert recall_paths == []
        vector_paths = [path for path in openapi["paths"] if "search" in path]
        assert vector_paths == []


class TestLeakScan:
    def test_canary_absent_from_jobs_audit_and_outbox(self, jobs_ctx: dict[str, Any]) -> None:
        ctx = jobs_ctx
        canary = "TOP_SECRET_CANARY_VALUE"
        ctx["notes"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            kind="idea",
            title=canary,
            body=canary,
            idempotency_key="leak-1",
        )
        task = ctx["tasks"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            title=canary,
            origin="conversation",
            idempotency_key="leak-2",
        )
        ctx["tasks"].transition(
            ctx["access"],
            task.task_id,
            "activate",
            expected_revision=1,
            origin="admin",
            reason="activation",
            idempotency_key="leak-3",
        )
        for _ in range(8):
            if ctx["worker"].run_once()["claimed"] == 0:
                break
        with ctx["store"].read() as tx:
            for table, column in (
                ("audit_events", "details"),
                ("outbox_jobs", "payload"),
                ("outbox_jobs", "last_error_code"),
            ):
                rows = tx.raw().execute(f"SELECT {column} FROM {table}").fetchall()
                for row in rows:
                    assert canary not in str(row[0]), (table, column)

    def _orchestrator(self, ctx: dict[str, Any]) -> Any:
        return StructuredRecallOrchestrator(
            ctx["store"],
            RecentContextService(ctx["store"], ctx["store"].clock),
            StateService(
                ctx["store"], ctx["store"].clock, gauge=ctx["gauge"], idempotency=ctx["idem"]
            ),
            FocusService(ctx["store"], ctx["store"].clock, idempotency=ctx["idem"]),
            clock=ctx["store"].clock,
            tasks=ctx["tasks"],
            events=ctx["events"],
        )

    def _pending_event(self, ctx: dict[str, Any], key: str, **overrides: Any) -> str:
        now = ctx["store"].clock.now_us()
        payload: dict[str, Any] = {
            "tenant_id": ctx["tenant"],
            "agent_id": ctx["agent"],
            "space_group_id": None,
            "space_id": None,
            "session_id": None,
            "kind": "task.due",
            "object_type": "task",
            "object_id": f"task-{key}",
            "occurrence_id": None,
            "scheduled_at_us": now,
            "deliver_after_us": now,
            "now_us": now,
        }
        payload.update(overrides)
        with ctx["store"].write() as tx:
            return CognitiveEventService.create_internal(tx, **payload)

    def test_pending_event_ids_follow_the_request_scope_matrix(
        self, jobs_ctx: dict[str, Any]
    ) -> None:
        """P0 gate (round 3): recall's pending_event_ids obey the CONCRETE
        request scope — agent-level events enter a Space-A request, Space A's
        events enter it, Space B's and other sessions' never do — instead of
        enumerating the whole access envelope."""
        ctx = jobs_ctx
        with ctx["store"].write() as tx:
            space_b = tx.insert_space(ctx["tenant"], "live_channel")
            session_1 = tx.insert_session(ctx["tenant"], ctx["space"], actor="t").id
            session_2 = tx.insert_session(ctx["tenant"], ctx["space"], actor="t").id
        wide = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"], space_b.id}),
            admin=True,
        )
        ctx["access"] = wide
        agent_level = self._pending_event(ctx, "r-agent")
        space_a = self._pending_event(ctx, "r-space-a", space_id=ctx["space"])
        space_b_id = self._pending_event(ctx, "r-space-b", space_id=space_b.id)
        in_session_1 = self._pending_event(ctx, "r-s1", space_id=ctx["space"], session_id=session_1)
        in_session_2 = self._pending_event(ctx, "r-s2", space_id=ctx["space"], session_id=session_2)
        orchestrator = self._orchestrator(ctx)

        def recall(session_id: str | None) -> tuple[str, ...]:
            result = orchestrator.recall(
                wide,
                StructuredRecallRequest(
                    request_id=f"scope-{session_id}",
                    agent_id=ctx["agent"],
                    space_id=ctx["space"],
                    session_id=session_id,
                    deadline_monotonic_us=10**15,
                ),
            )
            ids: tuple[str, ...] = result.pending_event_ids
            return ids

        # Space-A request without a session: agent-level + space-A only.
        assert set(recall(None)) == {agent_level, space_a}
        # Space-A request pinned to session 1 adds exactly that session's id.
        assert set(recall(session_1)) == {agent_level, space_a, in_session_1}
        # Space B and the other session never leak into either answer.
        for ids in (recall(None), recall(session_1)):
            assert space_b_id not in ids
            assert in_session_2 not in ids

    def test_pending_event_ids_not_starved_by_logically_dead_rows(
        self, jobs_ctx: dict[str, Any]
    ) -> None:
        """P1 gate (round 3): the request-scope match and the pending
        horizon run in SQL BEFORE the 50-id LIMIT, so a wall of dead rows
        cannot hide the live tail from recall."""
        ctx = jobs_ctx
        clock = ctx["store"].clock
        now = clock.now_us()
        for index in range(50):
            self._pending_event(
                ctx,
                f"dead-{index}",
                scheduled_at_us=now + index,
                expires_us=now - 1,
            )
        valid_id = self._pending_event(ctx, "alive", scheduled_at_us=now + 1_000)
        result = self._orchestrator(ctx).recall(
            ctx["access"],
            StructuredRecallRequest(
                request_id="starve",
                agent_id=ctx["agent"],
                space_id=ctx["space"],
                deadline_monotonic_us=10**15,
            ),
        )
        assert result.pending_event_ids == (valid_id,)
