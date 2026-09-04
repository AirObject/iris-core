"""Review round 3 regression tests (ADR-0016 §11): one adversarial
reproduction per finding from the independent review.

1. [P1] Graph traversal never evaluated the ENDPOINT ENTITIES' own privacy
   labels — a public edge exposed (and traversed through) a restricted
   entity.
2. [P1] BFS enqueued the target node BEFORE checking the canonical current
   revision, so a stale projection edge (correction pending its apply)
   still acted as an expansion springboard to the next hop.
3. [P1] verify_in_tx wrote pending_rebuild and then RAISED — the worker's
   transaction rolled back together with the verdict, leaving a corrupt
   generation marked ready. The verdict must persist through the real
   worker commit path.
4. [P1] rebuild settlement completed apply jobs of FUTURE payload versions
   this build cannot prove it covered.
5. [P2] Relationship profile subjects collected BOTH endpoints' every
   relationship claim; rebuild admitted only claims structurally pairing
   the two endpoints — incremental/fallback derive disagreed with the
   persisted generation (and read_profile fell back, leaking the third
   party under the pair subject).
6. [P2] verify only checked that stored sources were non-empty; it never
   revalidated them against canonical state nor detected missing canonical
   content (and the "canonical fallback comparison" test asserted nothing).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.domain.profile import ProfileSubjectKey, relationship_subject_id
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.phase8_helpers import TENANT, Phase8World


@pytest.fixture
def world(
    clocked_store: Store, mutable_clock: MutableClock, generous_gauge: BackpressureGauge
) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


def _sqlite(world: Phase8World, statement: str, params: tuple[Any, ...] = ()) -> None:
    connection = sqlite3.connect(world.store.runtime.database)
    try:
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()


def _restricted_entity(world: Phase8World, name: str) -> str:
    with world.store.write() as tx:
        entity = tx.identities.insert_entity(
            TENANT,
            EntityKind.PERSON,
            display_name=name,
            privacy_labels=["restricted"],
        )
    world.entities[name] = entity.id
    return entity.id


def _graph_candidates(
    world: Phase8World, speaker_entity_id: str, *, admin: bool = False
) -> tuple[Any, ...]:
    """Drive the REAL GraphRoute.collect against the trusted generation —
    direct route invocation, so the canonical relations route cannot mask
    the graph route's own verdict through cross-route dedupe."""
    from dataclasses import replace

    from iris_memory_core.application.ports import SystemMonotonicClock
    from iris_memory_core.application.recall import GraphRoute

    request = replace(
        world.service.build_request(
            request_id=f"r3-{speaker_entity_id[:8]}-{admin}-{world.clock.now_us()}",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 10_000_000,
            topic="friends",
            purpose="reply",
            token_budget=100_000,
        ),
        speaker_entity_id=speaker_entity_id,
    )
    route = GraphRoute(world.graph)
    monotonic = SystemMonotonicClock()
    with world.store.read() as tx:
        return route.collect(
            tx,
            request,
            world.admin_access if admin else world.access,
            monotonic.monotonic_us() + 10_000_000,
            world.clock.now_us(),
        )


def _enqueue_job(world: Phase8World, job_kind: str, dedupe: str) -> None:
    from iris_memory_core.application.outbox import enqueue_with_pressure
    from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, NewOutboxJob

    with world.store.write() as tx:
        enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id=TENANT,
                job_kind=job_kind,
                aggregate_type="projection",
                aggregate_id=job_kind.split(".")[0],
                source_revision=1,
                payload={"version": JOB_PAYLOAD_VERSION},
                dedupe_key=dedupe,
                priority=8,
            ),
            _gauge(),
        )


def _gauge() -> BackpressureGauge:
    """A gauge whose limits never trip (same policy as the conftest
    fixture, constructed inline for the non-factory worker paths)."""
    from iris_memory_core.application.backpressure import BackpressureConfig, FixedDiskProbe

    return BackpressureGauge(
        BackpressureConfig(
            soft_disk_free_bytes=10**12,
            hard_disk_free_bytes=10**11,
            retry_base_delay_us=1_000,
            retry_max_delay_us=10_000,
        ),
        probe=FixedDiskProbe(10**13),
    )


