"""Application ports for focus; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from iris_memory_core.domain.focus import FocusItemCurrent, FocusRevision


class FocusSurface(Protocol):
    """Repository surface for focus current rows and immutable revisions."""

    def erase_content(self, item_id: str, *, now_us: int) -> None: ...

    def get(self, item_id: str) -> FocusItemCurrent: ...
    def get_revision(self, revision_id: str) -> FocusRevision: ...
    def current_revision_row(self, item_id: str) -> FocusRevision: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        kind: str,
        summary: str,
        status: str,
        revision_id: str,
        activation: float,
        activation_base: float,
        last_activated_us: int,
        expires_us: int | None,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        item_id: str,
        tenant_id: str,
        revision: int,
        kind: str,
        summary: str,
        structured_payload: dict[str, object] | None,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        salience: float,
        activation: float,
        activation_base: float,
        importance: float,
        status: str,
        promotion_policy: str,
        promotion_target_type: str | None,
        promotion_target_id: str | None,
        last_activated_us: int,
        expires_us: int | None,
        created_by: str,
    ) -> str: ...
    def advance_pointer(
        self,
        item_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        activation: float | None = None,
        activation_base: float | None = None,
        last_activated_us: int | None = None,
    ) -> int: ...
    def set_initial_pointer(self, item_id: str, revision_id: str) -> int: ...
    def raise_pointer_mismatch(self, item_id: str, expected: int) -> None: ...
    def active_items(self, tenant_id: str, agent_id: str) -> Sequence[FocusItemCurrent]: ...
    def items_for_agent(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: tuple[str, ...] = ("active", "dormant"),
        kind: str | None = None,
        limit: int = 500,
    ) -> Sequence[FocusItemCurrent]: ...
    def maintenance_items(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        now_us: int,
        limit: int = 500,
    ) -> Sequence[FocusItemCurrent]: ...
    def history(self, item_id: str, *, limit: int = 100) -> Sequence[FocusRevision]: ...
