"""Phase 6 adversarial review regressions (ADR-0014 §12 review hardening).

One regression per attack surface probed in the adversarial self-review:
stale-snapshot resurrection, Forget-vs-FTS resurrection without the async
apply, generation-swap leftovers, usage forgery vectors, budget starvation
of protected layers, and privacy leakage through the FTS projection.
"""

from __future__ import annotations

import sqlite3

import pytest

from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import SystemMonotonicClock, Transaction
from iris_memory_core.application.recall import (
    RecallUsageReportInput,
    RecallUsageService,
    SearchService,
    StructuredRecallRequest,
)
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.storage.idempotency import IdempotencyManager
from tests.conftest import access_for
from tests.integration.recall.test_fts_recall_recall import (
    World,
    _bulk_claim,
    _last_bulk_id,
    _recall,
    _returned_ids,
)


def _seed_claim_via_service(world: World, key: str, text: str) -> str:
    return world.claims.remember(
        world.access,
        agent_id=world.agent,
        space_id=world.space,
        subject_entity_id=world.entity,
        predicate=f"p_{key}",
        value={"k": key},
        canonical_text=text,
        category="fact",
        evidence=[
            {
                "source_type": "observation",
                "source_id": ObservationService(world.store)
                .observe_batch(
                    world.access,
                    [
                        {
                            "agent_id": world.agent,
                            "role": "user",
                            "kind": "message.text",
                            "idempotency_key": f"adv-obs-{key}",
                            "occurred_us": world.clock.now_us(),
                            "committed_us": world.clock.now_us(),
                            "content": f"evidence {key}",
                            "space_id": world.space,
                        }
                    ],
                )
                .accepted_observation_ids[0],
            }
        ],
        idempotency_key=f"adv-{key}",
    ).claim_id


def test_forget_then_search_without_async_apply_never_resurrects(world: World) -> None:
    """The tombstone exclusion in the FTS query itself is the defense of last
    resort: even if the async fts.apply never ran, search finds nothing."""
    claim = _seed_claim_via_service(world, "resurrect", "resurrection physics omega")
    world.fts.rebuild("t1")
    assert world.fts.pointer_info("t1")["generation"] is not None
    world.forget.forget(
        world.access,
        ForgetSelector(kind=ForgetSelectorKind.RESOURCE, resource_type="claim", resource_id=claim),
        reason="adversarial",
        idempotency_key="adv-forget-1",
    )
    # NO fts.apply runs — the stale document row is still 'active'.
    with world.store.read() as tx:
        doc = tx.fts.document_for_resource("t1", "claim", claim)
    assert doc is not None and doc.doc_status == "active"
    search = SearchService(world.store, world.clock, world.fts)
    hits = search.search(
        world.access, agent_id=world.agent, space_id=world.space, query="resurrection"
    )
    assert claim not in {hit.resource_id for hit in hits}
    result = _recall(world, topic="resurrection physics omega")
    assert claim not in _returned_ids(result)


def test_rebuild_after_forget_never_resurrects(world: World) -> None:
    claim = _seed_claim_via_service(world, "resurrect2", "second resurrection physics omega")
    world.forget.forget(
        world.access,
        ForgetSelector(kind=ForgetSelectorKind.RESOURCE, resource_type="claim", resource_id=claim),
        reason="adversarial",
        idempotency_key="adv-forget-2",
    )
    report = world.fts.rebuild("t1")
    # The tombstoned claim is not part of the new generation at all.
    with world.store.read() as tx:
        doc = tx.fts.document_for_resource("t1", "claim", claim)
    assert doc is None
    assert report.document_count == 0
    result = _recall(world, topic="second resurrection")
    assert claim not in _returned_ids(result)


