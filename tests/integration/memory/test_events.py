"""Phase 4 CognitiveEvent delivery tests (§12, P4-EVENT-01/P4-SAFETY-01).

At-least-once pull, idempotent ACK, expiry + bounded summary merge, fence
re-delivery of the SAME event id, and the negative gates: delivered / acked /
expired / failed delivery never complete a task or step and never fabricate
evidence.
"""

from __future__ import annotations

from typing import Any

import pytest

from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.errors import (
    DomainError,
    InvalidRequestError,
    InvalidTransitionError,
    LeaseExpiredError,
    LeaseFencedError,
)
from iris_memory_core.domain.event import EventStatus
from iris_memory_core.domain.surface import SurfaceMode
from iris_memory_core.domain.task import TaskStepStatus
from tests.conftest import access_for


@pytest.fixture
def event_ctx(
    clocked_store: Any,
    clocked_tenant_id: str,
    phase2_agent: str,
    phase4_events: CognitiveEventService,
    phase4_tasks: Any,
) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space = tx.insert_space(clocked_tenant_id, "chat_group")
    access = access_for(
        clocked_tenant_id,
        agent_ids=frozenset({phase2_agent}),
        space_ids=frozenset({space.id}),
        admin=True,
    )
    return {
        "store": clocked_store,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "space": space.id,
        "access": access,
        "events": phase4_events,
        "tasks": phase4_tasks,
    }


def _event(ctx: dict[str, Any], key: str, **overrides: Any) -> str:
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
        # The injected clock owns the TTL too: without now_us the fallback
        # SystemClock would place expires_us on a different timeline than
        # every expiry assertion in this suite.
        "now_us": now,
    }
    payload.update(overrides)
    with ctx["store"].write() as tx:
        return CognitiveEventService.create_internal(tx, **payload)


class TestPullAndAck:
    def test_pending_without_holder_stays_pending_then_pull_delivers(
        self, event_ctx: dict[str, Any]
    ) -> None:
        ctx = event_ctx
        event_id = _event(ctx, "hold")
        listed = ctx["events"].list_events(ctx["access"], agent_id=ctx["agent"])
        assert [item[0].id for item in listed] == [event_id]
        assert listed[0][0].status == EventStatus.PENDING.value
        bundle = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        assert [item[0].id for item in bundle.events] == [event_id]
        with ctx["store"].read() as tx:
            pulled = tx.events.get(event_id)
        assert pulled.status == EventStatus.DELIVERED.value
        assert pulled.delivery_attempts == 1

    def test_not_due_event_is_not_pulled(self, event_ctx: dict[str, Any]) -> None:
        ctx = event_ctx
        clock = ctx["store"].clock
        _event(ctx, "future", deliver_after_us=clock.now_us() + 1_000_000)
        bundle = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        assert bundle.events == ()

    def test_ack_is_idempotent_hundred_times(self, event_ctx: dict[str, Any]) -> None:
        """§36 gate: 100 deliveries/ACKs of the same event stay idempotent —
        one acknowledged event, one ack id, one logical revision chain."""
        ctx = event_ctx
        event_id = _event(ctx, "ack100")
        ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        first = ctx["events"].ack(ctx["access"], event_id, idempotency_key="ack-1")
        for round_index in range(100):
            repeat = ctx["events"].ack(
                ctx["access"], event_id, idempotency_key=f"ack-r{round_index}"
            )
            assert repeat.ack_id == first.ack_id
        with ctx["store"].read() as tx:
            event = tx.events.get(event_id)
            revisions = tx.events.history(event_id)
        assert event.status == EventStatus.ACKNOWLEDGED.value
        assert event.delivery_attempts == 1
        # created → delivered → acknowledged: exactly three revisions, no more
        # regardless of how many ACK replays arrived.
        assert len(revisions) == 3

    def test_ack_requires_delivered_status(self, event_ctx: dict[str, Any]) -> None:
        ctx = event_ctx
        event_id = _event(ctx, "premature")
        with pytest.raises(InvalidTransitionError):
            ctx["events"].ack(ctx["access"], event_id, idempotency_key="pre-1")

    def test_redelivery_of_same_id_after_repull(self, event_ctx: dict[str, Any]) -> None:
        ctx = event_ctx
        event_id = _event(ctx, "re")
        first = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        assert [item[0].id for item in first.events] == [event_id]
        # A fence (see below) or a requeue returns the event to pending; the
        # next pull delivers the SAME id again (at-least-once).
        with ctx["store"].write() as tx:
            event = tx.events.get(event_id)
            assert ctx["events"].redeliver_after_fence(
                tx, agent_id=ctx["agent"], event=event, now_us=ctx["store"].clock.now_us()
            )
        second = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        assert [item[0].id for item in second.events] == [event_id]
        with ctx["store"].read() as tx:
            event = tx.events.get(event_id)
        assert event.delivery_attempts == 2
        assert event.status == EventStatus.DELIVERED.value


