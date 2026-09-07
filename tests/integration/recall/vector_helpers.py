"""Shared Phase 7 test context: a one-tenant world with claims, notes,
episodes, a deterministic embedding provider and a vector projection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.domain.vector import VectorSpaceConfig
from iris_memory_core.indexing.vector import VectorProjectionService
from iris_memory_core.providers.embedding import DeterministicEmbeddingProvider
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for

DEFAULT_SPACE = VectorSpaceConfig(model="test-embedding", dimension=8)


def vector_space(**overrides: Any) -> VectorSpaceConfig:
    values: dict[str, Any] = {"model": "test-embedding", "dimension": 8}
    values.update(overrides)
    return VectorSpaceConfig(**values)


def make_projection(
    store: Store,
    clock: MutableClock,
    *,
    space: VectorSpaceConfig | None = None,
    staleness_limit: int = 10_000,
    retirement_window_us: int = 24 * 3_600_000_000,
) -> VectorProjectionService:
    return VectorProjectionService(
        store,
        clock,
        provider=DeterministicEmbeddingProvider(space or DEFAULT_SPACE),
        vector_root=Path(store.runtime.database).parent / "vector",
        space=space or DEFAULT_SPACE,
        staleness_limit=staleness_limit,
        retirement_window_us=retirement_window_us,
    )


class VectorCtx:
    """One tenant with an agent, two spaces, claims/notes/episodes and a
    vector projection bound to a deterministic provider."""

    def __init__(self, store: Store, clock: MutableClock) -> None:
        from iris_memory_core.domain.identity import EntityKind

        self.store = store
        self.clock = clock
        with store.write() as tx:
            tx.insert_tenant("t1", status="active")
            self.agent = tx.insert_agent("t1", "A", actor="t").id
            self.other_agent = tx.insert_agent("t1", "B", actor="t").id
            self.space = tx.insert_space("t1", "chat_group").id
            self.other_space = tx.insert_space("t1", "direct").id
            self.entity = tx.identities.insert_entity(
                "t1", EntityKind.PERSON, display_name="Bob"
            ).id
        self.access = access_for(
            "t1",
            agent_ids=frozenset({self.agent, self.other_agent}),
            space_ids=frozenset({self.space, self.other_space}),
        )
        idem = IdempotencyManager(store)
        self.observations = ObservationService(store)
        self.claims = ClaimService(store, clock, idempotency=idem)
        self.notes = NoteService(store, clock, idempotency=idem)
        self.vector = make_projection(store, clock)

    # -- data helpers ---------------------------------------------------------

    def remember(self, key: str, text: str, **overrides: Any) -> Any:
        observation = self.observations.observe_batch(
            self.access,
            [
                {
                    "agent_id": self.agent,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": f"obs-{key}",
                    "occurred_us": self.clock.now_us(),
                    "committed_us": self.clock.now_us(),
                    "content": f"evidence for {key}",
                    "space_id": self.space,
                }
            ],
        ).accepted_observation_ids[0]
        payload: dict[str, Any] = {
            "agent_id": self.agent,
            "space_id": self.space,
            "subject_entity_id": self.entity,
            "predicate": f"p_{key}",
            "value": {"k": key},
            "canonical_text": text,
            "category": "fact",
            "evidence": [{"source_type": "observation", "source_id": observation}],
            "idempotency_key": f"idem-{key}",
        }
        payload.update(overrides)
        return self.claims.remember(self.access, **payload)

    def search(self, topic: str, limit: int = 20) -> list[Any]:
        query = self.vector.embed_query(topic)
        with self.store.read() as tx:
            return self.vector.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=self.agent,
                query_vector=query,
                limit=limit,
            )

    def search_ids(self, topic: str) -> set[str]:
        return {hit.resource_id for hit in self.search(topic)}

    def rebuild(self) -> Any:
        return self.vector.rebuild("t1")

    def pointer(self) -> Any:
        with self.store.read() as tx:
            return tx.vector.pointer("t1")
