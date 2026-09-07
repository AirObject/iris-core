"""RecentContextProjection integration tests (§9.1, P3-RECENT-01).

Committed-observation-only reads, session/space windows, deterministic
rebuilds, expiry/invalidation/shadow-swap/canonical fallback, tombstone and
privacy final checks, and the structural cross-space isolation guarantee.
"""

from __future__ import annotations

from typing import Any

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import (
    StructuredRecallOrchestrator,
    StructuredRecallRequest,
)
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import AccessDeniedError
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import (
    MutableClock,
    access_for,
)


def _observe(
    obs: ObservationService,
    access: AccessContext,
    agent: str,
    space: str,
    session: str | None,
    key: str,
    content: str,
    occurred: int,
    committed: int,
) -> Any:
    return obs.observe_batch(
        access,
        [
            {
                "agent_id": agent,
                "role": "user",
                "kind": "message.text",
                "idempotency_key": key,
                "occurred_us": occurred,
                "committed_us": committed,
                "content": content,
                "space_id": space,
                "session_id": session,
            }
        ],
    )


@pytest.fixture
def phase3(
    clocked_store: Store,
    generous_gauge: BackpressureGauge,
    clocked_tenant_id: str,
    phase2_agent: str,
    mutable_clock: MutableClock,
) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space_a = tx.insert_space(clocked_tenant_id, "chat_group")
        space_b = tx.insert_space(clocked_tenant_id, "live_channel")
        session_a1 = tx.insert_session(clocked_tenant_id, space_a.id, actor="t")
        session_a2 = tx.insert_session(clocked_tenant_id, space_a.id, actor="t")
    access = access_for(
        clocked_tenant_id,
        agent_ids=frozenset({phase2_agent}),
        space_ids=frozenset({space_a.id, space_b.id}),
    )
    idem = IdempotencyManager(clocked_store)
    return {
        "store": clocked_store,
        "gauge": generous_gauge,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "space_a": space_a.id,
        "space_b": space_b.id,
        "session_a1": session_a1.id,
        "session_a2": session_a2.id,
        "access": access,
        "observations": ObservationService(clocked_store, gauge=generous_gauge),
        "recent": RecentContextService(clocked_store, clocked_store.clock),
        "states": StateService(
            clocked_store, clocked_store.clock, gauge=generous_gauge, idempotency=idem
        ),
        "focus": FocusService(clocked_store, clocked_store.clock, idempotency=idem),
        "clock": mutable_clock,
    }


class TestWindowBasics:
    def test_session_and_space_windows(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        for i in range(3):
            _observe(
                ctx["observations"],
                ctx["access"],
                ctx["agent"],
                ctx["space_a"],
                ctx["session_a1"],
                f"a1-{i}",
                f"message {i}",
                now + i * 1000,
                now + i * 1000 + 1,
            )
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a2"],
            "a2-0",
            "other session",
            now,
            now + 1,
        )
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_b"],
            None,
            "b-0",
            "other space",
            now,
            now + 1,
        )

        session_view = ctx["recent"].get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        assert len(session_view.projection.hot_observation_refs) == 3
        session_ids = {ref.observation_id for ref in session_view.projection.hot_observation_refs}

        # §5.2 null semantics: a space window (session_id absent) may only
        # read the space's session-less observations; session content is
        # recovered through the session window above.
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            None,
            "space-level",
            "space scoped event",
            now + 5_000,
            now + 5_001,
        )
        space_view = ctx["recent"].get(
            ctx["access"], agent_id=ctx["agent"], space_id=ctx["space_a"]
        )
        space_ids = {ref.observation_id for ref in space_view.projection.hot_observation_refs}
        assert len(space_ids) == 1
        assert not (space_ids & session_ids)

    def test_projection_metadata_present(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "m1",
            "hello",
            now,
            now + 1,
        )
        view = ctx["recent"].get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        projection = view.projection
        assert projection is not None
        assert projection.builder_version >= 1
        assert projection.source_watermark >= 0
        assert projection.tail_observation_id == projection.hot_observation_refs[-1].observation_id
        assert len(projection.result_hash) == 64
        assert view.source == "canonical"  # no generation yet → fallback read

    def test_reads_only_committed_observations(self, phase3: dict[str, Any]) -> None:
        """A failed/invalid batch writes nothing — the window stays empty."""
        ctx = phase3
        from iris_memory_core.domain.errors import InvalidRequestError
        from iris_memory_core.domain.observation import InvalidObservationError

        with pytest.raises((InvalidRequestError, InvalidObservationError)):
            ctx["observations"].observe_batch(
                ctx["access"],
                [
                    {
                        "agent_id": ctx["agent"],
                        "role": "user",
                        "kind": "message.text",
                        "idempotency_key": "bad",
                        "occurred_us": 10,
                        "committed_us": 5,
                        "content": "never",
                        "space_id": ctx["space_a"],
                        "session_id": ctx["session_a1"],
                    }
                ],
            )
        view = ctx["recent"].get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        assert view.projection is not None
        assert view.projection.hot_observation_refs == ()
        assert view.projection.summary_segments == ()