def test_generation_swap_leaves_no_servable_leftovers(world: World) -> None:
    claim = _seed_claim_via_service(world, "swap", "generation swap physics omega")
    first = world.fts.rebuild("t1")
    # Simulate a leftover: a doc still bound to the OLD generation after a
    # later switch (the physical cleanup is async and may lag).
    world.claims.correct(
        world.access,
        claim,
        expected_revision=1,
        mode="supersede",
        value={"k": "v2"},
        canonical_text="generation swap corrected theta",
        evidence=[
            {
                "source_type": "observation",
                "source_id": ObservationService(world.store)
                .observe_batch(
                    world.access,
                    [
                        {
                            "agent_id": world.agent,
                            "role": "user",
                            "kind": "message.text",
                            "idempotency_key": "adv-obs-swap-correct",
                            "occurred_us": world.clock.now_us(),
                            "committed_us": world.clock.now_us(),
                            "content": "swap evidence",
                            "space_id": world.space,
                        }
                    ],
                )
                .accepted_observation_ids[0],
            }
        ],
        idempotency_key="adv-swap-correct",
    )
    second = world.fts.rebuild("t1")
    assert second.generation_id != first.generation_id
    # The leftover shape is structurally unrepresentable: a resource can
    # carry at most ONE document row per generation, and the search joins on
    # the CURRENT generation only. Forcing the duplicate is rejected.
    with pytest.raises(sqlite3.IntegrityError), world.store.write() as tx:
        tx.raw().execute(
            "INSERT INTO fts_documents (tenant_id, generation_id, resource_type, "
            "resource_id, resource_revision, agent_id, scope_key, canonical_status, "
            "privacy_labels, content_hash, occurred_us, index_text, doc_status, "
            "builder_version, source_watermark, tombstone_watermark, created_us) "
            "VALUES ('t1', ?, 'claim', ?, 1, ?, 'k', 'active', '[]', 'h', 1, "
            "'leftover duplicate', 'active', 1, 0, 0, 1)",
            (first.generation_id, claim, world.agent),
        )
    search = SearchService(world.store, world.clock, world.fts)
    # The stale-generation revision (omega, rev 1) is unfindable: generation
    # membership in SQL excludes everything outside the CURRENT pointer.
    stale_hits = search.search(
        world.access, agent_id=world.agent, space_id=world.space, query="omega"
    )
    assert claim not in {hit.resource_id for hit in stale_hits}
    # The CURRENT revision (theta, rev 2) is served with its real revision.
    current_hits = search.search(
        world.access, agent_id=world.agent, space_id=world.space, query="theta"
    )
    by_id = {hit.resource_id: hit for hit in current_hits}
    assert claim in by_id and by_id[claim].resource_revision == 2
    result = _recall(world, topic="generation swap corrected theta")
    returned = {c.resource_id: c for c in result.candidates}
    assert claim in returned and returned[claim].resource_revision == 2


def test_usage_forgery_vectors_all_fail(world: World) -> None:
    from tests.integration.recall.test_fts_recall_recall import _deterministic_world

    _deterministic_world(world)
    usage = RecallUsageService(world.store, world.clock)
    result = _recall(world, request_id="adv-usage", topic="deterministic quantum")
    ids = [candidate.candidate_id for candidate in result.candidates]
    assert ids
    forged = "cand:" + "0" * 16

    def report(**overrides: object) -> RecallUsageReportInput:
        payload = dict(
            request_id="adv-usage",
            host_cycle_id="adv-cycle",
            returned_candidate_ids=ids,
            host_selected_candidate_ids=ids,
            model_visible_candidate_ids=ids,
            persona_revision=result.persona_revision,
            reported_at_us=1,
        )
        payload.update(overrides)
        return RecallUsageReportInput(**payload)

    from iris_memory_core.domain.errors import InvalidRequestError, NotReadyError

    with pytest.raises(InvalidRequestError):
        usage.report(world.access, report(host_selected_candidate_ids=[*ids, forged]))
    with pytest.raises(InvalidRequestError):
        usage.report(world.access, report(model_visible_candidate_ids=[forged]))
    with pytest.raises(InvalidRequestError):
        usage.report(world.access, report(returned_candidate_ids=ids[:-1]))
    with pytest.raises(NotReadyError):
        usage.report(world.access, report(request_id="req-from-other-tenant"))
    # A caller outside the agent context cannot report at all.
    stranger = access_for("t1", agent_ids=frozenset({"other-agent"}))
    from iris_memory_core.domain.errors import AccessDeniedError

    with pytest.raises((AccessDeniedError, NotReadyError)):
        usage.report(stranger, report())


def test_starved_budget_keeps_speaker_identity_and_due_signal(world: World) -> None:
    from iris_memory_core.application.focus import FocusService
    from iris_memory_core.application.recall import StructuredRecallOrchestrator
    from iris_memory_core.application.recent import RecentContextService
    from iris_memory_core.application.state import StateService
    from iris_memory_core.storage.idempotency import IdempotencyManager

    now = world.clock.now_us()
    with world.store.write() as tx:
        _bulk_claim(
            tx,
            claim_id="adv-speaker-identity",
            tenant_id=world.tenant,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
            status="active",
            privacy_labels=(),
            valid_from_us=None,
            valid_until_us=None,
            recorded_at_us=now,
            text="adv speaker identity claim",
            subject_entity_id=world.entity,
            category="identity",
        )
        speaker_identity = _last_bulk_id[0]
        _bulk_claim(
            tx,
            claim_id="adv-noise",
            tenant_id=world.tenant,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
            status="active",
            privacy_labels=(),
            valid_from_us=None,
            valid_until_us=None,
            recorded_at_us=now,
            text="adv noise claim that must be trimmed away under a zero budget",
            subject_entity_id=world.entity,
        )
        noise = _last_bulk_id[0]
    orchestrator = StructuredRecallOrchestrator(
        world.store,
        RecentContextService(world.store, world.store.clock),
        StateService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store)),
        FocusService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store)),
        clock=world.store.clock,
        claims_enabled=True,
        monotonic=SystemMonotonicClock(),
    )
    request = StructuredRecallRequest(
        request_id="adv-starve",
        agent_id=world.agent,
        space_id=world.space,
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="adv speaker identity",
        token_budget=0,
        speaker_entity_id=world.entity,
    )
    result = orchestrator.recall(world.access, request)
    returned = _returned_ids(result)
    assert speaker_identity in returned
    assert noise not in returned