class TestExpiryAndSummary:
    def test_expired_event_never_resurrects(self, event_ctx: dict[str, Any]) -> None:
        ctx = event_ctx
        clock = ctx["store"].clock
        event_id = _event(ctx, "exp", expires_us=clock.now_us() + 500_000)
        clock.advance(600_000)
        bundle = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        assert bundle.events == ()
        assert bundle.expired == 1
        with ctx["store"].read() as tx:
            event = tx.events.get(event_id)
        assert event.status == EventStatus.EXPIRED.value
        with pytest.raises(InvalidTransitionError):
            ctx["events"].ack(ctx["access"], event_id, idempotency_key="exp-ack")

    def test_cancelled_event_is_terminal(self, event_ctx: dict[str, Any]) -> None:
        ctx = event_ctx
        event_id = _event(ctx, "cancel")
        ctx["events"].cancel(ctx["access"], event_id, reason="obsolete", idempotency_key="c1")
        with ctx["store"].read() as tx:
            assert tx.events.get(event_id).status == EventStatus.CANCELLED.value
        bundle = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        assert bundle.events == ()

    def test_large_expiry_batch_folds_into_one_bounded_summary(
        self, event_ctx: dict[str, Any]
    ) -> None:
        ctx = event_ctx
        clock = ctx["store"].clock
        for index in range(30):
            _event(
                ctx,
                f"bulk{index}",
                expires_us=clock.now_us() + 100,
                scheduled_at_us=clock.now_us() - index,
            )
        clock.advance(1_000)
        bundle = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        assert bundle.expired == 30
        with ctx["store"].read() as tx:
            summaries = (
                tx.raw()
                .execute(
                    "SELECT summary_of_count FROM cognitive_events "
                    "WHERE kind = 'summary.expired_events'"
                )
                .fetchall()
            )
        assert len(summaries) == 1
        assert summaries[0]["summary_of_count"] == 30


class TestHolderFence:
    def test_twenty_fence_scenarios_redeliver_same_id(self, event_ctx: dict[str, Any]) -> None:
        """§36 gate: 20 holder-fence / crash-recovery rounds re-deliver the
        SAME event id to the new holder, without ever duplicating it."""
        ctx = event_ctx
        coordinator = SurfaceCoordinatorService(ctx["store"], ctx["store"].clock)
        seen_ids: set[str] = set()
        for round_index in range(20):
            event_id = _event(ctx, f"fence{round_index}")
            first_holder = coordinator.acquire(
                ctx["access"],
                agent_id=ctx["agent"],
                holder_app_instance_id=ctx["access"].app_instance_id,
                ttl_us=60_000_000,
            )
            bundle = ctx["events"].pull(
                ctx["access"],
                agent_id=ctx["agent"],
                lease_id=first_holder.lease.lease_id,
                lease_epoch=first_holder.lease.lease_epoch,
            )
            assert [item[0].id for item in bundle.events] == [event_id]
            # The holder is fenced (preempted) before acknowledging.
            # Same authenticated instance preempts itself: the epoch bump is
            # what fences the in-flight holder.
            second_holder = coordinator.acquire(
                ctx["access"],
                agent_id=ctx["agent"],
                holder_app_instance_id=ctx["access"].app_instance_id,
                ttl_us=60_000_000,
                priority=5,  # preemption needs a higher priority than the holder
                allow_preempt=True,
                reason="holder fenced in fence-round test",
            )
            assert second_holder.lease.lease_epoch > first_holder.lease.lease_epoch
            with ctx["store"].write() as tx:
                event = tx.events.get(event_id)
                requeued = ctx["events"].redeliver_after_fence(
                    tx,
                    agent_id=ctx["agent"],
                    event=event,
                    now_us=ctx["store"].clock.now_us(),
                )
            assert requeued
            rebundle = ctx["events"].pull(
                ctx["access"],
                agent_id=ctx["agent"],
                lease_id=second_holder.lease.lease_id,
                lease_epoch=second_holder.lease.lease_epoch,
            )
            assert [item[0].id for item in rebundle.events] == [event_id]
            seen_ids.add(event_id)
            # The new holder finally ACKs; the round ends acknowledged and
            # the lease is released so the next round starts clean.
            ctx["events"].ack(ctx["access"], event_id, idempotency_key=f"fence-ack-{round_index}")
            coordinator.release(
                ctx["access"],
                second_holder.lease.lease_id,
                expected_epoch=second_holder.lease.lease_epoch,
                reason="round complete",
            )
        assert len(seen_ids) == 20
        with ctx["store"].read() as tx:
            total = tx.raw().execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
        # 20 logical events, no duplicates spawned by the fences.
        assert total == 20