class TestDeterministicRebuild:
    def test_three_rebuilds_are_identical(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        for i in range(12):
            _observe(
                ctx["observations"],
                ctx["access"],
                ctx["agent"],
                ctx["space_a"],
                ctx["session_a1"],
                f"m{i}",
                f"note {i}" * (i + 1),
                now + i * 1_000,
                now + i * 1_000 + 1,
            )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        hashes = []
        for _round_index in range(3):
            view = ctx["recent"].rebuild(
                admin,
                agent_id=ctx["agent"],
                space_id=ctx["space_a"],
                session_id=ctx["session_a1"],
                reason="determinism",
            )
            assert view.source == "generation"
            hashes.append(
                (
                    view.projection.result_hash,
                    tuple(r.observation_id for r in view.projection.hot_observation_refs),
                    tuple(s.segment_id for s in view.projection.summary_segments),
                    view.projection.token_estimate,
                )
            )
        assert hashes[0] == hashes[1] == hashes[2]

    def test_rebuild_ignores_outbox_order(self, phase3: dict[str, Any]) -> None:
        """The window is keyed by (occurred_us, id), never by arrival order."""
        ctx = phase3
        now = ctx["store"].clock.now_us()
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "late",
            "i happened first",
            now,
            now + 1,
        )
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "early",
            "i happened last",
            now + 9_000,
            now + 9_001,
        )
        view = ctx["recent"].get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        refs = view.projection.hot_observation_refs
        assert refs[-1].occurred_us >= refs[0].occurred_us
        assert view.projection.tail_observation_id == refs[-1].observation_id

    def test_builder_version_mismatch_falls_back_to_canonical(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "m1",
            "content",
            now,
            now + 1,
        )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        ctx["recent"].rebuild(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="build",
        )
        # A service pinned to a NEWER builder version must not serve the old
        # generation: it falls back to the canonical window instead.
        future = RecentContextService(ctx["store"], ctx["store"].clock, builder_version=99)
        view = future.get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        assert view.source == "canonical"
        assert view.degraded_reason == "builder_version_mismatch"
        # The current-version service still serves its own generation.
        stable = ctx["recent"].get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        assert stable.source == "generation"

    def test_invalidate_returns_to_canonical_then_rebuild(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "m1",
            "content",
            now,
            now + 1,
        )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        ctx["recent"].rebuild(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="build",
        )
        assert ctx["recent"].invalidate(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="stale",
        )
        view = ctx["recent"].get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        assert view.source == "canonical"
        assert view.degraded_reason == "no_generation"
        ctx["recent"].rebuild(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="rebuild",
        )
        assert (
            ctx["recent"]
            .get(
                ctx["access"],
                agent_id=ctx["agent"],
                space_id=ctx["space_a"],
                session_id=ctx["session_a1"],
            )
            .source
            == "generation"
        )

    def test_generation_expiry_falls_back(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.recent import RecentWindowPolicy

        ctx = phase3
        now = ctx["store"].clock.now_us()
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "m1",
            "content",
            now,
            now + 1,
        )
        short = RecentContextService(
            ctx["store"], ctx["store"].clock, policy=RecentWindowPolicy(ttl_us=1)
        )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        short.rebuild(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="short-ttl",
        )
        assert (
            short.get(
                ctx["access"],
                agent_id=ctx["agent"],
                space_id=ctx["space_a"],
                session_id=ctx["session_a1"],
            ).source
            == "generation"
        )
        ctx["clock"].advance(10_000_000)
        view = short.get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        assert view.source == "canonical"
        assert view.degraded_reason == "generation_expired"

    def test_rebuild_requires_admin_and_reason(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        with pytest.raises(AccessDeniedError):
            ctx["recent"].rebuild(
                ctx["access"], agent_id=ctx["agent"], space_id=ctx["space_a"], reason="nope"
            )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        from iris_memory_core.domain.errors import ReasonRequiredError

        with pytest.raises(ReasonRequiredError):
            ctx["recent"].rebuild(
                admin, agent_id=ctx["agent"], space_id=ctx["space_a"], reason=None
            )


class TestFinalChecks:
    def test_corrupt_generation_hash_falls_back(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        outcome = _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "hash-check",
            "integrity checked content",
            now,
            now + 1,
        )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        ctx["recent"].rebuild(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="hash-check",
        )
        with ctx["store"].write() as tx:
            tx.raw().execute(
                "UPDATE recent_context_generations SET result_hash = ? WHERE status = 'verified'",
                ("0" * 64,),
            )

        view = ctx["recent"].get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        assert view.source == "canonical"
        assert view.degraded_reason == "projection_invalid"
        assert view.projection is not None
        assert view.projection.referenced_ids() == outcome.accepted_observation_ids

    def test_generation_target_metadata_is_revalidated(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "target-check",
            "target checked content",
            now,
            now + 1,
        )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        ctx["recent"].rebuild(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="target-check",
        )
        with ctx["store"].write() as tx:
            tx.raw().execute(
                "UPDATE recent_context_generations SET space_id = ? WHERE status = 'verified'",
                (ctx["space_b"],),
            )

        view = ctx["recent"].get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        assert view.source == "canonical"
        assert view.degraded_reason == "generation_target_mismatch"

    def test_tombstoned_observation_never_served(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        outcome = _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "m1",
            "secret content",
            now,
            now + 1,
        )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        ctx["recent"].rebuild(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="build",
        )
        with ctx["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=ctx["tenant"],
                resource_type="observation",
                resource_id=outcome.accepted_observation_ids[0],
                reason_code="forget",
                deleted_by="admin",
            )
        view = ctx["recent"].get(
            ctx["access"],
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
        )
        assert view.source == "canonical"
        assert view.degraded_reason == "observation_tombstoned"
        assert view.projection.hot_observation_refs == ()

    def test_privacy_label_blocks_generation_serving(self, phase3: dict[str, Any]) -> None:
        """A restricted observation poisons the generation for callers that
        cannot see it — the read degrades to a canonical window that filters
        it out instead of leaking it through the projection."""
        ctx = phase3
        now = ctx["store"].clock.now_us()
        ctx["observations"].observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "restricted",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "restricted content",
                    "space_id": ctx["space_a"],
                    "session_id": ctx["session_a1"],
                    "privacy_labels": ["restricted"],
                }
            ],
        )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        ctx["recent"].rebuild(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="build",
        )
        non_admin = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        view = ctx["recent"].get(
            non_admin, agent_id=ctx["agent"], space_id=ctx["space_a"], session_id=ctx["session_a1"]
        )
        # Canonical fallback re-applies privacy: nothing restricted returned.
        for ref in view.projection.referenced_ids():
            observation = None
            with ctx["store"].read() as tx:
                observation = tx.observations.get(ref)
            assert "restricted" not in observation.privacy_labels


