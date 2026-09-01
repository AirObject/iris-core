"""Framework-neutral ports shared by application services.

Storage adapters implement these protocols; services depend on them, never on
SQLite directly (ADR-0007).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

from iris_memory_core.domain.event import (
    DEFAULT_MAX_DELIVERY_ATTEMPTS,
    CognitiveEventCurrent,
    CognitiveEventRevision,
)
from iris_memory_core.domain.focus import FocusItemCurrent, FocusRevision
from iris_memory_core.domain.identity import (
    BindingMethod,
    BindingState,
    EntityKind,
    EntityState,
    ExternalIdentityKey,
    FieldAuthority,
)
from iris_memory_core.domain.jobs import NewOutboxJob, OutboxJob
from iris_memory_core.domain.model import (
    Agent,
    AttributeWrite,
    AuditEvent,
    Binding,
    Entity,
    EntityRedirect,
    ExternalIdentity,
    IdempotencyRecord,
    IdempotentResult,
    IdentityAttribute,
    PersonaRevision,
    ResourceLink,
    Session,
    Space,
    SpaceGroup,
    SpaceGroupBinding,
    Tenant,
    Tombstone,
    WatermarkState,
)
from iris_memory_core.domain.note import NoteCurrent, NoteRevision
from iris_memory_core.domain.observation import (
    GapPolicy,
    ObservationDraft,
    StoredObservation,
)
from iris_memory_core.domain.recent import BuiltProjection, StoredGeneration
from iris_memory_core.domain.schedule import ScheduleRecord, TickRecord
from iris_memory_core.domain.state import (
    StateEntry,
    StateNamespacePolicy,
    StateRecord,
    StateRevision,
)
from iris_memory_core.domain.surface import LeaseView, SurfaceMode
from iris_memory_core.domain.task import (
    TaskCurrent,
    TaskDependencyEdge,
    TaskRevision,
    TaskStepCurrent,
    TaskStepRevision,
    TaskTriggerCurrent,
    TaskTriggerRevision,
    TriggerOccurrence,
)


class Clock(Protocol):
    def now(self) -> datetime: ...

    def now_us(self) -> int: ...


class SystemClock:
    """Wall clock in UTC; microseconds come from the same reading (§4.2)."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_us(self) -> int:
        return time.time_ns() // 1000


class MonotonicClock(Protocol):
    """Injectable monotonic clock for schedule/tick observations (§17.3)."""

    def monotonic_us(self) -> int: ...


class SystemMonotonicClock:
    def monotonic_us(self) -> int:
        return time.monotonic_ns() // 1000


class FixedMonotonicClock:
    """Test double: manually advanced monotonic time (sleep/restart tests)."""

    def __init__(self, start_us: int = 0) -> None:
        self._us = start_us

    def monotonic_us(self) -> int:
        return self._us

    def advance(self, delta_us: int) -> None:
        self._us += delta_us


class IdentifierGenerator(Protocol):
    def new(self) -> uuid.UUID: ...


class Uuid7Generator:
    """UUIDv7 identifiers per §4.1, stdlib-only.

    Millisecond-sorted with random tail; strict intra-millisecond ordering is
    provided by revisions and watermarks, not by ids.
    """

    def new(self) -> uuid.UUID:
        buffer = uuid.uuid4().bytes  # 16 random bytes as the base
        timestamp_ms = time.time_ns() // 1_000_000
        packed = timestamp_ms.to_bytes(6, "big")
        buffer = packed + buffer[6:]
        raw = bytearray(buffer)
        raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
        raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
        return uuid.UUID(bytes=bytes(raw))


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


class OutboxSurface(Protocol):
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


class StateSurface(Protocol):
    """Repository surface for state namespace policies, records, revisions."""

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


class FocusSurface(Protocol):
    """Repository surface for focus current rows and immutable revisions."""

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


