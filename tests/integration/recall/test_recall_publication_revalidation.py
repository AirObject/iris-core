"""Recall replay/publication must not restore candidates invalidated since collection."""

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.persona import PersonaService
from iris_memory_core.domain.errors import ConflictError
from tests.integration.recall.test_fts_recall_recall import World, _recall, _returned_ids
from tests.integration.recall.test_recall_deadline_scope_and_rebuild import (
    _observe_for_claim,
    _remember,
)


@pytest.mark.parametrize("window", ["replay", "publication", "publication-winner"])
@pytest.mark.parametrize("change", ["supersede", "retract", "expiry", "persona"])
def test_invalidated_response_is_rejected_without_rewriting_original_usage_identity(
    world: World, monkeypatch: pytest.MonkeyPatch, window: str, change: str
) -> None:
    created = _remember(
        world,
        "response-validity",
        "Original odyssey fact",
        valid_until_us=world.clock.now_us() + 1_000_000 if change == "expiry" else None,
    )
    original_recall = world.orchestrator.recall
    collected: list[Any] = []

    def mutate() -> None:
        if change == "expiry":
            world.clock.advance(1_000_001)
        elif change == "persona":
            service = PersonaService(world.store, world.clock)
            admin = replace(
                world.access,
                admin=True,
                capabilities=frozenset({"persona.read.v1", "persona.manage.v1"}),
            )
            view = service.current(admin, world.agent)
            service.publish_revision(
                admin,
                world.agent,
                expected_revision=view.revision.revision,
                core={"identity": "Changed identity"},
                traits={},
                narrative={},
                reason="revalidation test",
            )
        else:
            world.claims.correct(
                world.access,
                created.claim_id,
                expected_revision=1,
                mode=change,
                value="corrected",
                canonical_text="Corrected odyssey fact",
                evidence=[
                    {
                        "source_type": "observation",
                        "source_id": _observe_for_claim(world, "correction"),
                    }
                ]
                if change == "supersede"
                else [],
                idempotency_key="response-validity-correction",
            )

    if window == "replay":
        first = _recall(world, request_id="same-request", topic="odyssey")
        assert created.claim_id in _returned_ids(first)
        collected.append(first)
        mutate()
    else:

        def before_publication(access: Any, request: Any) -> Any:
            result = original_recall(access, request)
            assert created.claim_id in _returned_ids(result)
            collected.append(result)
            # A competing first response wins before this invocation enters
            # its publication transaction. The second replay path must check it.
            if window == "publication-winner":
                monkeypatch.setattr(world.orchestrator, "recall", original_recall)
                winner = _recall(world, request_id="same-request", topic="odyssey")
                collected[0] = winner
            mutate()
            return result

        monkeypatch.setattr(world.orchestrator, "recall", before_publication)

    with pytest.raises(ConflictError, match="no longer valid") as failure:
        _recall(world, request_id="same-request", topic="odyssey")
    assert created.claim_id not in str(failure.value)
    with world.store.read() as tx:
        saved = tx.usage.get_request(world.tenant, "same-request")
        if window == "publication":
            assert saved is None
        else:
            assert saved is not None
            from iris_memory_core.application.recall import _result_from_json

            restored = _result_from_json(saved["response_json"])
            assert restored == collected[0]
            assert restored.candidates == collected[0].candidates
    # A failed retry must not suppress new independent Recall work.
    monkeypatch.setattr(world.orchestrator, "recall", original_recall)
    fresh = _recall(world, request_id="new-request", topic="odyssey")
    if change in {"expiry", "retract"}:
        assert created.claim_id not in _returned_ids(fresh)
    if change == "supersede":
        revisions = {
            c.resource_revision for c in fresh.candidates if c.resource_id == created.claim_id
        }
        assert revisions == {2}


def test_authorized_historical_replay_keeps_its_original_evaluation_instant(world: World) -> None:
    created = _remember(world, "historical-replay", "Historical odyssey fact")
    as_of_us = world.clock.now_us()
    first = _recall(world, request_id="historical", topic="odyssey", as_of_us=as_of_us)
    assert created.claim_id in _returned_ids(first)
    world.clock.advance(1_000_000)
    world.claims.correct(
        world.access,
        created.claim_id,
        expected_revision=1,
        mode="retract",
        idempotency_key="historical-retract",
    )
    replay = _recall(world, request_id="historical", topic="odyssey", as_of_us=as_of_us)
    assert replay == first
