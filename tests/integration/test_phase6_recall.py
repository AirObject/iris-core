"""Phase 6 recall protocol tests (§18, ADR-0014 §3-5).

Hard-filter property cases (200 seeded cases per dimension), fresh-rehydrate
coordination barrier against concurrent Forget, replay determinism (100x),
degraded/partial envelopes (20x each scenario), protected budgets, missing
score semantics and the privacy leak scan.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import pytest

from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import SystemMonotonicClock, Transaction
from iris_memory_core.application.recall import (
    ClaimsRoute,
    FtsRoute,
    RecallCandidate,
    RecallService,
    RecentContextRoute,
    StructuredRecallOrchestrator,
    StructuredRecallRequest,
    check_deadline,
)
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    IdentityNotFoundError,
    InvalidRequestError,
    NotReadyError,
)
from iris_memory_core.domain.hashing import content_hash
from iris_memory_core.domain.identity import (
    BindingMethod,
    BindingState,
    EntityKind,
    ExternalIdentityKey,
)
from iris_memory_core.domain.memory import memory_scope_key
from iris_memory_core.indexing.fts import FtsProjectionService
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for

PROPERTY_CASES = 200
REPLAYS = 100
SCENARIO_RUNS = 20
BASE_US = 1_700_000_000_000_000

DIMENSIONS = (
    "tenant",
    "agent",
    "space_group",
    "space",
    "session",
    "privacy",
    "status",
    "time",
    "tombstone",
    "as_of",
    "revision",
)


@dataclass
class World:
    store: Store
    clock: MutableClock
    tenant: str
    agent: str
    other_agent: str
    other_tenant_agent: str
    space: str
    other_space: str
    session: str
    entity: str
    peer_entity: str
    speaker: str
    actors: tuple[Any, ...]
    access: AccessContext
    orchestrator: StructuredRecallOrchestrator
    service: RecallService
    fts: FtsProjectionService
    claims: ClaimService
    forget: ForgetService


def _bulk_claim(
    tx: Transaction,
    *,
    claim_id: str,
    tenant_id: str,
    agent_id: str,
    space_group_id: str | None,
    space_id: str | None,
    session_id: str | None,
    status: str,
    privacy_labels: tuple[str, ...],
    valid_from_us: int | None,
    valid_until_us: int | None,
    recorded_at_us: int,
    text: str,
    subject_entity_id: str,
    category: str = "fact",
) -> str:
    scope_key = memory_scope_key(tenant_id, agent_id, space_group_id, space_id, session_id)
    digest = content_hash({"text": text, "labels": list(privacy_labels)})
    generated_id = tx.claims.insert(
        tenant_id=tenant_id,
        agent_id=agent_id,
        space_group_id=space_group_id,
        space_id=space_id,
        session_id=session_id,
        scope_key=scope_key,
        subject_entity_id=subject_entity_id,
        predicate=f"p_{claim_id}",
        category=category,
        status=status,
        confidence=0.5,
        importance=0.5,
        accessibility=1.0,
        source_authority="user_statement",
        valid_from_us=valid_from_us,
        valid_until_us=valid_until_us,
        evidence_count=1,
        dedup_key=f"dedup-{claim_id}",
        recorded_at_us=recorded_at_us,
        extractor_version=None,
    )
    revision_id = tx.claims.insert_revision(
        claim_id=generated_id,
        tenant_id=tenant_id,
        revision=1,
        subject_entity_id=subject_entity_id,
        predicate=f"p_{claim_id}",
        value_json='{"k":1}',
        canonical_text=text,
        category=category,
        privacy_labels=privacy_labels,
        source_refs=(),
        status=status,
        confidence=0.5,
        importance=0.5,
        accessibility=1.0,
        source_authority="user_statement",
        valid_from_us=valid_from_us,
        valid_until_us=valid_until_us,
        recorded_at_us=recorded_at_us,
        extractor_version=None,
        content_hash=digest,
        created_by="test",
    )
    tx.claims.set_initial_pointer(generated_id, revision_id)
    _last_bulk_id[0] = generated_id
    return generated_id


def build_world(store: Store, clock: MutableClock) -> World:
    with store.write() as tx:
        tx.insert_tenant("t1", status="active")
        tx.insert_tenant("t2", status="active")
        agent = tx.insert_agent("t1", "A1", actor="t").id
        other_agent = tx.insert_agent("t1", "A2", actor="t").id
        other_tenant_agent = tx.insert_agent("t2", "A3", actor="t").id
        space = tx.insert_space("t1", "chat_group").id
        other_space = tx.insert_space("t1", "direct").id
        tx.insert_space("t2", "direct")
        session = tx.insert_session("t1", space, actor="t").id
        entity = tx.identities.insert_entity("t1", EntityKind.PERSON, display_name="Bob").id
        identity = tx.insert_external_identity(
            ExternalIdentityKey(
                tenant_id="t1", provider="qq", realm="default", external_id="bob-1"
            ),
            entity_id=None,
        )
        binding = tx.insert_binding(
            "t1",
            identity.id,
            entity,
            method=BindingMethod.ADMIN_CONFIRMATION,
            confidence=0.99,
            proof_digest="d",
            actor="t",
            reason_code="test",
        )
        tx.transition_binding(
            binding.id,
            BindingState.VERIFIED,
            expected_revision=binding.revision,
            actor="t",
            reason_code="test",
        )
        peer_entity = tx.identities.insert_entity("t1", EntityKind.PERSON, display_name="Peer").id
        tx.raw().execute(
            "INSERT INTO sessions (id, tenant_id, space_id, status, started_us) "
            "VALUES ('session-other', 't1', ?, 'open', 1)",
            (space,),
        )
        tx.advance_watermark("t1", agent, [("agent", agent, 1)])
    access = access_for(
        "t1",
        agent_ids=frozenset({agent}),
        space_ids=frozenset({space, other_space}),
        consent_entities=frozenset({entity}),
    )
    idem = IdempotencyManager(store)
    claims = ClaimService(store, clock, idempotency=idem)
    fts = FtsProjectionService(store, clock)
    recent = RecentContextService(store, clock)
    states = StateService(store, clock, idempotency=idem)
    focus = FocusService(store, clock, idempotency=idem)
    orchestrator = StructuredRecallOrchestrator(
        store,
        recent,
        states,
        focus,
        clock=clock,
        claims_enabled=True,
        relations_enabled=True,
        fts=fts,
        monotonic=SystemMonotonicClock(),
    )
    service = RecallService(orchestrator, store, clock)
    actors = (_actor("qq", "bob-1"),)
    speaker = service.resolve_speaker(access, actors)
    assert speaker == entity
    return World(
        store=store,
        clock=clock,
        tenant="t1",
        agent=agent,
        other_agent=other_agent,
        other_tenant_agent=other_tenant_agent,
        space=space,
        other_space=other_space,
        session=session,
        entity=entity,
        peer_entity=peer_entity,
        speaker=speaker,
        actors=actors,
        access=access,
        orchestrator=orchestrator,
        service=service,
        fts=fts,
        claims=claims,
        forget=ForgetService(store, clock, idempotency=idem),
    )


def _actor(provider: str, external_id: str) -> Any:
    from iris_memory_core.application.recall import ExternalActorRef

    return ExternalActorRef(provider=provider, external_id=external_id)


def _recall(
    world: World,
    *,
    request_id: str = "req-1",
    topic: str = "recall probe",
    as_of_us: int | None = None,
    token_budget: int = 1_000_000,
    allow_partial: bool = True,
    include_trace: bool = False,
    minimum_watermark: int | None = None,
    candidate_limits: dict[str, int] | None = None,
    session_id: str | None = None,
) -> Any:
    request = world.service.build_request(
        request_id=request_id,
        agent_id=world.agent,
        space_id=world.space,
        session_id=session_id,
        deadline_at_us=world.clock.now_us() + 5_000_000,
        topic=topic,
        as_of_us=as_of_us,
        token_budget=token_budget,
        allow_partial=allow_partial,
        include_trace=include_trace,
        minimum_watermark=minimum_watermark,
        candidate_limits=candidate_limits,
    )
    return world.service.recall(world.access, request, actors=world.actors)


def _returned_ids(result: Any) -> set[str]:
    return {candidate.resource_id for candidate in result.candidates}


# ---------------------------------------------------------------------------
# Hard-filter property cases: 200 seeded cases per dimension (§18.5)


_last_bulk_id: list[str] = [""]


def _seed_dimension(world: World, dimension: str) -> tuple[list[str], list[str]]:
    """Bulk-create 200 violating + 200 control claims for one dimension."""
    violating: list[str] = []
    control: list[str] = []
    now = world.clock.now_us()
    with world.store.write() as tx:
        for index in range(PROPERTY_CASES):
            vid = f"claim-v-{dimension}-{index:03d}"
            cid = f"claim-c-{dimension}-{index:03d}"
            common: dict[str, Any] = dict(
                claim_id=vid,
                tenant_id=world.tenant,
                agent_id=world.agent,
                space_group_id=None,
                space_id=world.space,
                session_id=None,
                status="active",
                privacy_labels=(),
                valid_from_us=None,
                valid_until_us=None,
                recorded_at_us=now - 10_000 + index,
                text=f"violating {dimension} {index}",
                subject_entity_id=world.entity,
            )
            if dimension == "tenant":
                common.update(tenant_id="t2", agent_id=world.other_tenant_agent, space_id=None)
            elif dimension == "agent":
                common.update(agent_id=world.other_agent)
            elif dimension == "space_group":
                common.update(space_group_id="sg-1", space_id=None)
            elif dimension == "space":
                common.update(space_id=world.other_space)
            elif dimension == "session":
                common.update(session_id="session-other")
            elif dimension == "privacy":
                common.update(privacy_labels=("restricted",))
            elif dimension == "status":
                common.update(status="retracted")
            elif dimension == "time":
                common.update(valid_from_us=now - 20_000, valid_until_us=now - 5_000)
            elif dimension == "tombstone":
                pass  # tombstoned after insert below
            elif dimension == "as_of":
                common.update(recorded_at_us=now + 10_000_000 + index)
            real_violating = _bulk_claim(tx, **common)
            if dimension == "tombstone":
                tx.record_tombstone(
                    tenant_id=world.tenant,
                    resource_type="claim",
                    resource_id=real_violating,
                    reason_code="test",
                    deleted_by="test",
                )
            violating.append(real_violating)
            _bulk_claim(
                tx,
                claim_id=cid,
                tenant_id=world.tenant,
                agent_id=world.agent,
                space_group_id=None,
                space_id=world.space,
                session_id=None,
                status="active",
                privacy_labels=(),
                valid_from_us=None,
                valid_until_us=None,
                recorded_at_us=now - 30_000 + index,
                text=f"control {dimension} {index}",
                subject_entity_id=world.entity,
            )
            control.append(_last_bulk_id[0])
        tx.advance_watermark(world.tenant, world.agent, [("claim", "bulk", 1)])
    return violating, control


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_hard_filter_dimensions_exclude_violating_candidates(world: World, dimension: str) -> None:
    if dimension == "revision":
        test_hard_filter_revision_dimension(world)
        return
    violating, control = _seed_dimension(world, dimension)
    rng = random.Random(20260902)
    as_of = world.clock.now_us() + 5_000_000 if dimension == "as_of" else None
    for case in range(PROPERTY_CASES):
        index = rng.randrange(PROPERTY_CASES)
        result = _recall(
            world,
            request_id=f"req-{dimension}-{case}",
            topic=f"probe {dimension}",
            as_of_us=as_of,
            candidate_limits={"claims": 500},
        )
        returned = _returned_ids(result)
        assert violating[index] not in returned, (dimension, case)
        assert control[index] in returned, (dimension, case)


def test_hard_filter_revision_dimension(world: World) -> None:
    """A stale-revision candidate is dropped by the FINAL rehydrate (§18.5)."""
    now = world.clock.now_us()
    with world.store.write() as tx:
        _bulk_claim(
            tx,
            claim_id="claim-stale-1",
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
            text="stale revision carrier",
            subject_entity_id=world.entity,
        )
        stale_id = _last_bulk_id[0]

    class _StaleClaimsRoute:
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
            # Fabricate one candidate claiming a SUPERSEDED revision.
            stale = [candidate for candidate in candidates if candidate.resource_id == stale_id]
            if not stale:
                return ()
            from dataclasses import replace

            return (replace(stale[0], resource_revision=99),)

    orchestrator = StructuredRecallOrchestrator(
        world.store,
        RecentContextService(world.store, world.store.clock),
        StateService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store)),
        FocusService(world.store, world.store.clock),
        clock=world.store.clock,
        routes=(_StaleClaimsRoute(),),
        monotonic=SystemMonotonicClock(),
    )
    request = StructuredRecallRequest(
        request_id="req-stale",
        agent_id=world.agent,
        space_id=world.space,
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="stale",
        token_budget=1_000_000,
    )
    for case in range(PROPERTY_CASES):
        result = orchestrator.recall(world.access, request)
        assert stale_id not in _returned_ids(result), case
        assert result.dropped_by_rehydrate >= 1


# ---------------------------------------------------------------------------
# Fresh rehydrate boundary + concurrent Forget (ADR-0014 §3)


def test_concurrent_forget_during_collection_never_resurrects(world: World) -> None:
    """Coordination barrier: the FTS route hits a doomed claim, the Forget
    commits BETWEEN collection and rehydrate, and the fresh rehydrate must
    observe the tombstone (old-content returns = 0)."""
    from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind

    doomed_holder: dict[str, str] = {"id": ""}

    class _BarrierRoute:
        """Collects the doomed candidate from FTS, THEN commits the Forget
        while the (older) snapshot already saw the row."""

        name = "fts"

        def __init__(self, fts: FtsProjectionService) -> None:
            self._inner = FtsRoute(fts)

        def collect(
            self,
            tx: Transaction,
            request: StructuredRecallRequest,
            access: AccessContext,
            deadline_us: int,
            now_us: int,
        ) -> tuple[RecallCandidate, ...]:
            candidates = self._inner.collect(tx, request, access, deadline_us, now_us)
            doomed = [c for c in candidates if c.resource_id == doomed_holder["id"]]
            if doomed:
                world.forget.forget(
                    world.access,
                    ForgetSelector(
                        kind=ForgetSelectorKind.RESOURCE,
                        resource_type="claim",
                        resource_id=doomed_holder["id"],
                    ),
                    reason="barrier",
                    idempotency_key=f"barrier-{doomed_holder['id']}",
                )
            return tuple(doomed)

    orchestrator = StructuredRecallOrchestrator(
        world.store,
        RecentContextService(world.store, world.store.clock),
        StateService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store)),
        FocusService(world.store, world.store.clock),
        clock=world.store.clock,
        routes=(
            RecentContextRoute(
                RecentContextService(world.store, world.store.clock),
                SystemMonotonicClock(),
            ),
            _BarrierRoute(world.fts),
        ),
        monotonic=SystemMonotonicClock(),
    )
    request = StructuredRecallRequest(
        request_id="req-barrier",
        agent_id=world.agent,
        space_id=world.space,
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="doomed quantum",
        token_budget=1_000_000,
    )
    doomed_ids = [
        world.claims.remember(
            world.access,
            agent_id=world.agent,
            space_id=world.space,
            subject_entity_id=world.entity,
            predicate=f"p_doomed_{run}",
            value={"run": run},
            canonical_text=f"doomed quantum secret alpha run {run}",
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
                                "idempotency_key": f"obs-doomed-{run}",
                                "occurred_us": world.clock.now_us(),
                                "committed_us": world.clock.now_us(),
                                "content": f"doomed evidence {run}",
                                "space_id": world.space,
                            }
                        ],
                    )
                    .accepted_observation_ids[0],
                }
            ],
            idempotency_key=f"idem-doomed-{run}",
        ).claim_id
        for run in range(SCENARIO_RUNS)
    ]
    world.fts.rebuild("t1")
    for run in range(SCENARIO_RUNS):
        doomed_holder["id"] = doomed_ids[run]
        result = orchestrator.recall(world.access, request)
        assert doomed_ids[run] not in _returned_ids(result), run
        assert result.dropped_by_rehydrate >= 1, run
        texts = [c.text for c in result.candidates]
        assert all("doomed quantum secret alpha" not in text for text in texts), run


# ---------------------------------------------------------------------------
# Determinism, budgets, missing scores, conflicts (§18.6)


def _deterministic_world(world: World) -> None:
    now = world.clock.now_us()
    observations = ObservationService(world.store)
    for i in range(6):
        observations.observe_batch(
            world.access,
            [
                {
                    "agent_id": world.agent,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"det-obs-{i}",
                    "occurred_us": now + i * 1_000,
                    "committed_us": now + i * 1_000,
                    "content": f"deterministic turn {i} quantum",
                    "space_id": world.space,
                    "session_id": world.session,
                }
            ],
        )
    for i in range(5):
        world.claims.remember(
            world.access,
            agent_id=world.agent,
            space_id=world.space,
            subject_entity_id=world.entity,
            predicate=f"p_det_{i}",
            value={"i": i},
            canonical_text=f"deterministic claim {i} quantum physics",
            category="identity" if i == 0 else "fact",
            confidence=0.4 + i * 0.1,
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": observations.observe_batch(
                        world.access,
                        [
                            {
                                "agent_id": world.agent,
                                "role": "user",
                                "kind": "message.text",
                                "idempotency_key": f"det-ev-{i}",
                                "occurred_us": now,
                                "committed_us": now,
                                "content": f"evidence {i}",
                                "space_id": world.space,
                            }
                        ],
                    ).accepted_observation_ids[0],
                }
            ],
            idempotency_key=f"det-claim-{i}",
        )
    world.fts.rebuild("t1")


def test_hundred_replays_are_byte_identical(world: World) -> None:
    _deterministic_world(world)
    signatures: set[tuple[Any, ...]] = set()
    for _replay in range(REPLAYS):
        result = _recall(
            world,
            request_id="req-det",
            topic="deterministic quantum",
            include_trace=False,
        )
        signature = tuple(
            (
                candidate.candidate_id,
                candidate.route,
                candidate.final_score,
                candidate.conflict_state,
                candidate.token_estimate,
                candidate.missing_components,
            )
            for candidate in result.candidates
        )
        signatures.add(signature)
    assert len(signatures) == 1
    assert len(next(iter(signatures))) >= 5  # multiple routes contributed


def test_missing_score_is_not_zero(world: World) -> None:
    from iris_memory_core.domain.recall import ScoredCandidate, compute_final_score

    full, _full_missing = compute_final_score(
        {
            name: 0.5
            for name in (
                "relevance",
                "authority",
                "confidence",
                "importance",
                "accessibility",
                "activation",
                "recency",
                "task_urgency",
            )
        }
    )
    partial, missing = compute_final_score({"relevance": 0.5})
    assert missing and "relevance" not in missing
    # A single present component at 0.5 still scores 0.5: the missing ones do
    # not drag the weighted mean toward zero.
    assert partial == pytest.approx(0.5)
    assert full == pytest.approx(0.5)
    zero, zero_missing = compute_final_score({"relevance": 0.0})
    # A REAL zero scores 0.0 while the missing components stay missing —
    # missing and zero are never conflated.
    assert zero == 0.0 and zero_missing
    assert ScoredCandidate is not None


def test_conflict_groups_marked_and_penalized() -> None:
    from iris_memory_core.domain.recall import (
        ScoredCandidate,
        mark_conflicts_and_redundancy,
    )

    def candidate(cid: str, group: str | None, h: str) -> ScoredCandidate:
        return ScoredCandidate(
            candidate_id=cid,
            route="claims",
            resource_type="claim",
            resource_id=cid,
            resource_revision=1,
            subject_entity_id="s",
            category="fact",
            content_hash=h,
            occurred_us=1,
            token_estimate=1,
            scores={"relevance": 1.0},
            conflict_group=group,
            final_score=1.0,
        )

    marked = mark_conflicts_and_redundancy(
        [
            candidate("a", "subj:p1", "h1"),
            candidate("b", "subj:p1", "h2"),
            candidate("c", None, "h1"),
        ]
    )
    by_id = {c.candidate_id: c for c in marked}
    assert by_id["a"].conflict_state == "conflicts"
    assert by_id["b"].conflict_state == "conflicts"
    assert by_id["c"].conflict_state == "redundant"
    assert by_id["a"].final_score < 1.0
    assert by_id["c"].final_score < by_id["a"].final_score or by_id["c"].final_score < 1.0


def test_protected_budgets_survive_starvation(world: World) -> None:
    from iris_memory_core.application.tasks import TaskService

    tasks = TaskService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store))
    tasks.create(
        world.access,
        agent_id=world.agent,
        title="starvation-proof due task",
        due_at_us=world.clock.now_us() - 1,
        idempotency_key="task-starve",
    )
    focus = FocusService(
        world.store, world.store.clock, idempotency=IdempotencyManager(world.store)
    )
    focus.create(
        world.access,
        agent_id=world.agent,
        kind="goal",
        summary="starvation-proof focus",
        idempotency_key="focus-starve",
    )
    orchestrator = StructuredRecallOrchestrator(
        world.store,
        RecentContextService(world.store, world.store.clock),
        StateService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store)),
        focus,
        clock=world.store.clock,
        tasks=tasks,
        claims_enabled=True,
        monotonic=SystemMonotonicClock(),
    )
    request = StructuredRecallRequest(
        request_id="req-starve",
        agent_id=world.agent,
        space_id=world.space,
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="starvation",
        token_budget=0,
    )
    # The speaker's necessary identity claims are equally protected (§18.6).
    now = world.clock.now_us()
    with world.store.write() as tx:
        _bulk_claim(
            tx,
            claim_id="claim-speaker-identity",
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
            text="speaker is a quantum physicist",
            subject_entity_id=world.entity,
            category="identity",
        )
        speaker_identity_id = _last_bulk_id[0]
    claims_orchestrator = StructuredRecallOrchestrator(
        world.store,
        RecentContextService(world.store, world.store.clock),
        StateService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store)),
        focus,
        clock=world.store.clock,
        tasks=tasks,
        claims_enabled=True,
        monotonic=SystemMonotonicClock(),
    )
    speaker_request = StructuredRecallRequest(
        request_id="req-starve-speaker",
        agent_id=world.agent,
        space_id=world.space,
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="starvation speaker",
        token_budget=0,
        speaker_entity_id=world.entity,
    )
    speaker_result = claims_orchestrator.recall(world.access, speaker_request)
    assert speaker_identity_id in _returned_ids(speaker_result)
    result = orchestrator.recall(world.access, request)
    routes_kept = {candidate.route for candidate in result.candidates}
    assert "tasks" in routes_kept
    assert "focus" in routes_kept


# ---------------------------------------------------------------------------
# Degradation / partial envelopes (20x per scenario)


def _enqueue_fts_apply_for(world: World, resource_id: str) -> None:
    """One unsettled fts.apply job attributed to the world's agent."""
    from iris_memory_core.application.outbox import enqueue_with_pressure
    from iris_memory_core.domain.jobs import NewOutboxJob

    with world.store.write() as tx:
        enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id=world.tenant,
                job_kind="fts.apply",
                aggregate_type="claim",
                aggregate_id=resource_id,
                source_revision=1,
                payload={
                    "version": 1,
                    "job_kind": "fts.apply",
                    "resource_type": "claim",
                    "resource_id": resource_id,
                },
                dedupe_key=f"fts-apply:{world.tenant}:claim:{resource_id}:1",
                agent_id=world.agent,
                coalesce_key=f"fts:claim:{resource_id}",
                priority=5,
                available_at_us=1,
            ),
            None,
        )


