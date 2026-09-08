"""Independent mutable Persona draft storage boundary."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from iris_memory_core.domain.persona_draft import PersonaDraft


class PersonaDraftSurface(Protocol):
    def replay_discard(
        self, tenant_id: str, agent_id: str, draft_id: str
    ) -> PersonaDraft | None: ...
    def get(self, tenant_id: str, agent_id: str, draft_id: str) -> PersonaDraft: ...
    def create(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        base_revision: int,
        policy_revision: int,
        fields: Mapping[str, Any],
        source_refs: Sequence[Mapping[str, object]],
        actor: str,
    ) -> PersonaDraft: ...
    def update(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        draft_id: str,
        expected_revision: int,
        base_revision: int,
        policy_revision: int,
        fields: Mapping[str, Any],
        source_refs: Sequence[Mapping[str, object]],
        actor: str,
    ) -> PersonaDraft: ...
    def discard(
        self, *, tenant_id: str, agent_id: str, draft_id: str, expected_revision: int, actor: str
    ) -> PersonaDraft: ...
    def mark_published(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        draft_id: str,
        expected_revision: int,
        published_revision_id: str,
        actor: str,
    ) -> PersonaDraft: ...
