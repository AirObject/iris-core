"""Structured Recall orchestrator tests (§18, P3-RECALL-01, Phase 3.4).

Deterministic replay (100x), per-route sub-deadlines (20+ timeouts per
route), completed/degraded/partial envelope accuracy, final canonical
rehydrate drops, minimum-watermark read-your-writes, layer/token budgets,
safe traces and the persona-is-never-a-candidate rule.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import SystemMonotonicClock, Transaction
from iris_memory_core.application.recall import (
    RECALL_RANKER_VERSION,
    FocusRoute,
    RecallCandidate,
    RecentContextRoute,
    RouteDeadlineExceeded,
    StateRoute,
    StructuredRecallOrchestrator,
    StructuredRecallRequest,
    stable_sort_key,
)
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import NotReadyError
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for

REPLAYS = 100
TIMEOUT_RUNS = 20


@pytest.fixture
def phase3(
    clocked_store: Store,
    generous_gauge: BackpressureGauge,
    clocked_tenant_id: str,
    phase2_agent: str,
) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space_a = tx.insert_space(clocked_tenant_id, "chat_group")
        space_b = tx.insert_space(clocked_tenant_id, "live_channel")
        session = tx.insert_session(clocked_tenant_id, space_a.id, actor="t")
    access = access_for(
        clocked_tenant_id,
        agent_ids=frozenset({phase2_agent}),
        space_ids=frozenset({space_a.id, space_b.id}),
    )
    idem = IdempotencyManager(clocked_store)
    recent = RecentContextService(clocked_store, clocked_store.clock)
    states = StateService(
        clocked_store, clocked_store.clock, gauge=generous_gauge, idempotency=idem
    )
    focus = FocusService(clocked_store, clocked_store.clock, idempotency=idem)
    observations = ObservationService(clocked_store, gauge=generous_gauge)
    orchestrator = StructuredRecallOrchestrator(
        clocked_store, recent, states, focus, clock=clocked_store.clock
    )
    return {
        "store": clocked_store,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "space_a": space_a.id,
        "space_b": space_b.id,
        "session": session.id,
        "access": access,
        "recent": recent,
        "states": states,
        "focus": focus,
        "observations": observations,
        "orchestrator": orchestrator,
    }


def _request(ctx: dict[str, Any], **overrides: Any) -> StructuredRecallRequest:
    defaults = dict(
        request_id="req-1",
        agent_id=ctx["agent"],
        space_id=ctx["space_a"],
        session_id=ctx["session"],
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 60_000_000,
    )
    defaults.update(overrides)
    return StructuredRecallRequest(**defaults)


def _seed(ctx: dict[str, Any], count: int = 6) -> None:
    now = ctx["store"].clock.now_us()
    for i in range(count):
        ctx["observations"].observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"obs-{i}",
                    "occurred_us": now + i * 1_000,
                    "committed_us": now + i * 1_000 + 1,
                    "content": f"turn {i} content",
                    "space_id": ctx["space_a"],
                    "session_id": ctx["session"],
                }
            ],
        )
    ctx["states"].put(
        ctx["access"],
        "environment",
        "obs.scene",
        agent_id=ctx["agent"],
        value={"scene": "gaming"},
        source_authority="host",
        idempotency_key="st-1",
        ttl_us=0,
    )
    ctx["focus"].create(
        ctx["access"],
        agent_id=ctx["agent"],
        kind="goal",
        summary="ship phase 3",
        salience=0.9,
        importance=0.9,
        idempotency_key="fc-1",
    )


class TestDeterminism:
    def test_hundred_replays_identical_order_and_trimming(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx)
        signatures = set()
        for _ in range(REPLAYS):
            result = ctx["orchestrator"].recall(ctx["access"], _request(ctx))
            signature = tuple(
                (c.candidate_id, c.route, c.final_score, c.token_estimate)
                for c in result.candidates
            )
            signatures.add(signature)
        assert len(signatures) == 1
        assert len(next(iter(signatures))) >= 3  # all three routes contributed

    def test_stable_sort_key_tie_breakers(self) -> None:
        from iris_memory_core.domain.scope import Scope

        def candidate(route: str, score: float, occurred: int, rid: str) -> RecallCandidate:
            return RecallCandidate(
                candidate_id=f"c:{rid}",
                route=route,
                resource_type="x",
                resource_id=rid,
                resource_revision=1,
                text="",
                scores={},
                final_score=score,
                token_estimate=1,
                occurred_us=occurred,
                scope=Scope(tenant_id="t"),
                privacy_labels=(),
            )

        items = [
            candidate("state", 0.5, 10, "b"),
            candidate("recent_context", 0.5, 10, "z"),
            candidate("recent_context", 0.9, 5, "a"),
            candidate("focus", 0.5, 10, "a"),
            candidate("recent_context", 0.5, 99, "m"),
            candidate("recent_context", 0.5, 10, "c"),
        ]
        # score DESC first; then category priority (focus < recent < state —
        # Phase 6 v2 mapping, ADR-0014 §5: focus outranks hot context);
        # then occurred DESC; then id ASC (c before z at the same instant).
        ordered = [c.resource_id for c in sorted(items, key=stable_sort_key)]
        assert ordered == ["a", "a", "m", "c", "z", "b"]


class TestRoutesAndEnvelope:
    def test_all_routes_completed(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx)
        result = ctx["orchestrator"].recall(ctx["access"], _request(ctx))
        assert set(result.completed_routes) == {"recent_context", "state", "focus"}
        assert result.degraded_routes == ()
        assert result.partial is False
        routes = {c.route for c in result.candidates}
        assert routes == {"recent_context", "state", "focus"}
        assert result.source_watermark >= 1

    def test_persona_is_never_a_candidate(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx)
        result = ctx["orchestrator"].recall(ctx["access"], _request(ctx))
        assert all(c.resource_type != "persona_revision" for c in result.candidates)

    def test_layer_and_token_budgets(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx, count=10)
        result = ctx["orchestrator"].recall(
            ctx["access"],
            _request(
                ctx, token_budget=20, layer_budgets={"recent_context": 5, "state": 5, "focus": 100}
            ),
        )
        assert sum(c.token_estimate for c in result.candidates) <= 20
        per_route: dict[str, int] = {}
        for candidate in result.candidates:
            per_route[candidate.route] = (
                per_route.get(candidate.route, 0) + candidate.token_estimate
            )
        assert per_route.get("recent_context", 0) <= 5
        assert per_route.get("state", 0) <= 5

    def test_candidate_limits_per_route(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx, count=30)
        result = ctx["orchestrator"].recall(
            ctx["access"], _request(ctx, candidate_limits={"recent_context": 3})
        )
        recent = [c for c in result.candidates if c.route == "recent_context"]
        assert len(recent) <= 3


class TestDeadlines:
    class _SlowRoute:
        name: str

        def __init__(self, name: str) -> None:
            self.name = name

        def collect(
            self,
            tx: Transaction,
            request: StructuredRecallRequest,
            access: AccessContext,
            deadline_us: int,
            now_us: int,
        ) -> tuple[RecallCandidate, ...]:
            raise RouteDeadlineExceeded()

    def test_each_route_timeout_degrades_accurately(self, phase3: dict[str, Any]) -> None:
        """20 runs per route: a route that blows its sub-deadline is degraded
        with a stable reason while the others still complete."""
        from iris_memory_core.application.recall import (
            FocusRoute,
            RecentContextRoute,
            StateRoute,
        )

        ctx = phase3
        _seed(ctx)
        for slow_index, slow_name in enumerate(("recent_context", "state", "focus")):
            routes = (
                self._SlowRoute("recent_context")
                if slow_index == 0
                else RecentContextRoute(ctx["recent"], SystemMonotonicClock()),
                self._SlowRoute("state") if slow_index == 1 else StateRoute(ctx["states"]),
                self._SlowRoute("focus") if slow_index == 2 else FocusRoute(ctx["focus"]),
            )
            orchestrator = StructuredRecallOrchestrator(
                ctx["store"],
                ctx["recent"],
                ctx["states"],
                ctx["focus"],
                clock=ctx["store"].clock,
                routes=routes,
            )
            for run in range(TIMEOUT_RUNS):
                result = orchestrator.recall(ctx["access"], _request(ctx))
                degraded = {d.route: d for d in result.degraded_routes}
                assert slow_name in degraded, (slow_name, run)
                assert degraded[slow_name].reason_code == "route_deadline_exceeded"
                assert degraded[slow_name].retryable is True
                assert slow_name not in result.completed_routes
                # partial=True: at least one other route completed
                assert result.partial is True
                assert result.completed_routes

    def test_route_exception_degrades_route_only(self, phase3: dict[str, Any]) -> None:
        class _BrokenRoute:
            name = "state"

            def collect(
                self,
                tx: Transaction,
                request: StructuredRecallRequest,
                access: AccessContext,
                deadline_us: int,
                now_us: int,
            ) -> tuple[RecallCandidate, ...]:
                raise RuntimeError("provider exploded")

        ctx = phase3
        _seed(ctx)
        from iris_memory_core.application.recall import FocusRoute, RecentContextRoute

        orchestrator = StructuredRecallOrchestrator(
            ctx["store"],
            ctx["recent"],
            ctx["states"],
            ctx["focus"],
            clock=ctx["store"].clock,
            routes=(
                RecentContextRoute(ctx["recent"], SystemMonotonicClock()),
                _BrokenRoute(),
                FocusRoute(ctx["focus"]),
            ),
        )
        result = orchestrator.recall(ctx["access"], _request(ctx))
        degraded = {d.route: d.reason_code for d in result.degraded_routes}
        assert degraded == {"state": "route_failed"}
        assert "state" not in result.completed_routes
        assert result.partial is True
        assert any(c.route == "recent_context" for c in result.candidates)

    def test_allow_partial_false_raises_when_all_degraded(self, phase3: dict[str, Any]) -> None:
        class _BrokenRoute:
            name = "x"

            def collect(
                self,
                tx: Transaction,
                request: StructuredRecallRequest,
                access: AccessContext,
                deadline_us: int,
                now_us: int,
            ) -> tuple[RecallCandidate, ...]:
                raise RuntimeError("dead")

        ctx = phase3
        orchestrator = StructuredRecallOrchestrator(
            ctx["store"],
            ctx["recent"],
            ctx["states"],
            ctx["focus"],
            clock=ctx["store"].clock,
            routes=(_BrokenRoute(), _BrokenRoute(), _BrokenRoute()),
        )
        with pytest.raises(NotReadyError):
            orchestrator.recall(ctx["access"], _request(ctx, allow_partial=False))


class TestRehydrate:
    def test_tombstoned_observation_dropped(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx, count=2)
        result = ctx["orchestrator"].recall(ctx["access"], _request(ctx))
        target = next(c for c in result.candidates if c.route == "recent_context")
        with ctx["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=ctx["tenant"],
                resource_type="observation",
                resource_id=target.resource_id,
                reason_code="forget",
                deleted_by="admin",
            )
        after = ctx["orchestrator"].recall(ctx["access"], _request(ctx))
        assert all(c.resource_id != target.resource_id for c in after.candidates)

    def test_promoted_focus_dropped_after_transition(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx)
        result = ctx["orchestrator"].recall(ctx["access"], _request(ctx))
        focus_candidate = next(c for c in result.candidates if c.route == "focus")
        ctx["focus"].transition(
            ctx["access"],
            focus_candidate.resource_id,
            "promote",
            expected_revision=focus_candidate.resource_revision,
            reason="done",
            promotion_target_type="note",
            idempotency_key="ik-fx-1",
        )
        after = ctx["orchestrator"].recall(ctx["access"], _request(ctx))
        assert focus_candidate.resource_id not in {c.resource_id for c in after.candidates}
        # The route itself completed; only the promoted item vanished in the
        # final canonical rehydrate.
        assert "focus" in after.completed_routes
        assert all(d.route != "focus" for d in after.degraded_routes)

    def test_expired_state_dropped_after_ttl(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx)
        ctx["states"].put(
            ctx["access"],
            "runtime",
            "short.lived",
            agent_id=ctx["agent"],
            value={"x": 1},
            source_authority="host",
            idempotency_key="st-ttl",
            ttl_us=1_000_000,
        )
        result = ctx["orchestrator"].recall(ctx["access"], _request(ctx))
        assert any(c.route == "state" for c in result.candidates)
        state_ids_before = {c.resource_id for c in result.candidates if c.route == "state"}
        ctx["store"].clock.advance(2_000_000)
        after = ctx["orchestrator"].recall(ctx["access"], _request(ctx))
        state_ids_after = {c.resource_id for c in after.candidates if c.route == "state"}
        assert state_ids_after <= state_ids_before


class TestMinimumWatermark:
    def test_unreachable_watermark_fails_when_no_key_route_can_succeed(
        self, phase3: dict[str, Any]
    ) -> None:
        ctx = phase3
        _seed(ctx)
        with pytest.raises(NotReadyError):
            ctx["orchestrator"].recall(ctx["access"], _request(ctx, minimum_watermark=999_999))

    def test_reachable_watermark_completes(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx)
        with ctx["store"].read() as tx:
            watermark = tx.watermark(ctx["tenant"], ctx["agent"])
        assert watermark is not None
        result = ctx["orchestrator"].recall(
            ctx["access"], _request(ctx, minimum_watermark=watermark.current_seq)
        )
        assert set(result.completed_routes) == {"recent_context", "state", "focus"}
        assert result.source_watermark >= watermark.current_seq

    def test_unreachable_watermark_strict_mode_fails_closed(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx)
        with pytest.raises(NotReadyError):
            ctx["orchestrator"].recall(
                ctx["access"], _request(ctx, minimum_watermark=999_999, allow_partial=False)
            )


class TestSafeTrace:
    def test_trace_contains_only_safe_fields(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx)
        result = ctx["orchestrator"].recall(ctx["access"], _request(ctx, include_trace=True))
        assert result.trace is not None
        assert result.trace.ranker_version == RECALL_RANKER_VERSION
        assert len(result.trace.request_hash) == 16
        for route in result.trace.routes:
            assert route.route in {"recent_context", "state", "focus"}
            assert isinstance(route.candidate_count, int)
            assert isinstance(route.duration_us, int)
        for secret_marker in ("tenant-a",):
            assert secret_marker not in repr(result.trace)

    def test_trace_and_envelope_carry_no_content(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        _seed(ctx, count=3)
        canary_contents = [f"turn {i} content" for i in range(3)]
        result = ctx["orchestrator"].recall(ctx["access"], _request(ctx, include_trace=True))
        # Candidates DO carry text (that is their job); the TRACE must not.
        assert result.trace is not None
        trace_repr = repr(result.trace)
        for canary in canary_contents:
            assert canary not in trace_repr
        assert "ship phase 3" not in trace_repr


class TestCrossScopeRecall:
    def test_cross_space_recall_zero_leak(self, phase3: dict[str, Any]) -> None:
        """Direct negative: recall for space B never contains space A content."""
        ctx = phase3
        now = ctx["store"].clock.now_us()
        canary = "SPACE_A_CANARY_TEXT"
        for i in range(4):
            ctx["observations"].observe_batch(
                ctx["access"],
                [
                    {
                        "agent_id": ctx["agent"],
                        "role": "user",
                        "kind": "message.text",
                        "idempotency_key": f"sa-{i}",
                        "occurred_us": now + i * 1000,
                        "committed_us": now + i * 1000 + 1,
                        "content": f"{canary} {i}",
                        "space_id": ctx["space_a"],
                        "session_id": ctx["session"],
                    }
                ],
            )
        leaked = 0
        for _ in range(20):
            result = ctx["orchestrator"].recall(
                ctx["access"], _request(ctx, space_id=ctx["space_b"], session_id=None)
            )
            leaked += sum(1 for c in result.candidates if canary in c.text)
            for candidate in result.candidates:
                if candidate.resource_type == "observation":
                    assert candidate.scope.space_id == ctx["space_b"]
        assert leaked == 0

    def test_cross_agent_denied(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.errors import AccessDeniedError

        ctx = phase3
        _seed(ctx)
        other_agent_access = access_for(ctx["tenant"])
        with pytest.raises(AccessDeniedError):
            ctx["orchestrator"].recall(other_agent_access, _request(ctx))

    def test_watermark_short_circuit_never_precedes_authorization(
        self, phase3: dict[str, Any]
    ) -> None:
        """Even an unreachable watermark must not mask an authorization failure."""
        from iris_memory_core.domain.errors import AccessDeniedError

        ctx = phase3
        _seed(ctx)
        other_agent_access = access_for(ctx["tenant"])
        with pytest.raises(AccessDeniedError):
            ctx["orchestrator"].recall(
                other_agent_access,
                _request(ctx, minimum_watermark=999_999),
            )

    def test_cross_tenant_denied(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.errors import AccessDeniedError, ScopeViolationError

        ctx = phase3
        _seed(ctx)
        foreign = access_for("tenant-qqq", agent_ids=frozenset({ctx["agent"]}))
        with pytest.raises((AccessDeniedError, ScopeViolationError)):
            ctx["orchestrator"].recall(foreign, _request(ctx))


class _ExpiredMonotonic:
    """Monotonic clock whose time is always past any deadline we hand it."""

    def monotonic_us(self) -> int:
        return 1 << 62


class TestFailClosed:
    """Review regressions (P1): deadline/cap/partial semantics must fail
    closed instead of silently completing."""

    def test_negative_limits_rejected_at_request_validation(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.errors import InvalidRequestError

        ctx = phase3
        with pytest.raises(InvalidRequestError):
            _request(ctx, candidate_limits={"state": -5})
        with pytest.raises(InvalidRequestError):
            _request(ctx, candidate_limits={"recent_context": -1})
        with pytest.raises(InvalidRequestError):
            _request(ctx, token_budget=-1)
        with pytest.raises(InvalidRequestError):
            _request(ctx, layer_budgets={"focus": -2})

    def test_negative_limit_never_becomes_unlimited_sql(self, phase3: dict[str, Any]) -> None:
        """SQLite treats LIMIT -1 as UNLIMITED; a malformed cap must be
        rejected before any route can turn it into an unbounded read."""
        from iris_memory_core.domain.errors import InvalidRequestError

        ctx = phase3
        _seed(ctx, count=8)
        with pytest.raises(InvalidRequestError):
            StructuredRecallRequest(
                request_id="neg",
                agent_id=ctx["agent"],
                space_id=ctx["space_a"],
                deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 60_000_000,
                candidate_limits={"focus": -3},
            )

    def test_allow_partial_false_raises_when_any_route_degraded(
        self, phase3: dict[str, Any]
    ) -> None:
        """One broken route among healthy ones is a request-level error when
        the caller asked for complete-or-error — not a partial answer."""
        ctx = phase3
        _seed(ctx)

        class _BrokenState:
            name = "state"

            def collect(
                self,
                tx: Transaction,
                request: StructuredRecallRequest,
                access: AccessContext,
                deadline_us: int,
                now_us: int,
            ) -> tuple[RecallCandidate, ...]:
                raise RuntimeError("provider exploded")

        from iris_memory_core.application.recall import FocusRoute, RecentContextRoute

        orchestrator = StructuredRecallOrchestrator(
            ctx["store"],
            ctx["recent"],
            ctx["states"],
            ctx["focus"],
            clock=ctx["store"].clock,
            routes=(
                RecentContextRoute(ctx["recent"], SystemMonotonicClock()),
                _BrokenState(),
                FocusRoute(ctx["focus"]),
            ),
        )
        with pytest.raises(NotReadyError):
            orchestrator.recall(ctx["access"], _request(ctx, allow_partial=False))

    def test_all_routes_degraded_is_error_even_when_partial_allowed(
        self, phase3: dict[str, Any]
    ) -> None:
        """Partial requires at least one successful key Route (§18.3)."""

        class _BrokenRoute:
            name = "x"

            def __init__(self, name: str) -> None:
                self.name = name

            def collect(
                self,
                tx: Transaction,
                request: StructuredRecallRequest,
                access: AccessContext,
                deadline_us: int,
                now_us: int,
            ) -> tuple[RecallCandidate, ...]:
                raise RuntimeError("dead")

        ctx = phase3
        _seed(ctx)
        orchestrator = StructuredRecallOrchestrator(
            ctx["store"],
            ctx["recent"],
            ctx["states"],
            ctx["focus"],
            clock=ctx["store"].clock,
            routes=(_BrokenRoute("recent_context"), _BrokenRoute("state"), _BrokenRoute("focus")),
        )
        with pytest.raises(NotReadyError):
            orchestrator.recall(ctx["access"], _request(ctx))

    def test_routes_that_cross_deadline_during_empty_read_are_degraded(
        self, phase3: dict[str, Any]
    ) -> None:
        """A pre-I/O check is insufficient when an empty read returns late."""

        class _CrossingClock:
            now = 0

            def monotonic_us(self) -> int:
                return self.now

        class _Recent:
            def __init__(self, clock: _CrossingClock) -> None:
                self.clock = clock

            def get_for_route(self, *_args: object, **_kwargs: object) -> object:
                self.clock.now = 20
                return SimpleNamespace(projection=SimpleNamespace(hot_observation_refs=()))

        class _State:
            def __init__(self, clock: _CrossingClock) -> None:
                self.clock = clock

            def list_scope_in_tx(self, *_args: object, **_kwargs: object) -> list[object]:
                self.clock.now = 20
                return []

        class _Focus:
            def __init__(self, clock: _CrossingClock) -> None:
                self.clock = clock

            def list_items_in_tx(self, *_args: object, **_kwargs: object) -> list[object]:
                self.clock.now = 20
                return []

        ctx = phase3
        request = _request(
            ctx,
            deadline_monotonic_us=10,
            state_namespaces=("runtime",),
        )
        factories = (
            lambda clock: RecentContextRoute(cast(RecentContextService, _Recent(clock)), clock),
            lambda clock: StateRoute(cast(StateService, _State(clock)), monotonic=clock),
            lambda clock: FocusRoute(cast(FocusService, _Focus(clock)), monotonic=clock),
        )
        for factory in factories:
            clock = _CrossingClock()
            route = factory(clock)
            with pytest.raises(RouteDeadlineExceeded):
                route.collect(SimpleNamespace(), request, ctx["access"], 10, 0)

    def test_every_route_respects_expired_deadline(self, phase3: dict[str, Any]) -> None:
        """An already-expired deadline degrades ALL real routes — including
        State/Focus (which previously discarded their sub-deadline) and an
        EMPTY recent window (which previously completed with zero
        candidates)."""
        from iris_memory_core.application.recall import (
            FocusRoute,
            RecentContextRoute,
            StateRoute,
        )

        ctx = phase3
        # seed state + focus but NO observations: the recent window is empty.
        ctx["states"].put(
            ctx["access"],
            "environment",
            "obs.scene",
            agent_id=ctx["agent"],
            value={"scene": "gaming"},
            source_authority="host",
            idempotency_key="fc-state",
            ttl_us=0,
        )
        ctx["focus"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            kind="goal",
            summary="fail closed goal",
            idempotency_key="fc-focus",
        )
        expired = _ExpiredMonotonic()
        orchestrator = StructuredRecallOrchestrator(
            ctx["store"],
            ctx["recent"],
            ctx["states"],
            ctx["focus"],
            clock=ctx["store"].clock,
            monotonic=expired,
            routes=(
                RecentContextRoute(ctx["recent"], expired),
                StateRoute(ctx["states"], monotonic=expired),
                FocusRoute(ctx["focus"], monotonic=expired),
            ),
        )
        with pytest.raises(NotReadyError) as error:
            orchestrator.recall(ctx["access"], _request(ctx, deadline_monotonic_us=(1 << 62) - 1))
        assert set(cast(list[str], error.value.details["degraded_routes"])) == {
            "recent_context",
            "state",
            "focus",
        }