def test_fts_behind_degrades_stably(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import iris_memory_core.indexing.fts as fts_module

    _deterministic_world(world)
    monkeypatch.setattr(fts_module, "fts_staleness_limit", lambda: 0)
    # "Behind" = the projection has unsettled fts.apply work for this agent:
    # one indexable change whose apply job has not completed yet.
    _enqueue_fts_apply_for(world, "lag-claim")
    for run in range(SCENARIO_RUNS):
        result = _recall(world, request_id=f"req-stale-{run}", topic="deterministic quantum")
        degraded = {d.route: d.reason_code for d in result.degraded_routes}
        assert degraded.get("fts") == "fts_generation_stale", run
        assert "fts" not in result.completed_routes
        assert result.partial is True
        assert "claims" in result.completed_routes


def test_route_timeout_degrades_stably(world: World) -> None:
    class _SlowRoute:
        name = "claims"

        def collect(
            self,
            tx: Transaction,
            request: StructuredRecallRequest,
            access: AccessContext,
            deadline_us: int,
            now_us: int,
        ) -> tuple[RecallCandidate, ...]:
            from iris_memory_core.application.recall import RouteDeadlineExceeded

            raise RouteDeadlineExceeded()

    orchestrator = StructuredRecallOrchestrator(
        world.store,
        RecentContextService(world.store, world.store.clock),
        StateService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store)),
        FocusService(world.store, world.store.clock),
        clock=world.store.clock,
        routes=(
            RecentContextRoute(
                RecentContextService(world.store, world.store.clock), SystemMonotonicClock()
            ),
            _SlowRoute(),
        ),
        monotonic=SystemMonotonicClock(),
    )
    request = StructuredRecallRequest(
        request_id="req-timeout",
        agent_id=world.agent,
        space_id=world.space,
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="timeout",
    )
    for run in range(SCENARIO_RUNS):
        result = orchestrator.recall(world.access, request)
        degraded = {d.route: d.reason_code for d in result.degraded_routes}
        assert degraded == {"claims": "route_deadline_exceeded"}, run
        assert result.partial is True
        assert "recent_context" in result.completed_routes


