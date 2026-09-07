"""Regressions for the remaining Phase 5/6/8 review findings."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import ExternalActorRef, GraphRoute, StructuredRecallResult
from iris_memory_core.domain.errors import ConflictError, InvalidRequestError
from iris_memory_core.domain.graph import GraphDegradedError
from iris_memory_core.domain.jobs import NewOutboxJob
from iris_memory_core.domain.profile import (
    ProfileDegradedError,
    ProfileSubjectKey,
    relationship_subject_id,
)
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.indexing.fts import FtsDegradedError, FtsProjectionService
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for
from tests.integration.recall.graph_profile_helpers import Phase8World
from tests.migration_support import migrate_through


@pytest.fixture
def world(clocked_store: Store, mutable_clock: MutableClock) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


def request(world: Phase8World, request_id: str = "review") -> Any:
    return world.service.build_request(
        request_id=request_id,
        agent_id=world.agent,
        space_id=world.space,
        deadline_at_us=world.clock.now_us() + 30_000_000,
        token_budget=100_000,
        topic="review",
    )


def empty_result(store: Store, request: Any, watermark: int) -> StructuredRecallResult:
    with store.read() as tx:
        agent = tx.get_agent(request.agent_id)
        assert agent.persona_current_revision_id is not None
        persona = tx.get_persona_revision(agent.persona_current_revision_id)
    return StructuredRecallResult(
        request_id=request.request_id,
        persona_revision=persona.revision,
        persona_content_hash=persona.content_hash,
        source_watermark=watermark,
        completed_routes=("claims",),
        degraded_routes=(),
        partial=False,
        candidates=(),
        dropped_by_rehydrate=0,
    )


@pytest.mark.parametrize("mode", ["forget", "retract"])
def test_diamond_evidence_cascade_revisits_surviving_fan_in(world: Phase8World, mode: str) -> None:
    root = world.remember("root", "root", world.entities["Speaker"])
    left = world.remember(
        "left",
        "left",
        world.entities["Speaker"],
        evidence=[{"source_type": "claim", "source_id": root.claim_id}],
    )
    right = world.remember(
        "right",
        "right",
        world.entities["Speaker"],
        evidence=[{"source_type": "claim", "source_id": root.claim_id}],
    )
    join = world.remember(
        "join",
        "join",
        world.entities["Speaker"],
        evidence=[{"source_type": "claim", "source_id": item.claim_id} for item in (left, right)],
    )
    tail = world.remember(
        "tail",
        "tail",
        world.entities["Speaker"],
        evidence=[{"source_type": "claim", "source_id": join.claim_id}],
    )
    if mode == "retract":
        world.claims.correct(
            world.access,
            root.claim_id,
            mode="retract",
            expected_revision=1,
            reason="review",
            idempotency_key="retract-root",
        )
    else:
        ForgetService(world.store, world.clock, idempotency=world.idem).forget(
            world.access,
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE, resource_type="claim", resource_id=root.claim_id
            ),
            reason="review",
            idempotency_key="forget-root",
        )
    with world.store.read() as tx:
        for item in (left, right, join, tail):
            claim = tx.claims.get(item.claim_id)
            assert claim.status == "retracted"
            assert claim.evidence_count == tx.claims.valid_evidence_count(claim.id) == 0


@pytest.mark.parametrize("change", ["admin", "consent", "custom_label", "app", "actor"])
def test_recall_replay_binds_actor_and_access(world: Phase8World, change: str) -> None:
    world.claims.remember(
        world.admin_access,
        agent_id=world.agent,
        space_id=world.space,
        subject_entity_id=world.entities["Speaker"],
        predicate="secret",
        value="secret",
        canonical_text="restricted replay canary",
        privacy_labels=["restricted"],
        evidence=[{"source_type": "observation", "source_id": world.observation()}],
        idempotency_key="secret",
    )
    access = replace(world.access, admin=True)
    actors = (ExternalActorRef(provider="qq", realm="default", external_id="default-speaker"),)
    req = request(world)
    first = world.service.recall(access, req, actors=actors)
    assert any(c.text == "restricted replay canary" for c in first.candidates)
    assert world.service.recall(access, req, actors=actors) == first
    if change == "admin":
        access = replace(access, admin=False)
    elif change == "consent":
        access = replace(access, consent_subject_entity_ids=frozenset({world.entities["Speaker"]}))
    elif change == "custom_label":
        access = replace(access, granted_custom_labels=frozenset({"custom:review"}))
    elif change == "app":
        access = replace(access, app_instance_id="different-app")
    else:
        world.bind_identity(world.entity("Speaker B"), "speaker-b")
        actors = (ExternalActorRef(provider="qq", realm="default", external_id="speaker-b"),)
    with pytest.raises(InvalidRequestError):
        world.service.recall(access, req, actors=actors)


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_recall_returns_persisted_winner(
    world: Phase8World,
    monkeypatch: pytest.MonkeyPatch,
    conflicting: bool,
) -> None:
    barrier = threading.Barrier(2)
    count = 0
    lock = threading.Lock()

    def collect(_access: Any, req: Any) -> StructuredRecallResult:
        nonlocal count
        with lock:
            count += 1
            watermark = count * 101
        barrier.wait(timeout=5)
        return empty_result(world.store, req, watermark)

    monkeypatch.setattr(world.service._orchestrator, "recall", collect)
    req = request(world)
    other = replace(req, topic="different") if conflicting else req
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(world.service.recall, world.access, item) for item in (req, other)]
        results, errors = [], []
        for future in futures:
            try:
                results.append(future.result(timeout=10))
            except InvalidRequestError as error:
                errors.append(error)
    assert len(errors) == int(conflicting)
    with world.store.read() as tx:
        stored = tx.usage.get_request("t1", req.request_id)
        assert stored is not None
        assert all(result.source_watermark == stored["source_watermark"] for result in results)


def test_forget_between_rehydrate_and_publication_cannot_resurrect(
    world: Phase8World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = world.remember("erase", "erasure race canary", world.entities["Speaker"])
    original = world.service._orchestrator.recall

    def collect(access: Any, req: Any) -> StructuredRecallResult:
        result = original(access, req)
        assert any(c.resource_id == claim.claim_id for c in result.candidates)
        ForgetService(world.store, world.clock, idempotency=world.idem).forget(
            access,
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE, resource_type="claim", resource_id=claim.claim_id
            ),
            reason="race",
            idempotency_key="erase-after-rehydrate",
        )
        return result

    monkeypatch.setattr(world.service._orchestrator, "recall", collect)
    req = request(world)
    with pytest.raises(ConflictError):
        world.service.recall(world.access, req)
    with world.store.read() as tx:
        assert tx.usage.get_request("t1", req.request_id) is None
    monkeypatch.setattr(world.service._orchestrator, "recall", original)
    result = world.service.recall(world.access, req)
    assert all(c.resource_id != claim.claim_id for c in result.candidates)
    assert world.service.recall(world.access, req) == result


def test_request_ids_are_tenant_local(world: Phase8World, monkeypatch: pytest.MonkeyPatch) -> None:
    with world.store.write() as tx:
        tx.insert_tenant("t2", status="active")
        agent = tx.insert_agent("t2", "Agent", actor="test").id
        space = tx.insert_space("t2", "direct").id
    access = access_for("t2", agent_ids=frozenset({agent}), space_ids=frozenset({space}))
    monkeypatch.setattr(
        world.service._orchestrator,
        "recall",
        lambda _access, req: empty_result(world.store, req, 1),
    )
    req = request(world, "ordinary-client-id")
    world.service.recall(world.access, req)
    world.service.recall(access, replace(req, agent_id=agent, space_id=space))
    with world.store.read() as tx:
        assert tx.usage.get_request("t1", req.request_id) is not None
        assert tx.usage.get_request("t2", req.request_id) is not None
        assert not tx.raw().execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "kind", ["claim.changed", "episode.changed", "note.changed", "memory.invalidated"]
)
def test_fts_counts_unconverted_producers(
    world: Phase8World,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    import iris_memory_core.indexing.fts as fts_module

    projection = FtsProjectionService(world.store, world.clock)
    projection.rebuild("t1")
    monkeypatch.setattr(fts_module, "fts_staleness_limit", lambda: 0)
    with world.store.write() as tx:
        tx.outbox.enqueue(
            NewOutboxJob(
                tenant_id="t1",
                agent_id=None if kind == "memory.invalidated" else world.agent,
                job_kind=kind,
                aggregate_type="claim",
                aggregate_id="pending",
                source_revision=1,
                payload={"version": 1},
                dedupe_key="pending-producer",
                available_at_us=world.clock.now_us(),
            )
        )
    with world.store.read() as tx, pytest.raises(FtsDegradedError):
        assert tx.outbox.unsettled_job_count("t1", world.agent, "fts.apply") == 0
        projection.search_in_tx(tx, tenant_id="t1", agent_id=world.agent, match_expression="test")
    with world.store.write() as tx:
        tx.raw().execute(
            "UPDATE outbox_jobs SET status = 'completed' WHERE dedupe_key = 'pending-producer'"
        )
    with world.store.read() as tx:
        assert (
            projection.search_in_tx(
                tx, tenant_id="t1", agent_id=world.agent, match_expression="test"
            )
            == []
        )


@pytest.mark.parametrize("kind", ["graph", "profile"])
def test_other_tenant_rebuild_does_not_clear_corruption(world: Phase8World, kind: str) -> None:
    world.remember("profile", "Speaker likes tea", world.entities["Speaker"], category="preference")
    world.rebuild()
    projection = getattr(world, kind)
    with world.store.write() as tx:
        tx.insert_tenant("t2", status="active")
        tx.insert_agent("t2", "Agent B", actor="test")
        tx.raw().execute(
            f"UPDATE {kind}_generations SET content_checksum = 'corrupt' WHERE tenant_id = 't1'"
        )
    with world.store.write() as tx:
        assert projection.verify_in_tx(tx, "t1") is False
    projection.rebuild("t2")
    error = GraphDegradedError if kind == "graph" else ProfileDegradedError
    with world.store.read() as tx, pytest.raises(error):
        projection.trusted_generation_in_tx(tx, tenant_id="t1", agent_id=world.agent)
    projection.rebuild("t1")
    with world.store.read() as tx:
        projection.trusted_generation_in_tx(tx, tenant_id="t1", agent_id=world.agent)


@pytest.mark.parametrize("kind", ["graph", "profile"])
def test_coalesce_preserves_future_payload_fence(world: Phase8World, kind: str) -> None:
    job = NewOutboxJob(
        tenant_id="t1",
        agent_id=world.agent,
        job_kind=f"{kind}.apply",
        aggregate_type="claim",
        aggregate_id="claim",
        source_revision=1,
        payload={"version": 1},
        payload_version=1,
        dedupe_key="v1",
        coalesce_key="review",
        available_at_us=world.clock.now_us(),
    )
    with world.store.write() as tx:
        first, _ = tx.outbox.enqueue(job)
        merged, _ = tx.outbox.enqueue(
            replace(
                job, source_revision=2, payload={"version": 2}, payload_version=99, dedupe_key="v2"
            )
        )
        assert merged.id == first.id and merged.payload_version == 99
        assert merged.payload == {"version": 2}
    getattr(world, kind).rebuild("t1")
    with world.store.read() as tx:
        assert tx.outbox.get(first.id).status == "pending"


def test_relationship_target_change_removes_old_subject(world: Phase8World) -> None:
    bob, carol, dave = (world.entity(name) for name in ("Bob", "Carol", "Dave"))
    claim = world.remember(
        "pair", "Bob trusts Carol", bob, category="relationship", value={"target_entity_id": carol}
    )
    world.profile.rebuild("t1")
    world.clock.advance(10)
    world.claims.correct(
        world.access,
        claim.claim_id,
        expected_revision=1,
        mode="supersede",
        value={"target_entity_id": dave},
        canonical_text="Bob trusts Dave",
        evidence=[{"source_type": "observation", "source_id": world.observation()}],
        idempotency_key="change-pair",
    )
    with world.store.write() as tx:
        world.profile.apply_change_in_tx(
            tx, tenant_id="t1", resource_type="claim", resource_id=claim.claim_id
        )
        pointer = tx.profile.pointer("t1")
        assert pointer is not None
        subjects = {
            row.subject_id
            for row in tx.profile.subjects_for_generation("t1", pointer.generation_id)
        }
        assert relationship_subject_id(bob, carol) not in subjects
        assert relationship_subject_id(bob, dave) in subjects
        tx.raw().execute("UPDATE outbox_jobs SET status = 'completed' WHERE tenant_id = 't1'")
        assert world.profile.verify_in_tx(tx, "t1")


@pytest.mark.parametrize("rebuilt", [False, True])
def test_profile_and_actor_resolve_terminal_redirect(world: Phase8World, rebuilt: bool) -> None:
    terminal = world.entity("Terminal")
    world.remember("old", "old profile", world.entities["Speaker"], category="identity")
    world.remember("new", "terminal profile", terminal, category="identity")
    if rebuilt:
        world.rebuild()
    world.identities.redirect_entity(
        world.admin_access, world.entities["Speaker"], terminal, expected_revision=2, reason="merge"
    )
    view = world.profile.read_profile(
        "t1", ProfileSubjectKey("entity", world.entities["Speaker"]), agent_id=world.agent
    )
    assert view.subject.subject_id == terminal
    assert view.fields and all("old profile" not in field.summary_text for field in view.fields)
    actors = (ExternalActorRef(provider="qq", realm="default", external_id="default-speaker"),)
    assert world.service.resolve_speaker(world.access, actors) == terminal


@pytest.mark.parametrize("stage", ["pending", "apply", "rebuild"])
def test_redirected_intermediate_never_expands_graph(world: Phase8World, stage: str) -> None:
    intermediate, tail, terminal = (world.entity(name) for name in ("Mid", "Tail", "Terminal"))
    world.relate("first-edge", world.entities["Speaker"], intermediate)
    world.relate("second-edge", intermediate, tail)
    world.rebuild()
    route = GraphRoute(world.graph, monotonic=SystemMonotonicClock())
    req = replace(request(world), speaker_entity_id=world.entities["Speaker"])
    with world.store.read() as tx:
        assert (
            len(
                route.collect(
                    tx, req, world.access, req.deadline_monotonic_us, world.clock.now_us()
                )
            )
            == 2
        )
    world.identities.redirect_entity(
        world.admin_access, intermediate, terminal, expected_revision=1, reason="merge"
    )
    if stage == "apply":
        with world.store.write() as tx:
            world.graph.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="entity", resource_id=intermediate
            )
    elif stage == "rebuild":
        world.graph.rebuild("t1")
    with world.store.read() as tx:
        assert (
            route.collect(tx, req, world.access, req.deadline_monotonic_us, world.clock.now_us())
            == ()
        )


def test_terminal_profile_privacy_is_checked_after_redirect(world: Phase8World) -> None:
    from iris_memory_core.domain.errors import AccessDeniedError
    from iris_memory_core.domain.scope import Scope

    terminal = world.entity("Restricted terminal")
    world.remember("terminal", "terminal profile", terminal, category="identity")
    with world.store.write() as tx:
        tx.raw().execute(
            "UPDATE entities SET privacy_labels = '[\"restricted\"]' WHERE id = ?", (terminal,)
        )
    world.identities.redirect_entity(
        world.admin_access, world.entities["Speaker"], terminal, expected_revision=2, reason="merge"
    )
    subject = ProfileSubjectKey("entity", world.entities["Speaker"])
    scope = Scope(tenant_id="t1", agent_id=world.agent, space_id=world.space)
    with pytest.raises(AccessDeniedError):
        world.profile.read_profile(
            "t1", subject, agent_id=world.agent, access=world.access, request_scope=scope
        )
    assert world.profile.read_profile(
        "t1", subject, agent_id=world.agent, access=world.admin_access, request_scope=scope
    ).fields


def test_recall_identity_migration_preserves_rows_and_composite_fk(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite3"
    migrate_through(database, 13)
    with sqlite3.connect(database) as db:
        # Minimal relational fixtures are copied from the actual pre-migration schema.
        db.execute(
            "INSERT INTO tenants (id, status, created_us, created_at) "
            "VALUES ('t1', 'active', 1, 'test')"
        )
        db.execute(
            "INSERT INTO agents (id, tenant_id, display_name, status, created_us, created_at) "
            "VALUES ('a1', 't1', 'A', 'active', 1, 'test')"
        )
        db.execute(
            "INSERT INTO recall_requests VALUES "
            "('same', 't1', 'a1', 0, 1, 0, 1, 3, 1, 0, '[]', 0, 'fingerprint', '[]', '{}', 1)"
        )
        db.execute(
            "INSERT INTO recall_usage_reports VALUES "
            "('usage', 't1', 'same', 'a1', 'app', 'cycle', 0, '[]', '[]', 1, 1)"
        )
        requests = db.execute("SELECT * FROM recall_requests").fetchall()
        usage = db.execute("SELECT * FROM recall_usage_reports").fetchall()
    applied = MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
    assert [item.version for item in applied] == [14, 15, 16, 17, 18, 19, 20, 21, 22]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM recall_requests").fetchall() == requests
        assert db.execute("SELECT * FROM recall_usage_reports").fetchall() == usage
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        keys = db.execute("PRAGMA foreign_key_list(recall_usage_reports)").fetchall()
        assert {(row[3], row[4]) for row in keys if row[2] == "recall_requests"} == {
            ("tenant_id", "tenant_id"),
            ("request_id", "id"),
        }
