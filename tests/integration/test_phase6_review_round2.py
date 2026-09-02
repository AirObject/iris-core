"""Adversarial review round 2 regressions (ADR-0014 §12, post-CI pass).

Nine release-blocking findings from the Phase 6 review, each verified
against the code and locked here:

1. Route deadline bounded wait — a BLOCKED route cannot hold the response.
2. Claims as_of — a claim active then but retracted now stays recallable.
3. FTS scope SQL — downward visibility (``D IS NULL OR D = R``), group
   content positively recallable, no upward leak to other sessions.
4. Purpose/data_purposes server-side gate; speaker resolved INSIDE the
   service from external actors (no internal-id injection point).
5. Rebuild completeness — keyset pagination everywhere (no silent 10k
   truncation) and the verification checksum recomputed from PERSISTED rows.
6. Staleness trust gate — the REQUESTING agent's unsettled ``fts.apply``
   backlog (counted from the live queue state) decides freshness: a lagging
   agent is never hidden behind a busier peer, and draining the queue
   clears the degradation.
7. Ranker — bm25 relevance INCREASES with match strength; redundancy keeps
   the highest-scored candidate, not the earliest input.
8. Usage replay — a diverging replay under the same identity is rejected;
   an identical one returns the STORED counts.
9. Retired-generation cleanup issues FTS5 delete commands (no ghost
   postings left in the shadow tables).
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import pytest

from iris_memory_core.application.episodes import EpisodeService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import (
    RecallUsageReportInput,
    RecallUsageService,
    SearchService,
    StructuredRecallOrchestrator,
    StructuredRecallRequest,
)
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    DomainError,
    IdentityNotFoundError,
)
from iris_memory_core.domain.fts import build_fts_query
from iris_memory_core.domain.recall import RankerV2, ScoredCandidate
from iris_memory_core.indexing import fts as fts_module
from iris_memory_core.indexing.fts import FTS_REASON_GENERATION_STALE, FtsDegradedError
from iris_memory_core.storage.idempotency import IdempotencyManager
from tests.conftest import MutableClock, access_for
from tests.integration.test_phase6_fts import _Ctx
from tests.integration.test_phase6_recall import World, _actor, _recall, _returned_ids


@pytest.fixture
def ctx(clocked_store: Any, mutable_clock: MutableClock) -> _Ctx:
    return _Ctx(clocked_store, mutable_clock)


# ---------------------------------------------------------------------------
# 1. Route deadline bounded wait


class _BlockingRoute:
    """A route that never cooperatively checks its deadline (worst case)."""

    name = "fts"

    def __init__(self, delay: float) -> None:
        self._delay = delay

    def collect(
        self, tx: Any, request: Any, access: Any, deadline_us: int, now_us: int
    ) -> tuple[Any, ...]:
        del tx, request, access, deadline_us, now_us
        time.sleep(self._delay)
        return ()


class _FastRoute:
    name = "claims"

    def collect(
        self, tx: Any, request: Any, access: Any, deadline_us: int, now_us: int
    ) -> tuple[Any, ...]:
        del tx, request, access, deadline_us, now_us
        return ()


class TestRouteDeadlineBoundedWait:
    def test_blocked_route_cannot_hold_the_response(self, world: World) -> None:
        from iris_memory_core.application.focus import FocusService
        from iris_memory_core.application.recent import RecentContextService
        from iris_memory_core.application.state import StateService

        idem = IdempotencyManager(world.store)
        orchestrator = StructuredRecallOrchestrator(
            world.store,
            RecentContextService(world.store, world.clock),
            StateService(world.store, world.clock, idempotency=idem),
            FocusService(world.store, world.clock, idempotency=idem),
            clock=world.clock,
            monotonic=SystemMonotonicClock(),
            routes=(
                _BlockingRoute(delay=0.5),
                _FastRoute(),
            ),
        )
        request = StructuredRecallRequest(
            request_id="req-deadline-bound",
            agent_id=world.agent,
            space_id=world.space,
            deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 50_000,
            topic="deadline bound",
            max_route_concurrency=2,
        )
        started = time.perf_counter()
        result = orchestrator.recall(world.access, request)
        elapsed = time.perf_counter() - started
        # The blocked route sleeps 500 ms; the 50 ms request deadline must
        # bound the RESPONSE (the worker thread is abandoned, not joined).
        # The margin absorbs scheduling jitter; the pre-fix behavior waited
        # for the full blocking call (>0.5 s).
        assert elapsed < 0.35, f"recall waited {elapsed * 1000:.0f} ms for a blocked route"
        assert result.completed_routes == ("claims",)
        degraded = {d.route: d for d in result.degraded_routes}
        assert degraded["fts"].reason_code == "route_deadline_exceeded"
        assert result.partial is True


# ---------------------------------------------------------------------------
# 2. Claims route as_of


def _observe_for_claim(world: World, key: str) -> str:
    from iris_memory_core.application.observation import ObservationService

    outcome = ObservationService(world.store).observe_batch(
        world.access,
        [
            {
                "agent_id": world.agent,
                "role": "user",
                "kind": "message.text",
                "idempotency_key": f"obs-{key}",
                "occurred_us": world.clock.now_us(),
                "committed_us": world.clock.now_us(),
                "content": f"evidence {key}",
                "space_id": world.space,
            }
        ],
    )
    return outcome.accepted_observation_ids[0]


def _remember(world: World, key: str, text: str, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "agent_id": world.agent,
        "space_id": world.space,
        "subject_entity_id": world.entity,
        "predicate": f"p_{key}",
        "value": {"k": key},
        "canonical_text": text,
        "category": "fact",
        "evidence": [{"source_type": "observation", "source_id": _observe_for_claim(world, key)}],
        "idempotency_key": f"idem-{key}",
    }
    payload.update(overrides)
    return world.claims.remember(world.access, **payload)


class TestClaimsAsOfHistorical:
    def test_retracted_now_was_active_then(self, world: World, mutable_clock: Any) -> None:
        remembered = _remember(world, "asof-hist", "historical probe zanzibar")
        as_of = mutable_clock.now_us()
        mutable_clock.advance(1_000_000)
        world.claims.correct(
            world.access,
            remembered.claim_id,
            expected_revision=remembered.revision,
            mode="retract",
            reason="test retract",
            idempotency_key="idem-asof-retract",
        )
        # Current time: retracted claims are invisible.
        now_result = _recall(world, request_id="req-asof-now", topic="zanzibar")
        assert remembered.claim_id not in _returned_ids(now_result)
        # At as_of: the claim was live and must be recallable — the keyset
        # query reconstructs the revision current at as_of instead of
        # prefiltering on the current retracted status (which would drop
        # the claim before rehydrate could ever see it).
        then_result = _recall(world, request_id="req-asof-then", topic="zanzibar", as_of_us=as_of)
        assert remembered.claim_id in _returned_ids(then_result)


# ---------------------------------------------------------------------------
# 3. FTS scope visibility


class TestFtsScopeVisibility:
    def test_space_request_sees_agent_level_claim(self, ctx: _Ctx) -> None:
        # The claim (and its observation evidence) live at agent level: an
        # agent-level claim cannot cite narrower space-scoped evidence.
        observation = ctx.observe("obs-aglvl-evidence", "agentlevel evidence")
        with ctx.store.write() as tx:
            tx.raw().execute(
                "UPDATE observations SET space_id = NULL, session_id = NULL WHERE id = ?",
                (observation,),
            )
        ctx.remember(
            "agentlevel",
            "agentlevel cosmic ray detector",
            space_id=None,
            evidence=[{"source_type": "observation", "source_id": observation}],
        )
        ctx.fts.rebuild("t1")
        # D.space IS NULL, R.space = S: downward visibility in SQL — the
        # pre-fix exact ``d.space_id = ?`` match dropped parent-scope docs.
        assert ctx.search_ids("cosmic") != set()

    def test_space_request_never_sees_other_session_docs(self, ctx: _Ctx) -> None:
        with ctx.store.write() as tx:
            tx.raw().execute(
                "INSERT INTO sessions (id, tenant_id, space_id, status, started_us) "
                "VALUES ('sess-x', 't1', ?, 'open', 1)",
                (ctx.space,),
            )
        ctx.remember("sessonly", "sessonly private orbital note", session_id="sess-x")
        ctx.fts.rebuild("t1")
        with ctx.store.read() as tx:
            at_space = ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("orbital"),
                space_id=ctx.space,
            )
            at_session = ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("orbital"),
                space_id=ctx.space,
                session_id="sess-x",
            )
        # A space-level request (R.session = None) must not match a document
        # stored in another session, even though the space matches: a
        # request-side null only ever matches documents at that broader
        # scope — the pre-fix SQL added NO clause for null dims.
        assert not at_space
        assert {hit.document.resource_id for hit in at_session} != set()

    def test_group_scoped_claim_is_positively_recallable(self, ctx: _Ctx) -> None:
        from tests.integration.test_phase6_recall import _bulk_claim

        with ctx.store.write() as tx:
            group = tx.insert_space_group("t1", "crew", "group", actor="t", reason_code="test").id
            group_claim = _bulk_claim(
                tx,
                claim_id="grp-1",
                tenant_id="t1",
                agent_id=ctx.agent,
                space_group_id=group,
                space_id=None,
                session_id=None,
                status="active",
                privacy_labels=(),
                valid_from_us=None,
                valid_until_us=None,
                recorded_at_us=10,
                text="group heliograph beacon",
                subject_entity_id=ctx.entity,
            )
            tx.advance_watermark("t1", ctx.agent, [("claim", "grp-1", 1)])
        ctx.fts.rebuild("t1")
        group_access = access_for(
            "t1",
            agent_ids=frozenset({ctx.agent}),
            space_group_ids=frozenset({group}),
            space_ids=frozenset({ctx.space}),
        )
        with ctx.store.read() as tx:
            hits = ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("heliograph"),
                space_group_id=group,
            )
        assert {hit.document.resource_id for hit in hits} == {group_claim}

        search = SearchService(ctx.store, ctx.clock, ctx.fts)
        results = search.search(
            group_access,
            agent_id=ctx.agent,
            space_group_id=group,
            query="heliograph beacon",
        )
        assert [c.resource_id for c in results] == [group_claim]

    def test_group_outside_access_envelope_is_denied(self, ctx: _Ctx) -> None:
        with ctx.store.write() as tx:
            group = tx.insert_space_group("t1", "crew", "group", actor="t", reason_code="test").id
        ungranted = access_for(
            "t1", agent_ids=frozenset({ctx.agent}), space_ids=frozenset({ctx.space})
        )
        search = SearchService(ctx.store, ctx.clock, ctx.fts)
        with pytest.raises(AccessDeniedError):
            search.search(ungranted, agent_id=ctx.agent, space_group_id=group, query="anything")


# ---------------------------------------------------------------------------
# 4. Purpose and ExternalActor boundaries


class TestPurposeAndActorBoundary:
    def test_purpose_outside_granted_data_purposes_is_denied(self, world: World) -> None:
        restricted = dataclasses.replace(world.access, data_purposes=frozenset({"tool"}))
        request = world.service.build_request(
            request_id="req-purpose-deny",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 5_000_000,
            topic="purpose gate",
            purpose="reply",  # a KNOWN purpose — but outside the grant
        )
        with pytest.raises(AccessDeniedError):
            world.service.recall(restricted, request, actors=world.actors)
        # The same request under a matching grant succeeds.
        granted_request = world.service.build_request(
            request_id="req-purpose-tool",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 5_000_000,
            topic="purpose gate",
            purpose="tool",
        )
        world.service.recall(restricted, granted_request, actors=world.actors)

    def test_empty_purpose_grant_keeps_known_purposes_open(self, world: World) -> None:
        # An access context that declares no purposes keeps the historical
        # behavior: every known purpose is acceptable.
        assert world.access.data_purposes == frozenset()
        _recall(world, request_id="req-purpose-open", topic="purpose open")

    def test_speaker_resolves_inside_the_service(self, world: World) -> None:
        request = world.service.build_request(
            request_id="req-actor-inside",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 5_000_000,
            topic="actor resolution",
        )
        result = world.service.recall(world.access, request, actors=world.actors)
        assert result.request_id == "req-actor-inside"

    def test_unknown_external_actor_is_rejected(self, world: World) -> None:
        request = world.service.build_request(
            request_id="req-actor-unknown",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 5_000_000,
            topic="actor resolution",
        )
        with pytest.raises(IdentityNotFoundError):
            world.service.recall(world.access, request, actors=(_actor("qq", "nobody-999"),))


# ---------------------------------------------------------------------------
# 5. Rebuild completeness and verification


class TestRebuildCompleteness:
    def test_rebuild_paginates_episodes_and_notes_without_truncation(self, ctx: _Ctx) -> None:
        idem = IdempotencyManager(ctx.store)
        episodes = EpisodeService(ctx.store, ctx.clock, idempotency=idem)
        notes = NoteService(ctx.store, ctx.clock, idempotency=idem)
        episode_ids: set[str] = set()
        for index in range(5):
            result = episodes.create(
                ctx.access,
                agent_id=ctx.agent,
                title=f"episode paginated {index}",
                summary="summary",
                space_id=ctx.space,
                idempotency_key=f"idem-ep-{index}",
            )
            episode_ids.add(result.episode_id)
            notes.create(
                ctx.access,
                agent_id=ctx.agent,
                kind="important",
                title=f"note paginated {index}",
                body="body",
                space_id=ctx.space,
                idempotency_key=f"idem-nt-{index}",
            )
        from iris_memory_core.indexing.fts import collect_fts_documents

        # batch=2 forces every resource family through multiple keyset
        # pages; the pre-fix code truncated episodes/notes at one page
        # (10k cap) and still reported the generation "verified".
        with ctx.store.read() as tx:
            documents = collect_fts_documents(tx, "t1", batch=2)
        indexed_episodes = {doc.resource_id for doc in documents if doc.resource_type == "episode"}
        note_count = sum(1 for doc in documents if doc.resource_type == "note")
        assert indexed_episodes == episode_ids
        assert note_count == 5

    def test_verification_recomputes_checksum_from_persisted_rows(self, ctx: _Ctx) -> None:
        from iris_memory_core.indexing.fts import collect_fts_documents

        ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        with ctx.store.read() as tx:
            pointer = tx.fts.pointer("t1")
            assert pointer is not None
            generation = tx.fts.get_generation(pointer.generation_id)
            documents = collect_fts_documents(tx, "t1")
        with ctx.store.write() as tx:
            row = (
                tx.raw()
                .execute(
                    "SELECT id FROM fts_documents WHERE generation_id = ? LIMIT 1",
                    (pointer.generation_id,),
                )
                .fetchone()
            )
            assert row is not None
            tx.raw().execute(
                "UPDATE fts_documents SET content_hash = 'tampered' WHERE id = ?",
                (row["id"],),
            )
        with ctx.store.read() as tx, pytest.raises(ConflictError):
            fts_module.FtsProjectionService._verify_generation(tx, generation, documents)

    def test_verification_detects_missing_rows(self, ctx: _Ctx) -> None:
        from iris_memory_core.indexing.fts import collect_fts_documents

        ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        with ctx.store.read() as tx:
            pointer = tx.fts.pointer("t1")
            assert pointer is not None
            generation = tx.fts.get_generation(pointer.generation_id)
            documents = collect_fts_documents(tx, "t1")
        with ctx.store.write() as tx:
            tx.raw().execute(
                "DELETE FROM fts_documents WHERE generation_id = ?",
                (pointer.generation_id,),
            )
        with ctx.store.read() as tx, pytest.raises(ConflictError):
            fts_module.FtsProjectionService._verify_generation(tx, generation, documents)


# ---------------------------------------------------------------------------
# 6. Staleness trust gate (unsettled fts.apply backlog, per agent)


def _enqueue_fts_apply(ctx: _Ctx, resource_id: str, seq: int) -> None:
    """One unsettled fts.apply job for the ctx agent (distinct resources ⇒
    no coalescing)."""
    from iris_memory_core.application.outbox import enqueue_with_pressure
    from iris_memory_core.domain.jobs import NewOutboxJob

    with ctx.store.write() as tx:
        enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id="t1",
                job_kind="fts.apply",
                aggregate_type="claim",
                aggregate_id=resource_id,
                source_revision=seq,
                payload={
                    "version": 1,
                    "job_kind": "fts.apply",
                    "resource_type": "claim",
                    "resource_id": resource_id,
                },
                dedupe_key=f"fts-apply:t1:claim:{resource_id}:{seq}",
                agent_id=ctx.agent,
                coalesce_key=f"fts:claim:{resource_id}",
                priority=5,
                available_at_us=1,
            ),
            None,
        )


class TestPerAgentStaleness:
    def test_lagging_agent_not_hidden_by_busier_peer(
        self, ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fts_module, "fts_staleness_limit", lambda: 5)
        ctx.remember("alpha", "alpha quantum physics notes")
        with ctx.store.write() as tx:
            second = tx.insert_agent("t1", "B", actor="t").id
        ctx.fts.rebuild("t1")
        # Agent A accumulates unsettled fts.apply work; the caught-up peer
        # has none. The gate is derived from each agent's OWN backlog, so a
        # busy peer can neither hide A's lag nor inherit it.
        for index in range(6):
            _enqueue_fts_apply(ctx, f"lag-{index}", index + 1)
        with ctx.store.read() as tx, pytest.raises(FtsDegradedError) as captured:
            ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
            )
        assert captured.value.reason_code == FTS_REASON_GENERATION_STALE
        with ctx.store.read() as tx:
            hits = ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=second,
                match_expression=build_fts_query("quantum"),
            )
        assert isinstance(hits, list)

    def test_draining_the_queue_clears_staleness(
        self, ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fts_module, "fts_staleness_limit", lambda: 5)
        ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        for index in range(6):
            _enqueue_fts_apply(ctx, f"drift-{index}", index + 1)
        with ctx.store.read() as tx, pytest.raises(FtsDegradedError):
            ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
            )
        # The worker drains the queue: each fenced completion CAS settles a
        # job and shrinks the backlog below the limit — the gate derives
        # from live queue state, so there is no watermark to forget.
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
        assert {hit.document.resource_id for hit in hits} != set()


# ---------------------------------------------------------------------------
# 7. Ranker ordering


class TestRankerOrdering:
    def test_bm25_relevance_increases_with_match_strength(self, ctx: _Ctx) -> None:
        weak = ctx.remember("weak", "quantum minor mention only once")
        strong = ctx.remember(
            "strong", "quantum quantum quantum quantum quantum entanglement deep dive"
        )
        ctx.fts.rebuild("t1")
        with ctx.store.read() as tx:
            hits = ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
                space_id=ctx.space,
            )
        relevance = {hit.document.resource_id: hit.relevance for hit in hits}
        assert set(relevance) == {weak.claim_id, strong.claim_id}
        # bm25 is more negative for stronger matches; the normalized
        # relevance must be HIGHER for the stronger document — the pre-fix
        # 1/(1+|bm25|) mapping inverted this ordering.
        assert relevance[strong.claim_id] > relevance[weak.claim_id]

    def test_redundancy_keeps_the_highest_scored_candidate(self) -> None:
        def candidate(cid: str, confidence: float) -> ScoredCandidate:
            return ScoredCandidate(
                candidate_id=cid,
                route="claims",
                resource_type="claim",
                resource_id=cid,
                resource_revision=1,
                subject_entity_id=None,
                category="fact",
                content_hash="same-hash",
                occurred_us=100,
                token_estimate=1,
                scores={"confidence": confidence},
            )

        # Input order: the LOW-scored duplicate arrives first. The stable
        # sort must keep the high-scored one and penalize the other (the
        # pre-fix swap-back-to-input-order could do the opposite).
        ranked = RankerV2().score([candidate("cand-low", 0.1), candidate("cand-high", 0.9)])
        states = {c.candidate_id: c.conflict_state for c in ranked}
        assert states["cand-high"] is None
        assert states["cand-low"] == "redundant"


# ---------------------------------------------------------------------------
# 8. Usage report replay


class TestUsageReplay:
    def test_identical_replay_returns_stored_counts(self, world: World) -> None:
        _remember(world, "usage-replay", "usage replay anchoring claim")
        result = _recall(world, request_id="req-usage-replay", topic="usage replay")
        returned = [c.candidate_id for c in result.candidates]
        assert returned, "recall must return candidates for the usage anchor"
        usage = RecallUsageService(world.store, world.clock)

        def report() -> RecallUsageReportInput:
            return RecallUsageReportInput(
                request_id="req-usage-replay",
                host_cycle_id="cycle-1",
                returned_candidate_ids=tuple(returned),
                host_selected_candidate_ids=tuple(returned),
                model_visible_candidate_ids=tuple(returned[:1]),
                persona_revision=result.persona_revision,
                reported_at_us=1_700_000_000_000_001,
            )

        first = usage.report(world.access, report())
        assert first.created is True
        replay = usage.report(world.access, report())
        assert replay.created is False
        assert replay.report_id == first.report_id
        assert replay.host_selected_count == first.host_selected_count
        assert replay.model_visible_count == first.model_visible_count

    def test_diverging_replay_is_rejected(self, world: World) -> None:
        _remember(world, "usage-conflict", "usage conflict anchoring claim")
        result = _recall(world, request_id="req-usage-conflict", topic="usage conflict")
        returned = [c.candidate_id for c in result.candidates]
        assert returned
        usage = RecallUsageService(world.store, world.clock)
        first = usage.report(
            world.access,
            RecallUsageReportInput(
                request_id="req-usage-conflict",
                host_cycle_id="cycle-1",
                returned_candidate_ids=tuple(returned),
                host_selected_candidate_ids=tuple(returned),
                model_visible_candidate_ids=tuple(returned[:1]),
                persona_revision=result.persona_revision,
                reported_at_us=1_700_000_000_000_001,
            ),
        )
        assert first.created is True
        # A DIFFERENT stage split under the same idempotency identity must
        # not silently replace or double-count the stored report.
        conflicting = RecallUsageReportInput(
            request_id="req-usage-conflict",
            host_cycle_id="cycle-1",
            returned_candidate_ids=tuple(returned),
            host_selected_candidate_ids=tuple(returned[:1]),
            model_visible_candidate_ids=tuple(returned[:1]),
            persona_revision=result.persona_revision,
            reported_at_us=1_700_000_000_000_002,
        )
        with pytest.raises(DomainError) as captured:
            usage.report(world.access, conflicting)
        assert "conflict" in str(captured.value).lower()


# ---------------------------------------------------------------------------
# 9. Ghost postings


class TestGhostPostings:
    def test_retired_generation_cleanup_leaves_no_shadow_rows(self, ctx: _Ctx) -> None:
        ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        ctx.remember("beta", "beta gravitational waves")
        second = ctx.fts.rebuild("t1")  # generation 1 becomes retired
        with ctx.store.write() as tx:
            deleted = tx.fts.delete_retired_generations("t1", keep=0)
        assert deleted == 1
        with ctx.store.read() as tx:
            live_docs = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM fts_documents WHERE generation_id = ?",
                    (second.generation_id,),
                )
                .fetchone()[0]
            )
            shadow_rows = tx.raw().execute("SELECT COUNT(*) FROM fts_index_docsize").fetchone()[0]
        # Without the FTS5 'delete' commands the external-content shadow
        # tables keep postings for the dropped generation's rowids and the
        # index bloats while the join hides the ghosts from results.
        assert shadow_rows == live_docs
