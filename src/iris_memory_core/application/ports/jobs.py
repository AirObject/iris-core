"""Application ports for jobs; storage and provider adapters implement these contracts."""

from __future__ import annotations

from typing import Protocol

from iris_memory_core.domain.jobs import NewOutboxJob, OutboxJob
from iris_memory_core.domain.schedule import ScheduleRecord, TickRecord


class OutboxSurface(Protocol):
    def settle_unleased_kind(
        self,
        tenant_id: str,
        job_kind: str,
        *,
        reason_code: str,
        now_us: int,
        payload_version: int = 1,
    ) -> int: ...

    def unsettled_tenant_job_count(self, tenant_id: str, job_kind: str) -> int: ...

    def enqueue(self, job: NewOutboxJob) -> tuple[OutboxJob, bool]: ...
    def replay(self, original: OutboxJob, *, available_at_us: int) -> OutboxJob: ...
    def release_expired_leases(self, now_us: int, *, requeue_delay_us: int) -> int: ...
    def claim(
        self,
        *,
        owner: str,
        now_us: int,
        lease_us: int,
        batch_size: int,
        enabled_kinds: tuple[str, ...] | frozenset[str],
        max_per_tenant: int,
        supported_payload_version: int = 1,
    ) -> tuple[OutboxJob, ...]: ...
    def heartbeat(
        self, job_id: str, *, owner: str, generation: int, now_us: int, extend_us: int
    ) -> int: ...
    def complete(
        self,
        job_id: str,
        *,
        owner: str,
        generation: int,
        now_us: int,
        source_revision: int,
    ) -> int: ...
    def mark_retryable(
        self,
        job_id: str,
        *,
        owner: str,
        generation: int,
        now_us: int,
        available_at_us: int,
        error_code: str,
        source_revision: int,
    ) -> int: ...
    def mark_dead(
        self,
        job_id: str,
        *,
        owner: str,
        generation: int,
        now_us: int,
        error_code: str,
        source_revision: int,
    ) -> int: ...
    def get(self, job_id: str) -> OutboxJob: ...
    def active_replay(self, original_id: str) -> OutboxJob | None: ...
    def by_dedupe_key(self, tenant_id: str, dedupe_key: str) -> OutboxJob | None: ...
    def by_coalesce_key(
        self, tenant_id: str, agent_id: str | None, job_kind: str, coalesce_key: str
    ) -> OutboxJob | None: ...
    def leased_count(self, owner: str) -> int: ...
    def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
        job_kind: str | None = None,
        limit: int = 100,
    ) -> tuple[OutboxJob, ...]: ...
    def pressure(
        self,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        *,
        lane: str | None = None,
    ) -> dict[str, int]: ...
    def oldest_pending_us(self) -> int | None: ...
    def unsettled_job_count(self, tenant_id: str, agent_id: str | None, job_kind: str) -> int: ...

    def unsettled_null_agent_job_count(self, tenant_id: str, job_kind: str) -> int: ...
    def status_counts(self) -> dict[str, int]: ...
    def tick_completion(self, tick_id: str, *, now_us: int, error_code: str | None) -> None: ...


class ScheduleSurface(Protocol):
    def insert(self, record: ScheduleRecord) -> ScheduleRecord: ...
    def get(self, schedule_id: str) -> ScheduleRecord: ...
    def due(self, now_us: int, *, limit: int = 100) -> tuple[ScheduleRecord, ...]: ...
    def due_lag_by_kind(self, now_us: int) -> dict[str, int]: ...
    def advance(
        self,
        schedule_id: str,
        *,
        expected_revision: int,
        next_tick_at_us: int,
        last_tick_at_us: int | None,
    ) -> ScheduleRecord: ...
    def set_enabled(
        self, schedule_id: str, *, enabled: bool, expected_revision: int
    ) -> ScheduleRecord: ...
    def record_tick(
        self,
        *,
        schedule_id: str,
        scheduled_at_us: int,
        occurrence_key: str,
        status: str,
        observed_wall_us: int | None,
        observed_monotonic_delta_us: int | None,
        outbox_id: str | None,
        reason_code: str | None,
    ) -> TickRecord: ...
    def get_tick(self, tick_id: str) -> TickRecord: ...
    def tick_by_occurrence(self, schedule_id: str, occurrence_key: str) -> TickRecord: ...
    def attach_outbox(self, tick_id: str, outbox_id: str) -> None: ...