def _run_kind(world: Phase8World, kind: str, handler: Any) -> int:
    return _run_kinds(world, {kind: handler})


def _run_kinds(world: Phase8World, handlers: dict[str, Any]) -> int:
    from iris_memory_core.application.outbox import OutboxService
    from iris_memory_core.jobs.worker import OutboxWorker

    worker = OutboxWorker(
        OutboxService(world.store, world.clock, gauge=_gauge()),
        handlers,
        owner="r3-worker",
    )
    executed = 0
    for _ in range(20):
        outcome = worker.run_once()
        executed += outcome.get("completed", 0)
        if outcome.get("claimed", 0) == 0 and outcome.get("completed", 0) == 0:
            break
    return executed


def _drain_projection_pipeline(world: Phase8World) -> None:
    """Simulate the whole projection pipeline settling WITHOUT running the
    applies (a dead worker completed the rows): quiescent backlog, stale
    projection — exactly the drift verification must catch."""
    _sqlite(
        world,
        "UPDATE outbox_jobs SET status = 'completed', completed_us = 1 "
        "WHERE tenant_id = ? AND status IN ('pending', 'leased', 'retryable') "
        "AND job_kind IN ('claim.changed', 'relation.changed', "
        "'memory.invalidated', 'graph.apply', 'profile.apply')",
        (TENANT,),
    )


# ---------------------------------------------------------------------------
# Finding 1 [P1]: endpoint entity privacy labels


class TestEndpointEntityPrivacy:
    def test_public_edge_to_restricted_endpoint_is_invisible(self, world: Phase8World) -> None:
        bob = world.entity("PrivBob")
        world.bind_identity(bob, "r3-priv-bob")
        carol = _restricted_entity(world, "RestrictedCarol")
        relation = world.relate("priv-r1", bob, carol, relation_type="knows")
        world.rebuild()

        # The edge's own labels are public, but the endpoint entity is
        # restricted: the non-admin traversal must not see OR cross it.
        candidates = _graph_candidates(world, bob)
        assert not any(candidate.resource_id == relation.relation_id for candidate in candidates)
        # ...and the route still serves OTHER public work (not a blanket
        # failure): sanity-check via the admin run below.
        # Admin access evaluates the restricted label and DOES see the edge.
        admin_candidates = _graph_candidates(world, bob, admin=True)
        assert any(candidate.resource_id == relation.relation_id for candidate in admin_candidates)

    def test_restricted_intermediate_is_not_a_springboard(self, world: Phase8World) -> None:
        bob = world.entity("HopBob")
        world.bind_identity(bob, "r3-hop-bob")
        carol = _restricted_entity(world, "HopRestrictedCarol")
        dave = world.entity("HopDave")
        eve = world.entity("HopEve")
        to_restricted = world.relate("hop-r1", bob, carol, relation_type="knows")
        behind_restricted = world.relate("hop-r2", carol, dave, relation_type="knows")
        public = world.relate("hop-r3", bob, eve, relation_type="knows")
        world.rebuild()

        candidates = _graph_candidates(world, bob)
        served = {candidate.resource_id for candidate in candidates}
        # The directly connected public relation serves...
        assert public.relation_id in served
        # ...the restricted endpoint's own edge does not, and — the actual
        # springboard finding — Dave's relation behind Carol does not either:
        # Carol never enters the frontier, so hop-r2 is never even read.
        assert to_restricted.relation_id not in served
        assert behind_restricted.relation_id not in served
        # Admin traversal reaches through the restricted middle legitimately.
        admin_served = {c.resource_id for c in _graph_candidates(world, bob, admin=True)}
        assert {
            to_restricted.relation_id,
            behind_restricted.relation_id,
            public.relation_id,
        } <= admin_served


# ---------------------------------------------------------------------------
# Finding 2 [P1]: canonical currency BEFORE frontier expansion


