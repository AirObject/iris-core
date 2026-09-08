"""Bounded statistics projection and observation storage port."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from iris_memory_core.domain.console import OperatorGrant


class StatisticsRepository(Protocol):
    def budget(self, milliseconds: int = 250) -> AbstractContextManager[None]: ...

    def coverage_from_us(self) -> int: ...

    def create_build(
        self,
        identifier: str,
        tenant_id: str,
        lower: int,
        upper: int,
        now: int,
        *,
        operation_id: str | None = None,
    ) -> None: ...

    def build(self, tenant_id: str, identifier: str) -> dict[str, Any] | None: ...

    def current_build(
        self, tenant_id: str, *, bucket_us: int | None = None, end_us: int | None = None
    ) -> dict[str, Any] | None: ...

    def advance(
        self,
        tenant_id: str,
        identifier: str,
        *,
        stage: int,
        after: tuple[int, str] | None,
        now: int,
        complete: bool = False,
    ) -> None: ...

    def cancel(self, tenant_id: str, identifier: str) -> None: ...

    def put(
        self,
        *,
        build_id: str,
        tenant_id: str,
        bucket: int,
        granularity: str,
        metric: str,
        atom_id: str,
        labels: dict[str, Any],
        value: dict[str, Any],
    ) -> None: ...

    def aggregate_hours(
        self, tenant_id: str, identifier: str, *, after: tuple[int, str] | None, limit: int = 201
    ) -> tuple[dict[str, Any], ...]: ...

    def atoms(
        self,
        tenant_id: str,
        build_id: str,
        metric: str,
        granularity: str,
        grant: OperatorGrant,
        *,
        bucket: int | None = None,
        limit: int = 4001,
    ) -> tuple[dict[str, Any], ...]: ...

    def recall_rows(
        self,
        tenant_id: str,
        lower: int,
        upper: int,
        *,
        after: tuple[int, str] | None,
        limit: int = 201,
    ) -> tuple[dict[str, Any], ...]: ...

    def audit_rows(
        self,
        tenant_id: str,
        lower: int,
        upper: int,
        *,
        after: tuple[int, str] | None,
        limit: int = 201,
    ) -> tuple[dict[str, Any], ...]: ...

    def live(
        self, tenant_id: str, source: str, *, limit: int = 1001
    ) -> tuple[dict[str, Any], ...]: ...

    def active_sessions(self, key_ids: tuple[str, ...], now: int) -> int: ...

    def projection(
        self, tenant_id: str, kind: str
    ) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...]]: ...

    def directory_size(self, metric: str) -> int | None: ...

    def file_sizes(self) -> dict[str, int]: ...