def test_minimum_watermark_unreachable_is_stable_error(world: World) -> None:
    for _run in range(SCENARIO_RUNS):
        with pytest.raises(NotReadyError) as captured:
            _recall(world, request_id=f"req-wm-{_run}", minimum_watermark=999_999_999)
        assert captured.value.code == "minimum_watermark_unavailable"


def test_disallowed_partial_is_stable_error(world: World) -> None:
    class _BrokenRoute:
        name = "claims"

        def collect(
            self,
            tx: Transaction,
            request: StructuredRecallRequest,
            access: AccessContext,
            deadline_us: int,
            now_us: int,
        ) -> tuple[RecallCandidate, ...]:
            raise RuntimeError("provider exploded")

    orchestrator = StructuredRecallOrchestrator(
        world.store,
        RecentContextService(world.store, world.store.clock),
        StateService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store)),
        FocusService(world.store, world.store.clock),
        clock=world.store.clock,
        claims_enabled=False,
        routes=(
            RecentContextRoute(
                RecentContextService(world.store, world.store.clock), SystemMonotonicClock()
            ),
            _BrokenRoute(),
        ),
        monotonic=SystemMonotonicClock(),
    )
    request = StructuredRecallRequest(
        request_id="req-nopartial",
        agent_id=world.agent,
        space_id=world.space,
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="nopartial",
        allow_partial=False,
    )
    for _run in range(SCENARIO_RUNS):
        with pytest.raises(NotReadyError):
            orchestrator.recall(world.access, request)


