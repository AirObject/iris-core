"""Phase 8 performance baseline: graph+profile hybrid recall latency.

Measures the full RecallService.recall pipeline (control transaction,
parallel routes including graph and profile, fresh rehydrate, usage
persistence) on a mixed dataset with a moderate graph, hot projections and
a real per-request deadline. Baseline gate: p95 within the 250 ms hybrid
recall budget (§30, same envelope as Phase 7).
"""

from __future__ import annotations

import time

from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import RecallService, StructuredRecallOrchestrator
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.identity import EntityKind, ExternalIdentityKey
from iris_memory_core.indexing.graph import GraphProjectionService
from iris_memory_core.indexing.profile import ProfileProjectionService
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for

WARMUP = 3
SAMPLES = 40
DEADLINE_MS = 250
#: A 60-entity ring graph + 60 claims: enough structure for the traversal
#: to do real per-edge work without dominating the shared test budget.
GRAPH_ENTITIES = 60


def _build_world(
    clocked_store: Store, mutable_clock: MutableClock
) -> tuple[RecallService, GraphProjectionService, ProfileProjectionService, dict[str, str]]:
    with clocked_store.write() as tx:
        tx.insert_tenant("t1", status="active")
        agent = tx.insert_agent("t1", "A", actor="t").id
        space = tx.insert_space("t1", "chat_group", actor="t").id
        speaker = tx.identities.insert_entity("t1", EntityKind.PERSON, display_name="S").id
        tx.identities.insert_external_identity(
            ExternalIdentityKey(
                tenant_id="t1", provider="qq", realm="default", external_id="perf-1"
            ),
            entity_id=speaker,
        )
    access = access_for("t1", agent_ids=frozenset({agent}), space_ids=frozenset({space}))
    admin_access = access_for(
        "t1", agent_ids=frozenset({agent}), space_ids=frozenset({space}), admin=True
    )
    from iris_memory_core.application.identity import IdentityService

    identities = IdentityService(clocked_store)
    with clocked_store.read() as tx:
        external = tx.identities.find_external_identity(
            ExternalIdentityKey(
                tenant_id="t1", provider="qq", realm="default", external_id="perf-1"
            )
        )
    assert external is not None
    binding = identities.propose_binding(
        admin_access, external.id, speaker, proof_digest="perf", reason="perf"
    )
    identities.confirm_binding(
        admin_access, binding.id, expected_revision=binding.revision, reason="perf"
    )
    idem = IdempotencyManager(clocked_store)
    observations = ObservationService(clocked_store)
    claims = ClaimService(clocked_store, mutable_clock, idempotency=idem)
    graph = GraphProjectionService(clocked_store, mutable_clock)
    profile = ProfileProjectionService(clocked_store, mutable_clock)

    # Claims + relation ring for the graph/profile projections.
    entities = [speaker]
    for index in range(GRAPH_ENTITIES):
        with clocked_store.write() as tx:
            entity = tx.identities.insert_entity(
                "t1", EntityKind.PERSON, display_name=f"P{index}"
            ).id
        entities.append(entity)
        observation = observations.observe_batch(
            access,
            [
                {
                    "agent_id": agent,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"perf-obs-{index}",
                    "occurred_us": mutable_clock.now_us(),
                    "committed_us": mutable_clock.now_us(),
                    "content": f"evidence {index}",
                    "space_id": space,
                }
            ],
        ).accepted_observation_ids[0]
        claims.remember(
            access,
            agent_id=agent,
            space_id=space,
            subject_entity_id=entity,
            predicate=f"p_fact_{index}",
            value={"k": index},
            canonical_text=f"person {index} works on project {(index * 7) % 13}",
            category="fact",
            importance=0.8,
            evidence=[{"source_type": "observation", "source_id": observation}],
            idempotency_key=f"perf-claim-{index}",
        )
        claims.remember(
            access,
            agent_id=agent,
            space_id=space,
            subject_entity_id=entities[index],
            predicate="p_knows",
            value={"target_entity_id": entity},
            canonical_text=f"person {index} knows person {index + 1}",
            category="relationship",
            evidence=[{"source_type": "observation", "source_id": observation}],
            idempotency_key=f"perf-rel-claim-{index}",
        )
    from iris_memory_core.application.episodes import RelationService

    relations = RelationService(clocked_store, mutable_clock, idempotency=idem)
    for index in range(GRAPH_ENTITIES):
        observation = observations.observe_batch(
            access,
            [
                {
                    "agent_id": agent,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"perf-obs-r{index}",
                    "occurred_us": mutable_clock.now_us(),
                    "committed_us": mutable_clock.now_us(),
                    "content": f"relation evidence {index}",
                    "space_id": space,
                }
            ],
        ).accepted_observation_ids[0]
        relations.create(
            access,
            agent_id=agent,
            source_entity_id=entities[index],
            relation_type="knows",
            target_entity_id=entities[index + 1],
            space_id=space,
            evidence=[{"source_type": "observation", "source_id": observation}],
            idempotency_key=f"perf-relation-{index}",
        )
    graph.rebuild("t1")
    profile.rebuild("t1")
    orchestrator = StructuredRecallOrchestrator(
        clocked_store,
        RecentContextService(clocked_store, mutable_clock),
        StateService(clocked_store, mutable_clock, idempotency=idem),
        FocusService(clocked_store, mutable_clock),
        clock=mutable_clock,
        claims_enabled=True,
        relations_enabled=True,
        graph=graph,
        profile=profile,
        monotonic=SystemMonotonicClock(),
    )
    service = RecallService(orchestrator, clocked_store, mutable_clock)
    return service, graph, profile, {"agent": agent, "space": space, "speaker": speaker}


def test_graph_profile_hybrid_recall_latency(
    clocked_store: Store, mutable_clock: MutableClock
) -> None:
    service, _graph, _profile, ctx = _build_world(clocked_store, mutable_clock)
    access = access_for(
        "t1", agent_ids=frozenset({ctx["agent"]}), space_ids=frozenset({ctx["space"]})
    )
    from iris_memory_core.application.recall import ExternalActorRef

    actors = (ExternalActorRef(provider="qq", realm="default", external_id="perf-1"),)
    durations: list[float] = []
    for round_index in range(WARMUP + SAMPLES):
        request = service.build_request(
            request_id=f"perf-{round_index}",
            agent_id=ctx["agent"],
            space_id=ctx["space"],
            deadline_at_us=mutable_clock.now_us() + DEADLINE_MS * 1_000,
            topic="project knows",
            purpose="reply",
            token_budget=100_000,
        )
        started = time.perf_counter()
        result = service.recall(access, request, actors=actors)
        durations.append((time.perf_counter() - started) * 1000.0)
        assert "graph" in result.completed_routes, result.degraded_routes
        assert "profile" in result.completed_routes, result.degraded_routes
    measured = durations[WARMUP:]
    measured.sort()
    p50 = measured[len(measured) // 2]
    p95 = measured[int(len(measured) * 0.95) - 1]
    print(
        f"\ngraph+profile hybrid recall: p50={p50:.1f}ms p95={p95:.1f}ms "
        f"max={measured[-1]:.1f}ms samples={len(measured)} budget={DEADLINE_MS}ms"
    )
    assert p95 < DEADLINE_MS
