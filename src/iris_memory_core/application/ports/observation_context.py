"""Private typed context persistence port."""

from __future__ import annotations

from typing import Any, Protocol

from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.scope import Scope


class ObservationContextSurface(Protocol):
    def page(
        self,
        scope: Scope,
        *,
        watermark: int,
        start_us: int,
        end_us: int,
        after: tuple[int, int, str] | None,
        limit: int,
        pending_only: bool = False,
    ) -> tuple[StoredObservation, ...]: ...

    def targets(self, *, limit: int) -> tuple[Scope, ...]: ...

    def mark_scanned(self, scope: Scope) -> None: ...

    def scan_position(self, admission_key: str) -> tuple[int, int, str] | None: ...
    def advance_scan_position(
        self, admission_key: str, position: tuple[int, int, str] | None
    ) -> None: ...

    def insert_batch(
        self,
        batch_id: str,
        scope: Scope,
        *,
        job_id: str,
        watermark: int,
        observations: tuple[StoredObservation, ...],
    ) -> None: ...

    def batch(self, batch_id: str) -> dict[str, Any] | None: ...

    def complete(self, batch_id: str, episode_ids: list[str]) -> None: ...

    def summaries(self, scope: Scope, *, limit: int) -> tuple[str, ...]: ...

    def processing(self, observation_id: str) -> str: ...

    def cursor(
        self, token: str, *, tenant_id: str, app_instance_id: str, query_hash: str
    ) -> dict[str, Any] | None: ...

    def save_cursor(
        self,
        *,
        tenant_id: str,
        app_instance_id: str,
        query_hash: str,
        watermark: int,
        position: tuple[int, int, str],
    ) -> str: ...

    def expired_background(
        self, *, before_us: int, limit: int
    ) -> tuple[StoredObservation, ...]: ...

    def has_live_dependency(self, tenant_id: str, observation_id: str) -> bool: ...