class TestStaleEdgeExpansion:
    def _chain(self, world: Phase8World) -> tuple[Any, Any, str]:
        bob = world.entity("StaleBob")
        world.bind_identity(bob, "r3-stale-bob")
        carol = world.entity("StaleCarol")
        dave = world.entity("StaleDave")
        first = world.remember(
            "sc1",
            "Bob knows Carol",
            bob,
            category="relationship",
            value={"target_entity_id": carol},
        )
        second = world.remember(
            "sc2",
            "Carol knows Dave",
            carol,
            category="relationship",
            value={"target_entity_id": dave},
        )
        return first, second, bob

    def test_stale_edge_neither_serves_nor_expands(self, world: Phase8World) -> None:
        first, second, bob = self._chain(world)
        world.rebuild()
        # Sanity: before the drift, the second hop IS reachable via c1→c2.
        served = {c.resource_id for c in _graph_candidates(world, bob)}
        assert second.claim_id in served

        # Drift the first edge WITHOUT applying: a dispute keeps the claim
        # visible but bumps the current revision — the projection edge is
        # now stale and the apply backlog (1 unsettled job) is far below the
        # staleness limit, so the trusted generation still serves.
        world.claims.correct(
            world.access,
            claim_id=first.claim_id,
            expected_revision=first.revision,
            mode="dispute",
            evidence=[{"source_type": "observation", "source_id": world.observation()}],
            reason="r3 drift",
            idempotency_key="r3-stale-1",
        )
        served = {c.resource_id for c in _graph_candidates(world, bob)}
        # The stale edge's own resource never serves...
        assert first.claim_id not in served
        # ...and — the finding — it must not EXPAND either: Carol is not on
        # the frontier, so Carol→Dave is unreachable through the stale edge.
        assert second.claim_id not in served

        # After the pipeline lands (claim.changed schedules the apply, then
        # the apply runs), the corrected edge set serves again — the block
        # was the lag window, not a permanent loss.
        from iris_memory_core.jobs.handlers import claim_changed_handler, graph_apply_handler

        _run_kinds(
            world,
            {
                "claim.changed": claim_changed_handler(world.clock, gauge=_gauge()),
                "graph.apply": graph_apply_handler(world.graph),
            },
        )
        served = {c.resource_id for c in _graph_candidates(world, bob)}
        assert second.claim_id in served


# ---------------------------------------------------------------------------
# Finding 3 [P1]: verify verdict persists through the worker transaction


class TestVerifyVerdictPersistence:
    def test_graph_cleanup_job_persists_pending_rebuild(self, world: Phase8World) -> None:
        from iris_memory_core.jobs.handlers import graph_cleanup_handler

        bob = world.entity("PersistBob")
        carol = world.entity("PersistCarol")
        world.relate("pr-r1", bob, carol)
        world.rebuild()
        with world.store.read() as tx:
            pointer = tx.graph.pointer(TENANT)
            assert pointer is not None
        _sqlite(
            world,
            "UPDATE graph_generations SET content_checksum = 'r3deadbeef' WHERE id = ?",
            (pointer.generation_id,),
        )
        _enqueue_job(world, "graph.cleanup", f"r3-graph-cleanup-{world.clock.now_us()}")
        executed = _run_kind(world, "graph.cleanup", graph_cleanup_handler(world.graph))
        assert executed >= 1
        # The verdict survived the worker's commit (an escaping exception
        # would have rolled this back to 'ready').
        with world.store.read() as tx:
            assert tx.graph.projection_state() == "pending_rebuild"

    def test_profile_cleanup_job_persists_pending_rebuild(self, world: Phase8World) -> None:
        from iris_memory_core.jobs.handlers import profile_cleanup_handler

        bob = world.entity("PersistProfileBob")
        world.remember("pp1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        _sqlite(
            world,
            "UPDATE profile_generations SET content_checksum = 'r3deadbeef' "
            "WHERE id = (SELECT generation_id FROM profile_current WHERE tenant_id = ?)",
            (TENANT,),
        )
        _enqueue_job(world, "profile.cleanup", f"r3-profile-cleanup-{world.clock.now_us()}")
        executed = _run_kind(world, "profile.cleanup", profile_cleanup_handler(world.profile))
        assert executed >= 1
        with world.store.read() as tx:
            assert tx.profile.projection_state() == "pending_rebuild"