class TestPullAuthorization:
    """P0 audit gates: pull must respect the per-event space envelope and a
    named lease must be a live lease of THIS tenant/agent/app."""

    def test_event_outside_space_envelope_is_not_pulled(self, event_ctx: dict[str, Any]) -> None:
        ctx = event_ctx
        # The event lives in a space the pulling app cannot see.
        with ctx["store"].write() as tx:
            hidden_space = tx.insert_space(ctx["tenant"], "chat_group")
        event_id = _event(ctx, "hidden", space_id=hidden_space.id)
        narrow = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"]}),
            admin=True,
        )
        bundle = ctx["events"].pull(narrow, agent_id=ctx["agent"])
        assert bundle.events == ()
        with ctx["store"].read() as tx:
            event = tx.events.get(event_id)
        assert event.status == EventStatus.PENDING.value
        # An app whose envelope contains the space delivers it normally.
        wide = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"], hidden_space.id}),
            admin=True,
        )
        bundle = ctx["events"].pull(wide, agent_id=ctx["agent"])
        assert [item[0].id for item in bundle.events] == [event_id]

    def test_pull_proof_pairing_and_unknown_lease_follow_the_mode_matrix(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """Round-4 P1: a presented pull proof follows the surface mode matrix.

        Pairing errors (id without epoch or the reverse) are request-shape
        errors in EVERY mode. An unknown lease id is ignored under off
        (lease-less delivery, no warning), warned-and-ignored under advisory
        and fenced under required — off must never reject on lease policy.
        """
        ctx = event_ctx
        event_id = _event(ctx, "lease-guard")
        with pytest.raises(InvalidRequestError):
            # A named lease without its epoch can never be arbitrated by the
            # fence, so it is rejected up front.
            ctx["events"].pull(ctx["access"], agent_id=ctx["agent"], lease_id="lease-no-epoch")
        with pytest.raises(InvalidRequestError):
            ctx["events"].pull(ctx["access"], agent_id=ctx["agent"], lease_epoch=3)
        coordinator = SurfaceCoordinatorService(ctx["store"], ctx["store"].clock)
        # off: the unknown proof is ignored; the same pull still delivers,
        # lease-less (nothing is stamped from a proof nobody holds).
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id="lease-does-not-exist",
            lease_epoch=1,
        )
        assert [item[0].id for item in bundle.events] == [event_id]
        with ctx["store"].read() as tx:
            assert tx.events.get(event_id).delivered_lease_id is None
        # advisory: delivered again lease-less, but the reason is reported.
        advisory_id = _event(ctx, "lease-guard-adv")
        coordinator.set_mode(
            ctx["access"], ctx["agent"], SurfaceMode.ADVISORY, reason="mode matrix test"
        )
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id="lease-does-not-exist",
            lease_epoch=1,
        )
        assert [item[0].id for item in bundle.events] == [advisory_id]
        assert bundle.lease_warning == "stale_lease_id"
        with ctx["store"].read() as tx:
            assert tx.events.get(advisory_id).delivered_lease_id is None
        # required: the same proof fails closed with the stable code.
        blocked_id = _event(ctx, "lease-guard-req")
        coordinator.set_mode(
            ctx["access"], ctx["agent"], SurfaceMode.REQUIRED, reason="mode matrix test"
        )
        with pytest.raises(LeaseFencedError):
            ctx["events"].pull(
                ctx["access"],
                agent_id=ctx["agent"],
                lease_id="lease-does-not-exist",
                lease_epoch=1,
            )
        with ctx["store"].read() as tx:
            assert tx.events.get(blocked_id).status == EventStatus.PENDING.value

    def test_expired_pull_proof_follows_the_mode_matrix(self, event_ctx: dict[str, Any]) -> None:
        """A lease whose expires_us has passed is dead even while its row
        still reads active (the surface expiry job may not have flipped
        it) — but only ``required`` turns that into a rejection: off
        ignores the dead proof and delivers lease-less, advisory warns."""
        ctx = event_ctx
        coordinator = SurfaceCoordinatorService(ctx["store"], ctx["store"].clock)
        holder = coordinator.acquire(
            ctx["access"],
            agent_id=ctx["agent"],
            holder_app_instance_id=ctx["access"].app_instance_id,
            ttl_us=60_000_000,
        )
        expired_id = _event(ctx, "lease-dead")
        ctx["store"].clock.advance(61_000_000)
        # off: the dead proof is ignored — the event still delivers, but as
        # a lease-less delivery (the dead lease never becomes the holder).
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=holder.lease.lease_id,
            lease_epoch=holder.lease.lease_epoch,
        )
        assert [item[0].id for item in bundle.events] == [expired_id]
        with ctx["store"].read() as tx:
            assert tx.events.get(expired_id).delivered_lease_id is None
        # advisory: warn, never block.
        advisory_id = _event(ctx, "lease-dead-adv")
        coordinator.set_mode(
            ctx["access"], ctx["agent"], SurfaceMode.ADVISORY, reason="mode matrix test"
        )
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=holder.lease.lease_id,
            lease_epoch=holder.lease.lease_epoch,
        )
        assert [item[0].id for item in bundle.events] == [advisory_id]
        assert bundle.lease_warning == "no_active_lease"
        # required: fail closed with the stable lease_expired code.
        blocked_id = _event(ctx, "lease-dead-req")
        coordinator.set_mode(
            ctx["access"], ctx["agent"], SurfaceMode.REQUIRED, reason="mode matrix test"
        )
        with pytest.raises(LeaseExpiredError):
            ctx["events"].pull(
                ctx["access"],
                agent_id=ctx["agent"],
                lease_id=holder.lease.lease_id,
                lease_epoch=holder.lease.lease_epoch,
            )
        with ctx["store"].read() as tx:
            assert tx.events.get(blocked_id).status == EventStatus.PENDING.value

    def test_other_agents_lease_proof_follows_the_mode_matrix(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """Presenting ANOTHER agent's live lease: off ignores it (lease-less
        delivery — the stranger's lease is never stamped as holder),
        advisory warns, required fences with the stable code."""
        ctx = event_ctx
        with ctx["store"].write() as tx:
            stranger = tx.insert_agent(ctx["tenant"], "Lease Stranger", actor="test")
        stranger_access = access_for(
            ctx["tenant"],
            agent_ids=frozenset({stranger.id}),
            space_ids=frozenset({ctx["space"]}),
            admin=True,
            app_instance_id="app-stranger",
        )
        coordinator = SurfaceCoordinatorService(ctx["store"], ctx["store"].clock)
        stranger_lease = coordinator.acquire(
            stranger_access,
            agent_id=stranger.id,
            holder_app_instance_id="app-stranger",
            ttl_us=60_000_000,
        )
        event_id = _event(ctx, "lease-foreign")
        # off: ignored — the delivery carries no lease at all.
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=stranger_lease.lease.lease_id,
            lease_epoch=stranger_lease.lease.lease_epoch,
        )
        assert [item[0].id for item in bundle.events] == [event_id]
        with ctx["store"].read() as tx:
            assert tx.events.get(event_id).delivered_lease_id is None
        # required: fenced with the stable code, nothing delivered.
        blocked_id = _event(ctx, "lease-foreign-req")
        coordinator.set_mode(
            ctx["access"], ctx["agent"], SurfaceMode.REQUIRED, reason="mode matrix test"
        )
        with pytest.raises(LeaseFencedError):
            ctx["events"].pull(
                ctx["access"],
                agent_id=ctx["agent"],
                lease_id=stranger_lease.lease.lease_id,
                lease_epoch=stranger_lease.lease.lease_epoch,
            )
        with ctx["store"].read() as tx:
            assert tx.events.get(blocked_id).status == EventStatus.PENDING.value
        coordinator.release(
            stranger_access,
            stranger_lease.lease.lease_id,
            expected_epoch=stranger_lease.lease.lease_epoch,
            reason="test cleanup",
        )