def test_all_routes_deadline_exceeded_is_deadline_error(world: World) -> None:
    from iris_memory_core.domain.errors import DeadlineExceededError

    class _Slow:
        name = "x"

        def collect(
            self,
            tx: Transaction,
            request: StructuredRecallRequest,
            access: AccessContext,
            deadline_us: int,
            now_us: int,
        ) -> tuple[RecallCandidate, ...]:
            check_deadline(SystemMonotonicClock(), 0)
            raise AssertionError("unreachable")

    orchestrator = StructuredRecallOrchestrator(
        world.store,
        RecentContextService(world.store, world.store.clock),
        StateService(world.store, world.store.clock, idempotency=IdempotencyManager(world.store)),
        FocusService(world.store, world.store.clock),
        clock=world.store.clock,
        routes=(_Slow(), _Slow()),
        monotonic=SystemMonotonicClock(),
    )
    request = StructuredRecallRequest(
        request_id="req-deadline",
        agent_id=world.agent,
        space_id=world.space,
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="deadline",
    )
    for _run in range(SCENARIO_RUNS):
        with pytest.raises(DeadlineExceededError) as captured:
            orchestrator.recall(world.access, request)
        assert captured.value.code == "deadline_exceeded"


# ---------------------------------------------------------------------------
# Actor resolution, persona, filters, privacy narrowing


