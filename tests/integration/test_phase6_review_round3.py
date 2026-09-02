"""Adversarial review round 3 regressions (ADR-0014 §13, post-CI pass).

Six unclosed boundaries and one new issue from the third review pass, each
verified against the code and locked here:

1. The staleness trust gate is the requesting agent's unsettled ``fts.apply``
   BACKLOG — a newer out-of-order apply can no longer MAX-jump a watermark
   over still-pending holes (false fresh), and projection jobs are attributed
   to the resource's owning agent even when the triggering event carried
   none.
2. ``speaker_entity_id`` on the caller-constructible request model is
   neutralized at the service boundary — only server-side actor resolution
   may set it.
3. A request naming BOTH a space group and a space must name a REAL binding
   (``space_group_bindings``), not two separately authorized dims.
4. Single-route and ``max_route_concurrency=1`` execution are deadline
   bounded too — there is no synchronous unbounded path anymore.
5. Claim and relation valid time is evaluated AT ``as_of`` for historical
   reads ("valid then, expired now" stays recallable; "not yet effective
   then" does not leak in).
6. A tombstoned note never re-enters a rebuilt generation (its body would
   resurface in the index and defeat async physical cleanup).
7. Recall request-level idempotency REPLAYS the stored first response: a
   transport retry after state changes returns the original candidate set,
   a diverging body under the same id is rejected, and an erasure that
   invalidated a referenced resource scrubs the stored body so the replay
   fails closed.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import (
    SearchService,
    StructuredRecallOrchestrator,
    StructuredRecallRequest,
)
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    DeadlineExceededError,
    InvalidRequestError,
)
from iris_memory_core.domain.fts import build_fts_query
from iris_memory_core.indexing import fts as fts_module
from iris_memory_core.indexing.fts import FTS_REASON_GENERATION_STALE, FtsDegradedError
from iris_memory_core.storage.idempotency import IdempotencyManager
from tests.conftest import MutableClock, access_for
from tests.integration.test_phase6_fts import _Ctx
from tests.integration.test_phase6_recall import World, _recall, _returned_ids
from tests.integration.test_phase6_review_round2 import (
    _BlockingRoute,
    _enqueue_fts_apply,
    _FastRoute,
    _remember,
)


@pytest.fixture
def ctx(clocked_store: Any, mutable_clock: MutableClock) -> _Ctx:
    return _Ctx(clocked_store, mutable_clock)


# ---------------------------------------------------------------------------
# 1. Staleness trust gate = unsettled fts.apply backlog (per agent)


class TestBacklogGate:
    def test_pending_holes_are_not_skipped_by_a_newer_apply(
        self, ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-fix gate advanced an applied watermark to the live seq on
        ONE apply, so a single processed event trusted the index while older
        jobs were still queued. The backlog gate cannot be fooled: the older
        jobs keep counting until their fenced completion CAS commits."""
        monkeypatch.setattr(fts_module, "fts_staleness_limit", lambda: 5)
        remembered = ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        for index in range(6):
            _enqueue_fts_apply(ctx, f"hole-{index}", index + 1)
        # A NEWER event for the already-indexed claim is applied out of
        # order — under watermark bookkeeping this is exactly the jump that
        # wrongly trusted the index.
        with ctx.store.write() as tx:
            ctx.fts.apply_change_in_tx(
                tx,
                tenant_id="t1",
                resource_type="claim",
                resource_id=remembered.claim_id,
            )
        with ctx.store.read() as tx, pytest.raises(FtsDegradedError) as captured:
            ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
            )
        assert captured.value.reason_code == FTS_REASON_GENERATION_STALE
        # Only settling the queued work restores trust.
        with ctx.store.write() as tx:
            tx.raw().execute(
                "UPDATE outbox_jobs SET status = 'completed' "
                "WHERE tenant_id = 't1' AND job_kind = 'fts.apply'"
            )
        with ctx.store.read() as tx:
            hits = ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
                space_id=ctx.space,
            )
        assert {hit.document.resource_id for hit in hits} == {remembered.claim_id}

    def test_agentless_trigger_attributed_to_the_resource_owner(
        self, ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forget-driven invalidations ride memory.invalidated jobs with
        agent_id=None; the scheduled fts.apply must still land on the
        resource owner's backlog, or the owner's gate would never see it."""
        from iris_memory_core.jobs.handlers import _schedule_fts_apply

        monkeypatch.setattr(fts_module, "fts_staleness_limit", lambda: 0)
        remembered = ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        with ctx.store.write() as tx:
            _schedule_fts_apply(
                tx,
                tenant_id="t1",
                resource_type="claim",
                resource_id=remembered.claim_id,
                agent_id=None,
                clock=ctx.clock,
            )
        with ctx.store.read() as tx:
            backlog = tx.outbox.unsettled_job_count("t1", ctx.agent, "fts.apply")
        assert backlog == 1
        with ctx.store.read() as tx, pytest.raises(FtsDegradedError) as captured:
            ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
            )
        assert captured.value.reason_code == FTS_REASON_GENERATION_STALE


# ---------------------------------------------------------------------------
# 2. Caller-supplied speaker_entity_id is never honored


class TestSpeakerInjection:
    def test_actorless_request_neutralizes_the_field(self, world: World, monkeypatch: Any) -> None:
        request = world.service.build_request(
            request_id="req-inject-neutralized",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 5_000_000,
            topic="injection",
        )
        # The caller-constructible model lets a hostile caller write the
        # INTERNAL entity id directly; the service must drop it before the
        # orchestrator (and the protected-budget logic) ever sees it.
        import dataclasses

        poisoned = dataclasses.replace(request, speaker_entity_id=world.entity)
        captured: dict[str, Any] = {}
        original = world.orchestrator.recall

        def spy(access: Any, inner: StructuredRecallRequest) -> Any:
            captured["speaker_entity_id"] = inner.speaker_entity_id
            return original(access, inner)

        monkeypatch.setattr(world.orchestrator, "recall", spy)
        world.service.recall(world.access, poisoned)  # actors omitted
        assert captured["speaker_entity_id"] is None
        # The server-side path still resolves and forwards the speaker (a
        # DIFFERENT request id: the same id would replay the stored
        # actorless response without ever reaching the orchestrator).
        resolved = world.service.resolve_speaker(world.access, world.actors)
        resolved_request = world.service.build_request(
            request_id="req-inject-resolved",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 5_000_000,
            topic="injection",
        )
        world.service.recall(world.access, resolved_request, actors=world.actors)
        assert captured["speaker_entity_id"] == resolved


# ---------------------------------------------------------------------------
# 3. space_group_id + space_id must name a real binding


class TestGroupSpaceBinding:
    def _groups(self, ctx: _Ctx) -> tuple[str, str]:
        with ctx.store.write() as tx:
            first = tx.insert_space_group("t1", "crew", "group", actor="t", reason_code="t").id
            second = tx.insert_space_group("t1", "other", "group", actor="t", reason_code="t").id
        return first, second

    def _bind(self, ctx: _Ctx, group: str) -> None:
        with ctx.store.write() as tx:
            space = tx.get_space(ctx.space)
            tx.bind_space_to_group(
                "t1",
                ctx.space,
                group,
                expected_revision=space.revision,
                actor="t",
                reason_code="test",
            )

    def _request(self, ctx: _Ctx, group: str) -> StructuredRecallRequest:
        return StructuredRecallRequest(
            request_id="req-binding",
            agent_id=ctx.agent,
            space_id=ctx.space,
            space_group_id=group,
            deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
            topic="binding",
        )

    def test_unrelated_authorized_pair_is_denied(self, ctx: _Ctx) -> None:
        """Each dim individually authorized, but the space is bound to a
        DIFFERENT group: the combination must not read the union of both."""
        bound, unbound = self._groups(ctx)
        self._bind(ctx, bound)
        ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        access = access_for(
            "t1",
            agent_ids=frozenset({ctx.agent}),
            space_group_ids=frozenset({unbound, bound}),
            space_ids=frozenset({ctx.space}),
        )
        with ctx.store.read() as tx, pytest.raises(AccessDeniedError):
            StructuredRecallOrchestrator._authorize_request(tx, access, self._request(ctx, unbound))
        with pytest.raises(AccessDeniedError):
            SearchService(ctx.store, ctx.clock, ctx.fts).search(
                access,
                agent_id=ctx.agent,
                space_group_id=unbound,
                space_id=ctx.space,
                query="quantum",
            )

    def test_unbound_space_with_any_group_is_denied(self, ctx: _Ctx) -> None:
        bound, other = self._groups(ctx)
        # The space is bound to `bound`, so requesting it under `other` is
        # denied; an access envelope granting BOTH groups changes nothing.
        self._bind(ctx, bound)
        access = access_for(
            "t1",
            agent_ids=frozenset({ctx.agent}),
            space_group_ids=frozenset({other, bound}),
            space_ids=frozenset({ctx.space}),
        )
        with ctx.store.read() as tx, pytest.raises(AccessDeniedError):
            StructuredRecallOrchestrator._authorize_request(tx, access, self._request(ctx, other))

    def test_bound_pair_is_accepted(self, ctx: _Ctx) -> None:
        bound, _other = self._groups(ctx)
        self._bind(ctx, bound)
        ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        access = access_for(
            "t1",
            agent_ids=frozenset({ctx.agent}),
            space_group_ids=frozenset({bound}),
            space_ids=frozenset({ctx.space}),
        )
        with ctx.store.read() as tx:
            StructuredRecallOrchestrator._authorize_request(tx, access, self._request(ctx, bound))
        results = SearchService(ctx.store, ctx.clock, ctx.fts).search(
            access, agent_id=ctx.agent, space_group_id=bound, space_id=ctx.space, query="quantum"
        )
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# 4. Single-route / single-thread execution is deadline bounded


class TestSingleRouteDeadline:
    def _orchestrator(self, world: World, routes: tuple[Any, ...]) -> Any:
        from iris_memory_core.application.focus import FocusService
        from iris_memory_core.application.recent import RecentContextService
        from iris_memory_core.application.state import StateService

        idem = IdempotencyManager(world.store)
        return StructuredRecallOrchestrator(
            world.store,
            RecentContextService(world.store, world.clock),
            StateService(world.store, world.clock, idempotency=idem),
            FocusService(world.store, world.clock, idempotency=idem),
            clock=world.clock,
            monotonic=SystemMonotonicClock(),
            routes=routes,
        )

    def test_single_route_cannot_block_forever(self, world: World) -> None:
        orchestrator = self._orchestrator(world, (_BlockingRoute(delay=0.5),))
        request = StructuredRecallRequest(
            request_id="req-single-route",
            agent_id=world.agent,
            space_id=world.space,
            deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 50_000,
            topic="single route",
        )
        started = time.perf_counter()
        # No route completed and every degradation is a deadline: the stable
        # §18.3 error — raised AFTER the bounded wait, never after the block.
        with pytest.raises(DeadlineExceededError):
            orchestrator.recall(world.access, request)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.35, f"single-route recall waited {elapsed * 1000:.0f} ms"

    def test_concurrency_one_still_bounded(self, world: World) -> None:
        orchestrator = self._orchestrator(world, (_FastRoute(), _BlockingRoute(delay=0.5)))
        request = StructuredRecallRequest(
            request_id="req-concurrency-one",
            agent_id=world.agent,
            space_id=world.space,
            deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 50_000,
            topic="concurrency one",
            max_route_concurrency=1,
        )
        started = time.perf_counter()
        result = orchestrator.recall(world.access, request)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.35, f"concurrency=1 recall waited {elapsed * 1000:.0f} ms"
        assert result.completed_routes == ("claims",)
        degraded = {d.route: d for d in result.degraded_routes}
        assert degraded["fts"].reason_code == "route_deadline_exceeded"


# ---------------------------------------------------------------------------
# 5. Valid time evaluated at as_of


class TestValidTimeAtAsOf:
    def test_valid_then_expired_now_is_recallable_at_as_of(
        self, world: World, mutable_clock: Any
    ) -> None:
        start = mutable_clock.now_us()
        expiring = _remember(
            world,
            "validtime-expiring",
            "expiring tachyon ledger",
            valid_from_us=start,
            valid_until_us=start + 10_000_000,
        )
        as_of = mutable_clock.now_us()
        mutable_clock.advance(20_000_000)  # the window has closed by now
        now_result = _recall(world, request_id="req-vt-now", topic="tachyon")
        then_result = _recall(world, request_id="req-vt-then", topic="tachyon", as_of_us=as_of)
        # The historical read evaluates the valid window AT as_of: the claim
        # was valid then and must survive; the current read sees it expired.
        assert expiring.claim_id in _returned_ids(then_result)
        assert expiring.claim_id not in _returned_ids(now_result)

    def test_not_yet_effective_then_is_excluded_from_as_of(
        self, world: World, mutable_clock: Any
    ) -> None:
        start = mutable_clock.now_us()
        future = _remember(
            world,
            "validtime-future",
            "future chroniton ledger",
            valid_from_us=start + 10_000_000,
            valid_until_us=None,
        )
        as_of = mutable_clock.now_us()
        then_result = _recall(world, request_id="req-vt-future", topic="chroniton", as_of_us=as_of)
        mutable_clock.advance(20_000_000)  # effective by now
        now_result = _recall(world, request_id="req-vt-effective", topic="chroniton")
        # The historical read must not leak a claim that was not yet
        # effective at as_of; the current read sees it valid.
        assert future.claim_id not in _returned_ids(then_result)
        assert future.claim_id in _returned_ids(now_result)


# ---------------------------------------------------------------------------
# 6. Tombstoned notes never re-enter a rebuilt generation


class TestTombstonedNoteRebuild:
    def test_tombstoned_note_excluded_from_new_generation(self, ctx: _Ctx) -> None:
        idem = IdempotencyManager(ctx.store)
        notes = NoteService(ctx.store, ctx.clock, idempotency=idem)
        live = notes.create(
            ctx.access,
            agent_id=ctx.agent,
            kind="important",
            title="live note",
            body="survives the rebuild",
            space_id=ctx.space,
            idempotency_key="idem-note-live",
        )
        doomed = notes.create(
            ctx.access,
            agent_id=ctx.agent,
            kind="important",
            title="doomed note",
            body="erased before the rebuild",
            space_id=ctx.space,
            idempotency_key="idem-note-doomed",
        )
        with ctx.store.write() as tx:
            tx.record_tombstone(
                tenant_id="t1",
                resource_type="note",
                resource_id=doomed.note_id,
                reason_code="test_erasure",
                deleted_by="t",
            )
        ctx.fts.rebuild("t1")
        with ctx.store.read() as tx:
            pointer = tx.fts.pointer("t1")
            assert pointer is not None
            indexed = {
                (doc.resource_type, doc.resource_id)
                for doc in tx.fts.documents_for_generation(pointer.generation_id)
            }
        assert ("note", live.note_id) in indexed
        # The pre-fix enumeration fed the tombstoned body straight into the
        # new generation (the SQL status filter was the only defense).
        assert ("note", doomed.note_id) not in indexed


# ---------------------------------------------------------------------------
# 7. Request-level response replay


class TestRequestReplay:
    def test_transport_retry_replays_the_first_response(
        self, world: World, mutable_clock: Any
    ) -> None:
        _remember(world, "replay-anchor", "replay anchor odyssey")
        first = _recall(world, request_id="req-replay", topic="replay odyssey")
        assert first.candidates
        # State moves on between the attempt and the retry — and the retry
        # carries a fresh wall-clock deadline; neither may change the reply.
        _remember(world, "replay-late", "late arrival odyssey")
        mutable_clock.advance(1_000)
        second = _recall(world, request_id="req-replay", topic="replay odyssey")
        assert second.candidates == first.candidates
        assert second.retrieved_count == first.retrieved_count
        assert second.source_watermark == first.source_watermark
        assert "replay-late" not in _returned_ids(second)

    def test_diverging_body_under_the_same_id_is_rejected(self, world: World) -> None:
        _recall(world, request_id="req-replay-diverge", topic="first topic")
        with pytest.raises(InvalidRequestError) as captured:
            _recall(world, request_id="req-replay-diverge", topic="second topic")
        assert "already used" in str(captured.value)

    def test_erasure_scrubs_the_stored_response(self, world: World, mutable_clock: Any) -> None:
        from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind

        remembered = _remember(world, "replay-erased", "replay erasure odyssey")
        result = _recall(world, request_id="req-replay-erased", topic="replay erasure")
        assert remembered.claim_id in _returned_ids(result)
        mutable_clock.advance(1_000)
        world.forget.forget(
            world.access,
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE,
                resource_type="claim",
                resource_id=remembered.claim_id,
            ),
            reason="replay scrub",
            idempotency_key="idem-replay-scrub",
        )
        # The tombstone and the response scrub committed together; a retry
        # of the request id must fail CLOSED instead of resurrecting the
        # erased body through the replay path.
        with pytest.raises(ConflictError):
            _recall(world, request_id="req-replay-erased", topic="replay erasure")

    def test_replayed_response_still_supports_usage(self, world: World) -> None:
        from iris_memory_core.application.recall import (
            RecallUsageReportInput,
            RecallUsageService,
        )

        _remember(world, "replay-usage", "replay usage odyssey")
        first = _recall(world, request_id="req-replay-usage", topic="replay usage")
        returned = [c.candidate_id for c in first.candidates]
        assert returned
        replay = _recall(world, request_id="req-replay-usage", topic="replay usage")
        assert [c.candidate_id for c in replay.candidates] == returned
        usage = RecallUsageService(world.store, world.clock)
        report = usage.report(
            world.access,
            RecallUsageReportInput(
                request_id="req-replay-usage",
                host_cycle_id="cycle-1",
                returned_candidate_ids=tuple(returned),
                host_selected_candidate_ids=tuple(returned),
                model_visible_candidate_ids=tuple(returned[:1]),
                persona_revision=replay.persona_revision,
                reported_at_us=1_700_000_000_000_001,
            ),
        )
        assert report.created is True