class TestSweepFenceRedelivery:
    """P1 audit gate: a fenced holder is requeued by the SWEEP itself, while
    the event's expiry horizon is still open and its attempts are far below
    the cap — the path the old expiry-only candidacy could never reach."""

    def test_fenced_delivered_event_requeued_by_sweep_then_redelivered_same_id(
        self, event_ctx: dict[str, Any]
    ) -> None:
        ctx = event_ctx
        coordinator = SurfaceCoordinatorService(ctx["store"], ctx["store"].clock)
        event_id = _event(ctx, "sweep-fence")
        holder = coordinator.acquire(
            ctx["access"],
            agent_id=ctx["agent"],
            holder_app_instance_id=ctx["access"].app_instance_id,
            ttl_us=60_000_000,
        )
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=holder.lease.lease_id,
            lease_epoch=holder.lease.lease_epoch,
        )
        assert [item[0].id for item in bundle.events] == [event_id]
        # The holder is preempted and never acks; the horizon stays open.
        successor = coordinator.acquire(
            ctx["access"],
            agent_id=ctx["agent"],
            holder_app_instance_id=ctx["access"].app_instance_id,
            ttl_us=60_000_000,
            priority=5,
            allow_preempt=True,
            reason="sweep-fence test preempts the holder",
        )
        # One pull whose sweep pre-pass requeues the fenced event — and,
        # because the sweep runs BEFORE the delivery loop, the very same
        # pull re-delivers the SAME id to the new holder.
        middle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=successor.lease.lease_id,
            lease_epoch=successor.lease.lease_epoch,
        )
        assert [item[0].id for item in middle.events] == [event_id]
        with ctx["store"].read() as tx:
            after_sweep = tx.events.get(event_id)
        assert after_sweep.status == EventStatus.DELIVERED.value
        assert after_sweep.delivery_attempts == 2
        ctx["events"].ack(ctx["access"], event_id, idempotency_key="sweep-fence-ack")
        coordinator.release(
            ctx["access"],
            successor.lease.lease_id,
            expected_epoch=successor.lease.lease_epoch,
            reason="round complete",
        )
        with ctx["store"].read() as tx:
            total = tx.raw().execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
        assert total == 1  # no duplicates spawned

    def test_configurable_max_attempts_expires_via_sweep(self, event_ctx: dict[str, Any]) -> None:
        """The delivery-attempt cap is the SERVICE's configuration, not a
        hardcoded SQL constant: with max_attempts=1 a single unacked pull
        expires on the next sweep."""
        from iris_memory_core.application.events import CognitiveEventService
        from iris_memory_core.storage.idempotency import IdempotencyManager

        ctx = event_ctx
        strict = CognitiveEventService(
            ctx["store"],
            ctx["store"].clock,
            max_attempts=1,
            idempotency=IdempotencyManager(ctx["store"]),
        )
        event_id = _event(ctx, "attempts-cap")
        bundle = strict.pull(ctx["access"], agent_id=ctx["agent"])
        assert [item[0].id for item in bundle.events] == [event_id]
        clock = ctx["store"].clock
        _event(ctx, "attempts-cap-2")
        clock.advance(10)
        second = strict.pull(ctx["access"], agent_id=ctx["agent"])
        # The over-attempt event expired in the sweep; only the fresh one
        # survives as a delivery.
        assert [item[0].id for item in second.events] != [event_id]
        with ctx["store"].read() as tx:
            expired = tx.events.get(event_id)
        assert expired.status == EventStatus.EXPIRED.value


class TestNegativeCompletionGates:
    """Delivered / ACK / Expired / failed delivery NEVER complete a task or
    step and never fabricate completion evidence (P4-SAFETY-01)."""

    def _task_with_effect_step(self, ctx: dict[str, Any], key: str) -> tuple[str, str, int]:
        task = ctx["tasks"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            title=f"task {key}",
            origin="explicit_tool",
            idempotency_key=f"ng-t-{key}",
        )
        step = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="act",
            title="Do the effect",
            expected_effect="external message delivered",
            idempotency_key=f"ng-s-{key}",
        )
        return task.task_id, step.step_id, step.revision

    def test_delivered_ack_expired_do_not_complete_task_or_step(
        self, event_ctx: dict[str, Any]
    ) -> None:
        ctx = event_ctx
        clock = ctx["store"].clock
        for scenario in ("delivered", "acked", "expired"):
            task_id, step_id, _ = self._task_with_effect_step(ctx, scenario)
            event_id = _event(ctx, f"ng-{scenario}", object_type="task_step", object_id=step_id)
            if scenario == "delivered":
                ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
            elif scenario == "acked":
                ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
                ctx["events"].ack(ctx["access"], event_id, idempotency_key=f"ng-a-{scenario}")
            else:
                with ctx["store"].write() as tx:
                    tx.events.advance_pointer(
                        event_id,
                        expected_revision=1,
                        revision=2,
                        revision_id=tx.events.current_revision_row(event_id).id,
                        status="pending",
                    )
                clock.advance(8 * 86_400_000_000)  # past the default horizon
                ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
            with ctx["store"].read() as tx:
                task = tx.tasks.get_task(task_id)
                step = tx.tasks.current_step_revision_row(step_id)
            assert task.status != "completed", scenario
            assert step.status not in (TaskStepStatus.COMPLETED.value,), scenario
            assert step.completion_evidence_refs == (), scenario

    def test_event_evidence_is_not_completion_evidence(self, event_ctx: dict[str, Any]) -> None:
        """A CognitiveEvent may not serve as step-completion evidence: only
        committed observations (real external effects) qualify."""
        from iris_memory_core.domain.errors import InvalidRequestError

        ctx = event_ctx
        task_id, step_id, revision = self._task_with_effect_step(ctx, "evidence")
        event_id = _event(ctx, "ng-evidence", object_type="cognitive_event", object_id="x")
        ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        ctx["events"].ack(ctx["access"], event_id, idempotency_key="ng-e-ack")
        started = ctx["tasks"].transition_step(
            ctx["access"],
            task_id,
            step_id,
            "start",
            expected_revision=revision,
            reason="go",
            idempotency_key="ng-e-start",
        )
        with pytest.raises(InvalidRequestError) as captured:
            ctx["tasks"].transition_step(
                ctx["access"],
                task_id,
                step_id,
                "complete",
                expected_revision=started.revision,
                reason="done",
                completion_evidence_refs=[
                    {"resource_type": "cognitive_event", "resource_id": event_id}
                ],
                idempotency_key="ng-e-complete",
            )
        assert "evidence resource type not allowed" in str(captured.value)

    def test_failed_delivery_leaves_event_requeued_not_completed(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """A lease that dies mid-delivery (crash before the host saw it) is
        fenced on the next sweep: the event returns to pending, the SAME id
        is delivered again, and no task/step state moved."""
        ctx = event_ctx
        task_id, step_id, _revision = self._task_with_effect_step(ctx, "fail")
        event_id = _event(ctx, "ng-fail", object_type="task_step", object_id=step_id)
        ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])  # "delivery" happened
        with ctx["store"].write() as tx:
            event = tx.events.get(event_id)
            assert ctx["events"].redeliver_after_fence(
                tx, agent_id=ctx["agent"], event=event, now_us=ctx["store"].clock.now_us()
            )
        with ctx["store"].read() as tx:
            event = tx.events.get(event_id)
        assert event.status == EventStatus.PENDING.value
        assert event.id == event_id  # SAME id, no duplicate spawned
        with ctx["store"].read() as tx:
            task = tx.tasks.get_task(task_id)
            step = tx.tasks.current_step_revision_row(step_id)
        assert task.status != "completed"
        assert step.status != TaskStepStatus.COMPLETED.value