def test_speaker_resolution_uses_registry_not_caller_claims(world: World) -> None:
    # The public request shape has NO entity_id field; the speaker comes from
    # the registry exclusively (§18.1).
    from iris_memory_core.application.recall import ExternalActorRef

    speaker = world.service.resolve_speaker(
        world.access, (ExternalActorRef(provider="qq", external_id="bob-1"),)
    )
    assert speaker == world.entity
    with pytest.raises(IdentityNotFoundError):
        world.service.resolve_speaker(
            world.access, (ExternalActorRef(provider="qq", external_id="ghost"),)
        )


def test_persona_metadata_is_top_level_and_never_a_candidate(world: World) -> None:
    result = _recall(world, topic="persona probe")
    assert result.persona_revision >= 1
    assert result.persona_content_hash
    assert all(c.resource_type != "persona_revision" for c in result.candidates)
    assert result.source_watermark >= 1


def test_requested_privacy_labels_narrow_only(world: World) -> None:
    now = world.clock.now_us()
    with world.store.write() as tx:
        gold_id = _bulk_claim(
            tx,
            claim_id="claim-gold",
            tenant_id=world.tenant,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
            status="active",
            privacy_labels=("custom:gold",),
            valid_from_us=None,
            valid_until_us=None,
            recorded_at_us=now,
            text="custom gold labeled claim",
            subject_entity_id=world.entity,
        )
    granting_access = access_for(
        world.tenant,
        agent_ids=frozenset({world.agent}),
        space_ids=frozenset({world.space}),
        consent_entities=frozenset({world.entity}),
        custom_labels=frozenset({"custom:gold"}),
    )

    def run(request_id: str, narrowing: frozenset[str] | None) -> set[str]:
        request = world.service.build_request(
            request_id=request_id,
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 5_000_000,
            topic="custom gold",
            token_budget=1_000_000,
            requested_privacy_labels=narrowing,
        )
        return _returned_ids(world.service.recall(granting_access, request, actors=world.actors))

    # Granted + unnamed: visible.
    assert gold_id in run("req-narrow-open", None)
    # Granted + naming the label: still visible (narrowing keeps it).
    assert gold_id in run("req-narrow-gold", frozenset({"custom:gold"}))
    # Granted + naming a DIFFERENT label: narrowed away.
    assert gold_id not in run("req-narrow-silver", frozenset({"custom:silver"}))
    # Narrowing can never WIDEN past evaluate_privacy: without the grant the
    # custom-labeled claim stays invisible even when the request names it.
    ungranted = world.service.build_request(
        request_id="req-narrow-ungranted",
        agent_id=world.agent,
        space_id=world.space,
        deadline_at_us=world.clock.now_us() + 5_000_000,
        topic="custom gold",
        token_budget=1_000_000,
        requested_privacy_labels=frozenset({"custom:gold"}),
    )
    result = world.service.recall(world.access, ungranted, actors=world.actors)
    assert gold_id not in _returned_ids(result)