class TestCrossSpaceIsolation:
    def test_space_b_never_sees_space_a_content(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        canary = "SPACE_A_PRIVATE_CANARY"
        for i in range(5):
            _observe(
                ctx["observations"],
                ctx["access"],
                ctx["agent"],
                ctx["space_a"],
                ctx["session_a1"],
                f"secret-{i}",
                f"{canary} {i}",
                now + i * 1000,
                now + i * 1000 + 1,
            )
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"], ctx["space_b"]}),
        )
        ctx["recent"].rebuild(
            admin,
            agent_id=ctx["agent"],
            space_id=ctx["space_a"],
            session_id=ctx["session_a1"],
            reason="build",
        )

        # Direct recent read for space B: zero space-A refs.
        view_b = ctx["recent"].get(ctx["access"], agent_id=ctx["agent"], space_id=ctx["space_b"])
        assert view_b.projection.referenced_ids() == ()

        # Recall route for space B: zero space-A candidates, zero canary text.
        orchestrator = StructuredRecallOrchestrator(
            ctx["store"], ctx["recent"], ctx["states"], ctx["focus"], clock=ctx["store"].clock
        )
        result = orchestrator.recall(
            ctx["access"],
            StructuredRecallRequest(
                request_id="iso",
                agent_id=ctx["agent"],
                space_id=ctx["space_b"],
                deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
            ),
        )
        leaked = [c for c in result.candidates if canary in c.text]
        assert leaked == []
        # every returned observation candidate must live in space B
        for candidate in result.candidates:
            if candidate.resource_type == "observation":
                assert candidate.scope.space_id == ctx["space_b"]

    def test_space_group_membership_does_not_leak_raw_content(self, phase3: dict[str, Any]) -> None:
        """Same SpaceGroup ≠ shared raw conversation (§5.3): a group-scoped
        recall request still cannot read another space's window."""
        from iris_memory_core.application.provisioning import ProvisioningService

        ctx = phase3
        admin = access_for(ctx["tenant"], admin=True)
        provisioning = ProvisioningService(ctx["store"])
        group = provisioning.create_space_group(
            admin, "community", description="shared memes", reason="test"
        )
        with ctx["store"].write() as tx:
            space_a = tx.get_space(ctx["space_a"])
            tx.bind_space_to_group(
                ctx["tenant"],
                ctx["space_a"],
                group.id,
                expected_revision=space_a.revision,
                actor="admin",
                reason_code="bind",
            )
            space_b = tx.get_space(ctx["space_b"])
            tx.bind_space_to_group(
                ctx["tenant"],
                ctx["space_b"],
                group.id,
                expected_revision=space_b.revision,
                actor="admin",
                reason_code="bind",
            )
        now = ctx["store"].clock.now_us()
        canary = "GROUP_LEAK_CANARY"
        _observe(
            ctx["observations"],
            ctx["access"],
            ctx["agent"],
            ctx["space_a"],
            ctx["session_a1"],
            "grp",
            canary,
            now,
            now + 1,
        )
        # recall from space B (same group) must not return space A content
        orchestrator = StructuredRecallOrchestrator(
            ctx["store"], ctx["recent"], ctx["states"], ctx["focus"], clock=ctx["store"].clock
        )
        result = orchestrator.recall(
            ctx["access"],
            StructuredRecallRequest(
                request_id="grp",
                agent_id=ctx["agent"],
                space_id=ctx["space_b"],
                deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
            ),
        )
        assert all(canary not in candidate.text for candidate in result.candidates)