class NoteSurface(Protocol):
    """Repository surface for note current rows and immutable revisions."""

    def get(self, note_id: str) -> NoteCurrent: ...
    def get_revision(self, revision_id: str) -> NoteRevision: ...
    def current_revision_row(self, note_id: str) -> NoteRevision: ...
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
        title: str,
        status: str,
        importance: float,
        review_after_us: int | None,
        snooze_until_us: int | None,
        due_at_us: int | None,
        content_hash: str,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        note_id: str,
        tenant_id: str,
        revision: int,
        kind: str,
        title: str,
        body: str,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        importance: float,
        status: str,
        review_after_us: int | None,
        snooze_until_us: int | None,
        due_at_us: int | None,
        promotion_target_type: str | None,
        promotion_target_id: str | None,
        archived_us: int | None,
        content_hash: str,
        created_by: str,
    ) -> str: ...
    def set_initial_pointer(self, note_id: str, revision_id: str) -> int: ...
    def advance_pointer(
        self,
        note_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        importance: float | None = None,
        review_after_us: int | None = None,
        snooze_until_us: int | None = None,
        snooze_until_clear: bool = False,
        due_at_us: int | None = None,
        archived_us_set: bool = False,
        archived_us: int | None = None,
        archived_us_clear: bool = False,
    ) -> int: ...
    def raise_pointer_mismatch(self, note_id: str, expected: int) -> None: ...
    def list_notes(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: tuple[str, ...] = ("inbox", "pinned", "snoozed"),
        kind: str | None = None,
        limit: int = 100,
    ) -> Sequence[NoteCurrent]: ...
    def review_due(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 500
    ) -> Sequence[NoteCurrent]: ...
    def duplicate_candidates(
        self, note: NoteCurrent, *, limit: int = 50
    ) -> Sequence[NoteCurrent]: ...
    def history(self, note_id: str, *, limit: int = 100) -> Sequence[NoteRevision]: ...


