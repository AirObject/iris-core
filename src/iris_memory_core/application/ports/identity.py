"""Application ports for identity; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol

from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.domain.model import Binding, Entity, EntityRedirect


class IdentitySurface(Protocol):
    """Repository surface for entities used by the self-subject resolution."""

    def entities_by_id(self, tenant_id: str, entity_ids: Collection[str]) -> dict[str, Entity]: ...

    def all_verified_bindings(self, tenant_id: str) -> tuple[Binding, ...]: ...

    def verified_bindings_for_entity(
        self, tenant_id: str, entity_id: str
    ) -> tuple[Binding, ...]: ...

    def external_identity_ids(self, tenant_id: str) -> set[str]: ...

    def insert_entity(
        self,
        tenant_id: str,
        kind: EntityKind,
        *,
        display_name: str = "",
        privacy_labels: Sequence[str] = (),
        actor: str = "system",
    ) -> Entity: ...

    def insert_entity_redirect(
        self,
        tenant_id: str,
        from_entity_id: str,
        to_entity_id: str,
        *,
        actor: str,
        reason_code: str,
    ) -> EntityRedirect: ...
