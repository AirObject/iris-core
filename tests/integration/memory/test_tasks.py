"""Phase 4 Task/Step/Dependency integration tests (§11, P4-TASK-01).

Activation privilege, evidence-before-completion, dependency DAG + cycle
rejection, deterministic readiness, and the 50-way concurrent
expected-revision gate.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any

import pytest

from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    DomainError,
    InvalidRequestError,
    InvalidTransitionError,
    RevisionMismatchError,
    TaskDependencyCycleError,
)
from iris_memory_core.domain.task import TaskStatus, TaskStepStatus
from tests.conftest import access_for


@pytest.fixture
def task_ctx(
    clocked_store: Any,
    generous_gauge: Any,
    clocked_tenant_id: str,
    phase2_agent: str,
    phase4_tasks: TaskService,
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
        "gauge": generous_gauge,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "space": space.id,
        "access": access,
        "tasks": phase4_tasks,
    }


def _create(ctx: dict[str, Any], key: str, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "agent_id": ctx["agent"],
        "title": f"task {key}",
        "origin": "explicit_tool",
    }
    payload.update(overrides)
    return ctx["tasks"].create(ctx["access"], idempotency_key=f"task-{key}", **payload)


class TestTaskLifecycle:
    def test_conversation_origin_creates_proposed_and_cannot_self_activate(
        self, task_ctx: dict[str, Any]
    ) -> None:
        ctx = task_ctx
        created = _create(ctx, "conv", origin="conversation")
        with ctx["store"].read() as tx:
            assert tx.tasks.get_task(created.task_id).status == "proposed"
        # §11.5: activation requires explicit tool / policy / admin.
        non_admin = access_for(ctx["tenant"], agent_ids=frozenset({ctx["agent"]}))
        with pytest.raises(AccessDeniedError):
            ctx["tasks"].transition(
                non_admin,
                created.task_id,
                "activate",
                expected_revision=1,
                origin="conversation",
                reason="go",
                idempotency_key="a1",
            )
        activated = ctx["tasks"].transition(
            ctx["access"],
            created.task_id,
            "activate",
            expected_revision=1,
            origin="explicit_tool",
            reason="go",
            idempotency_key="a2",
        )
        assert activated.status == TaskStatus.ACTIVE.value

    def test_full_lifecycle_walk(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task = _create(ctx, "walk", origin="conversation")
        revision = 1
        walk = (
            ("activate", "admin"),
            ("wait", "admin"),
            ("block", "admin"),
            # A blocked task must unblock before completion (§11.5: the
            # state machine keeps blocked/completed disjoint).
            ("activate", "admin"),
            ("complete", "admin"),
        )
        for round_index, (target, origin) in enumerate(walk):
            moved = ctx["tasks"].transition(
                ctx["access"],
                task.task_id,
                target,
                expected_revision=revision,
                origin=origin,
                reason="walk",
                idempotency_key=f"walk-{round_index}-{target}",
            )
            revision += 1
            assert moved.revision == revision
        assert moved.status == TaskStatus.COMPLETED.value
        assert moved.completed_us is not None
        archived = ctx["tasks"].transition(
            ctx["access"],
            task.task_id,
            "archive",
            expected_revision=revision,
            origin="admin",
            reason="file",
            idempotency_key="walk-archive",
        )
        assert archived.status == TaskStatus.ARCHIVED.value
        with pytest.raises(InvalidTransitionError):
            ctx["tasks"].transition(
                ctx["access"],
                task.task_id,
                "activate",
                expected_revision=archived.revision,
                origin="admin",
                reason="no",
                idempotency_key="walk-nope",
            )

    def test_patch_requires_expected_revision(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task = _create(ctx, "patch")
        with pytest.raises(RevisionMismatchError):
            ctx["tasks"].patch(
                ctx["access"],
                task.task_id,
                expected_revision=9,
                next_action="x",
                idempotency_key="p1",
            )
        patched = ctx["tasks"].patch(
            ctx["access"],
            task.task_id,
            expected_revision=1,
            next_action="do it",
            idempotency_key="p2",
        )
        assert patched.next_action == "do it"


class TestTaskSteps:
    def test_steps_use_stable_keys_and_derived_readiness(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task = _create(ctx, "steps")
        first = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="write",
            title="Write it",
            idempotency_key="s1",
        )
        second = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="review",
            title="Review it",
            idempotency_key="s2",
        )
        steps = ctx["tasks"].list_steps(ctx["access"], task.task_id)
        by_key = {revision.stable_key: revision for _, revision in steps}
        # A dependency-free step becomes ready deterministically.
        assert by_key["write"].status == TaskStepStatus.READY.value
        # Adding an unmet dependency RETRACTS the successor's readiness.
        ctx["tasks"].add_dependency(
            ctx["access"],
            task.task_id,
            predecessor_step_id=first.step_id,
            successor_step_id=second.step_id,
            idempotency_key="d1",
        )
        after = {
            revision.stable_key: revision
            for _, revision in ctx["tasks"].list_steps(ctx["access"], task.task_id)
        }
        assert after["review"].status == TaskStepStatus.PENDING.value
        with pytest.raises(DomainError) as captured:
            ctx["tasks"].create_step(
                ctx["access"],
                task.task_id,
                stable_key="write",
                title="Duplicate",
                idempotency_key="s3",
            )
        assert captured.value.code == "conflict"  # stable_key identity holds

    def test_manual_ready_transition_rejected(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task = _create(ctx, "manualready")
        step = ctx["tasks"].create_step(
            ctx["access"], task.task_id, stable_key="a", title="A", idempotency_key="s1"
        )
        with pytest.raises(InvalidTransitionError):
            ctx["tasks"].transition_step(
                ctx["access"],
                task.task_id,
                step.step_id,
                "ready",
                expected_revision=step.revision,
                reason="x",
                idempotency_key="mr1",
            )

    def test_completion_of_expected_effect_step_requires_committed_evidence(
        self, task_ctx: dict[str, Any]
    ) -> None:
        ctx = task_ctx
        from iris_memory_core.storage.idempotency import IdempotencyManager

        observations = ObservationService(ctx["store"], gauge=ctx["gauge"])
        idem = IdempotencyManager(ctx["store"])
        now = ctx["store"].clock.now_us()
        # A committed observation and a partial (unsent) one.
        committed = observations.observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "assistant",
                    "kind": "message.sent",
                    "idempotency_key": "obs-1",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "sent!",
                    "space_id": ctx["space"],
                }
            ],
        )
        partial = observations.observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "assistant",
                    "kind": "message.draft",
                    "idempotency_key": "obs-2",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "effect_state": "partial",
                    "content": "unsent",
                    "space_id": ctx["space"],
                    "effect_proof": {"confirmed_range": [0, 5]},
                }
            ],
        )
        committed_id = committed.accepted_observation_ids[0]
        partial_id = partial.accepted_observation_ids[0]
        del idem

        # The task lives in the same space as its evidence: evidence must sit
        # inside the task's scope (scope_allows over space_group/space/session).
        task = _create(ctx, "effect", space_id=ctx["space"])
        step = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="send",
            title="Send the message",
            expected_effect="message delivered to the space",
            idempotency_key="se1",
        )
        # Completion is only reachable from in_progress — start first.
        started = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "start",
            expected_revision=step.revision,
            reason="go",
            idempotency_key="se1a",
        )
        # 1. No evidence at all → rejected.
        with pytest.raises(InvalidRequestError):
            ctx["tasks"].transition_step(
                ctx["access"],
                task.task_id,
                step.step_id,
                "complete",
                expected_revision=started.revision,
                reason="done",
                idempotency_key="se2",
            )
        # 2. Partial (unsent) observation is NOT evidence (§15.2).
        with pytest.raises(InvalidRequestError):
            ctx["tasks"].transition_step(
                ctx["access"],
                task.task_id,
                step.step_id,
                "complete",
                expected_revision=started.revision,
                reason="done",
                completion_evidence_refs=[
                    {"resource_type": "observation", "resource_id": partial_id}
                ],
                idempotency_key="se3",
            )
        # 3. Complete with the committed observation.
        completed = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "complete",
            expected_revision=started.revision,
            reason="done",
            completion_evidence_refs=[
                {"resource_type": "observation", "resource_id": committed_id}
            ],
            idempotency_key="se4",
        )
        assert completed.status == TaskStepStatus.COMPLETED.value
        assert completed.completion_evidence_refs

    def test_task_completion_requires_terminal_steps(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task = _create(ctx, "unresolved")
        step = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="only",
            title="Only step",
            idempotency_key="us1",
        )
        del step
        with pytest.raises(InvalidTransitionError) as captured:
            ctx["tasks"].transition(
                ctx["access"],
                task.task_id,
                "complete",
                expected_revision=1,
                origin="admin",
                reason="done",
                idempotency_key="us2",
            )
        assert captured.value.code == "invalid_state_transition"


class TestDependencies:
    def test_cycle_rejected_stably(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task = _create(ctx, "cycle")
        first = ctx["tasks"].create_step(
            ctx["access"], task.task_id, stable_key="a", title="A", idempotency_key="c1"
        )
        second = ctx["tasks"].create_step(
            ctx["access"], task.task_id, stable_key="b", title="B", idempotency_key="c2"
        )
        third = ctx["tasks"].create_step(
            ctx["access"], task.task_id, stable_key="c", title="C", idempotency_key="c3"
        )
        ctx["tasks"].add_dependency(
            ctx["access"],
            task.task_id,
            predecessor_step_id=first.step_id,
            successor_step_id=second.step_id,
            idempotency_key="cd1",
        )
        ctx["tasks"].add_dependency(
            ctx["access"],
            task.task_id,
            predecessor_step_id=second.step_id,
            successor_step_id=third.step_id,
            idempotency_key="cd2",
        )
        with pytest.raises(TaskDependencyCycleError) as captured:
            ctx["tasks"].add_dependency(
                ctx["access"],
                task.task_id,
                predecessor_step_id=third.step_id,
                successor_step_id=first.step_id,
                idempotency_key="cd3",
            )
        assert captured.value.code == "task_dependency_cycle"
        with pytest.raises(TaskDependencyCycleError):
            ctx["tasks"].add_dependency(
                ctx["access"],
                task.task_id,
                predecessor_step_id=first.step_id,
                successor_step_id=first.step_id,
                idempotency_key="cd4",
            )

    def test_cross_task_dependency_rejected(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task_a = _create(ctx, "ta")
        task_b = _create(ctx, "tb")
        step_a = ctx["tasks"].create_step(
            ctx["access"], task_a.task_id, stable_key="a", title="A", idempotency_key="x1"
        )
        step_b = ctx["tasks"].create_step(
            ctx["access"], task_b.task_id, stable_key="b", title="B", idempotency_key="x2"
        )
        with pytest.raises(InvalidRequestError):
            ctx["tasks"].add_dependency(
                ctx["access"],
                task_a.task_id,
                predecessor_step_id=step_a.step_id,
                successor_step_id=step_b.step_id,
                idempotency_key="x3",
            )

    def test_completing_predecessor_releases_successor(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task = _create(ctx, "release")
        first = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="first",
            title="First",
            idempotency_key="r1",
        )
        second = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="second",
            title="Second",
            idempotency_key="r2",
        )
        ctx["tasks"].add_dependency(
            ctx["access"],
            task.task_id,
            predecessor_step_id=first.step_id,
            successor_step_id=second.step_id,
            idempotency_key="r3",
        )

        def statuses() -> dict[str, str]:
            return {
                revision.stable_key: revision.status
                for _, revision in ctx["tasks"].list_steps(ctx["access"], task.task_id)
            }

        assert statuses()["second"] == TaskStepStatus.PENDING.value
        started = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            first.step_id,
            "start",
            expected_revision=2,
            reason="go",
            idempotency_key="r4a",
        )
        ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            first.step_id,
            "complete",
            expected_revision=started.revision,
            reason="done",
            idempotency_key="r4",
        )
        assert statuses()["second"] == TaskStepStatus.READY.value
        # completed_or_skipped also releases via skipping.
        third = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="third",
            title="Third",
            idempotency_key="r5",
        )
        ctx["tasks"].add_dependency(
            ctx["access"],
            task.task_id,
            predecessor_step_id=second.step_id,
            successor_step_id=third.step_id,
            condition="completed_or_skipped",
            idempotency_key="r6",
        )
        second_now = ctx["tasks"].list_steps(ctx["access"], task.task_id)
        second_revision = next(r for _, r in second_now if r.stable_key == "second")
        ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            second.step_id,
            "skip",
            expected_revision=second_revision.revision,
            reason="not needed",
            idempotency_key="r7",
        )
        assert statuses()["third"] == TaskStepStatus.READY.value


class TestConcurrencyGate:
    def test_fifty_threads_same_expected_revision_one_winner(
        self, task_ctx: dict[str, Any]
    ) -> None:
        """§36 Phase 4 gate: 50 concurrent identical CAS transitions — exactly
        one succeeds, the other 49 get a stable revision_mismatch, and only
        ONE set of logical audit/outbox effects exists."""
        ctx = task_ctx
        task = _create(ctx, "race", origin="conversation")
        with ctx["store"].read() as tx:
            watermark_before = tx.watermark(ctx["tenant"], ctx["agent"]).current_seq

        def attempt(index: int) -> str:
            try:
                ctx["tasks"].transition(
                    ctx["access"],
                    task.task_id,
                    "activate",
                    expected_revision=1,
                    origin="explicit_tool",
                    reason="race",
                    idempotency_key=f"race-{index}",
                )
                return "ok"
            except RevisionMismatchError:
                return "mismatch"
            except Exception as error:
                return f"other:{type(error).__name__}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(attempt, range(50)))
        assert results.count("ok") == 1
        assert results.count("mismatch") == 49
        assert not [item for item in results if item.startswith("other:")]
        with ctx["store"].read() as tx:
            final = tx.tasks.get_task(task.task_id)
            assert final.status == TaskStatus.ACTIVE.value
            assert final.current_revision == 2
            revision_rows = (
                tx.raw()
                .execute("SELECT COUNT(*) FROM task_revisions WHERE task_id = ?", (task.task_id,))
                .fetchone()[0]
            )
            audits = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM audit_events WHERE resource_type = 'task' "
                    "AND resource_id = ? AND action = 'task.active'",
                    (task.task_id,),
                )
                .fetchone()[0]
            )
            transition_jobs = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM outbox_jobs WHERE aggregate_type = 'task' "
                    "AND aggregate_id = ? AND source_revision = 2",
                    (task.task_id,),
                )
                .fetchone()[0]
            )
            watermark_bumps = (
                tx.raw()
                .execute(
                    "SELECT current_seq FROM agent_watermarks WHERE tenant_id = ? AND agent_id = ?",
                    (ctx["tenant"], ctx["agent"]),
                )
                .fetchone()[0]
            )
        assert revision_rows == 2
        assert audits == 1
        # Exactly one logical outbox effect for THE TRANSITION (revision 2);
        # the creation's own job (revision 1) is a separate logical effect.
        assert transition_jobs == 1
        # The race advanced the watermark EXACTLY once (the winner); the 49
        # losers wrote nothing at all.
        assert watermark_bumps == watermark_before + 1


class TestTaskSecurity:
    def test_cross_tenant_agent_and_space_denied(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task = _create(ctx, "sec")
        stranger = access_for("tenant-b", agent_ids=frozenset({"nope"}))
        with pytest.raises(AccessDeniedError):
            ctx["tasks"].get_task_view(stranger, task.task_id)
        wrong_agent = access_for(ctx["tenant"], agent_ids=frozenset({"other"}))
        with pytest.raises(AccessDeniedError):
            ctx["tasks"].get_task_view(wrong_agent, task.task_id)

    def test_tombstoned_task_hidden(self, task_ctx: dict[str, Any]) -> None:
        ctx = task_ctx
        task = _create(ctx, "gone")
        with ctx["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=ctx["tenant"],
                resource_type="task",
                resource_id=task.task_id,
                reason_code="forget",
                deleted_by="test",
            )
        assert ctx["tasks"].get_task_view(ctx["access"], task.task_id) is None
        assert ctx["tasks"].list_tasks(ctx["access"], agent_id=ctx["agent"]) == []

    def test_foreign_observations_are_never_completion_evidence(
        self, task_ctx: dict[str, Any]
    ) -> None:
        """P0 audit gate: evidence is fetched by ID, so its OWN scope dims —
        not the caller's — decide admissibility. A committed observation of
        another agent (same tenant) or another tenant can never complete
        this task's step, however real it is."""
        from iris_memory_core.application.observation import ObservationService

        ctx = task_ctx
        observations = ObservationService(ctx["store"], gauge=ctx["gauge"])
        now = ctx["store"].clock.now_us()
        # Another agent inside the SAME tenant, observing into the same space.
        with ctx["store"].write() as tx:
            stranger_agent = tx.insert_agent(ctx["tenant"], "Stranger", actor="test")
        stranger_access = access_for(
            ctx["tenant"],
            agent_ids=frozenset({stranger_agent.id}),
            space_ids=frozenset({ctx["space"]}),
            admin=True,
        )
        stranger_obs = observations.observe_batch(
            stranger_access,
            [
                {
                    "agent_id": stranger_agent.id,
                    "role": "assistant",
                    "kind": "message.sent",
                    "idempotency_key": "foreign-agent-obs",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "someone else's effect",
                    "space_id": ctx["space"],
                }
            ],
        )
        # A whole other tenant with its own agent and space.
        with ctx["store"].write() as tx:
            tx.insert_tenant("tenant-evidence", status="active")
            foreign_agent = tx.insert_agent("tenant-evidence", "Foreign", actor="test")
            foreign_space = tx.insert_space("tenant-evidence", "chat_group")
        foreign_access = access_for(
            "tenant-evidence",
            agent_ids=frozenset({foreign_agent.id}),
            space_ids=frozenset({foreign_space.id}),
            admin=True,
        )
        foreign_obs = observations.observe_batch(
            foreign_access,
            [
                {
                    "agent_id": foreign_agent.id,
                    "role": "assistant",
                    "kind": "message.sent",
                    "idempotency_key": "foreign-tenant-obs",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "another tenant's effect",
                    "space_id": foreign_space.id,
                }
            ],
        )

        task = _create(ctx, "evscope")
        step = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="act",
            title="Do the effect",
            expected_effect="message delivered",
            idempotency_key="evs-step",
        )
        started = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "start",
            expected_revision=step.revision,
            reason="go",
            idempotency_key="evs-start",
        )
        for index, foreign_id in enumerate(
            (stranger_obs.accepted_observation_ids[0], foreign_obs.accepted_observation_ids[0])
        ):
            with pytest.raises(InvalidRequestError, match="tenant and agent"):
                ctx["tasks"].transition_step(
                    ctx["access"],
                    task.task_id,
                    step.step_id,
                    "complete",
                    expected_revision=started.revision,
                    reason="done",
                    completion_evidence_refs=[
                        {"resource_type": "observation", "resource_id": foreign_id}
                    ],
                    idempotency_key=f"evs-foreign-{index}",
                )
        # The step is still in_progress — nothing but the rejections landed.
        with ctx["store"].read() as tx:
            after = tx.tasks.get_step(step.step_id)
        assert after.status == TaskStepStatus.IN_PROGRESS.value

    def test_cross_space_observation_is_not_completion_evidence(
        self, task_ctx: dict[str, Any]
    ) -> None:
        """P0 audit gate (round 2): evidence admissibility runs the FULL
        scope_allows final check — a committed observation of the same
        tenant+agent but ANOTHER space must not complete a space-A task's
        step."""
        from iris_memory_core.application.observation import ObservationService

        ctx = task_ctx
        observations = ObservationService(ctx["store"], gauge=ctx["gauge"])
        now = ctx["store"].clock.now_us()
        with ctx["store"].write() as tx:
            other_space = tx.insert_space(ctx["tenant"], "chat_group")
        wide_access = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"], other_space.id}),
            admin=True,
        )
        foreign_space_obs = observations.observe_batch(
            wide_access,
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "assistant",
                    "kind": "message.sent",
                    "idempotency_key": "cross-space-obs",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "an effect in another space",
                    "space_id": other_space.id,
                }
            ],
        )
        observation_id = foreign_space_obs.accepted_observation_ids[0]

        task = _create(ctx, "evspace", space_id=ctx["space"])
        step = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="act",
            title="Do the effect",
            expected_effect="message delivered",
            idempotency_key="evs2-step",
        )
        started = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "start",
            expected_revision=step.revision,
            reason="go",
            idempotency_key="evs2-start",
        )
        with pytest.raises(InvalidRequestError, match="outside the task's scope"):
            ctx["tasks"].transition_step(
                ctx["access"],
                task.task_id,
                step.step_id,
                "complete",
                expected_revision=started.revision,
                reason="done",
                completion_evidence_refs=[
                    {"resource_type": "observation", "resource_id": observation_id}
                ],
                idempotency_key="evs2-complete",
            )
        # Nothing landed: the step is still in_progress.
        with ctx["store"].read() as tx:
            after = tx.tasks.get_step(step.step_id)
        assert after.status == TaskStepStatus.IN_PROGRESS.value

    def test_artifact_evidence_goes_through_the_canonical_validator(
        self, task_ctx: dict[str, Any]
    ) -> None:
        """Phase 5 (ADR-0013 §5): the canonical artifact repository exists,
        so artifact evidence is admitted ONLY through its validator — the
        artifact's own tenant/agent/scope/status/tombstone state decides. A
        fabricated id, a foreign-space artifact and a tombstoned artifact are
        all rejected; a real in-scope artifact completes the step."""
        from iris_memory_core.application.artifacts import ArtifactService
        from iris_memory_core.application.forget import ForgetService
        from iris_memory_core.domain.errors import NotFoundError
        from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
        from iris_memory_core.storage.idempotency import IdempotencyManager

        ctx = task_ctx
        artifacts = ArtifactService(
            ctx["store"], ctx["store"].clock, idempotency=IdempotencyManager(ctx["store"])
        )
        # Agent-level artifacts: visible to the agent-level task under the
        # downward-visibility model (a space-level artifact would not be).
        good = artifacts.ingest_inline(
            ctx["access"],
            agent_id=ctx["agent"],
            content=b"effect output",
            media_type="text/plain",
            idempotency_key="art-good",
        )
        tombstoned = artifacts.ingest_inline(
            ctx["access"],
            agent_id=ctx["agent"],
            content=b"stale output",
            media_type="text/plain",
            idempotency_key="art-stale",
        )
        forget = ForgetService(
            ctx["store"], ctx["store"].clock, idempotency=IdempotencyManager(ctx["store"])
        )
        forget.forget(
            ctx["access"],
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE,
                resource_type="artifact",
                resource_id=tombstoned.artifact_id,
            ),
            reason="stale",
            idempotency_key="art-forget",
        )
        task = _create(ctx, "artifact")
        step = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="act",
            title="Do the effect",
            expected_effect="message delivered",
            idempotency_key="art-step",
        )
        started = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "start",
            expected_revision=step.revision,
            reason="go",
            idempotency_key="art-start",
        )
        # Fabricated and tombstoned artifacts never complete the step.
        for index, artifact_id in enumerate(("does-not-exist", tombstoned.artifact_id)):
            with pytest.raises((InvalidRequestError, NotFoundError)):
                ctx["tasks"].transition_step(
                    ctx["access"],
                    task.task_id,
                    step.step_id,
                    "complete",
                    expected_revision=started.revision,
                    reason="done",
                    completion_evidence_refs=[
                        {"resource_type": "artifact", "resource_id": artifact_id}
                    ],
                    idempotency_key=f"art-complete-{index}",
                )
        with ctx["store"].read() as tx:
            after_step = tx.tasks.get_step(step.step_id)
        assert after_step.status == TaskStepStatus.IN_PROGRESS.value
        # A real, canonical, in-scope artifact completes the step.
        completed = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "complete",
            expected_revision=after_step.current_revision,
            reason="done",
            completion_evidence_refs=[
                {"resource_type": "artifact", "resource_id": good.artifact_id}
            ],
            idempotency_key="art-complete-good",
        )
        assert completed.status == TaskStepStatus.COMPLETED.value
        # The task-level path validates with the same rules (the fabricated
        # id is rejected before any state can change).
        with ctx["store"].read() as tx:
            task_row = tx.tasks.get_task(task.task_id)
        with pytest.raises((InvalidRequestError, NotFoundError)):
            ctx["tasks"].transition(
                ctx["access"],
                task.task_id,
                "complete",
                expected_revision=task_row.current_revision,
                origin="admin",
                reason="done",
                completion_evidence_refs=[
                    {"resource_type": "artifact", "resource_id": "does-not-exist"}
                ],
                idempotency_key="art-task-complete",
            )

    def test_artifact_evidence_respects_privacy_labels(self, task_ctx: dict[str, Any]) -> None:
        """Review round 2 (P1-3): artifact evidence admission includes the
        PRIVACY evaluation — a restricted artifact never completes a step for
        a caller without admin (the only grant that sees ``restricted``)."""
        from iris_memory_core.application.artifacts import ArtifactService
        from iris_memory_core.storage.idempotency import IdempotencyManager

        ctx = task_ctx
        artifacts = ArtifactService(
            ctx["store"], ctx["store"].clock, idempotency=IdempotencyManager(ctx["store"])
        )
        secret = artifacts.ingest_inline(
            ctx["access"],
            agent_id=ctx["agent"],
            content=b"restricted effect output",
            media_type="text/plain",
            privacy_labels=["restricted"],
            idempotency_key="art-priv",
        )
        task = _create(ctx, "art-priv")
        step = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="act",
            title="Do the effect",
            expected_effect="message delivered",
            idempotency_key="art-priv-step",
        )
        started = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "start",
            expected_revision=step.revision,
            reason="go",
            idempotency_key="art-priv-start",
        )
        non_admin = access_for(
            ctx["tenant"], agent_ids=frozenset({ctx["agent"]}), space_ids=frozenset({ctx["space"]})
        )
        with pytest.raises(AccessDeniedError):
            ctx["tasks"].transition_step(
                non_admin,
                task.task_id,
                step.step_id,
                "complete",
                expected_revision=started.revision,
                reason="done",
                completion_evidence_refs=[
                    {"resource_type": "artifact", "resource_id": secret.artifact_id}
                ],
                idempotency_key="art-priv-denied",
            )
        with ctx["store"].read() as tx:
            after = tx.tasks.get_step(step.step_id)
        assert after.status == TaskStepStatus.IN_PROGRESS.value
        completed = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "complete",
            expected_revision=after.current_revision,
            reason="done",
            completion_evidence_refs=[
                {"resource_type": "artifact", "resource_id": secret.artifact_id}
            ],
            idempotency_key="art-priv-admin",
        )
        assert completed.status == TaskStepStatus.COMPLETED.value

    def test_observation_evidence_respects_privacy_labels(self, task_ctx: dict[str, Any]) -> None:
        """Review round 3 (R3-2): observation evidence admission includes the
        PRIVACY evaluation too — a restricted observation never completes a
        step for a caller without admin."""
        from iris_memory_core.application.observation import ObservationService

        ctx = task_ctx
        now = 2_000_000
        restricted = ObservationService(ctx["store"], gauge=ctx["gauge"]).observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "assistant",
                    "kind": "message.sent",
                    "idempotency_key": "obs-priv",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "restricted effect output",
                    "space_id": ctx["space"],
                    "privacy_labels": ["restricted"],
                }
            ],
        )
        observation_id = restricted.accepted_observation_ids[0]

        task = _create(ctx, "obs-priv", space_id=ctx["space"])
        step = ctx["tasks"].create_step(
            ctx["access"],
            task.task_id,
            stable_key="act",
            title="Do the effect",
            expected_effect="message delivered",
            idempotency_key="obs-priv-step",
        )
        started = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "start",
            expected_revision=step.revision,
            reason="go",
            idempotency_key="obs-priv-start",
        )
        non_admin = access_for(
            ctx["tenant"], agent_ids=frozenset({ctx["agent"]}), space_ids=frozenset({ctx["space"]})
        )
        with pytest.raises(AccessDeniedError):
            ctx["tasks"].transition_step(
                non_admin,
                task.task_id,
                step.step_id,
                "complete",
                expected_revision=started.revision,
                reason="done",
                completion_evidence_refs=[
                    {"resource_type": "observation", "resource_id": observation_id}
                ],
                idempotency_key="obs-priv-denied",
            )
        with ctx["store"].read() as tx:
            after = tx.tasks.get_step(step.step_id)
        assert after.status == TaskStepStatus.IN_PROGRESS.value
        completed = ctx["tasks"].transition_step(
            ctx["access"],
            task.task_id,
            step.step_id,
            "complete",
            expected_revision=after.current_revision,
            reason="done",
            completion_evidence_refs=[
                {"resource_type": "observation", "resource_id": observation_id}
            ],
            idempotency_key="obs-priv-admin",
        )
        assert completed.status == TaskStepStatus.COMPLETED.value
