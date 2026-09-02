"""Phase 5 Episode/Relation integration tests: scope stitching rules,
evidence requirements and lifecycle CAS."""

from __future__ import annotations

from typing import Any

import pytest

from iris_memory_core.application.episodes import EpisodeService, RelationService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.domain.errors import (
    DomainError,
    InvalidRequestError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for


@pytest.fixture
def memory_ctx(
    clocked_store: Store,
    mutable_clock: MutableClock,
    phase5_claims: ClaimService,
    phase5_episodes: EpisodeService,
    phase5_relations: RelationService,
) -> dict[str, object]:
    with clocked_store.write() as tx:
        tx.insert_tenant("t1", status="active")
        agent = tx.insert_agent("t1", "A1", actor="test").id
        space_a = tx.insert_space("t1", "chat_group").id
        space_b = tx.insert_space("t1", "direct").id
        tx.raw().execute(
            "INSERT INTO sessions (id, tenant_id, space_id, status, started_us) "
            "VALUES ('sess-a', 't1', ?, 'open', 1)",
            (space_a,),
        )
        tx.raw().execute(
            "INSERT INTO sessions (id, tenant_id, space_id, status, started_us) "
            "VALUES ('sess-b', 't1', ?, 'open', 1)",
            (space_b,),
        )
        bob = tx.identities.insert_entity("t1", EntityKind.PERSON, display_name="Bob").id
        ann = tx.identities.insert_entity("t1", EntityKind.PERSON, display_name="Ann").id
    access = access_for("t1", agent_ids=frozenset({agent}), space_ids=frozenset({space_a}))
    wide = access_for("t1", agent_ids=frozenset({agent}), space_ids=frozenset({space_a, space_b}))
    return {
        "store": clocked_store,
        "clock": mutable_clock,
        "claims": phase5_claims,
        "episodes": phase5_episodes,
        "relations": phase5_relations,
        "agent": agent,
        "space_a": space_a,
        "space_b": space_b,
        "bob": bob,
        "ann": ann,
        "access": access,
        "wide": wide,
    }


class TestEpisodes:
    def test_create_and_seal(self, memory_ctx: dict[str, Any]) -> None:
        episodes: EpisodeService = memory_ctx["episodes"]
        access = memory_ctx["access"]
        created = episodes.create(
            access,
            agent_id=memory_ctx["agent"],
            summary="Evening chat about tea",
            title="Tea talk",
            space_id=memory_ctx["space_a"],
            session_id="sess-a",
            idempotency_key="ep-1",
        )
        assert created.revision == 1
        sealed = episodes.transition(
            access,
            created.episode_id,
            "seal",
            expected_revision=1,
            reason="window closed",
            idempotency_key="ep-seal",
        )
        assert sealed.status == "sealed"

    def test_observation_refs_from_other_space_rejected(self, memory_ctx: dict[str, Any]) -> None:
        from iris_memory_core.application.observation import ObservationService
        from tests.integration.test_phase5_claims import _gauge

        episodes: EpisodeService = memory_ctx["episodes"]
        observations = ObservationService(memory_ctx["store"], gauge=_gauge())
        wide = memory_ctx["wide"]
        outcome = observations.observe_batch(
            wide,
            [
                {
                    "agent_id": memory_ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "ep-obs-b",
                    "occurred_us": 1,
                    "committed_us": 1,
                    "content": "space b content",
                    "space_id": memory_ctx["space_b"],
                    "session_id": "sess-b",
                }
            ],
        )
        foreign_observation = outcome.accepted_observation_ids[0]
        with pytest.raises(InvalidRequestError):
            episodes.create(
                wide,
                agent_id=memory_ctx["agent"],
                summary="stitch attempt",
                space_id=memory_ctx["space_a"],
                session_id="sess-a",
                observation_refs=[
                    {"resource_type": "observation", "resource_id": foreign_observation}
                ],
                idempotency_key="ep-2",
            )

    def test_unknown_participant_rejected(self, memory_ctx: dict[str, Any]) -> None:
        episodes: EpisodeService = memory_ctx["episodes"]
        with pytest.raises(NotFoundError):
            episodes.create(
                memory_ctx["access"],
                agent_id=memory_ctx["agent"],
                summary="x",
                participant_entity_ids=["ghost-entity"],
                idempotency_key="ep-3",
            )

    def test_supersede_terminal(self, memory_ctx: dict[str, Any]) -> None:
        episodes: EpisodeService = memory_ctx["episodes"]
        access = memory_ctx["access"]
        created = episodes.create(
            access, agent_id=memory_ctx["agent"], summary="x", idempotency_key="ep-4"
        )
        sealed = episodes.transition(
            access,
            created.episode_id,
            "seal",
            expected_revision=1,
            reason="r",
            idempotency_key="ep-4s",
        )
        superseded = episodes.transition(
            access,
            created.episode_id,
            "supersede",
            expected_revision=sealed.revision,
            reason="re-bounded",
            idempotency_key="ep-4sup",
        )
        assert superseded.status == "superseded"
        with pytest.raises(DomainError):
            episodes.transition(
                access,
                created.episode_id,
                "seal",
                expected_revision=superseded.revision,
                reason="r",
                idempotency_key="ep-4x",
            )

    def test_transition_cas(self, memory_ctx: dict[str, Any]) -> None:
        episodes: EpisodeService = memory_ctx["episodes"]
        access = memory_ctx["access"]
        created = episodes.create(
            access, agent_id=memory_ctx["agent"], summary="x", idempotency_key="ep-5"
        )
        with pytest.raises(RevisionMismatchError):
            episodes.transition(
                access,
                created.episode_id,
                "seal",
                expected_revision=42,
                reason="r",
                idempotency_key="ep-5s",
            )

    def test_extractor_version_and_scores_roundtrip(self, memory_ctx: dict[str, Any]) -> None:
        episodes: EpisodeService = memory_ctx["episodes"]
        access = memory_ctx["access"]
        created = episodes.create(
            access,
            agent_id=memory_ctx["agent"],
            summary="emotional evening",
            importance=0.9,
            valence=0.7,
            arousal=0.3,
            extractor_version="episode-extractor/1.2",
            idempotency_key="ep-6",
        )
        pair = episodes.get(access, created.episode_id)
        assert pair is not None
        episode, revision = pair
        assert revision.extractor_version == "episode-extractor/1.2"
        assert revision.valence == 0.7 and revision.arousal == 0.3
        assert episode.importance == 0.9


class TestRelations:
    def _claim_evidence(self, memory_ctx: dict[str, Any], key: str) -> list[dict[str, object]]:
        from iris_memory_core.application.observation import ObservationService
        from tests.integration.test_phase5_claims import _gauge

        observations = ObservationService(memory_ctx["store"], gauge=_gauge())
        outcome = observations.observe_batch(
            memory_ctx["access"],
            [
                {
                    "agent_id": memory_ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"rel-obs-{key}",
                    "occurred_us": 1,
                    "committed_us": 1,
                    "content": "they know each other",
                }
            ],
        )
        return [
            {
                "source_type": "observation",
                "source_id": outcome.accepted_observation_ids[0],
                "relation": "supports",
            }
        ]

    def test_relation_requires_evidence(self, memory_ctx: dict[str, Any]) -> None:
        relations: RelationService = memory_ctx["relations"]
        with pytest.raises(InvalidRequestError):
            relations.create(
                memory_ctx["access"],
                agent_id=memory_ctx["agent"],
                source_entity_id=memory_ctx["bob"],
                relation_type="knows",
                target_entity_id=memory_ctx["ann"],
                evidence=[],
                idempotency_key="rel-1",
            )

    def test_relation_lifecycle_and_dedup(self, memory_ctx: dict[str, Any]) -> None:
        relations: RelationService = memory_ctx["relations"]
        access = memory_ctx["access"]
        created = relations.create(
            access,
            agent_id=memory_ctx["agent"],
            source_entity_id=memory_ctx["bob"],
            relation_type="knows",
            target_entity_id=memory_ctx["ann"],
            evidence=self._claim_evidence(memory_ctx, "a"),
            idempotency_key="rel-2",
        )
        assert created.revision == 1 and not created.deduped
        deduped = relations.create(
            access,
            agent_id=memory_ctx["agent"],
            source_entity_id=memory_ctx["bob"],
            relation_type="knows",
            target_entity_id=memory_ctx["ann"],
            evidence=self._claim_evidence(memory_ctx, "b"),
            idempotency_key="rel-3",
        )
        assert deduped.deduped and deduped.relation_id == created.relation_id
        # Different validity window → a different relation row, never merged.
        other_window = relations.create(
            access,
            agent_id=memory_ctx["agent"],
            source_entity_id=memory_ctx["bob"],
            relation_type="knows",
            target_entity_id=memory_ctx["ann"],
            valid_from_us=1,
            valid_until_us=2,
            evidence=self._claim_evidence(memory_ctx, "c"),
            idempotency_key="rel-4",
        )
        assert not other_window.deduped

    def test_self_loop_rejected(self, memory_ctx: dict[str, Any]) -> None:
        relations: RelationService = memory_ctx["relations"]
        with pytest.raises(InvalidRequestError):
            relations.create(
                memory_ctx["access"],
                agent_id=memory_ctx["agent"],
                source_entity_id=memory_ctx["bob"],
                relation_type="knows",
                target_entity_id=memory_ctx["bob"],
                evidence=self._claim_evidence(memory_ctx, "d"),
                idempotency_key="rel-5",
            )

    def test_cross_tenant_endpoint_rejected(self, memory_ctx: dict[str, Any]) -> None:
        relations: RelationService = memory_ctx["relations"]
        with memory_ctx["store"].write() as tx:
            tx.insert_tenant("t9", status="active")
            foreign = tx.identities.insert_entity("t9", EntityKind.PERSON).id
        with pytest.raises(DomainError):
            relations.create(
                memory_ctx["access"],
                agent_id=memory_ctx["agent"],
                source_entity_id=foreign,
                relation_type="knows",
                target_entity_id=memory_ctx["ann"],
                evidence=self._claim_evidence(memory_ctx, "e"),
                idempotency_key="rel-6",
            )

    def test_model_inference_cannot_create_relation_without_evidence(
        self, memory_ctx: dict[str, Any]
    ) -> None:
        """Nickname similarity is not an evidence source: the evidence
        requirement is structural, so inferred relations cannot be written
        by the API at all (Phase 10 extraction goes through the same door)."""
        relations: RelationService = memory_ctx["relations"]
        with pytest.raises((InvalidRequestError, NotFoundError)):
            relations.create(
                memory_ctx["access"],
                agent_id=memory_ctx["agent"],
                source_entity_id=memory_ctx["bob"],
                relation_type="knows",
                target_entity_id=memory_ctx["ann"],
                evidence=[
                    {
                        "source_type": "claim",
                        "source_id": "some-similar-claim",
                        "relation": "supports",
                    }
                ],
                idempotency_key="rel-7",
            )