def test_restricted_indexed_content_never_reaches_non_admin(world: World) -> None:
    secret = "RESTRICTED-CANARY-OMEGA"
    now = world.clock.now_us()
    with world.store.write() as tx:
        _bulk_claim(
            tx,
            claim_id="adv-restricted",
            tenant_id=world.tenant,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
            status="active",
            privacy_labels=("restricted",),
            valid_from_us=None,
            valid_until_us=None,
            recorded_at_us=now,
            text=secret,
            subject_entity_id=world.entity,
        )
        restricted_id = _last_bulk_id[0]
    world.fts.rebuild("t1")
    # The restricted claim IS indexed (labels ride on the doc)…
    with world.store.read() as tx:
        doc = tx.fts.document_for_resource("t1", "claim", restricted_id)
    assert doc is not None and doc.doc_status == "active"
    # …but neither recall nor /v1/search ever returns it to a non-admin, and
    # the secret text never appears anywhere in the envelope.
    result = _recall(world, topic="RESTRICTED CANARY OMEGA", include_trace=True)
    assert restricted_id not in _returned_ids(result)
    blob = repr(result.candidates) + repr(result.trace) + repr(result.degraded_routes)
    assert secret not in blob
    search = SearchService(world.store, world.clock, world.fts)
    hits = search.search(world.access, agent_id=world.agent, space_id=world.space, query="canary")
    assert restricted_id not in {hit.resource_id for hit in hits}
    assert all(secret not in hit.text for hit in hits)


def test_fresh_rehydrate_boundary_survives_in_tx_forget(world: World) -> None:
    """A Forget committed from INSIDE a route's own transaction window is
    still observed: the rehydrate runs in a later, fresh snapshot."""
    claim = _seed_claim_via_service(world, "barrier2", "in-tx barrier physics omega")
    world.fts.rebuild("t1")
    from iris_memory_core.application.focus import FocusService
    from iris_memory_core.application.recall import (
        ClaimsRoute,
        RecallCandidate,
        StructuredRecallOrchestrator,
    )
    from iris_memory_core.application.recent import RecentContextService
    from iris_memory_core.application.state import StateService

    class _CollectThenForget:
        name = "claims"

        def __init__(self) -> None:
            self._inner = ClaimsRoute()

        def collect(
            self,
            tx: Transaction,
            request: StructuredRecallRequest,
            access: AccessContext,
            deadline_us: int,
            now_us: int,
        ) -> tuple[RecallCandidate, ...]:
            candidates = self._inner.collect(tx, request, access, deadline_us, now_us)
            picked = [c for c in candidates if c.resource_id == claim]
            if picked:
                # Commit a tombstone AFTER collecting, from another
                # connection, while this read snapshot stays open.
                world.forget.forget(
                    world.access,
                    ForgetSelector(
                        kind=ForgetSelectorKind.RESOURCE,
                        resource_type="claim",
                        resource_id=claim,
                    ),
                    reason="barrier",
                    idempotency_key="barrier-in-tx",
                )
            return tuple(picked)

    orchestrator = StructuredRecallOrchestrator(
        world.store,
        RecentContextService(world.store, world.store.clock),
        StateService(
            world.store,
            world.store.clock,
            idempotency=IdempotencyManager(world.store),
        ),
        FocusService(world.store, world.store.clock),
        clock=world.store.clock,
        routes=(_CollectThenForget(),),
        monotonic=SystemMonotonicClock(),
    )
    request = StructuredRecallRequest(
        request_id="adv-barrier",
        agent_id=world.agent,
        space_id=world.space,
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="in-tx barrier",
        token_budget=1_000_000,
    )
    result = orchestrator.recall(world.access, request)
    assert claim not in _returned_ids(result)
    assert result.dropped_by_rehydrate >= 1


def test_stopword_topic_completes_fts_with_zero_not_degraded(world: World) -> None:
    _seed_claim_via_service(world, "stopword", "stopword probe physics omega")
    world.fts.rebuild("t1")
    result = _recall(world, topic="the of and")
    degraded = {d.route: d.reason_code for d in result.degraded_routes}
    assert "fts" not in degraded
    assert "fts" in result.completed_routes
