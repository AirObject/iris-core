"""Phase 8 shared test helpers (ADR-0016).

One world builder wiring tenant/agents/spaces/entities, the claim/relation/
identity services, both projections and the recall orchestrator with the
graph and profile routes enabled.
"""

from __future__ import annotations

import random
from typing import Any

from iris_memory_core.application.episodes import RelationService
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import (
    ExternalActorRef,
    RecallService,
    StructuredRecallOrchestrator,
)
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.identity import EntityKind, ExternalIdentityKey
from iris_memory_core.indexing.graph import GraphProjectionService
from iris_memory_core.indexing.profile import ProfileProjectionService
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for

TENANT = "t1"


class Phase8World:
    _request_counter = 0
    _obs_counter = 0

    def __init__(self, store: Store, clock: MutableClock) -> None:
        self.store = store
        self.clock = clock
        with store.write() as tx:
            tx.insert_tenant(TENANT, status="active")
            self.agent = tx.insert_agent(TENANT, "Agent A", actor="t").id
            self.other_agent = tx.insert_agent(TENANT, "Agent B", actor="t").id
            self.space = tx.insert_space(TENANT, "chat_group", actor="t").id
            self.other_space = tx.insert_space(TENANT, "direct", actor="t").id
        self.entities: dict[str, str] = {}
        # A registered default speaker so actorless helper calls resolve.
        self.speaker_entity = None  # bound after the services below exist
        self.access = access_for(
            TENANT,
            agent_ids=frozenset({self.agent}),
            space_ids=frozenset({self.space, self.other_space}),
        )
        self.admin_access = access_for(
            TENANT,
            agent_ids=frozenset({self.agent}),
            space_ids=frozenset({self.space, self.other_space}),
            admin=True,
        )
        self.other_agent_access = access_for(
            TENANT,
            agent_ids=frozenset({self.other_agent}),
            space_ids=frozenset({self.space, self.other_space}),
        )
        self.restricted_access = access_for(
            TENANT,
            agent_ids=frozenset({self.agent}),
            space_ids=frozenset({self.space, self.other_space}),
            custom_labels=frozenset({"restricted"}),
        )
        self.idem = IdempotencyManager(store)
        self.observations = ObservationService(store)
        self.claims = ClaimService(store, clock, idempotency=self.idem)
        self.relations = RelationService(store, clock, idempotency=self.idem)
        self.identities = IdentityService(store)
        self.speaker_entity = self.entity("Speaker")
        self.bind_identity(self.speaker_entity, "default-speaker")
        self.profile = ProfileProjectionService(store, clock)
        self.graph = GraphProjectionService(store, clock)
        orchestrator = StructuredRecallOrchestrator(
            store,
            RecentContextService(store, clock),
            StateService(store, clock, idempotency=self.idem),
            FocusService(store, clock),
            clock=clock,
            claims_enabled=True,
            relations_enabled=True,
            graph=self.graph,
            profile=self.profile,
            monotonic=SystemMonotonicClock(),
        )
        self.service = RecallService(orchestrator, store, clock)

    # -- canonical writers ----------------------------------------------------

    def entity(self, name: str, *, kind: EntityKind = EntityKind.PERSON) -> str:
        with self.store.write() as tx:
            entity = tx.identities.insert_entity(TENANT, kind, display_name=name)
        self.entities[name] = entity.id
        return entity.id

    def observation(
        self,
        content: str = "evidence",
        *,
        space_id: str | None = None,
        agent_id: str | None = None,
        access: Any | None = None,
    ) -> str:
        type(self)._obs_counter += 1
        return self.observations.observe_batch(
            access or self.access,
            [
                {
                    "agent_id": agent_id or self.agent,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"p8-obs-{type(self)._obs_counter}",
                    "occurred_us": self.clock.now_us(),
                    "committed_us": self.clock.now_us(),
                    "content": content,
                    "space_id": space_id or self.space,
                }
            ],
        ).accepted_observation_ids[0]

    def remember(
        self,
        key: str,
        text: str,
        subject: str,
        **overrides: Any,
    ) -> Any:
        payload: dict[str, Any] = {
            "agent_id": self.agent,
            "space_id": self.space,
            "subject_entity_id": subject,
            "predicate": f"p_{key}",
            "value": {"k": key},
            "canonical_text": text,
            "category": "fact",
            "evidence": [{"source_type": "observation", "source_id": self.observation()}],
            "idempotency_key": f"p8-idem-{key}",
        }
        payload.update(overrides)
        return self.claims.remember(self.access, **payload)

    def relate(
        self,
        key: str,
        source: str,
        target: str,
        *,
        relation_type: str = "friends_with",
        privacy_labels: list[str] | None = None,
        valid_from_us: int | None = None,
        valid_until_us: int | None = None,
        agent_id: str | None = None,
        space_id: str | None = None,
        access: Any | None = None,
    ) -> Any:
        return self.relations.create(
            access or self.access,
            agent_id=agent_id or self.agent,
            source_entity_id=source,
            relation_type=relation_type,
            target_entity_id=target,
            space_id=space_id if space_id is not None else self.space,
            privacy_labels=privacy_labels,
            valid_from_us=valid_from_us,
            valid_until_us=valid_until_us,
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": self.observation(
                        space_id=space_id if space_id is not None else self.space,
                        agent_id=agent_id or self.agent,
                        access=access,
                    ),
                }
            ],
            idempotency_key=f"p8-rel-{key}",
        )

    def bind_identity(self, entity_id: str, external_id: str) -> Any:
        with self.store.write() as tx:
            identity = tx.identities.insert_external_identity(
                ExternalIdentityKey(
                    tenant_id=TENANT,
                    provider="qq",
                    realm="default",
                    external_id=external_id,
                ),
                entity_id=entity_id,
            )
        binding = self.identities.propose_binding(
            self.admin_access,
            identity.id,
            entity_id,
            proof_digest=f"proof-{external_id}",
            reason="test",
        )
        return self.identities.confirm_binding(
            self.admin_access, binding.id, expected_revision=binding.revision, reason="test"
        )

    # -- projections ----------------------------------------------------------

    def rebuild(self) -> None:
        self.profile.rebuild(TENANT)
        self.graph.rebuild(TENANT)

    # -- recall ---------------------------------------------------------------

    def recall(self, topic: str = "friends", **overrides: Any) -> Any:
        type(self)._request_counter += 1
        actors = overrides.pop("actors", None)
        deadline_slack_us = overrides.pop("deadline_slack_us", 5_000_000)
        request = self.service.build_request(
            request_id=overrides.pop("request_id", f"p8-req-{type(self)._request_counter}"),
            agent_id=self.agent,
            space_id=self.space,
            deadline_at_us=self.clock.now_us() + deadline_slack_us,
            topic=topic,
            purpose="reply",
            token_budget=100_000,
            **overrides,
        )
        if actors is None:
            actors = (
                ExternalActorRef(provider="qq", realm="default", external_id="default-speaker"),
            )
        return self.service.recall(self.access, request, actors=actors)

    def recall_as_speaker(self, external_id: str, topic: str = "friends", **overrides: Any) -> Any:
        return self.recall(
            topic,
            actors=(ExternalActorRef(provider="qq", realm="default", external_id=external_id),),
            **overrides,
        )


def seeded_rng(seed: int) -> random.Random:
    return random.Random(seed)