# ---------------------------------------------------------------------------
# Finding 4 [P1]: rebuild settlement is payload-version scoped


class TestRebuildSettlementPayloadVersions:
    def test_rebuild_settles_only_understood_payload_versions(self, world: Phase8World) -> None:
        from iris_memory_core.domain.graph import GRAPH_APPLY_PAYLOAD_VERSION
        from iris_memory_core.domain.profile import PROFILE_APPLY_PAYLOAD_VERSION

        bob = world.entity("SettleBob")
        carol = world.entity("SettleCarol")
        world.relate("st-r1", bob, carol)
        for version, tag in (
            (GRAPH_APPLY_PAYLOAD_VERSION, "v1"),
            (GRAPH_APPLY_PAYLOAD_VERSION + 1, "v2"),
        ):
            _sqlite(
                world,
                "INSERT INTO outbox_jobs (id, tenant_id, agent_id, job_kind, "
                "aggregate_type, aggregate_id, source_revision, payload, "
                "payload_version, dedupe_key, coalesce_key, priority, status, "
                "available_at_us, attempt_count, max_attempts, lease_generation, "
                "created_us) VALUES (?, ?, ?, 'graph.apply', 'claim', ?, 1, ?, ?, "
                "?, ?, 5, 'pending', 0, 0, 8, 0, 1)",
                (
                    f"r3-settle-{tag}",
                    TENANT,
                    world.agent,
                    f"settle-{tag}",
                    json.dumps({"version": version}),
                    version,
                    f"r3-settle-{tag}",
                    f"graph:claim:settle-{tag}",
                ),
            )
        world.graph.rebuild(TENANT)
        with world.store.read() as tx:
            jobs = {
                job.aggregate_id: job
                for job in tx.outbox.list_jobs(tenant_id=TENANT, job_kind="graph.apply")
                if job.aggregate_id in ("settle-v1", "settle-v2")
            }
        # The version this build understands settles (covered by the rebuild
        # snapshot); the future version stays queued for a build that can
        # prove it covered those semantics.
        assert jobs["settle-v1"].status == "completed"
        assert jobs["settle-v2"].status == "pending"

        # Profile settlement follows the same discipline.
        _sqlite(
            world,
            "INSERT INTO outbox_jobs (id, tenant_id, agent_id, job_kind, "
            "aggregate_type, aggregate_id, source_revision, payload, "
            "payload_version, dedupe_key, coalesce_key, priority, status, "
            "available_at_us, attempt_count, max_attempts, lease_generation, "
            "created_us) VALUES ('r3-settle-p2', ?, ?, 'profile.apply', 'claim', "
            "'settle-p2', 1, ?, ?, 'r3-settle-p2', 'profile:claim:settle-p2', "
            "5, 'pending', 0, 0, 8, 0, 1)",
            (
                TENANT,
                world.agent,
                json.dumps({"version": PROFILE_APPLY_PAYLOAD_VERSION + 1}),
                PROFILE_APPLY_PAYLOAD_VERSION + 1,
            ),
        )
        world.profile.rebuild(TENANT)
        with world.store.read() as tx:
            job = next(
                candidate
                for candidate in tx.outbox.list_jobs(tenant_id=TENANT, job_kind="profile.apply")
                if candidate.aggregate_id == "settle-p2"
            )
        assert job.status == "pending"


# ---------------------------------------------------------------------------
# Finding 5 [P2]: relationship subjects pair only their endpoints