class TestEventSecurity:
    def test_cross_tenant_and_agent_denied(self, event_ctx: dict[str, Any]) -> None:
        ctx = event_ctx
        event_id = _event(ctx, "sec")
        stranger = access_for("tenant-b", agent_ids=frozenset({"no"}))
        with pytest.raises(DomainError):
            ctx["events"].list_events(stranger, agent_id=ctx["agent"])
        # Cross-tenant by-ID reads/mutations fail closed (tenant gate first).
        with pytest.raises(DomainError):
            ctx["events"].get(stranger, event_id)
        with pytest.raises(DomainError):
            ctx["events"].ack(stranger, event_id, idempotency_key="sec-1")

    def test_tombstoned_event_hidden(self, event_ctx: dict[str, Any]) -> None:
        ctx = event_ctx
        event_id = _event(ctx, "gone")
        with ctx["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=ctx["tenant"],
                resource_type="cognitive_event",
                resource_id=event_id,
                reason_code="forget",
                deleted_by="test",
            )
        assert ctx["events"].list_events(ctx["access"], agent_id=ctx["agent"]) == []
        bundle = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        assert bundle.events == ()


class TestAuditRoundTwo:
    """Second review round (2026-09-01): event lifecycle boundaries the first
    regression pass left open — expired-holder fencing, sweep/delivery
    ordering, envelope starvation, listing states, and ACK holder identity."""

    def test_time_expired_lease_is_fenced_by_sweep_and_redelivered(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """P1 gate: a lease past its expires_us is dead even while its row
        still reads active — the sweep requeues the delivered event instead
        of leaving it stranded on a holder that no longer exists."""
        ctx = event_ctx
        event_id = _event(ctx, "time-fence")
        coordinator = SurfaceCoordinatorService(ctx["store"], ctx["store"].clock)
        holder = coordinator.acquire(
            ctx["access"],
            agent_id=ctx["agent"],
            holder_app_instance_id=ctx["access"].app_instance_id,
            ttl_us=60_000_000,
        )
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=holder.lease.lease_id,
            lease_epoch=holder.lease.lease_epoch,
        )
        assert [item[0].id for item in bundle.events] == [event_id]
        # The lease expires in TIME; the surface expiry job has not flipped
        # the row, so it still reads 'active'.
        ctx["store"].clock.advance(61_000_000)
        with ctx["store"].read() as tx:
            lease_row = tx.surfaces.get_lease(holder.lease.lease_id)
        assert lease_row.status == "active"
        with ctx["store"].write() as tx:
            expired = ctx["events"].expire_sweep_in_tx(
                tx, agent_id_scope=(ctx["tenant"], ctx["agent"])
            )
        assert expired == 0  # requeued by the fence, not expired
        with ctx["store"].read() as tx:
            after = tx.events.get(event_id)
        assert after.status == EventStatus.PENDING.value
        # A fresh holder receives the SAME id again.
        successor = coordinator.acquire(
            ctx["access"],
            agent_id=ctx["agent"],
            holder_app_instance_id=ctx["access"].app_instance_id,
            ttl_us=60_000_000,
        )
        again = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=successor.lease.lease_id,
            lease_epoch=successor.lease.lease_epoch,
        )
        assert [item[0].id for item in again.events] == [event_id]
        coordinator.release(
            ctx["access"],
            successor.lease.lease_id,
            expected_epoch=successor.lease.lease_epoch,
            reason="test cleanup",
        )

    def test_first_delivery_at_attempt_cap_is_returned_alive_and_ackable(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """P1 gate: with max_attempts=1 the FIRST pull returns the event in
        'delivered' (not dead-on-arrival 'expired'), so the holder's ACK
        lands instead of hitting invalid_state_transition."""
        from iris_memory_core.application.events import CognitiveEventService
        from iris_memory_core.storage.idempotency import IdempotencyManager

        ctx = event_ctx
        strict = CognitiveEventService(
            ctx["store"],
            ctx["store"].clock,
            max_attempts=1,
            idempotency=IdempotencyManager(ctx["store"]),
        )
        event_id = _event(ctx, "alive-at-cap")
        bundle = strict.pull(ctx["access"], agent_id=ctx["agent"])
        assert [item[0].id for item in bundle.events] == [event_id]
        assert bundle.expired == 0
        with ctx["store"].read() as tx:
            after = tx.events.get(event_id)
        assert after.status == EventStatus.DELIVERED.value
        assert after.delivery_attempts == 1
        result = strict.ack(ctx["access"], event_id, idempotency_key="cap-ack-1")
        assert result.event_id == event_id

    def test_invisible_events_do_not_starve_the_visible_tail(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """P1 gate: the space envelope is applied in SQL, so a batch full of
        envelope-invisible events can never push a visible pending event
        beyond the LIMIT."""
        from iris_memory_core.storage.plans import CognitiveEventRepository  # noqa: F401

        ctx = event_ctx
        clock = ctx["store"].clock
        now = clock.now_us()
        with ctx["store"].write() as tx:
            hidden_space = tx.insert_space(ctx["tenant"], "chat_group")
        # 50 invisible events, ALL scheduled before the visible one: under
        # LIMIT-then-filter they would permanently occupy the batch.
        for index in range(50):
            _event(
                ctx,
                f"starve-{index}",
                space_id=hidden_space.id,
                scheduled_at_us=now + index,
                deliver_after_us=now,
            )
        visible_id = _event(
            ctx, "starve-visible", scheduled_at_us=now + 1_000, deliver_after_us=now
        )
        narrow = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"]}),
            admin=True,
        )
        bundle = ctx["events"].pull(narrow, agent_id=ctx["agent"])
        assert [item[0].id for item in bundle.events] == [visible_id]
        with ctx["store"].read() as tx:
            event = tx.events.get(visible_id)
        assert event.status == EventStatus.DELIVERED.value

    def test_listing_covers_acknowledged_expired_cancelled_and_space_scoped(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """P1 gate: the listing uses the access envelope (space-scoped events
        of registered spaces ARE listed) and passes statuses to SQL, so the
        OpenAPI-documented acknowledged/expired/cancelled filters return
        real rows instead of a structural empty set."""
        ctx = event_ctx
        clock = ctx["store"].clock
        acked_id = _event(ctx, "ls-acked")
        ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        ctx["events"].ack(ctx["access"], acked_id, idempotency_key="ls-a1")
        cancelled_id = _event(ctx, "ls-cancelled")
        ctx["events"].cancel(
            ctx["access"], cancelled_id, reason="obsolete", idempotency_key="ls-c1"
        )
        expired_id = _event(ctx, "ls-expired")
        clock.advance(8 * 86_400_000_000)  # past the one-week horizon
        with ctx["store"].write() as tx:
            ctx["events"].expire_sweep_in_tx(tx, agent_id_scope=(ctx["tenant"], ctx["agent"]))
        space_scoped_id = _event(ctx, "ls-space", space_id=ctx["space"])
        listed_by_status = {
            status: {
                item[0].id
                for item in ctx["events"].list_events(
                    ctx["access"], agent_id=ctx["agent"], statuses=(status,)
                )
            }
            for status in ("acknowledged", "cancelled", "expired", "pending")
        }
        assert listed_by_status["acknowledged"] == {acked_id}
        assert listed_by_status["cancelled"] == {cancelled_id}
        assert listed_by_status["expired"] == {expired_id}
        # The space-scoped event is inside this access's registered spaces.
        assert listed_by_status["pending"] == {space_scoped_id}

    def test_only_the_recorded_delivery_holder_may_ack(self, event_ctx: dict[str, Any]) -> None:
        """P1 gate: envelope access is not enough to ACK a lease-held
        delivery — another app instance of the same agent/space must be
        refused, or it would swallow the delivery and block the fence."""
        from iris_memory_core.domain.errors import AccessDeniedError

        ctx = event_ctx
        event_id = _event(ctx, "ack-holder")
        coordinator = SurfaceCoordinatorService(ctx["store"], ctx["store"].clock)
        holder = coordinator.acquire(
            ctx["access"],
            agent_id=ctx["agent"],
            holder_app_instance_id=ctx["access"].app_instance_id,
            ttl_us=60_000_000,
        )
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=holder.lease.lease_id,
            lease_epoch=holder.lease.lease_epoch,
        )
        assert [item[0].id for item in bundle.events] == [event_id]
        neighbor = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"]}),
            admin=True,
            app_instance_id="app-neighbor",
        )
        with pytest.raises(AccessDeniedError, match="recorded delivery holder"):
            ctx["events"].ack(neighbor, event_id, idempotency_key="ack-n1")
        # The event is still delivered — the failed ACK changed nothing.
        with ctx["store"].read() as tx:
            after = tx.events.get(event_id)
        assert after.status == EventStatus.DELIVERED.value
        # The holder itself acks normally.
        result = ctx["events"].ack(ctx["access"], event_id, idempotency_key="ack-h1")
        assert result.event_id == event_id
        coordinator.release(
            ctx["access"],
            holder.lease.lease_id,
            expected_epoch=holder.lease.lease_epoch,
            reason="test cleanup",
        )


