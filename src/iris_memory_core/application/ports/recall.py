"""Application ports for recall; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.recent import BuiltProjection, StoredGeneration


class RecentContextSurface(Protocol):
    """Repository surface for recent-context generations and pointers."""

    def observation_window(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_id: str,
        session_id: str | None,
        limit: int,
    ) -> Sequence[StoredObservation]: ...
    def insert_generation(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str,
        session_id: str | None,
        target_key: str,
        projection: BuiltProjection,
        expires_us: int | None,
    ) -> str: ...
    def current(self, target_key: str) -> StoredGeneration | None: ...
    def swap_pointer(
        self,
        *,
        target_key: str,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str,
        session_id: str | None,
        current_generation_id: str,
    ) -> None: ...
    def retire_pointer(self, target_key: str) -> bool: ...
    def expire_stale(self, now_us: int) -> int: ...


class RecallUsageSurface(Protocol):
    """Repository surface for recall request archives and usage reports."""

    def insert_request(
        self,
        *,
        request_id: str,
        tenant_id: str,
        agent_id: str,
        persona_revision: int,
        source_watermark: int,
        tombstone_watermark: int,
        schema_version: int,
        ranker_version: int,
        token_estimator_version: int,
        retrieved_count: int,
        returned_candidate_ids: Sequence[str],
        request_fingerprint: str,
        resource_ids: Sequence[str] = (),
        response_json: str | None = None,
        duration_us: int | None = None,
        statistics_json: str | None = None,
        now_us: int | None = None,
    ) -> None: ...

    def get_request(self, tenant_id: str, request_id: str) -> object | None: ...

    def returned_candidate_ids(self, tenant_id: str, request_id: str) -> tuple[str, ...] | None: ...

    def scrub_request_responses(self, tenant_id: str, resource_ids: Sequence[str]) -> int: ...

    def insert_report(
        self,
        *,
        tenant_id: str,
        request_id: str,
        agent_id: str,
        app_instance_id: str,
        host_cycle_id: str,
        persona_revision: int,
        host_selected_ids: Sequence[str],
        model_visible_ids: Sequence[str],
        reported_at_us: int,
        now_us: int | None = None,
    ) -> tuple[str, bool]: ...

    def get_report(self, tenant_id: str, request_id: str, host_cycle_id: str) -> object | None: ...

    def reports_for_request(self, tenant_id: str, request_id: str) -> Sequence[object]: ...

    def insert_activation(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        request_id: str,
        host_cycle_id: str,
        candidate_id: str,
        stage: str,
        resource_type: str,
        resource_id: str,
        resource_revision: int,
        activation_delta: float,
        applied: bool,
        reject_reason: str | None,
        now_us: int,
    ) -> tuple[str, bool]: ...
