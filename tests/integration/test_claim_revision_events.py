"""Committed corrections publish bounded old-revision invalidation without a Worker."""

from typing import Any

import pytest

from iris_memory_core.application.episodes import RelationService
from tests.integration.test_phase5_claims import _Ctx
from tests.integration.test_phase5_claims import ctx as claim_ctx

ctx = claim_ctx


def events(ctx: _Ctx, **scope: Any) -> Any:
    with ctx.store.read() as tx:
        return tx.reflection.events_after(
            tenant_id=scope.get("tenant_id", "t1"),
            after_cursor=0,
            agent_ids=scope.get("agent_ids", [ctx.agent]),
            space_ids=scope.get("space_ids", [ctx.space]),
            space_group_ids=[],
            limit=100,
        )


@pytest.mark.parametrize("mode", ["supersede", "dispute", "retract"])
def test_correction_atomically_invalidates_only_the_predecessor_and_replays_once(
    ctx: _Ctx, mode: str
) -> None:
    created = ctx.remember("event", space_id=ctx.space)
    evidence = [{"source_type": "observation", "source_id": ctx.observe("new-evidence")}]
    arguments: dict[str, Any] = {
        "expected_revision": 1,
        "mode": mode,
        "value": "new value",
        "canonical_text": "new text",
        "evidence": evidence if mode != "retract" else [],
        "idempotency_key": "correction-event",
    }
    corrected = ctx.claims.correct(ctx.access, created.claim_id, **arguments)
    assert corrected.revision == 2
    assert ctx.claims.correct(ctx.access, created.claim_id, **arguments).replayed
    [event] = events(ctx)
    assert event.event_type == "revision.invalidated.v1"
    assert event.resource_refs == (
        {"resource_type": "claim", "resource_id": created.claim_id, "revision": 1},
    )
    assert event.id.startswith("claim-revision:")
    assert event.space_id == ctx.space
    with ctx.store.read() as tx:
        assert tx.claims.get(created.claim_id).current_revision == 2
        watermark = tx.watermark("t1", ctx.agent)
        assert watermark is not None
        assert event.source_watermark == watermark.current_seq
        assert not tx.is_tombstoned("t1", "claim", created.claim_id)
    assert events(ctx, space_ids=[ctx.other_space]) == ()
    assert events(ctx, agent_ids=[]) == ()
    assert events(ctx, tenant_id="t2", agent_ids=[ctx.other_agent]) == ()


def test_notification_failure_rolls_back_the_correction_and_allows_original_key_retry(
    ctx: _Ctx,
) -> None:
    created = ctx.remember("rollback", space_id=ctx.space)
    evidence = [{"source_type": "observation", "source_id": ctx.observe("rollback-evidence")}]
    arguments: dict[str, Any] = {
        "expected_revision": 1,
        "value": "corrected",
        "evidence": evidence,
        "idempotency_key": "retry-original-key",
    }
    with ctx.store.write() as tx:
        tx.raw().execute(
            "CREATE TRIGGER fail_correction_event BEFORE INSERT ON service_events "
            "WHEN NEW.id LIKE 'claim-revision:%' "
            "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"
        )
    with pytest.raises(Exception, match="injected event failure"):
        ctx.claims.correct(ctx.access, created.claim_id, **arguments)
    assert events(ctx) == ()
    with ctx.store.read() as tx:
        assert tx.claims.get(created.claim_id).current_revision == 1
    with ctx.store.write() as tx:
        tx.raw().execute("DROP TRIGGER fail_correction_event")
    assert ctx.claims.correct(ctx.access, created.claim_id, **arguments).revision == 2
    assert len(events(ctx)) == 1


@pytest.mark.parametrize("inject_failure", [False, True])
def test_retraction_publishes_transitive_claim_and_relation_events_in_one_transaction(
    ctx: _Ctx, phase5_relations: RelationService, inject_failure: bool
) -> None:
    source = ctx.remember("cascade-source", predicate="source")
    chain = [source.claim_id]
    for index in range(2):
        derived = ctx.claims.remember(
            ctx.wide_access,
            agent_id=ctx.agent,
            space_id=ctx.other_space,
            subject_entity_id=ctx.entity,
            predicate=f"derived-{index}",
            value=index,
            canonical_text=f"Derived {index}",
            evidence=[{"source_type": "claim", "source_id": chain[-1]}],
            idempotency_key=f"cascade-derived-{index}",
        )
        chain.append(derived.claim_id)
    relation = phase5_relations.create(
        ctx.wide_access,
        agent_id=ctx.agent,
        space_id=ctx.other_space,
        source_entity_id=ctx.entity,
        relation_type="knows",
        target_entity_id=ctx.other_entity,
        evidence=[{"source_type": "claim", "source_id": chain[-1]}],
        idempotency_key="cascade-relation",
    )
    arguments: dict[str, Any] = {
        "expected_revision": 1,
        "mode": "retract",
        "idempotency_key": "cascade-retract",
    }
    if inject_failure:
        with ctx.store.write() as tx:
            tx.raw().execute(
                "CREATE TRIGGER fail_cascade_event BEFORE INSERT ON service_events "
                "WHEN NEW.id LIKE 'relation-revision:%' "
                "BEGIN SELECT RAISE(ABORT, 'injected cascade failure'); END"
            )
        with pytest.raises(Exception, match="injected cascade failure"):
            ctx.claims.correct(ctx.access, source.claim_id, **arguments)
        assert events(ctx, space_ids=[ctx.space, ctx.other_space]) == ()
        with ctx.store.write() as tx:
            for claim_id in chain:
                current = tx.claims.get(claim_id)
                assert current.current_revision == 1 and current.status == "active"
                assert current.evidence_count == 1
            assert tx.relations.get(relation.relation_id).current_revision == 1
            tx.raw().execute("DROP TRIGGER fail_cascade_event")
    assert ctx.claims.correct(ctx.access, source.claim_id, **arguments).revision == 2
    assert ctx.claims.correct(ctx.access, source.claim_id, **arguments).replayed
    all_events = events(ctx, space_ids=[ctx.space, ctx.other_space])
    expected = {("claim", claim_id) for claim_id in chain}
    expected.add(("relation", relation.relation_id))
    assert len(all_events) == 4
    assert {
        (event.resource_refs[0]["resource_type"], event.resource_refs[0]["resource_id"])
        for event in all_events
    } == expected
    assert all(event.resource_refs[0]["revision"] == 1 for event in all_events)
    # The global source event is visible in either space; derived events retain
    # their own scope instead of inheriting the correction caller's scope.
    assert len(events(ctx)) == 1
    assert len(events(ctx, space_ids=[ctx.other_space])) == 4
    with ctx.store.read() as tx:
        for kind, resource_id in expected:
            affected = (
                tx.claims.get(resource_id) if kind == "claim" else tx.relations.get(resource_id)
            )
            assert affected.current_revision == 2 and affected.status == "retracted"
            assert not tx.is_tombstoned("t1", kind, resource_id)
