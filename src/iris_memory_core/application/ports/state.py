"""Application ports for state; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from iris_memory_core.domain.state import (
    StateEntry,
    StateNamespacePolicy,
    StateRecord,
    StateRevision,
)


class StateSurface(Protocol):
    """Repository surface for state namespace policies, records, revisions."""

    def erase_content(self, record_id: str, *, now_us: int) -> None: ...

    def policy(self, tenant_id: str, namespace: str) -> StateNamespacePolicy | None: ...
    def upsert_policy(self, policy: StateNamespacePolicy, *, tenant_id: str) -> None: ...
    def find(self, scope_key: str, namespace: str, key: str) -> StateRecord | None: ...
    def get(self, record_id: str) -> StateRecord: ...
    def current_revision(self, revision_id: str) -> StateRevision: ...
    def insert(
        self,
        *,
        scope_key: str,
        tenant_id: str,
        agent_id: str | None,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        namespace: str,
        key: str,
        revision_id: str,
        revision: int,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        record_id: str,
        tenant_id: str,
        revision: int,
        value_json: str,
        source_ref: str | None,
        source_authority: str,
        observed_us: int,
        expires_us: int | None,
        coalesce_key: str | None,
    ) -> str: ...
    def advance_pointer(
        self, record_id: str, *, expected_revision: int, revision: int, revision_id: str
    ) -> int: ...
    def set_initial_pointer(self, record_id: str, revision_id: str) -> int: ...
    def revision_count(self, record_id: str) -> int: ...
    def prune_history(self, record_id: str, *, keep: int) -> int: ...
    def history(self, record_id: str, *, limit: int = 50) -> Sequence[StateRevision]: ...
    def list_scope(
        self,
        *,
        tenant_id: str,
        agent_id: str | None,
        namespace: str | None,
        space_id: str | None,
        session_id: str | None,
        prefix: str | None,
        limit: int = 100,
    ) -> Sequence[StateEntry]: ...