class TestAuditRoundThree:
    """Third review round (2026-09-01): scope-grouped expiry summaries, ACK
    holder liveness, LIMIT-before-filter starvation, and post-CAS bundle
    consistency."""

    def _expired_event(self, ctx: dict[str, Any], key: str, **overrides: Any) -> str:
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
            "expires_us": now - 1,  # already past its horizon
        }
        payload.update(overrides)
        with ctx["store"].write() as tx:
            return CognitiveEventService.create_internal(tx, **payload)

    def _sweep(self, ctx: dict[str, Any]) -> int:
        with ctx["store"].write() as tx:
            expired: int = ctx["events"].expire_sweep_in_tx(
                tx, agent_id_scope=(ctx["tenant"], ctx["agent"])
            )
            return expired

    def test_expiry_summary_stays_inside_its_scope_group(self, event_ctx: dict[str, Any]) -> None:
        """P0 gate: 21 Space-B expiries fold into a Space-B summary. A
        Space-A puller receives no summary, no leaked first id, no count —
        the summary never gets promoted to agent scope."""
        ctx = event_ctx
        with ctx["store"].write() as tx:
            space_b = tx.insert_space(ctx["tenant"], "chat_group")
        hidden_ids = {
            self._expired_event(ctx, f"sum-b-{index}", space_id=space_b.id) for index in range(21)
        }
        assert self._sweep(ctx) == 21
        with ctx["store"].read() as tx:
            rows = (
                tx.raw()
                .execute(
                    "SELECT id, kind, space_id, object_id, summary_of_count "
                    "FROM cognitive_events WHERE kind = 'summary.expired_events'"
                )
                .fetchall()
            )
        assert len(rows) == 1
        summary = rows[0]
        assert summary["space_id"] == space_b.id  # inherited, not agent-level
        assert summary["object_id"] in hidden_ids  # same-group reference only
        assert summary["summary_of_count"] == 21
        with ctx["store"].read() as tx:
            links = (
                tx.raw()
                .execute(
                    "SELECT target_id FROM resource_links "
                    "WHERE source_type = 'cognitive_event' AND source_id = ? "
                    "AND relation = 'summary_of'",
                    (summary["id"],),
                )
                .fetchall()
            )
        assert {row["target_id"] for row in links} <= hidden_ids
        # A Space-A-only puller receives nothing: no summary, no hidden ids.
        space_a_puller = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"]}),
            admin=True,
        )
        bundle = ctx["events"].pull(space_a_puller, agent_id=ctx["agent"])
        assert bundle.events == ()
        listed = ctx["events"].list_events(space_a_puller, agent_id=ctx["agent"])
        assert [item[0].id for item in listed] == []
        # A Space-B puller sees exactly the same-scope summary.
        space_b_puller = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({space_b.id}),
            admin=True,
        )
        bundle_b = ctx["events"].pull(space_b_puller, agent_id=ctx["agent"])
        assert [item[0].id for item in bundle_b.events] == [summary["id"]]

    def test_expiry_summaries_never_stitch_scopes_past_the_threshold(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """P0 gate: three scope groups each above the threshold produce three
        summaries that never cross-reference; two groups below the threshold
        produce none even though their total exceeds it."""
        ctx = event_ctx
        with ctx["store"].write() as tx:
            space_a2 = tx.insert_space(ctx["tenant"], "chat_group")
            session_s = tx.insert_session(ctx["tenant"], space_a2.id, actor="t").id
        agent_ids = {self._expired_event(ctx, f"mix-agent-{index}") for index in range(21)}
        space_ids = {
            self._expired_event(ctx, f"mix-space-{index}", space_id=space_a2.id)
            for index in range(21)
        }
        session_ids = {
            self._expired_event(
                ctx, f"mix-session-{index}", space_id=space_a2.id, session_id=session_s
            )
            for index in range(21)
        }
        assert self._sweep(ctx) == 63
        with ctx["store"].read() as tx:
            summaries = (
                tx.raw()
                .execute(
                    "SELECT id, space_id, session_id, summary_of_count FROM cognitive_events "
                    "WHERE kind = 'summary.expired_events'"
                )
                .fetchall()
            )
        assert len(summaries) == 3
        by_scope = {(row["space_id"], row["session_id"]): row for row in summaries}
        assert set(by_scope) == {(None, None), (space_a2.id, None), (space_a2.id, session_s)}
        with ctx["store"].read() as tx:
            links = (
                tx.raw()
                .execute(
                    "SELECT source_id, target_id FROM resource_links WHERE relation = 'summary_of'"
                )
                .fetchall()
            )
        # No summary references an event of another scope group.
        for row in links:
            if row["source_id"] == by_scope[(None, None)]["id"]:
                assert row["target_id"] in agent_ids
            elif row["source_id"] == by_scope[(space_a2.id, None)]["id"]:
                assert row["target_id"] in space_ids
            else:
                assert row["target_id"] in session_ids
        # Below-threshold groups never merge into a summary via stitching.
        ctx2_ids = {
            self._expired_event(ctx, f"split-{index}", space_id=space_a2.id) for index in range(10)
        } | {self._expired_event(ctx, f"split2-{index}") for index in range(15)}
        self._sweep(ctx)
        with ctx["store"].read() as tx:
            total = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM cognitive_events WHERE kind = 'summary.expired_events'"
                )
                .fetchone()[0]
            )
        assert total == 3  # no new summary for the 10+15 split
        assert len(ctx2_ids) == 25

    def test_ack_rejected_when_lease_expired_in_time_but_row_still_active(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """P1 gate: a holder whose lease passed expires_us cannot ACK — even
        while the surface row still reads active — and the sweep then
        re-delivers the SAME id to a fresh holder, whose ACK sticks and whose
        replays keep returning the first ack_id."""
        from iris_memory_core.domain.errors import AccessDeniedError

        ctx = event_ctx
        event_id = _event(ctx, "ack-live")
        coordinator = SurfaceCoordinatorService(ctx["store"], ctx["store"].clock)
        holder = coordinator.acquire(
            ctx["access"],
            agent_id=ctx["agent"],
            holder_app_instance_id=ctx["access"].app_instance_id,
            ttl_us=60_000_000,
        )
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=holder.lease.lease_id,
            lease_epoch=holder.lease.lease_epoch,
        )
        assert [item[0].id for item in bundle.events] == [event_id]
        ctx["store"].clock.advance(61_000_000)
        with ctx["store"].read() as tx:
            assert tx.surfaces.get_lease(holder.lease.lease_id).status == "active"
        with pytest.raises(AccessDeniedError, match="expired"):
            ctx["events"].ack(
                ctx["access"],
                event_id,
                lease_id=holder.lease.lease_id,
                lease_epoch=holder.lease.lease_epoch,
                idempotency_key="ack-live-1",
            )
        # The rejection changed nothing: still delivered, still unacked.
        with ctx["store"].read() as tx:
            after = tx.events.get(event_id)
        assert after.status == EventStatus.DELIVERED.value
        assert after.acknowledged_us is None
        # The sweep requeues it and a fresh holder completes the ACK.
        with ctx["store"].write() as tx:
            ctx["events"].expire_sweep_in_tx(tx, agent_id_scope=(ctx["tenant"], ctx["agent"]))
        successor = coordinator.acquire(
            ctx["access"],
            agent_id=ctx["agent"],
            holder_app_instance_id=ctx["access"].app_instance_id,
            ttl_us=60_000_000,
        )
        again = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=successor.lease.lease_id,
            lease_epoch=successor.lease.lease_epoch,
        )
        assert [item[0].id for item in again.events] == [event_id]
        first = ctx["events"].ack(
            ctx["access"],
            event_id,
            lease_id=successor.lease.lease_id,
            lease_epoch=successor.lease.lease_epoch,
            idempotency_key="ack-live-2",
        )
        # Idempotent replay: same ack id, no revision growth.
        replay = ctx["events"].ack(
            ctx["access"],
            event_id,
            lease_id=successor.lease.lease_id,
            lease_epoch=successor.lease.lease_epoch,
            idempotency_key="ack-live-3",
        )
        assert replay.ack_id == first.ack_id
        with ctx["store"].read() as tx:
            assert len(tx.events.history(event_id)) == 5  # created→delivered→fenced→delivered→acked
        coordinator.release(
            ctx["access"],
            successor.lease.lease_id,
            expected_epoch=successor.lease.lease_epoch,
            reason="test cleanup",
        )

    def test_logically_dead_events_do_not_starve_the_visible_tail(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """P1 gate: pending rows past their horizon and tombstoned rows are
        excluded IN SQL, so 50 dead rows cannot push the 51st live event past
        the listing LIMIT."""
        ctx = event_ctx
        clock = ctx["store"].clock
        now = clock.now_us()
        # 50 logically-expired pending rows, all scheduled FIRST.
        dead_ids = [
            self._expired_event(ctx, f"dead-{index}", scheduled_at_us=now + index)
            for index in range(50)
        ]
        # 50 tombstoned live-horizon rows next.
        tomb_ids = [
            _event(ctx, f"tomb-{index}", scheduled_at_us=now + 100 + index) for index in range(50)
        ]
        with ctx["store"].write() as tx:
            for event_id in tomb_ids:
                tx.record_tombstone(
                    tenant_id=ctx["tenant"],
                    resource_type="cognitive_event",
                    resource_id=event_id,
                    reason_code="forget",
                    deleted_by="test",
                )
        valid_id = _event(ctx, "alive-tail", scheduled_at_us=now + 1_000)
        listed = ctx["events"].list_events(ctx["access"], agent_id=ctx["agent"], limit=50)
        assert [item[0].id for item in listed] == [valid_id]
        # The pull path is equally immune (tombstones and past-horizon rows
        # are excluded in SQL). Its sweep pre-pass folds the 50 dead rows
        # into ONE agent-scope summary — a legitimate NEW pending event —
        # which must not crowd out the live tail either.
        bundle = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        pulled_ids = [item[0].id for item in bundle.events]
        assert valid_id in pulled_ids
        assert not set(dead_ids) & set(pulled_ids)
        assert not set(tomb_ids) & set(pulled_ids)

    def test_pull_returns_post_cas_current_with_delivered_revision(
        self, event_ctx: dict[str, Any]
    ) -> None:
        """P1 gate: the bundle carries the POST-CAS current — status
        delivered, advanced revision, stamped attempts/lease/delivery time —
        never the pending snapshot read at loop start."""
        ctx = event_ctx
        event_id = _event(ctx, "bundle-current")
        with ctx["store"].read() as tx:
            before = tx.events.get(event_id)
        assert before.status == EventStatus.PENDING.value
        coordinator = SurfaceCoordinatorService(ctx["store"], ctx["store"].clock)
        holder = coordinator.acquire(
            ctx["access"],
            agent_id=ctx["agent"],
            holder_app_instance_id=ctx["access"].app_instance_id,
            ttl_us=60_000_000,
        )
        pull_now = ctx["store"].clock.now_us()
        bundle = ctx["events"].pull(
            ctx["access"],
            agent_id=ctx["agent"],
            lease_id=holder.lease.lease_id,
            lease_epoch=holder.lease.lease_epoch,
        )
        assert len(bundle.events) == 1
        current, revision = bundle.events[0]
        assert current.id == event_id == revision.event_id
        assert current.status == EventStatus.DELIVERED.value == revision.status
        assert current.current_revision == revision.revision == before.current_revision + 1
        assert current.delivery_attempts == revision.delivery_attempts == 1
        assert current.delivered_lease_id == holder.lease.lease_id
        assert current.delivered_lease_epoch == holder.lease.lease_epoch
        assert current.last_delivery_us == revision.last_delivery_us == pull_now
        coordinator.release(
            ctx["access"],
            holder.lease.lease_id,
            expected_epoch=holder.lease.lease_epoch,
            reason="test cleanup",
        )
