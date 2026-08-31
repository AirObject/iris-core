"""Framework-neutral ports shared by application services.

Storage adapters implement these protocols; services depend on them, never on
SQLite directly (ADR-0007).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

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
