"""Evidence-driven Phase 10 pipeline and replay/fencing integration evidence."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.reflection import ReflectionPipeline
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    InvalidRequestError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.jobs import NewOutboxJob
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.jobs.worker import OutboxWorker, phase10_handlers
from iris_memory_core.providers.cognitive import DeterministicCognitiveProvider, ProviderGovernance
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store


def _world(store: Store) -> tuple[str, str, str, AccessContext]:
    tenant = "phase10-tenant"
    with store.write() as tx:
        tx.insert_tenant(tenant, status="active")
    bootstrap = AccessContext(tenant, "bootstrap", admin=True)
    provisioning = ProvisioningService(store)
    agent = provisioning.create_agent(bootstrap, "Phase 10").id
    access = AccessContext(tenant, "host", agent_ids=frozenset({agent}), admin=True)
    space = provisioning.create_space(access, "direct", agent_id=agent, reason="test").id
    access = AccessContext(
        tenant,
        "host",
        agent_ids=frozenset({agent}),
        allowed_space_ids=frozenset({space}),
        admin=True,
    )
    return tenant, agent, space, access


def _observation(
    store: Store,
    access: AccessContext,
    agent: str,
    space: str,
    sequence: int,
    *,
    privacy_labels: list[str] | None = None,
) -> str:
    now = store.clock.now_us()
    result = ObservationService(store).observe_batch(
        access,
        [
            {
                "agent_id": agent,
                "space_id": space,
                "role": "user",
                "kind": "message",
                "idempotency_key": f"observation-{sequence}",
                "occurred_us": now + sequence,
                "committed_us": now + sequence,
                "content": f"I prefer tea number {sequence}",
                "privacy_labels": privacy_labels or [],
            }
        ],
    )
    return result.accepted_observation_ids[0]


def _consolidation_job(
    store: Store,
    tenant: str,
    agent: str,
    space: str,
    *,
    suffix: str = "",
    source_revision: int | None = None,
) -> NewOutboxJob:
    with store.read() as tx:
        watermark = tx.watermark(tenant, agent)
    assert watermark is not None
    fixed_revision = watermark.current_seq if source_revision is None else source_revision
    now = store.clock.now_us()
    return NewOutboxJob(
        tenant_id=tenant,
        agent_id=agent,
        job_kind="episode.consolidation",
        aggregate_type="observation_window",
        aggregate_id=f"window-request{suffix}",
        source_revision=fixed_revision,
        payload={
            "version": 2,
            "job_kind": "episode.consolidation",
            "scope": {"space_id": space},
            "topic_key": "preferences",
            "window_start_us": now - 1,
            "window_end_us": now + 1_000_000,
        },
        dedupe_key=f"consolidation:{agent}:{fixed_revision}{suffix}",
        available_at_us=now,
    )


def test_pipeline_commits_evidence_bound_claim_and_is_replay_stable(clocked_store: Store) -> None:
    tenant, agent, space, access = _world(clocked_store)
    observation_id = _observation(clocked_store, access, agent, space, 1)
    candidate: dict[str, Any] = {
        "type": "claim",
        "payload": {
            "predicate": "preference.drink",
            "value": "tea",
            "canonical_text": "prefers tea",
            "category": "preference",
            "confidence": 0.7,
            "importance": 0.5,
            "source_authority": "extracted",
        },
        "evidence": [
            {
                "observation_id": observation_id,
                "observation_revision": 1,
                "start": 2,
                "end": 13,
            }
        ],
        "scope": {"tenant_id": tenant, "agent_id": agent, "space_id": space},
        "privacy_labels": [],
    }
    provider = DeterministicCognitiveProvider(
        [candidate], summary={"title": "Preferences", "summary": "User prefers tea."}
    )
    pipeline = ReflectionPipeline(
        clocked_store,
        clocked_store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    outbox = OutboxService(clocked_store, clocked_store.clock)
    initial_job = _consolidation_job(clocked_store, tenant, agent, space)
    outbox.enqueue(initial_job)
    worker = OutboxWorker(outbox, phase10_handlers(pipeline=pipeline), owner="phase10-worker")
    outcomes = [worker.run_once() for _ in range(4)]
    assert sum(item["completed"] for item in outcomes) >= 4
    with clocked_store.read() as tx:
        windows = (
            tx.raw().execute("SELECT id,status,episode_id FROM consolidation_windows").fetchall()
        )
        runs = (
            tx.raw()
            .execute("SELECT id,run_fingerprint,status,candidate_count FROM reflection_records")
            .fetchall()
        )
        candidates = (
            tx.raw()
            .execute(
                "SELECT fingerprint,decision,canonical_resource_type,canonical_resource_id "
                "FROM cognitive_candidates"
            )
            .fetchall()
        )
        evidence = (
            tx.raw()
            .execute(
                "SELECT observation_id,observation_revision,span_start,span_end "
                "FROM reflection_evidence"
            )
            .fetchall()
        )
    assert len(windows) == 1 and windows[0][1] == "sealed" and windows[0][2]
    assert len(runs) == 1 and runs[0][2:] == ("committed", 1)
    assert len(candidates) == 1 and candidates[0][1:3] == ("accepted", "claim")
    assert [tuple(row) for row in evidence] == [(observation_id, 1, 2, 13)]

    # Three identical fixed-input enqueue attempts collapse to the same
    # logical window/run/candidate even though the Outbox sees new ids.
    for replay in range(3):
        outbox.enqueue(
            _consolidation_job(
                clocked_store,
                tenant,
                agent,
                space,
                suffix=f":replay-{replay}",
                source_revision=initial_job.source_revision,
            )
        )
        for _ in range(4):
            worker.run_once()
    with clocked_store.read() as tx:
        assert tx.raw().execute("SELECT COUNT(*) FROM consolidation_windows").fetchone()[0] == 1
        assert tx.raw().execute("SELECT COUNT(*) FROM reflection_records").fetchone()[0] == 1
        assert tx.raw().execute("SELECT COUNT(*) FROM cognitive_candidates").fetchone()[0] == 1
        assert tx.raw().execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1


def test_provider_call_occurs_without_sqlite_write_transaction(clocked_store: Store) -> None:
    tenant, agent, space, access = _world(clocked_store)
    _observation(clocked_store, access, agent, space, 1)
    observed: list[bool] = []

    class CheckingProvider(DeterministicCognitiveProvider):
        def summarize(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            observed.append(clocked_store.runtime.writer_lock.locked())
            return {"title": "safe", "summary": "safe"}

    provider = CheckingProvider()
    pipeline = ReflectionPipeline(
        clocked_store,
        clocked_store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    job, _ = OutboxService(clocked_store, clocked_store.clock).enqueue(
        _consolidation_job(clocked_store, tenant, agent, space)
    )
    pipeline.prepare_window(job)
    assert observed == [False]


def test_conflicting_candidate_facts_are_deterministically_disputed(
    clocked_store: Store,
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    observation_id = _observation(clocked_store, access, agent, space, 1)
    candidates = []
    for value in ("coffee", "tea"):
        candidates.append(
            {
                "type": "claim",
                "payload": {
                    "predicate": "preference.drink",
                    "value": value,
                    "canonical_text": f"prefers {value}",
                    "category": "preference",
                    "source_authority": "extracted",
                },
                "evidence": [
                    {
                        "observation_id": observation_id,
                        "observation_revision": 1,
                        "start": 0,
                        "end": 8,
                    }
                ],
                "scope": {"tenant_id": tenant, "agent_id": agent, "space_id": space},
                "privacy_labels": [],
            }
        )
    provider = DeterministicCognitiveProvider(candidates)
    pipeline = ReflectionPipeline(
        clocked_store,
        clocked_store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    outbox = OutboxService(clocked_store, clocked_store.clock)
    outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    worker = OutboxWorker(outbox, phase10_handlers(pipeline=pipeline), owner="conflict-worker")
    for _ in range(6):
        worker.run_once()
    with clocked_store.read() as tx:
        statuses = [
            str(row[0])
            for row in tx.raw().execute("SELECT status FROM claims ORDER BY id").fetchall()
        ]
        candidate_decisions = (
            tx.raw()
            .execute("SELECT COUNT(*) FROM cognitive_candidates WHERE decision='accepted'")
            .fetchone()[0]
        )
    assert statuses == ["disputed", "disputed"]
    assert candidate_decisions == 2


def test_persona_evaluation_uses_phase9_policy_and_proposal_boundary(
    clocked_store: Store,
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    observation_id = _observation(clocked_store, access, agent, space, 1)
    persona_admin = AccessContext(
        tenant,
        "persona-admin",
        agent_ids=frozenset({agent}),
        capabilities=frozenset({"persona.manage.v1"}),
        admin=True,
    )
    PersonaService(
        clocked_store, clocked_store.clock, IdempotencyManager(clocked_store)
    ).replace_policy(
        persona_admin,
        agent,
        expected_revision=1,
        config={
            "mode": "manual",
            "allowed_fields": ["traits.style"],
            "max_single_delta": 1.0,
            "max_cumulative_delta": 1.0,
            "min_evidence": 1,
            "min_distinct_sources": 1,
            "min_confidence": 0.5,
        },
        reason="enable reflection proposals",
        idempotency_key="persona-policy-phase10",
    )
    candidate = {
        "type": "persona_proposal",
        "payload": {"patch": {"traits": {"style": "warm"}}, "confidence": 0.8},
        "evidence": [
            {
                "observation_id": observation_id,
                "observation_revision": 1,
                "start": 0,
                "end": 8,
            }
        ],
        "scope": {"tenant_id": tenant, "agent_id": agent, "space_id": space},
        "privacy_labels": [],
    }
    provider = DeterministicCognitiveProvider([candidate])
    pipeline = ReflectionPipeline(
        clocked_store,
        clocked_store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    outbox = OutboxService(clocked_store, clocked_store.clock)
    outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    worker = OutboxWorker(outbox, phase10_handlers(pipeline=pipeline), owner="persona-worker")
    # Policy replacement and Episode publication enqueue their own projection
    # work, so drain a bounded number of claims instead of assuming a fixed
    # queue position for persona.evaluation.
    for _ in range(32):
        worker.run_once()
    with clocked_store.read() as tx:
        current = tx.personas.current(agent)
        proposals = (
            tx.raw().execute("SELECT status,target_fields_json FROM persona_proposals").fetchall()
        )
        candidate_row = (
            tx.raw()
            .execute("SELECT decision,canonical_resource_type FROM cognitive_candidates")
            .fetchone()
        )
    assert current.revision == 1
    assert len(proposals) == 1
    assert tuple(proposals[0]) == ("proposed", '["traits.style"]')
    assert tuple(candidate_row) == ("accepted", "persona_proposal")


@pytest.mark.parametrize(
    ("illegal_class", "reason"),
    [
        ("unknown_entity", "unknown_entity"),
        ("scope", "scope_violation"),
        ("privacy", "privacy_denied"),
    ],
)
def test_two_hundred_database_bound_illegal_candidates_commit_zero(
    clocked_store: Store, illegal_class: str, reason: str
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    observation_id = _observation(
        clocked_store,
        access,
        agent,
        space,
        1,
        privacy_labels=[f"space:{space}"] if illegal_class == "privacy" else [],
    )
    candidates: list[dict[str, Any]] = []
    for seed in range(200):
        candidate: dict[str, Any] = {
            "type": "claim",
            "payload": {
                "predicate": f"illegal.{seed}",
                "value": seed,
                "confidence": 0.5,
                "importance": 0.5,
                "source_authority": "extracted",
            },
            "evidence": [
                {
                    "observation_id": observation_id,
                    "observation_revision": 1,
                    "start": 0,
                    "end": 8,
                }
            ],
            "scope": {"tenant_id": tenant, "agent_id": agent, "space_id": space},
            "privacy_labels": [],
        }
        if illegal_class == "unknown_entity":
            candidate["type"] = "relation"
            candidate["payload"] = {
                "source_entity_id": f"missing-source-{seed}",
                "target_entity_id": f"missing-target-{seed}",
                "relation_type": "related_to",
            }
        elif illegal_class == "scope":
            candidate["scope"] = {
                "tenant_id": tenant,
                "agent_id": f"foreign-agent-{seed}",
                "space_id": space,
            }
        candidates.append(candidate)
    provider = DeterministicCognitiveProvider(candidates)
    pipeline = ReflectionPipeline(
        clocked_store,
        clocked_store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    outbox = OutboxService(clocked_store, clocked_store.clock)
    outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    worker = OutboxWorker(outbox, phase10_handlers(pipeline=pipeline), owner="illegal-worker")
    for _ in range(6):
        worker.run_once()
    with clocked_store.read() as tx:
        canonical_relations = tx.raw().execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        canonical_claims = tx.raw().execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        rejected = (
            tx.raw()
            .execute(
                "SELECT COUNT(*) FROM cognitive_candidates WHERE decision='rejected' "
                "AND reject_reason=?",
                (reason,),
            )
            .fetchone()[0]
        )
    assert rejected == 200
    assert canonical_relations == 0
    assert canonical_claims == 0


def test_invalid_and_over_limit_provider_outputs_are_persisted_twenty_times(
    clocked_store: Store,
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    observation_id = _observation(clocked_store, access, agent, space, 1)

    class InvalidRootProvider(DeterministicCognitiveProvider):
        def extract(self, *args: object, **kwargs: object) -> Any:
            del args, kwargs
            return "{not-json"

    base = {
        "type": "claim",
        "payload": {
            "predicate": "over.limit",
            "value": True,
            "source_authority": "extracted",
        },
        "evidence": [
            {
                "observation_id": observation_id,
                "observation_revision": 1,
                "start": 0,
                "end": 8,
            }
        ],
        "scope": {"tenant_id": tenant, "agent_id": agent, "space_id": space},
        "privacy_labels": [],
    }
    summary = DeterministicCognitiveProvider()
    pipeline = ReflectionPipeline(
        clocked_store,
        clocked_store.clock,
        governance=ProviderGovernance(),
        extraction=summary,
        summarization=summary,
    )
    outbox = OutboxService(clocked_store, clocked_store.clock)
    stored, _ = outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    claimed = outbox.claim("seed", kinds=frozenset({"episode.consolidation"})).jobs[0]
    assert claimed.id == stored.id
    assert outbox.execute(claimed, pipeline.episode_consolidation_work, owner="seed") == "completed"
    with clocked_store.read() as tx:
        window_id = str(tx.raw().execute("SELECT id FROM consolidation_windows").fetchone()[0])
    reflection_job = NewOutboxJob(
        tenant_id=tenant,
        agent_id=agent,
        job_kind="reflection.generate",
        aggregate_type="consolidation_window",
        aggregate_id=window_id,
        source_revision=stored.source_revision,
        payload={"version": 2, "job_kind": "reflection.generate", "window_id": window_id},
        dedupe_key="invalid-output-probe",
        available_at_us=clocked_store.clock.now_us(),
    )
    job, _ = outbox.enqueue(reflection_job)
    providers = (
        InvalidRootProvider(),
        DeterministicCognitiveProvider([base for _ in range(257)]),
    )
    for provider in providers:
        invalid_pipeline = ReflectionPipeline(
            clocked_store,
            clocked_store.clock,
            governance=ProviderGovernance(),
            extraction=provider,
            summarization=summary,
        )
        for _ in range(20):
            with pytest.raises(InvalidRequestError, match=r"invalid schema|candidate limit"):
                invalid_pipeline.prepare_reflection(job)
    with clocked_store.read() as tx:
        outcomes = (
            tx.raw()
            .execute("SELECT COUNT(*) FROM provider_outcomes WHERE outcome='invalid_output'")
            .fetchone()[0]
        )
        candidates = tx.raw().execute("SELECT COUNT(*) FROM cognitive_candidates").fetchone()[0]
    assert outcomes == 40
    assert candidates == 0


def test_dead_letter_replay_one_hundred_times_keeps_one_logical_episode(
    clocked_store: Store,
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    _observation(clocked_store, access, agent, space, 1)
    provider = DeterministicCognitiveProvider()
    pipeline = ReflectionPipeline(
        clocked_store,
        clocked_store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    outbox = OutboxService(clocked_store, clocked_store.clock)
    original, _ = outbox.enqueue(
        replace(
            _consolidation_job(clocked_store, tenant, agent, space),
            max_attempts=1,
        )
    )
    leased = outbox.claim("dead-worker", kinds=frozenset({"episode.consolidation"})).jobs[0]

    def fail(_job: Any) -> Any:
        raise NotFoundError("deterministic dead letter")

    assert outbox.execute(leased, fail, owner="dead-worker") == "dead"
    worker = OutboxWorker(
        outbox,
        {"episode.consolidation": pipeline.episode_consolidation_work},
        owner="replay-worker",
        concurrency=1,
    )
    replay_ids: set[str] = set()
    for iteration in range(100):
        replay = outbox.replay_dead_letter(
            access, original.id, reason=f"quantitative replay {iteration}"
        )
        replay_ids.add(replay.id)
        outcome = worker.run_once()
        assert outcome["completed"] == 1
    with clocked_store.read() as tx:
        episodes = tx.raw().execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        windows = tx.raw().execute("SELECT COUNT(*) FROM consolidation_windows").fetchone()[0]
        self_evidence = (
            tx.raw()
            .execute(
                "SELECT COUNT(*) FROM reflection_evidence WHERE observation_id LIKE 'candidate:%' "
                "OR observation_id LIKE 'reflection:%'"
            )
            .fetchone()[0]
        )
    assert len(replay_ids) == 100
    assert episodes == 1
    assert windows == 1
    assert self_evidence == 0


@pytest.mark.parametrize("mutation", ["correct", "forget", "source_revision"])
def test_stale_source_mutations_fence_fifty_prepared_commits(
    clocked_store: Store, mutation: str
) -> None:
    tenant, agent, space, access = _world(clocked_store)
    observation_id = _observation(clocked_store, access, agent, space, 1)
    provider = DeterministicCognitiveProvider()
    pipeline = ReflectionPipeline(
        clocked_store,
        clocked_store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    outbox = OutboxService(clocked_store, clocked_store.clock)
    consolidation, _ = outbox.enqueue(_consolidation_job(clocked_store, tenant, agent, space))
    leased = outbox.claim("consolidator", kinds=frozenset({"episode.consolidation"})).jobs[0]
    assert (
        outbox.execute(leased, pipeline.episode_consolidation_work, owner="consolidator")
        == "completed"
    )
    reflection_job = outbox.claim("reflector", kinds=frozenset({"reflection.generate"})).jobs[0]
    prepared = pipeline.prepare_reflection(reflection_job)
    if mutation == "forget":
        ForgetService(
            clocked_store,
            clocked_store.clock,
            idempotency=IdempotencyManager(clocked_store),
        ).forget(
            access,
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE,
                resource_type="observation",
                resource_id=observation_id,
            ),
            reason="quantitative source revocation",
            idempotency_key="phase10-source-forget",
        )
    else:
        with clocked_store.write() as tx:
            if mutation == "correct":
                tx.raw().execute(
                    "UPDATE observations SET content=?,record_fingerprint=? WHERE id=?",
                    ("corrected source content", "b" * 64, observation_id),
                )
            else:
                tx.raw().execute(
                    "UPDATE observations SET revision=revision+1 WHERE id=?",
                    (observation_id,),
                )
    stale_successes = 0
    for _ in range(50):
        try:
            with clocked_store.write() as tx:
                pipeline.commit_reflection(tx, prepared)
            stale_successes += 1
        except RevisionMismatchError:
            pass
    with clocked_store.read() as tx:
        reflections = tx.raw().execute("SELECT COUNT(*) FROM reflection_records").fetchone()[0]
        candidates = tx.raw().execute("SELECT COUNT(*) FROM cognitive_candidates").fetchone()[0]
    assert consolidation.source_revision == prepared.job.source_revision
    assert stale_successes == 0
    assert reflections == 0
    assert candidates == 0