class TestRelationshipPairIsolation:
    def test_relationship_subject_contains_only_its_pair(self, world: Phase8World) -> None:
        bob = world.entity("PairBob")
        carol = world.entity("PairCarol")
        dave = world.entity("PairDave")
        world.remember(
            "rbc",
            "Bob trusts Carol",
            bob,
            category="relationship",
            value={"target_entity_id": carol},
        )
        world.remember(
            "rbd",
            "Bob trusts Dave",
            bob,
            category="relationship",
            value={"target_entity_id": dave},
        )
        world.rebuild()
        pair = ProfileSubjectKey("relationship", relationship_subject_id(bob, carol))
        view = world.profile.read_profile(TENANT, pair, agent_id=world.agent)
        # The stored subject checksum matches the canonical derivation —
        # before the fix this fell back to canonical (which disagreed with
        # the persisted generation by including BOTH claims).
        assert view.source == "projection"
        fields = {f.field for f in view.fields}
        assert fields == {f"p_rbc@{bob}"}, "the Bob|Carol subject must not carry Bob→Dave"
        with world.store.read() as tx:
            canonical = world.profile.derive_subject_fields(tx, TENANT, pair)
        assert canonical == view.fields
        # The other pair keeps its own claim.
        other = ProfileSubjectKey("relationship", relationship_subject_id(bob, dave))
        other_view = world.profile.read_profile(TENANT, other, agent_id=world.agent)
        assert other_view.source == "projection"
        assert {f.field for f in other_view.fields} == {f"p_rbd@{bob}"}


# ---------------------------------------------------------------------------
# Finding 6 [P2]: verify enforces canonical equality once quiescent


class TestVerifyCanonicalEquality:
    def test_verify_tolerates_pipeline_lag(self, world: Phase8World) -> None:
        bob = world.entity("LagBob")
        world.remember("lag1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        # A change still in the producer stage (claim.changed pending, the
        # graph/profile applies not even scheduled): the projection is
        # ALLOWED to differ — verification must not flip pending_rebuild.
        world.remember("lag2", "Bob likes tea", bob, category="preference")
        with world.store.write() as tx:
            assert world.profile.verify_in_tx(tx, TENANT) is True
            assert world.graph.verify_in_tx(tx, TENANT) is True
        with world.store.read() as tx:
            assert tx.profile.projection_state() == "ready"
            assert tx.graph.projection_state() == "ready"

    def test_graph_verify_flags_dead_apply_drift(self, world: Phase8World) -> None:
        bob = world.entity("DeadBob")
        carol = world.entity("DeadCarol")
        world.relate("dead-r1", bob, carol)
        world.rebuild()
        # A new canonical relation whose apply never runs (the pipeline rows
        # settle without executing): quiescent backlog, missing edge.
        world.relate("dead-r2", carol, bob, relation_type="likes")
        _drain_projection_pipeline(world)
        with world.store.write() as tx:
            assert world.graph.verify_in_tx(tx, TENANT) is False
        with world.store.read() as tx:
            assert tx.graph.projection_state() == "pending_rebuild"

    def test_profile_verify_flags_dead_apply_drift(self, world: Phase8World) -> None:
        bob = world.entity("DeadProfileBob")
        world.remember("dead-p1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        world.remember("dead-p2", "Bob likes tea", bob, category="preference")
        _drain_projection_pipeline(world)
        with world.store.write() as tx:
            assert world.profile.verify_in_tx(tx, TENANT) is False
        with world.store.read() as tx:
            assert tx.profile.projection_state() == "pending_rebuild"

    def test_profile_verify_flags_invalid_source_after_drain(self, world: Phase8World) -> None:
        bob = world.entity("DriftSourceBob")
        result = world.remember("ds1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        # Dispute the stored source claim and let the pipeline settle
        # without the apply: the persisted field still references revision 1
        # — sources must be revalidated, not merely non-empty.
        world.claims.correct(
            world.access,
            claim_id=result.claim_id,
            expected_revision=result.revision,
            mode="dispute",
            evidence=[{"source_type": "observation", "source_id": world.observation()}],
            reason="r3 drift source",
            idempotency_key="r3-drift-source-1",
        )
        _drain_projection_pipeline(world)
        with world.store.write() as tx:
            assert world.profile.verify_in_tx(tx, TENANT) is False
        with world.store.read() as tx:
            assert tx.profile.projection_state() == "pending_rebuild"
