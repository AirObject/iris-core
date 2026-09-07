"""Application ports for surface; storage and provider adapters implement these contracts."""

from __future__ import annotations

from typing import Protocol

from iris_memory_core.domain.surface import LeaseView, SurfaceMode


class SurfaceLeaseSurface(Protocol):
    def state(self, tenant_id: str, agent_id: str) -> tuple[str, int, int]: ...
    def set_mode(
        self, tenant_id: str, agent_id: str, mode: SurfaceMode, *, expected_revision: int
    ) -> int: ...
    def next_epoch(self, tenant_id: str, agent_id: str) -> int: ...
    def insert_lease(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        holder_space_id: str | None,
        holder_app_instance_id: str,
        lease_epoch: int,
        priority: int,
        ttl_us: int,
    ) -> LeaseView: ...
    def get_lease(self, lease_id: str) -> LeaseView: ...
    def active_lease(self, tenant_id: str, agent_id: str) -> LeaseView | None: ...
    def expire_stale(self, tenant_id: str, agent_id: str, *, now_us: int) -> int: ...
    def fence(
        self, lease_id: str, *, expected_epoch: int, expected_revision: int, now_us: int
    ) -> int: ...
    def heartbeat(
        self,
        lease_id: str,
        *,
        expected_epoch: int,
        expected_owner: str,
        now_us: int,
        ttl_us: int,
    ) -> int: ...
    def release(
        self, lease_id: str, *, expected_epoch: int, expected_owner: str, now_us: int
    ) -> int: ...
    def record_event(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        lease_id: str,
        lease_epoch: int,
        event: str,
        actor: str,
        reason_code: str = "",
        details: dict[str, object] | None = None,
    ) -> None: ...
    def event_count(self, tenant_id: str, agent_id: str) -> int: ...
    def lease_counts(self) -> dict[str, int]: ...
