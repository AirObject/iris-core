"""Round-3 P0 gate: Phase 4 application-plane writes honor the Active
Surface online gate (§25.3).

The gate is the Phase 2 coordinator's own ``check_online`` — off passes,
advisory warns without blocking, required fails closed with the stable
lease_expired/lease_fenced codes — wired into every public Phase 4 write:
Note create/update/archive/promote, Task create/patch/transition, Step
create/transition, Dependency create, Trigger create, CognitiveEvent pull
and ACK. Maintenance-plane workers (review sweep, trigger scan, event
sweeps) never present a lease and stay ungated.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from typing import Any, cast

import pytest

from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.ports import IdempotencyRunner
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.errors import AccessDeniedError, LeaseExpiredError, LeaseFencedError
from iris_memory_core.domain.surface import SurfaceMode
from tests.conftest import access_for

LEASE_TTL_US = 60_000_000


@pytest.fixture
def gate_ctx(
    clocked_store: Any,
    clocked_tenant_id: str,
    phase2_agent: str,
    idempotency: Any,
    surface: SurfaceCoordinatorService,
) -> dict[str, Any]:
    access = access_for(
        clocked_tenant_id,
        agent_ids=frozenset({phase2_agent}),
        admin=True,
    )
    tasks = TaskService(
        clocked_store, clocked_store.clock, idempotency=idempotency, surface=surface
    )
    notes = NoteService(
        clocked_store, clocked_store.clock, idempotency=idempotency, surface=surface
    )
    events = CognitiveEventService(
        clocked_store, clocked_store.clock, idempotency=idempotency, surface=surface
    )
    # Prerequisites run under the default OFF mode; the mode under test is
    # switched per test below.
    task = tasks.create(
        access,
        agent_id=phase2_agent,
        title="gate base task",
        origin="explicit_tool",
        idempotency_key="gate-task",
    )
    step_a = tasks.create_step(
        access, task.task_id, stable_key="a", title="A", idempotency_key="gate-step-a"
    )
    step_b = tasks.create_step(
        access, task.task_id, stable_key="b", title="B", idempotency_key="gate-step-b"
    )
    note = notes.create(
        access,
        agent_id=phase2_agent,
        kind="idea",
        title="gate note",
        idempotency_key="gate-note",
    )
    note_to_promote = notes.create(
        access,
        agent_id=phase2_agent,
        kind="idea",
        title="gate promotable",
        idempotency_key="gate-note-promo",
    )
    tasks.create_trigger(
        access,
        task.task_id,
        kind="at_time",
        schedule_spec={"at_us": clocked_store.clock.now_us() - 1},
        idempotency_key="gate-trigger",
    )
    with clocked_store.read() as tx:
        task_revision = tx.tasks.get_task(task.task_id).current_revision
    return {
        "task_revision": task_revision,
        "store": clocked_store,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "access": access,
        "surface": surface,
        "idempotency": idempotency,
        "tasks": tasks,
        "notes": notes,
        "events": events,
        "task": task,
        "step_a": step_a,
        "step_b": step_b,
        "note": note,
        "note_to_promote": note_to_promote,
    }


def _operations(ctx: dict[str, Any]) -> dict[str, Callable[..., Any]]:
    """One closure per public application-plane Phase 4 write."""
    tasks = ctx["tasks"]
    notes = ctx["notes"]
    events = ctx["events"]
    access = ctx["access"]
    agent = ctx["agent"]
    clock = ctx["store"].clock
    counter = itertools.count()

    def fresh_key() -> str:
        return f"gate-op-{next(counter)}"

    def pull_with_current_lease(**_ignored: Any) -> None:
        # The ACK op needs a delivered event first: the harness pulls with
        # the lease CURRENTLY live for the holder (or lease-less when the
        # plane is ungated), then ACKs with the proof under test.
        now = clock.now_us()
        with ctx["store"].write() as tx:
            fresh = CognitiveEventService.create_internal(
                tx,
                tenant_id=ctx["tenant"],
                agent_id=agent,
                space_group_id=None,
                space_id=None,
                session_id=None,
                kind="task.due",
                object_type="task",
                object_id="gate-ack-op",
                occurrence_id=None,
                scheduled_at_us=now,
                deliver_after_us=now,
                now_us=now,
            )
        lease = ctx.get("live_lease")
        bundle = (
            events.pull(access, agent_id=agent, lease_id=lease["id"], lease_epoch=lease["epoch"])
            if lease
            else events.pull(access, agent_id=agent)
        )
        assert any(item[0].id == fresh for item in bundle.events)
        ctx["ack_event_id"] = fresh

    def ack_op(**proof: Any) -> None:
        pull_with_current_lease()
        events.ack(access, ctx["ack_event_id"], idempotency_key=fresh_key(), **proof)

    def pull_op(**proof: Any) -> None:
        events.pull(access, agent_id=agent, **proof)

    return {
        "note_create": lambda **p: notes.create(
            access, agent_id=agent, kind="idea", title="op", idempotency_key=fresh_key(), **p
        ),
        "note_update": lambda **p: notes.update(
            access,
            ctx["note"].note_id,
            expected_revision=1,
            title="op",
            idempotency_key=fresh_key(),
            **p,
        ),
        "note_archive": lambda **p: notes.transition(
            access,
            ctx["note"].note_id,
            "archive",
            expected_revision=1,
            reason="r",
            idempotency_key=fresh_key(),
            **p,
        ),
        "note_promote": lambda **p: notes.transition(
            access,
            ctx["note_to_promote"].note_id,
            "promote",
            expected_revision=1,
            reason="r",
            promotion_target_type="task",
            idempotency_key=fresh_key(),
            **p,
        ),
        "task_create": lambda **p: tasks.create(
            access,
            agent_id=agent,
            title="op",
            origin="explicit_tool",
            idempotency_key=fresh_key(),
            **p,
        ),
        "task_patch": lambda **p: tasks.patch(
            access,
            ctx["task"].task_id,
            expected_revision=ctx["task_revision"],
            title="op",
            idempotency_key=fresh_key(),
            **p,
        ),
        "task_transition": lambda **p: tasks.transition(
            access,
            ctx["task"].task_id,
            "wait",
            expected_revision=ctx["task_revision"],
            origin="explicit_tool",
            reason="r",
            idempotency_key=fresh_key(),
            **p,
        ),
        "step_create": lambda **p: tasks.create_step(
            access,
            ctx["task"].task_id,
            stable_key=f"s{next(counter)}",
            title="op",
            idempotency_key=fresh_key(),
            **p,
        ),
        "step_transition": lambda **p: tasks.transition_step(
            access,
            ctx["task"].task_id,
            ctx["step_a"].step_id,
            "start",
            expected_revision=ctx["step_a"].revision,
            reason="r",
            idempotency_key=fresh_key(),
            **p,
        ),
        "dependency_create": lambda **p: tasks.add_dependency(
            access,
            ctx["task"].task_id,
            predecessor_step_id=ctx["step_a"].step_id,
            successor_step_id=ctx["step_b"].step_id,
            idempotency_key=fresh_key(),
            **p,
        ),
        "trigger_create": lambda **p: tasks.create_trigger(
            access,
            ctx["task"].task_id,
            kind="at_time",
            schedule_spec={"at_us": clock.now_us() + 10**9},
            idempotency_key=fresh_key(),
            **p,
        ),
        "event_pull": pull_op,
        "event_ack": ack_op,
    }


def _set_mode(ctx: dict[str, Any], mode: SurfaceMode) -> None:
    ctx["surface"].set_mode(
        access_for(ctx["tenant"], admin=True),
        ctx["agent"],
        mode,
        reason="gate matrix test",
    )


def _acquire(ctx: dict[str, Any], *, app_instance_id: str = "app-1") -> dict[str, Any]:
    holder = access_for(
        ctx["tenant"],
        agent_ids=frozenset({ctx["agent"]}),
        admin=True,
        app_instance_id=app_instance_id,
    )
    acquired = ctx["surface"].acquire(holder, ctx["agent"], ttl_us=LEASE_TTL_US)
    lease = {"id": acquired.lease.lease_id, "epoch": acquired.lease.lease_epoch}
    ctx["live_lease"] = lease
    return lease


class TestSurfaceGateMatrix:
    @pytest.mark.parametrize(
        "op_name",
        [
            "note_create",
            "note_update",
            "note_archive",
            "note_promote",
            "task_create",
            "task_patch",
            "task_transition",
            "step_create",
            "step_transition",
            "dependency_create",
            "trigger_create",
            "event_pull",
            "event_ack",
        ],
    )
    def test_off_mode_allows_every_write_without_proof(
        self, gate_ctx: dict[str, Any], op_name: str
    ) -> None:
        _operations(gate_ctx)[op_name]()

    @pytest.mark.parametrize(
        "op_name",
        [
            "note_create",
            "note_update",
            "note_archive",
            "note_promote",
            "task_create",
            "task_patch",
            "task_transition",
            "step_create",
            "step_transition",
            "dependency_create",
            "trigger_create",
            "event_pull",
            "event_ack",
        ],
    )
    def test_required_mode_rejects_every_write_without_proof(
        self, gate_ctx: dict[str, Any], op_name: str
    ) -> None:
        _set_mode(gate_ctx, SurfaceMode.REQUIRED)
        with pytest.raises((LeaseExpiredError, LeaseFencedError)):
            _operations(gate_ctx)[op_name]()

    def test_advisory_mode_never_blocks_and_records_the_warning(
        self, gate_ctx: dict[str, Any]
    ) -> None:
        _set_mode(gate_ctx, SurfaceMode.ADVISORY)
        # No lease exists at all — advisory still lets every write through.
        ops = _operations(gate_ctx)
        for op_name in (
            "note_create",
            "task_create",
            "task_patch",
            "event_pull",
        ):
            ops[op_name]()
        # The warning semantics survive: the audit trail carries the
        # advisory lease_warning (no_active_lease) instead of silence.
        with gate_ctx["store"].read() as tx:
            row = (
                tx.raw()
                .execute(
                    "SELECT details FROM audit_events WHERE action = 'task.created' "
                    "ORDER BY created_us DESC LIMIT 1"
                )
                .fetchone()
            )
        assert row is not None
        details = json.loads(row[0])
        assert details.get("lease_warning") == "no_active_lease"

    def test_required_rejects_incomplete_expired_stale_and_neighbor_proofs(
        self, gate_ctx: dict[str, Any]
    ) -> None:
        _set_mode(gate_ctx, SurfaceMode.REQUIRED)
        ops = _operations(gate_ctx)
        lease = _acquire(gate_ctx)
        # 1. lease_id without its epoch can never be arbitrated.
        with pytest.raises((LeaseExpiredError, LeaseFencedError)):
            ops["task_create"](lease_id=lease["id"])
        # 2. An in-time-expired lease is dead even while its row still reads
        #    active (the surface expiry job has not flipped it).
        gate_ctx["store"].clock.advance(LEASE_TTL_US + 1)
        with pytest.raises((LeaseExpiredError, LeaseFencedError)):
            ops["task_create"](lease_id=lease["id"], lease_epoch=lease["epoch"])
        # Re-acquire so the domain is live again, then fence the old proof.
        successor = _acquire(gate_ctx, app_instance_id="app-1")
        assert successor["epoch"] > lease["epoch"]
        # 3. The superseded epoch is fenced.
        with pytest.raises((LeaseExpiredError, LeaseFencedError)):
            ops["task_create"](lease_id=lease["id"], lease_epoch=lease["epoch"])
        # 4. A neighbor app presenting the live proof is fenced on the
        #    lease-binding paths — and since round 4 the write gate binds
        #    the proof to its recorded holder too, so the exfiltrated pair
        #    is inert everywhere (see TestAuditRoundFour for the matrix).
        neighbor = access_for(
            gate_ctx["tenant"],
            agent_ids=frozenset({gate_ctx["agent"]}),
            admin=True,
            app_instance_id="app-neighbor",
        )
        exfiltrated = gate_ctx["surface"].current(neighbor, gate_ctx["agent"])
        assert exfiltrated is not None and exfiltrated.lease_id == successor["id"]
        with pytest.raises(LeaseFencedError):
            gate_ctx["events"].pull(
                neighbor,
                agent_id=gate_ctx["agent"],
                lease_id=successor["id"],
                lease_epoch=successor["epoch"],
            )
        # 5. The legitimate holder's live proof passes every write.
        for op_name in (
            "note_create",
            "note_update",
            "task_create",
            "task_patch",
            "step_create",
            "dependency_create",
            "trigger_create",
            "event_pull",
            "event_ack",
        ):
            ops[op_name](lease_id=successor["id"], lease_epoch=successor["epoch"])

    def test_maintenance_plane_stays_ungated_under_required(self, gate_ctx: dict[str, Any]) -> None:
        """Workers never present a lease: review sweep, trigger scan and the
        event sweeps keep running under required mode (§25.3)."""
        _set_mode(gate_ctx, SurfaceMode.REQUIRED)
        store = gate_ctx["store"]
        with store.write() as tx:
            report = gate_ctx["tasks"].trigger_scan(
                tx, tenant_id=gate_ctx["tenant"], agent_id=gate_ctx["agent"]
            )
        assert report.events_created == 1  # the fixture's due at_time trigger fired
        with store.write() as tx:
            gate_ctx["notes"].review_sweep(
                tx, tenant_id=gate_ctx["tenant"], agent_id=gate_ctx["agent"]
            )
        with store.write() as tx:
            gate_ctx["events"].expire_sweep_in_tx(
                tx, agent_id_scope=(gate_ctx["tenant"], gate_ctx["agent"])
            )


class TestAuditRoundFour:
    """Round-4 regressions: the proof is the HOLDER's credential, and the
    gate runs before the idempotency cache."""

    def test_neighbor_cannot_replay_an_exfiltrated_proof(self, gate_ctx: dict[str, Any]) -> None:
        """P0-1: ``current`` hands the live lease view to every granted app,
        so id+epoch alone cannot be the credential — under required the
        neighbor's writes/pull with the exfiltrated pair are fenced, and
        under advisory they pass with ``not_lease_holder`` on the audit
        trail (advisory never blocks)."""
        ctx = gate_ctx
        _set_mode(ctx, SurfaceMode.REQUIRED)
        lease = _acquire(ctx)
        neighbor = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            admin=True,
            app_instance_id="app-neighbor",
        )
        # The exfiltration path the audit demonstrated: the neighbor can
        # READ the live proof — which is exactly why presenting it must not
        # be authorization.
        leaked = ctx["surface"].current(neighbor, ctx["agent"])
        assert leaked is not None
        assert leaked.lease_id == lease["id"] and leaked.lease_epoch == lease["epoch"]
        proof = {"lease_id": lease["id"], "lease_epoch": lease["epoch"]}
        with pytest.raises(LeaseFencedError):
            ctx["notes"].create(
                neighbor,
                agent_id=ctx["agent"],
                kind="idea",
                title="stolen",
                idempotency_key="r4-neighbor-note",
                **proof,
            )
        with pytest.raises(LeaseFencedError):
            ctx["tasks"].create(
                neighbor,
                agent_id=ctx["agent"],
                title="stolen",
                origin="explicit_tool",
                idempotency_key="r4-neighbor-task",
                **proof,
            )
        with pytest.raises(LeaseFencedError):
            ctx["events"].pull(neighbor, agent_id=ctx["agent"], **proof)
        # Advisory keeps its never-block promise, but the identity mismatch
        # is recorded on the audit trail instead of silence.
        _set_mode(ctx, SurfaceMode.ADVISORY)
        created = ctx["notes"].create(
            neighbor,
            agent_id=ctx["agent"],
            kind="idea",
            title="warned",
            idempotency_key="r4-neighbor-note-adv",
            **proof,
        )
        assert created.revision == 1
        with ctx["store"].read() as tx:
            row = (
                tx.raw()
                .execute(
                    "SELECT details FROM audit_events WHERE action = 'note.created' "
                    "AND resource_id = ? ",
                    (created.note_id,),
                )
                .fetchone()
            )
        assert row is not None
        assert json.loads(row[0]).get("lease_warning") == "not_lease_holder"

    def test_gate_runs_before_the_idempotency_cache(self, gate_ctx: dict[str, Any]) -> None:
        """P0-2: a completed idempotency record must not answer a caller
        whose lease died — but a legitimate retry by the NEW holder (same
        key, same logical payload, fresh proof) still replays instead of
        colliding with idempotency_key_reused."""
        ctx = gate_ctx
        _set_mode(ctx, SurfaceMode.REQUIRED)
        lease = _acquire(ctx)
        notes = ctx["notes"]
        # First write succeeds under the live lease.
        first = notes.create(
            ctx["access"],
            agent_id=ctx["agent"],
            kind="idea",
            title="cached",
            idempotency_key="r4-cache-key",
            lease_id=lease["id"],
            lease_epoch=lease["epoch"],
        )
        assert not first.replayed
        # The lease dies; the cached record must NOT answer anymore.
        ctx["store"].clock.advance(LEASE_TTL_US + 1)
        with pytest.raises((LeaseExpiredError, LeaseFencedError)):
            notes.create(
                ctx["access"],
                agent_id=ctx["agent"],
                kind="idea",
                title="cached",
                idempotency_key="r4-cache-key",
                lease_id=lease["id"],
                lease_epoch=lease["epoch"],
            )
        # Lease rotation: the same app instance re-acquires (higher epoch).
        successor = _acquire(ctx)
        assert successor["epoch"] > lease["epoch"]
        replay = notes.create(
            ctx["access"],
            agent_id=ctx["agent"],
            kind="idea",
            title="cached",
            idempotency_key="r4-cache-key",
            lease_id=successor["id"],
            lease_epoch=successor["epoch"],
        )
        assert replay.replayed and replay.note_id == first.note_id
        # Same key with a DIFFERENT logical payload is still refused.
        from iris_memory_core.domain.errors import IdempotencyKeyReusedError

        with pytest.raises(IdempotencyKeyReusedError):
            notes.create(
                ctx["access"],
                agent_id=ctx["agent"],
                kind="idea",
                title="different",
                idempotency_key="r4-cache-key",
                lease_id=successor["id"],
                lease_epoch=successor["epoch"],
            )
        # Exactly one note was ever written.
        with ctx["store"].read() as tx:
            count = (
                tx.raw()
                .execute("SELECT COUNT(*) FROM notes WHERE tenant_id = ?", (ctx["tenant"],))
                .fetchone()[0]
            )
        assert count == 3  # two fixture notes + the one cached note

    def test_ack_audit_records_the_advisory_lease_warning(self, gate_ctx: dict[str, Any]) -> None:
        """P2: the ACK audit details carry the advisory lease_warning, same
        discipline as the Note/Task write paths."""
        ctx = gate_ctx
        _set_mode(ctx, SurfaceMode.ADVISORY)
        now = ctx["store"].clock.now_us()
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
                object_id="r4-ack-warning",
                occurrence_id=None,
                scheduled_at_us=now,
                deliver_after_us=now,
                now_us=now,
            )
        bundle = ctx["events"].pull(ctx["access"], agent_id=ctx["agent"])
        assert [item[0].id for item in bundle.events] == [event_id]
        ctx["events"].ack(ctx["access"], event_id, idempotency_key="r4-ack-warn")
        with ctx["store"].read() as tx:
            row = (
                tx.raw()
                .execute(
                    "SELECT details FROM audit_events "
                    "WHERE action = 'cognitive_event.acknowledged' AND resource_id = ?",
                    (event_id,),
                )
                .fetchone()
            )
        assert row is not None
        assert json.loads(row[0]).get("lease_warning") == "no_active_lease"


class TestAuditRoundFive:
    """Round-5 regressions: authorization precedes Surface inspection and a
    cache preflight cannot be raced by a lease preemption before commit."""

    def test_preemption_between_preflight_and_idempotency_cannot_commit(
        self, gate_ctx: dict[str, Any]
    ) -> None:
        ctx = gate_ctx
        _set_mode(ctx, SurfaceMode.REQUIRED)
        lease = _acquire(ctx)
        neighbor = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            admin=True,
            app_instance_id="app-neighbor",
        )

        class PreemptingRunner:
            def __init__(self) -> None:
                self.fired = False

            def run(self, **kwargs: Any) -> Any:
                if not self.fired:
                    self.fired = True
                    ctx["surface"].acquire(
                        neighbor,
                        ctx["agent"],
                        ttl_us=LEASE_TTL_US,
                        priority=10,
                        allow_preempt=True,
                        reason="round-5 race",
                    )
                return ctx["idempotency"].run(**kwargs)

        notes = NoteService(
            ctx["store"],
            ctx["store"].clock,
            idempotency=cast(IdempotencyRunner, PreemptingRunner()),
            surface=ctx["surface"],
        )
        with pytest.raises(LeaseFencedError):
            notes.create(
                ctx["access"],
                agent_id=ctx["agent"],
                kind="idea",
                title="must not commit after fence",
                idempotency_key="round-5-race",
                lease_id=lease["id"],
                lease_epoch=lease["epoch"],
            )
        current = ctx["surface"].current(neighbor, ctx["agent"])
        assert current is not None and current.holder_app_instance_id == "app-neighbor"
        with ctx["store"].read() as tx:
            count = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM notes WHERE title = ?",
                    ("must not commit after fence",),
                )
                .fetchone()[0]
            )
        assert count == 0

    def test_create_authorization_precedes_surface_preflight(
        self, gate_ctx: dict[str, Any]
    ) -> None:
        ctx = gate_ctx
        with ctx["store"].write() as tx:
            hidden_agent = tx.insert_agent(ctx["tenant"], "Hidden Agent", actor="test")
        ctx["surface"].set_mode(
            access_for(ctx["tenant"], admin=True),
            hidden_agent.id,
            SurfaceMode.REQUIRED,
            reason="round-5 authorization order",
        )
        # No grant for hidden_agent and no lease proof. Both creates must fail
        # with authorization, never reveal whether the Surface is required or
        # has an active holder.
        with pytest.raises(AccessDeniedError):
            ctx["notes"].create(
                ctx["access"],
                agent_id=hidden_agent.id,
                kind="idea",
                title="hidden note",
                idempotency_key="round-5-hidden-note",
            )
        with pytest.raises(AccessDeniedError):
            ctx["tasks"].create(
                ctx["access"],
                agent_id=hidden_agent.id,
                title="hidden task",
                origin="explicit_tool",
                idempotency_key="round-5-hidden-task",
            )
        with ctx["store"].read() as tx:
            records = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key IN (?, ?)",
                    ("round-5-hidden-note", "round-5-hidden-task"),
                )
                .fetchone()[0]
            )
        assert records == 0