class TestHorizontalAuthorization:
    def test_cross_tenant_agent_space_denied(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        other_tenant_access = access_for(
            "tenant-zzz", agent_ids=frozenset({ctx["agent"]}), space_ids=frozenset({ctx["space_a"]})
        )
        with pytest.raises(AccessDeniedError):
            ctx["recent"].get(other_tenant_access, agent_id=ctx["agent"], space_id=ctx["space_a"])
        no_space_access = access_for(
            ctx["tenant"], agent_ids=frozenset({ctx["agent"]}), space_ids=frozenset()
        )
        with pytest.raises(AccessDeniedError):
            ctx["recent"].get(no_space_access, agent_id=ctx["agent"], space_id=ctx["space_a"])
        no_agent_access = access_for(
            ctx["tenant"], agent_ids=frozenset(), space_ids=frozenset({ctx["space_a"]})
        )
        with pytest.raises(AccessDeniedError):
            ctx["recent"].get(no_agent_access, agent_id=ctx["agent"], space_id=ctx["space_a"])

    def test_session_of_other_space_rejected(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.errors import InvalidRequestError

        ctx = phase3
        with pytest.raises((InvalidRequestError, AccessDeniedError)):
            ctx["recent"].get(
                ctx["access"],
                agent_id=ctx["agent"],
                space_id=ctx["space_b"],
                session_id=ctx["session_a1"],
            )
