"""Application ports for events; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol

from iris_memory_core.domain.event import (
    DEFAULT_MAX_DELIVERY_ATTEMPTS,
    CognitiveEventCurrent,
    CognitiveEventRevision,
)


class CognitiveEventSurface(Protocol):
    """Repository surface for cognitive event rows and delivery history."""

    def get(self, event_id: str) -> CognitiveEventCurrent: ...
    def get_revision(self, revision_id: str) -> CognitiveEventRevision: ...
    def current_revision_row(self, event_id: str) -> CognitiveEventRevision: ...
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
        object_type: str,
        object_id: str,
        occurrence_id: str | None,
        scheduled_at_us: int,
        deliver_after_us: int,
        expires_us: int | None,
        delivery_target: str | None,
        summary_of_count: int = 0,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        event_id: str,
        tenant_id: str,
        revision: int,
        status: str,
        delivery_attempts: int,
        last_delivery_us: int | None,
        delivered_lease_id: str | None,
        delivered_lease_epoch: int | None,
        ack_id: str | None,
        acknowledged_us: int | None,
        reason_code: str | None,
        created_by: str,
    ) -> str: ...
    def set_initial_pointer(self, event_id: str, revision_id: str) -> int: ...
    def advance_pointer(
        self,
        event_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        delivery_attempts: int | None = None,
        last_delivery_us: int | None = None,
        last_delivery_set: bool = False,
        delivered_lease_id: str | None = None,
        delivered_lease_epoch: int | None = None,
        lease_set: bool = False,
        ack_id: str | None = None,
        ack_id_set: bool = False,
        acknowledged_us: int | None = None,
        acknowledged_set: bool = False,
    ) -> int: ...
    def raise_pointer_mismatch(self, event_id: str, expected: int) -> None: ...
    def pullable_events(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        now_us: int,
        limit: int = 50,
        allowed_space_ids: Collection[str] | None = None,
    ) -> Sequence[CognitiveEventCurrent]: ...
    def pending_events(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: Sequence[str] = ("pending", "delivered"),
        limit: int = 200,
        allowed_space_ids: Collection[str] | None = None,
        now_us: int | None = None,
        request_scope: tuple[str | None, str | None, str | None] | None = None,
    ) -> Sequence[CognitiveEventCurrent]: ...
    def expiry_candidates(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        now_us: int,
        limit: int = 500,
        max_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
    ) -> Sequence[CognitiveEventCurrent]: ...
    def fence_candidates(
        self, tenant_id: str, agent_id: str, *, limit: int = 500
    ) -> Sequence[CognitiveEventCurrent]: ...
    def history(self, event_id: str, *, limit: int = 100) -> Sequence[CognitiveEventRevision]: ...