def test_other_members_private_memory_never_enters_a_shared_space(world: World) -> None:
    now = world.clock.now_us()
    with world.store.write() as tx:
        _bulk_claim(
            tx,
            claim_id="claim-peer-private",
            tenant_id=world.tenant,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
            status="active",
            privacy_labels=(f"entity:{world.peer_entity}:private",),
            valid_from_us=None,
            valid_until_us=None,
            recorded_at_us=now,
            text="peer private secret",
            subject_entity_id=world.peer_entity,
        )
        private_id = _last_bulk_id[0]
    result = _recall(world, topic="peer private secret")
    assert private_id not in _returned_ids(result)


def test_deadline_is_converted_once_from_wall_clock(world: World) -> None:
    # A deadline already in the past clamps to "now" rather than raising;
    # a generous deadline leaves positive budget.
    request = world.service.build_request(
        request_id="req-past",
        agent_id=world.agent,
        space_id=world.space,
        deadline_at_us=world.clock.now_us() - 60_000_000,
        topic="past deadline",
    )
    assert request.deadline_monotonic_us > 0
    generous = world.service.build_request(
        request_id="req-future",
        agent_id=world.agent,
        space_id=world.space,
        deadline_at_us=world.clock.now_us() + 60_000_000,
        topic="future deadline",
    )
    assert generous.deadline_monotonic_us > request.deadline_monotonic_us


def test_unknown_purpose_rejected(world: World) -> None:
    with pytest.raises(InvalidRequestError):
        world.service.build_request(
            request_id="req-purpose",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 1_000_000,
            topic="purpose",
            purpose="chitchat",
        )


# ---------------------------------------------------------------------------
# Privacy leak scan (§31.3)


def test_trace_and_errors_never_leak_filtered_content(world: World) -> None:
    now = world.clock.now_us()
    secret = "TOPSECRET-CANARY-CONTENT"
    with world.store.write() as tx:
        _bulk_claim(
            tx,
            claim_id="claim-canary",
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
        canary_id = _last_bulk_id[0]
    result = _recall(world, topic="TOPSECRET", include_trace=True)
    assert canary_id not in _returned_ids(result)
    blob = repr(result.trace) + repr(result.degraded_routes) + repr(result.candidates)
    assert secret not in blob
    with world.store.read() as tx:
        rows = tx.raw().execute("SELECT * FROM recall_usage_reports").fetchall()
        if rows:
            assert secret not in repr(rows)
