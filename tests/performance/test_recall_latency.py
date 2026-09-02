"""Phase 6 performance gates: structured Recall p95 <= 50 ms, FTS Recall
p95 <= 100 ms (§30, ADR-0014).

Measurement method (recorded in the verification report):
- hardware: the CI host running this test (see the report section);
- dataset: 60 claims (mixed categories/labels), 20 observations in the hot
  window, 8 state records, 6 focus items, 5 relations, 1 FTS generation of
  60 documents (claim/episode/note mix is claim-heavy here);
- text length: 40..120 characters per claim, one-topic queries;
- concurrency: sequential calls; routes execute bounded-parallel internally
  (max 4) exactly as in production;
- candidate/token caps: defaults (20 per structured route, 2000 token
  budget default is raised to 100_000 so trimming never truncates);
- index state: HOT — the FTS generation is built before measurement and the
  first (uncached) call is discarded as warm-up;
- metric: wall time of RecallService.recall() from call to return, including
  the fresh rehydrate transaction and the usage persistence write.
"""

from __future__ import annotations

import time

from iris_memory_core.application.observation import ObservationService
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.test_phase6_recall import World, build_world

STRUCTURED_P95_BUDGET_MS = 50.0
FTS_P95_BUDGET_MS = 100.0
SAMPLES = 40
CLAIM_COUNT = 60
OBSERVATION_COUNT = 20


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _seed(world: World) -> None:
    observations = ObservationService(world.store)
    now = world.clock.now_us()
    for i in range(OBSERVATION_COUNT):
        observations.observe_batch(
            world.access,
            [
                {
                    "agent_id": world.agent,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"perf-obs-{i}",
                    "occurred_us": now + i * 1_000,
                    "committed_us": now + i * 1_000,
                    "content": f"performance turn {i} quantum physics discussion",
                    "space_id": world.space,
                    "session_id": world.session,
                }
            ],
        )
    for i in range(CLAIM_COUNT):
        world.claims.remember(
            world.access,
            agent_id=world.agent,
            space_id=world.space,
            subject_entity_id=world.entity,
            predicate=f"perf_{i}",
            value={"i": i},
            canonical_text=(
                f"performance claim {i} about quantum physics and gravitational "
                f"waves with index {i}"
            ),
            category="identity" if i % 10 == 0 else "fact",
            confidence=0.4 + (i % 6) * 0.1,
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
                                "idempotency_key": f"perf-ev-{i}",
                                "occurred_us": now,
                                "committed_us": now,
                                "content": f"evidence {i}",
                                "space_id": world.space,
                            }
                        ],
                    ).accepted_observation_ids[0],
                }
            ],
            idempotency_key=f"perf-claim-{i}",
        )
    world.fts.rebuild("t1")


def _measure(world: World, topic: str, samples: int) -> list[float]:
    durations: list[float] = []
    for sample in range(samples):
        request = world.service.build_request(
            # Request ids are per-topic: the same id under a different body
            # is now a rejected replay conflict, not a silent re-run.
            request_id=f"perf-req-{topic.replace(' ', '-')}-{sample}",
            agent_id=world.agent,
            space_id=world.space,
            session_id=world.session,
            deadline_at_us=world.clock.now_us() + 60_000_000,  # 60 ms budget
            topic=topic,
            token_budget=100_000,
        )
        started = time.perf_counter()
        world.service.recall(world.access, request, actors=world.actors)
        durations.append((time.perf_counter() - started) * 1000.0)
    return durations


def test_structured_recall_p95_under_50ms(
    clocked_store: Store, mutable_clock: MutableClock
) -> None:
    mutable_clock.set(1_700_000_000_000_000)
    world = build_world(clocked_store, mutable_clock)
    _seed(world)
    # Structured-only: the FTS route runs but the topic does not match the
    # index content beyond noise; the structured routes dominate.
    _measure(world, "turn discussion", samples=3)  # warm-up
    durations = _measure(world, "turn discussion", samples=SAMPLES)
    p95 = _percentile(durations, 0.95)
    assert p95 <= STRUCTURED_P95_BUDGET_MS, durations


def test_fts_recall_p95_under_100ms(clocked_store: Store, mutable_clock: MutableClock) -> None:
    mutable_clock.set(1_700_000_000_000_000)
    world = build_world(clocked_store, mutable_clock)
    _seed(world)
    _measure(world, "quantum physics gravitational", samples=3)  # warm-up
    durations = _measure(world, "quantum physics gravitational", samples=SAMPLES)
    p95 = _percentile(durations, 0.95)
    assert p95 <= FTS_P95_BUDGET_MS, durations