class TaskSurface(Protocol):
    """Repository surface for tasks, steps, dependencies, triggers, occurrences."""

    def get_task(self, task_id: str) -> TaskCurrent: ...
    def get_task_revision(self, revision_id: str) -> TaskRevision: ...
    def current_task_revision_row(self, task_id: str) -> TaskRevision: ...
    def insert_task(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        parent_task_id: str | None,
        title: str,
        owner_kind: str,
        owner_entity_id: str | None,
        status: str,
        priority: int,
        due_at_us: int | None,
    ) -> str: ...
    def insert_task_revision(
        self,
        *,
        task_id: str,
        tenant_id: str,
        revision: int,
        title: str,
        goal: str,
        owner_kind: str,
        owner_entity_id: str | None,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        status: str,
        priority: int,
        next_action: str | None,
        progress_note: str | None,
        due_at_us: int | None,
        completed_us: int | None,
        created_by: str,
    ) -> str: ...
    def set_initial_task_pointer(self, task_id: str, revision_id: str) -> int: ...
    def advance_task_pointer(
        self,
        task_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        next_action: str | None = None,
        next_action_set: bool = False,
        due_at_us: int | None = None,
        due_at_set: bool = False,
        completed_us: int | None = None,
        completed_us_set: bool = False,
    ) -> int: ...
    def raise_task_pointer_mismatch(self, task_id: str, expected: int) -> None: ...
    def list_tasks(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: tuple[str, ...] = ("proposed", "active", "waiting", "blocked"),
        limit: int = 100,
    ) -> Sequence[TaskCurrent]: ...
    def due_tasks(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 50
    ) -> Sequence[TaskCurrent]: ...
    def task_history(self, task_id: str, *, limit: int = 100) -> Sequence[TaskRevision]: ...
    def get_step(self, step_id: str) -> TaskStepCurrent: ...
    def get_step_revision(self, revision_id: str) -> TaskStepRevision: ...
    def current_step_revision_row(self, step_id: str) -> TaskStepRevision: ...
    def find_step_by_key(self, task_id: str, stable_key: str) -> TaskStepCurrent | None: ...
    def insert_step(
        self,
        *,
        task_id: str,
        tenant_id: str,
        stable_key: str,
        title: str,
        ordinal: int,
        status: str,
    ) -> str: ...
    def insert_step_revision(
        self,
        *,
        step_id: str,
        task_id: str,
        tenant_id: str,
        revision: int,
        stable_key: str,
        title: str,
        description: str | None,
        privacy_labels: tuple[str, ...],
        status: str,
        ordinal: int,
        expected_effect: str | None,
        completion_evidence_refs: tuple[dict[str, object], ...],
        started_us: int | None,
        completed_us: int | None,
        created_by: str,
    ) -> str: ...
    def set_initial_step_pointer(self, step_id: str, revision_id: str) -> int: ...
    def advance_step_pointer(
        self,
        step_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        started_us: int | None = None,
        started_us_set: bool = False,
        completed_us: int | None = None,
        completed_us_set: bool = False,
    ) -> int: ...
    def raise_step_pointer_mismatch(self, step_id: str, expected: int) -> None: ...
    def steps_for_task(self, task_id: str) -> Sequence[TaskStepCurrent]: ...
    def step_history(self, step_id: str, *, limit: int = 100) -> Sequence[TaskStepRevision]: ...
    def dependencies_for_task(self, task_id: str) -> Sequence[TaskDependencyEdge]: ...
    def get_dependency(self, dependency_id: str) -> TaskDependencyEdge: ...
    def insert_dependency(
        self,
        *,
        task_id: str,
        tenant_id: str,
        predecessor_step_id: str,
        successor_step_id: str,
        condition: str,
    ) -> str: ...
    def insert_dependency_revision(
        self,
        *,
        dependency_id: str,
        task_id: str,
        tenant_id: str,
        revision: int,
        predecessor_step_id: str,
        successor_step_id: str,
        condition: str,
        created_by: str,
    ) -> str: ...
    def set_initial_dependency_pointer(self, dependency_id: str, revision_id: str) -> int: ...
    def dependency_revision_number(self, revision_id: str) -> int: ...
    def get_trigger(self, trigger_id: str) -> TaskTriggerCurrent: ...
    def get_trigger_revision(self, revision_id: str) -> TaskTriggerRevision: ...
    def current_trigger_revision_row(self, trigger_id: str) -> TaskTriggerRevision: ...
    def insert_trigger(
        self,
        *,
        task_id: str,
        task_step_id: str | None,
        tenant_id: str,
        agent_id: str,
        kind: str,
        timezone: str,
        catch_up_policy: str,
        misfire_grace_us: int,
        max_occurrences_per_run: int,
        enabled: bool,
        next_fire_at_us: int | None,
    ) -> str: ...
    def insert_trigger_revision(
        self,
        *,
        trigger_id: str,
        task_id: str,
        tenant_id: str,
        revision: int,
        kind: str,
        task_step_id: str | None,
        schedule_spec: dict[str, object] | None,
        condition_spec: dict[str, object] | None,
        timezone: str,
        catch_up_policy: str,
        misfire_grace_us: int,
        max_occurrences_per_run: int,
        enabled: bool,
        created_by: str,
    ) -> str: ...
    def set_initial_trigger_pointer(self, trigger_id: str, revision_id: str) -> int: ...
    def advance_trigger_pointer(
        self,
        trigger_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        enabled: bool | None = None,
    ) -> int: ...
    def raise_trigger_pointer_mismatch(self, trigger_id: str, expected: int) -> None: ...
    def set_trigger_schedule_state(
        self,
        trigger_id: str,
        *,
        expected_fire_at_us: int | None,
        next_fire_at_us: int | None,
        last_scan_us: int | None = None,
    ) -> int: ...
    def triggers_for_scan(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 500
    ) -> Sequence[TaskTriggerCurrent]: ...
    def triggers_for_task(self, task_id: str) -> Sequence[TaskTriggerCurrent]: ...
    def insert_occurrence(
        self,
        *,
        trigger_id: str,
        trigger_revision: int,
        tenant_id: str,
        scheduled_at_us: int,
        occurrence_key: str,
        status: str,
        reason_code: str | None = None,
    ) -> tuple[str, bool]: ...
    def get_occurrence(self, occurrence_id: str) -> TriggerOccurrence: ...
    def attach_occurrence_event(self, occurrence_id: str, *, cognitive_event_id: str) -> int: ...
    def occurrences_for_trigger(
        self, trigger_id: str, *, limit: int = 200
    ) -> Sequence[TriggerOccurrence]: ...


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


