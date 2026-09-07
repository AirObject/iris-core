"""Application ports for observation; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from iris_memory_core.domain.observation import GapPolicy, ObservationDraft, StoredObservation


class ObservationSurface(Protocol):
    """Repository surface for the observation journal and source cursors."""

    def get(self, observation_id: str) -> StoredObservation: ...
    def record_fingerprint(self, observation_id: str) -> str: ...
    def find_by_idempotency_key(
        self, tenant_id: str, agent_id: str, idempotency_key: str
    ) -> StoredObservation | None: ...
    def find_by_cursor(
        self, tenant_id: str, agent_id: str, source_stream: str, cursor: int
    ) -> StoredObservation | None: ...
    def find_by_occurrence(
        self, tenant_id: str, agent_id: str, occurrence_id: str
    ) -> StoredObservation | None: ...
    def find_by_source_event(
        self, tenant_id: str, agent_id: str, source_event_id: str
    ) -> StoredObservation | None: ...
    def for_trigger_scan(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        want_kind: str | None,
        want_role: str | None,
        after_us: int | None,
        limit: int,
        trigger_id: str | None = None,
        trigger_revision: int = 0,
    ) -> Sequence[StoredObservation]: ...
    def insert(self, draft: ObservationDraft, fingerprint: str) -> StoredObservation: ...
    def observations_by_actor_entity(
        self, tenant_id: str, entity_id: str, *, limit: int = 10_000
    ) -> tuple[str, ...]: ...
    def observations_for_session(
        self, tenant_id: str, space_id: str, session_id: str, *, limit: int = 10_000
    ) -> tuple[str, ...]: ...
    def observations_for_space(
        self, tenant_id: str, space_id: str, *, limit: int = 10_000
    ) -> tuple[str, ...]: ...
    def scrub_content(self, observation_id: str) -> int: ...
    def cursor_state(
        self, tenant_id: str, agent_id: str, source_stream: str
    ) -> tuple[int | None, GapPolicy]: ...
    def advance_cursor(
        self,
        tenant_id: str,
        agent_id: str,
        source_stream: str,
        position: int,
        gap_policy: GapPolicy,
    ) -> None: ...