class Transaction(Protocol):
    """Repository surface available inside one unit of work."""

    @property
    def observations(self) -> ObservationSurface: ...

    @property
    def outbox(self) -> OutboxSurface: ...

    @property
    def schedules(self) -> ScheduleSurface: ...

    @property
    def surfaces(self) -> SurfaceLeaseSurface: ...

    @property
    def recent(self) -> RecentContextSurface: ...

    @property
    def states(self) -> StateSurface: ...

    @property
    def focus(self) -> FocusSurface: ...

    @property
    def notes(self) -> NoteSurface: ...

    @property
    def tasks(self) -> TaskSurface: ...

    @property
    def events(self) -> CognitiveEventSurface: ...

    # --- tenants, agents, spaces -------------------------------------
    def insert_tenant(self, tenant_id: str, *, status: str) -> Tenant: ...
    def get_tenant(self, tenant_id: str) -> Tenant: ...
    def insert_agent(self, tenant_id: str, display_name: str, *, actor: str) -> Agent: ...
    def get_agent(self, agent_id: str) -> Agent: ...
    def get_persona_revision(self, revision_id: str) -> PersonaRevision: ...
    def insert_space_group(
        self,
        tenant_id: str,
        name: str,
        description: str,
        *,
        actor: str,
        reason_code: str,
    ) -> SpaceGroup: ...
    def get_space_group(self, space_group_id: str) -> SpaceGroup: ...
    def update_space_group(
        self,
        space_group_id: str,
        *,
        name: str,
        description: str,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> SpaceGroup: ...
    def insert_space(
        self,
        tenant_id: str,
        kind: str,
        *,
        agent_id: str | None = None,
        actor: str = "system",
    ) -> Space: ...
    def get_space(self, space_id: str) -> Space: ...
    def bind_space_to_group(
        self,
        tenant_id: str,
        space_id: str,
        space_group_id: str,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> SpaceGroupBinding: ...
    def unbind_space_from_group(
        self,
        tenant_id: str,
        space_id: str,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> SpaceGroupBinding | None: ...
    def get_active_group_binding(self, space_id: str) -> SpaceGroupBinding | None: ...
    def list_group_bindings(self, space_group_id: str) -> tuple[SpaceGroupBinding, ...]: ...
    def insert_session(self, tenant_id: str, space_id: str, *, actor: str) -> Session: ...
    def get_session(self, session_id: str) -> Session: ...

    # --- identity registry -------------------------------------------
    def insert_entity(
        self,
        tenant_id: str,
        kind: EntityKind,
        *,
        display_name: str = "",
        privacy_labels: Sequence[str] = (),
        actor: str,
    ) -> Entity: ...
    def get_entity(self, entity_id: str) -> Entity: ...
    def update_entity_state(
        self,
        entity_id: str,
        state: EntityState,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> Entity: ...
    def insert_external_identity(
        self, key: ExternalIdentityKey, *, entity_id: str | None
    ) -> ExternalIdentity: ...
    def get_external_identity(self, external_identity_id: str) -> ExternalIdentity: ...
    def find_external_identity(self, key: ExternalIdentityKey) -> ExternalIdentity | None: ...
    def insert_binding(
        self,
        tenant_id: str,
        external_identity_id: str,
        entity_id: str,
        *,
        method: BindingMethod,
        confidence: float,
        proof_digest: str,
        actor: str,
        reason_code: str,
    ) -> Binding: ...
    def get_binding(self, binding_id: str) -> Binding: ...
    def transition_binding(
        self,
        binding_id: str,
        target: BindingState,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
        note: str = "",
    ) -> Binding: ...
    def binding_state_events(self, binding_id: str) -> tuple[tuple[int, BindingState], ...]: ...
    def verified_binding_for(self, external_identity_id: str) -> Binding | None: ...
    def bindings_for(self, external_identity_id: str) -> tuple[Binding, ...]: ...
    def insert_entity_redirect(
        self,
        tenant_id: str,
        from_entity_id: str,
        to_entity_id: str,
        *,
        actor: str,
        reason_code: str,
    ) -> EntityRedirect: ...
    def redirect_map(self, tenant_id: str) -> dict[str, str]: ...
    def get_entity_redirect(self, from_entity_id: str) -> EntityRedirect | None: ...
    def redirects_active_at(self, tenant_id: str, at_us: int) -> dict[str, str]: ...
    def record_identity_attribute(
        self,
        tenant_id: str,
        entity_id: str,
        field: str,
        value: str,
        authority: FieldAuthority,
        source_ref: str,
        *,
        actor: str,
        effective_us: int | None = None,
    ) -> AttributeWrite: ...
    def current_identity_attribute(
        self, tenant_id: str, entity_id: str, field: str
    ) -> IdentityAttribute | None: ...
    def conflicted_attributes(self, entity_id: str) -> tuple[IdentityAttribute, ...]: ...

    # --- consistency ledger ------------------------------------------
    def audit(
        self,
        *,
        tenant_id: str,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        reason_code: str,
        details: dict[str, object] | None = None,
        revision: int | None = None,
    ) -> AuditEvent: ...
    def advance_watermark(
        self,
        tenant_id: str,
        agent_id: str,
        entries: Sequence[tuple[str, str, int]],
    ) -> int: ...
    def watermark(self, tenant_id: str, agent_id: str) -> WatermarkState | None: ...
    def record_tombstone(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        reason_code: str,
        deleted_by: str,
    ) -> Tombstone: ...
    def is_tombstoned(self, tenant_id: str, resource_type: str, resource_id: str) -> bool: ...
    def insert_resource_link(
        self,
        *,
        tenant_id: str,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
        relation: str,
    ) -> ResourceLink: ...
    def links_for_source(
        self,
        tenant_id: str,
        source_type: str,
        source_id: str,
        *,
        target_type: str | None = None,
        relation: str | None = None,
    ) -> Sequence[ResourceLink]: ...
    def tombstone_watermark(self) -> int: ...


class UnitOfWork(Protocol):
    """Owns short transactions; the process runs a single writer gate."""

    def write(self) -> AbstractContextManager[Transaction]: ...

    def read(self) -> AbstractContextManager[Transaction]: ...


class IdempotencyRunner(Protocol):
    def begin(
        self,
        *,
        tenant_id: str,
        app_instance_id: str,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> IdempotencyRecord: ...
    def complete(
        self,
        record: IdempotencyRecord,
        *,
        response_code: str,
        response_body: str,
        resource_refs: Sequence[str],
        transaction_ref: str,
    ) -> IdempotencyRecord: ...
    def recover_expired(self) -> tuple[IdempotencyRecord, ...]: ...
    def load(
        self,
        *,
        tenant_id: str,
        app_instance_id: str,
        operation: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None: ...
    def run(
        self,
        *,
        tenant_id: str,
        app_instance_id: str,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        execute: Callable[[Transaction], tuple[str, str, Sequence[str]]],
    ) -> IdempotentResult: ...
