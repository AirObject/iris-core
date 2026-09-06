"""Framework-neutral ports shared by application services.

Storage adapters implement these protocols; services depend on them, never on
SQLite directly (ADR-0007).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, Protocol

from iris_memory_core.application.console.ports import ConsoleReadRepository, ConsoleRepository
from iris_memory_core.domain.event import (
    DEFAULT_MAX_DELIVERY_ATTEMPTS,
    CognitiveEventCurrent,
    CognitiveEventRevision,
)
from iris_memory_core.domain.focus import FocusItemCurrent, FocusRevision
from iris_memory_core.domain.fts import (
    FtsCurrentPointer,
    FtsDocumentInput,
    FtsDocumentRecord,
    FtsGenerationRecord,
)
from iris_memory_core.domain.graph import (
    GraphCurrentPointer,
    GraphEdgeDraft,
    GraphEdgeRecord,
    GraphGenerationRecord,
)
from iris_memory_core.domain.identity import (
    BindingMethod,
    BindingState,
    EntityKind,
    EntityState,
    ExternalIdentityKey,
    FieldAuthority,
)
from iris_memory_core.domain.jobs import NewOutboxJob, OutboxJob
from iris_memory_core.domain.memory import (
    ArtifactRecord,
    ClaimCurrent,
    ClaimRevision,
    EpisodeCurrent,
    EpisodeRevision,
    EvidenceRecord,
    RelationCurrent,
    RelationRevision,
)
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
from iris_memory_core.domain.persona import (
    PersonaPolicy,
    PersonaProposal,
    PersonaProposalStatus,
    PersonaRecord,
    PersonaState,
)
from iris_memory_core.domain.profile import (
    ProfileCurrentPointer,
    ProfileFieldDraft,
    ProfileFieldRecord,
    ProfileGenerationRecord,
    ProfileSubjectKey,
    ProfileSubjectRecord,
)
from iris_memory_core.domain.recent import BuiltProjection, StoredGeneration
from iris_memory_core.domain.reflection import (
    Candidate,
    CandidateRecord,
    ConsolidationWindow,
    CredentialRecord,
    ProviderKind,
    ProviderOutcome,
    ReflectionRecord,
    ServiceEvent,
    VersionSet,
)
from iris_memory_core.domain.retention import ForgetRequest, LegalHold, RetentionPolicy
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
from iris_memory_core.domain.vector import (
    VectorCurrentPointer,
    VectorGenerationRecord,
    VectorIdMapRecord,
    VectorSpaceConfig,
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


class ExtractionProvider(Protocol):
    """Untrusted structured candidate producer; no provider SDK leaks inward."""

    model_id: str

    def extract(
        self,
        observations: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]: ...


class SummarizationProvider(Protocol):
    model_id: str

    def summarize(
        self,
        observations: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class ReconciliationProvider(Protocol):
    model_id: str

    def reconcile(
        self,
        candidates: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]: ...


class PersonaEvolutionProvider(Protocol):
    model_id: str

    def propose(
        self,
        evidence: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]: ...


class CognitiveProviderRunner(Protocol):
    """Governed provider admission; adapters own timeout/budget/circuit state."""

    def call(
        self,
        kind: ProviderKind,
        *,
        tenant_id: str,
        agent_id: str | None,
        request_material: Mapping[str, object],
        estimated_cost_microunits: int,
        invoke: Callable[[float], Any],
    ) -> tuple[Any, ProviderOutcome]: ...


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
        include_tombstoned: bool = False,
        limit: int = 100,
        cursor_created_us: int | None = None,
        cursor_id: str | None = None,
    ) -> Sequence[NoteCurrent]: ...
    def review_due(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 500
    ) -> Sequence[NoteCurrent]: ...
    def duplicate_candidates(
        self, note: NoteCurrent, *, limit: int = 50
    ) -> Sequence[NoteCurrent]: ...
    def history(self, note_id: str, *, limit: int = 100) -> Sequence[NoteRevision]: ...
    def notes_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]: ...
    def notes_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]: ...
    def erase_content(self, note_id: str, *, now_us: int) -> None: ...


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


class EpisodeSurface(Protocol):
    """Repository surface for episode current rows and immutable revisions."""

    def get(self, episode_id: str) -> EpisodeCurrent: ...
    def get_revision(self, revision_id: str) -> EpisodeRevision: ...
    def current_revision_row(self, episode_id: str) -> EpisodeRevision: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        title: str,
        status: str,
        importance: float,
        started_at_us: int | None,
        ended_at_us: int | None,
        extractor_version: str | None,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        episode_id: str,
        tenant_id: str,
        revision: int,
        title: str,
        summary: str,
        participant_entity_ids: tuple[str, ...],
        observation_refs: tuple[dict[str, object], ...],
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        status: str,
        importance: float,
        valence: float | None,
        arousal: float | None,
        started_at_us: int | None,
        ended_at_us: int | None,
        extractor_version: str | None,
        content_hash: str,
        created_by: str,
    ) -> str: ...
    def set_initial_pointer(self, episode_id: str, revision_id: str) -> int: ...
    def advance_pointer(
        self,
        episode_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        importance: float | None = None,
    ) -> int: ...
    def raise_pointer_mismatch(self, episode_id: str, expected: int) -> None: ...
    def history(self, episode_id: str, *, limit: int = 100) -> Sequence[EpisodeRevision]: ...
    def list_episodes(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: Sequence[str] = ("open", "sealed"),
        limit: int = 100,
        cursor_updated_us: int | None = None,
        cursor_id: str | None = None,
    ) -> Sequence[EpisodeCurrent]: ...
    def episodes_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]: ...
    def episodes_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]: ...
    def episodes_with_participant(
        self, tenant_id: str, subject_entity_id: str, *, agent_id: str | None = None
    ) -> tuple[str, ...]: ...
    def erase_content(self, episode_id: str, *, now_us: int) -> None: ...


class ClaimSurface(Protocol):
    """Repository surface for claims, revisions and evidence."""

    def get(self, claim_id: str) -> ClaimCurrent: ...
    def get_revision(self, revision_id: str) -> ClaimRevision: ...
    def current_revision_row(self, claim_id: str) -> ClaimRevision: ...
    def find_live_by_dedup_key(self, tenant_id: str, dedup_key: str) -> ClaimCurrent | None: ...
    def revision_current_at(self, claim_id: str, as_of_us: int) -> ClaimRevision | None: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        subject_entity_id: str,
        predicate: str,
        category: str,
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        source_authority: str,
        valid_from_us: int | None,
        valid_until_us: int | None,
        evidence_count: int,
        dedup_key: str,
        recorded_at_us: int,
        extractor_version: str | None,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        claim_id: str,
        tenant_id: str,
        revision: int,
        subject_entity_id: str,
        predicate: str,
        value_json: str,
        canonical_text: str,
        category: str,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        source_authority: str,
        valid_from_us: int | None,
        valid_until_us: int | None,
        recorded_at_us: int,
        extractor_version: str | None,
        content_hash: str,
        created_by: str,
    ) -> str: ...
    def set_initial_pointer(self, claim_id: str, revision_id: str) -> int: ...
    def stamp_revision_superseded(self, revision_id: str, *, superseded_at_us: int) -> int: ...
    def advance_pointer(
        self,
        claim_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        confidence: float | None = None,
        importance: float | None = None,
        accessibility: float | None = None,
        source_authority: str | None = None,
        valid_from_us: int | None = None,
        valid_from_set: bool = False,
        valid_until_us: int | None = None,
        valid_until_set: bool = False,
        superseded_at_us: int | None = None,
        superseded_at_set: bool = False,
        evidence_count: int | None = None,
    ) -> int: ...
    def raise_pointer_mismatch(self, claim_id: str, expected: int) -> None: ...
    def insert_evidence(
        self,
        *,
        claim_id: str,
        tenant_id: str,
        source_type: str,
        source_id: str,
        source_revision: int | None,
        relation: str,
        source_authority: str,
        evidence_span: str | None,
        created_by: str,
        recorded_at_us: int | None = None,
    ) -> tuple[EvidenceRecord, bool]: ...
    def evidence_for_claim(self, claim_id: str) -> Sequence[EvidenceRecord]: ...
    def invalidate_evidence_for_source(
        self, tenant_id: str, source_type: str, source_id: str, *, now_us: int
    ) -> int: ...
    def valid_evidence_count(self, claim_id: str) -> int: ...
    def recount_evidence(self, claim_id: str) -> int: ...
    def all_current_claim_pairs(
        self,
        tenant_id: str,
        *,
        statuses: Sequence[str] = ...,
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]: ...

    def claims_for_group(
        self, tenant_id: str, space_group_id: str, *, categories: Sequence[str]
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]: ...

    def claims_targeting_entity(
        self, tenant_id: str, entity_id: str
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]: ...

    def claims_for_subject_predicate(
        self, tenant_id: str, agent_id: str, subject_entity_id: str, predicate: str | None
    ) -> Sequence[ClaimCurrent]: ...
    def claims_for_subject(
        self, tenant_id: str, subject_entity_id: str, *, agent_id: str | None = None
    ) -> Sequence[ClaimCurrent]: ...
    def claims_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> Sequence[ClaimCurrent]: ...
    def claims_for_space(self, tenant_id: str, space_id: str) -> Sequence[ClaimCurrent]: ...
    def claims_citing_source(
        self, tenant_id: str, source_type: str, source_id: str
    ) -> Sequence[ClaimCurrent]: ...
    def erase_content(self, claim_id: str, *, now_us: int) -> None: ...
    def set_history_available_from(self, claim_id: str, available_from_us: int) -> None: ...
    def prune_revisions(self, claim_id: str, *, keep: int) -> int: ...
    def earliest_kept_recorded_at(self, claim_id: str) -> int: ...
    def history(self, claim_id: str, *, limit: int = 100) -> Sequence[ClaimRevision]: ...
    def search_page(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        statuses: Sequence[str],
        subject_entity_id: str | None = None,
        predicate: str | None = None,
        category: str | None = None,
        valid_at_us: int | None = None,
        as_of_us: int | None = None,
        exclude_ids: Collection[str] = (),
        cursor_updated_us: int | None = None,
        cursor_id: str | None = None,
        limit: int = 100,
        scope_mode: str = "request",
    ) -> Sequence[tuple[ClaimCurrent, ClaimRevision]]: ...


class RelationSurface(Protocol):
    """Repository surface for canonical relations."""

    def get(self, relation_id: str) -> RelationCurrent: ...

    def all_current_relation_pairs(
        self,
        tenant_id: str,
        *,
        statuses: Sequence[str] = ...,
    ) -> tuple[tuple[RelationCurrent, RelationRevision], ...]: ...
    def get_revision(self, revision_id: str) -> RelationRevision: ...
    def current_revision_row(self, relation_id: str) -> RelationRevision: ...
    def find_live(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        space_group_id: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        valid_from_us: int | None = None,
        valid_until_us: int | None = None,
    ) -> RelationCurrent | None: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        valid_from_us: int | None,
        valid_until_us: int | None,
        evidence_count: int,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        relation_id: str,
        tenant_id: str,
        revision: int,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        privacy_labels: tuple[str, ...],
        evidence_refs: tuple[dict[str, object], ...],
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        valid_from_us: int | None,
        valid_until_us: int | None,
        content_hash: str,
        created_by: str,
    ) -> str: ...
    def set_initial_pointer(self, relation_id: str, revision_id: str) -> int: ...
    def stamp_revision_superseded(self, revision_id: str, *, superseded_at_us: int) -> int: ...
    def advance_pointer(
        self,
        relation_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        confidence: float | None = None,
        importance: float | None = None,
        accessibility: float | None = None,
        valid_from_us: int | None = None,
        valid_from_set: bool = False,
        valid_until_us: int | None = None,
        valid_until_set: bool = False,
        evidence_count: int | None = None,
    ) -> int: ...
    def raise_pointer_mismatch(self, relation_id: str, expected: int) -> None: ...
    def history(self, relation_id: str, *, limit: int = 100) -> Sequence[RelationRevision]: ...
    def relations_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]: ...
    def relations_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]: ...
    def relations_for_entity(
        self, tenant_id: str, entity_id: str, *, agent_id: str | None = None
    ) -> tuple[str, ...]: ...
    def erase_content(self, relation_id: str, *, now_us: int) -> None: ...
    def insert_evidence(
        self,
        *,
        relation_id: str,
        tenant_id: str,
        source_type: str,
        source_id: str,
        source_revision: int | None,
        relation: str,
        source_authority: str,
        evidence_span: str | None,
        created_by: str,
        recorded_at_us: int | None = None,
    ) -> bool: ...
    def evidence_for_relation(self, relation_id: str) -> Sequence[EvidenceRecord]: ...
    def invalidate_evidence_for_source(
        self, tenant_id: str, source_type: str, source_id: str, *, now_us: int
    ) -> int: ...
    def valid_evidence_count(self, relation_id: str) -> int: ...
    def recount_evidence(self, relation_id: str) -> int: ...
    def relations_citing_source(
        self, tenant_id: str, source_type: str, source_id: str
    ) -> tuple[str, ...]: ...


class ArtifactSurface(Protocol):
    """Repository surface for artifact metadata and the controlled blob store."""

    def get(self, artifact_id: str) -> ArtifactRecord: ...
    def find_active_by_hash(
        self,
        tenant_id: str,
        scope_key: str,
        content_hash: str,
        storage_kind: str,
        privacy_labels: tuple[str, ...] = (),
    ) -> ArtifactRecord | None: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        media_type: str,
        storage_kind: str,
        locator: str,
        content: bytes | None,
        content_hash: str,
        size_bytes: int,
        privacy_labels: tuple[str, ...],
        source_ref: dict[str, object] | None,
        status: str,
        artifact_id: str | None = None,
    ) -> str: ...
    def next_artifact_id(self) -> str: ...
    def set_status(self, artifact_id: str, status: str) -> int: ...
    def bump_refcount(self, artifact_id: str, delta: int) -> int: ...
    def inline_content(self, artifact_id: str) -> bytes: ...
    def write_blob(self, locator: str, payload: bytes) -> object: ...
    def read_blob(self, locator: str, *, expected_hash: str, expected_size: int) -> bytes: ...
    def blob_exists(self, locator: str) -> bool: ...
    def unlink_blob(self, locator: str) -> bool: ...
    def tombstoned_local_blob_locators(
        self, tenant_id: str, *, tombstone_seq_lo: int, tombstone_seq_hi: int
    ) -> tuple[str, ...]: ...
    def tombstone_row(self, artifact_id: str, *, now_us: int) -> None: ...
    def artifacts_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]: ...
    def artifacts_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]: ...
    def all_active_ids(self, tenant_id: str) -> tuple[str, ...]: ...


class RetentionSurface(Protocol):
    """Repository surface for policies, legal holds and the forget ledger."""

    def upsert_policy(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        action: str,
        privacy_label: str | None,
        threshold_days: int,
        created_by: str,
    ) -> RetentionPolicy: ...
    def set_policy_enabled(self, policy_id: str, *, enabled: bool) -> int: ...
    def list_policies(self, tenant_id: str) -> Sequence[RetentionPolicy]: ...
    def insert_hold(
        self,
        *,
        tenant_id: str,
        space_id: str | None,
        session_id: str | None,
        subject_entity_id: str | None,
        agent_id: str | None,
        reason_code: str,
        created_by: str,
    ) -> LegalHold: ...
    def release_hold(self, hold_id: str, *, released_us: int | None = None) -> LegalHold: ...
    def get_hold(self, hold_id: str) -> LegalHold: ...
    def active_holds(self, tenant_id: str) -> Sequence[LegalHold]: ...
    def insert_forget_request(
        self,
        *,
        tenant_id: str,
        selector_key: str,
        selector_json: str,
        reason_code: str,
        requested_by: str,
        created_us: int,
        tombstone_seq_lo: int,
        tombstone_seq_hi: int,
        target_count: int,
        erased_count: int,
        protected_skipped: int,
        held_skipped: int,
        app_instance_id: str = "",
        idempotency_key: str = "",
        erase_content: bool = False,
    ) -> ForgetRequest: ...
    def find_forget_request(
        self,
        tenant_id: str,
        selector_key: str,
        created_us: int,
        *,
        app_instance_id: str = "",
        idempotency_key: str = "",
        reason_code: str = "",
        erase_content: bool = False,
    ) -> ForgetRequest | None: ...
    def ledger_since(self, tenant_id: str, *, created_after_us: int) -> Sequence[ForgetRequest]: ...
    def ledger_max_created_us(self, tenant_id: str) -> int: ...
    def claim_agents(self, tenant_id: str) -> tuple[str, ...]: ...
    def note_agents(self, tenant_id: str) -> tuple[str, ...]: ...
    def episode_agents(self, tenant_id: str) -> tuple[str, ...]: ...
    def live_relation_ids(self, tenant_id: str, *, limit: int = 500) -> tuple[str, ...]: ...
    def stale_observation_ids(self, tenant_id: str, *, before_us: int) -> tuple[str, ...]: ...


class IdentitySurface(Protocol):
    """Repository surface for entities used by the self-subject resolution."""

    def entities_by_id(self, tenant_id: str, entity_ids: Collection[str]) -> dict[str, Entity]: ...

    def all_verified_bindings(self, tenant_id: str) -> tuple[Binding, ...]: ...

    def verified_bindings_for_entity(
        self, tenant_id: str, entity_id: str
    ) -> tuple[Binding, ...]: ...

    def external_identity_ids(self, tenant_id: str) -> set[str]: ...

    def insert_entity(
        self,
        tenant_id: str,
        kind: EntityKind,
        *,
        display_name: str = "",
        privacy_labels: Sequence[str] = (),
        actor: str = "system",
    ) -> Entity: ...

    def insert_entity_redirect(
        self,
        tenant_id: str,
        from_entity_id: str,
        to_entity_id: str,
        *,
        actor: str,
        reason_code: str,
    ) -> EntityRedirect: ...


class FtsSurface(Protocol):
    """Repository surface for the FTS5 projection (ADR-0014 §1-2)."""

    def ensure_index(self) -> bool: ...

    def drop_index(self) -> None: ...

    def projection_state(self) -> str: ...

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None: ...

    def reset_projection(self, *, now_us: int | None = None) -> None: ...

    def insert_generation(
        self,
        *,
        tenant_id: str,
        builder_version: int,
        tokenizer_version: int,
        config_json: str,
        source_watermark: int,
        tombstone_watermark: int,
        document_count: int,
        content_checksum: str,
        now_us: int | None = None,
    ) -> FtsGenerationRecord: ...

    def get_generation(self, generation_id: str) -> FtsGenerationRecord: ...

    def generations_for_tenant(self, tenant_id: str) -> tuple[FtsGenerationRecord, ...]: ...

    def retire_generation(self, generation_id: str) -> int: ...

    def pointer(self, tenant_id: str) -> FtsCurrentPointer | None: ...

    def switch_pointer(
        self, *, tenant_id: str, generation: FtsGenerationRecord, now_us: int | None = None
    ) -> None: ...

    def upsert_document(
        self,
        *,
        generation_id: str,
        document: FtsDocumentInput,
        source_watermark: int,
        tombstone_watermark: int,
        builder_version: int,
        now_us: int | None = None,
    ) -> FtsDocumentRecord: ...

    def invalidate_document(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        now_us: int | None = None,
    ) -> int: ...

    def document_for_resource(
        self, tenant_id: str, resource_type: str, resource_id: str
    ) -> FtsDocumentRecord | None: ...

    def delete_invalid_documents(self, tenant_id: str, *, limit: int = 500) -> int: ...

    def delete_retired_generations(
        self, tenant_id: str, *, keep: int = 2, now_us: int | None = None
    ) -> int: ...

    def documents_for_generation(self, generation_id: str) -> tuple[FtsDocumentRecord, ...]: ...

    def count_documents(self, generation_id: str) -> int: ...

    def sample_query(
        self, generation_id: str, match_expression: str, *, limit: int = 5
    ) -> tuple[int, ...]: ...

    def search(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        generation_id: str,
        match_expression: str,
        space_group_id: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        statuses: Sequence[str] = ("active", "disputed", "open", "sealed", "inbox", "pinned"),
        valid_at_us: int | None = None,
        limit: int = 50,
    ) -> tuple[tuple[FtsDocumentRecord, float], ...]: ...


class EmbeddingProvider(Protocol):
    """Port for external embedding capability (§24.1-24.2, ADR-0015 §2).

    Application services depend on this interface only; adapters own the
    transport, validation, timeouts, rate limiting and circuit breaking.
    Implementations must never log the submitted text — only digests,
    lengths, model names, batch sizes and durations.
    """

    @property
    def space(self) -> VectorSpaceConfig: ...

    def embed_batch(
        self,
        texts: Sequence[str],
        *,
        deadline_monotonic_us: int | None = None,
    ) -> list[Sequence[float]]:
        """Embed a batch; returns one L2-normalized vector per input.

        Raises the adapter's provider error on dimension mismatch, NaN/Inf,
        non-numeric output, timeout, rate limiting or an open circuit.
        ``deadline_monotonic_us`` bounds the call by the caller's remaining
        route budget: the adapter caps each transport call at
        ``min(configured timeout, remaining deadline)`` and fails fast once
        the deadline has passed (the socket timeout, not a background
        thread, bounds the worst case).
        """


class VectorSurface(Protocol):
    """Repository surface for the vector projection (ADR-0015 §3-5)."""

    def projection_state(self) -> str: ...

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None: ...

    def reset_projection(self, *, now_us: int | None = None) -> None: ...

    def allocate_surrogate_ids(self, count: int) -> tuple[int, ...]: ...

    def id_map_get(
        self, tenant_id: str, resource_type: str, resource_id: str
    ) -> VectorIdMapRecord | None: ...

    def id_map_by_surrogate(
        self, tenant_id: str, surrogate_id: int
    ) -> VectorIdMapRecord | None: ...

    def id_map_count(self, tenant_id: str, *, active_only: bool = True) -> int: ...

    def id_map_upsert(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        resource_revision: int,
        surrogate_id: int,
        agent_id: str,
        space: VectorSpaceConfig,
        content_hash: str,
        now_us: int | None = None,
        incorporated_generation: str | None = None,
    ) -> VectorIdMapRecord: ...

    def id_map_stamp_generation(
        self,
        tenant_id: str,
        generation_id: str,
        surrogates: Sequence[int],
    ) -> int: ...

    def id_map_invalidate(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        now_us: int | None = None,
    ) -> int: ...

    def id_map_delete_invalid(self, tenant_id: str, *, limit: int = 500) -> int: ...

    def delta_upsert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        resource_type: str,
        resource_id: str,
        resource_revision: int,
        op: str,
        source_watermark: int,
        now_us: int | None = None,
    ) -> None: ...

    def delta_count(self, tenant_id: str, agent_id: str | None = None) -> int: ...

    def delta_clear(self, tenant_id: str) -> int: ...

    def id_map_invalidate_tombstoned(self, tenant_id: str, *, now_us: int | None = None) -> int: ...

    def insert_generation(
        self,
        *,
        tenant_id: str,
        space: VectorSpaceConfig,
        source_watermark: int,
        tombstone_watermark: int,
        vector_count: int,
        content_checksum: str,
        id_map_checksum: str,
        index_checksum: str,
        agent_watermarks: dict[str, int],
        generation_id: str | None = None,
        now_us: int | None = None,
    ) -> VectorGenerationRecord: ...

    def get_generation(self, generation_id: str) -> VectorGenerationRecord: ...

    def generations_for_tenant(self, tenant_id: str) -> tuple[VectorGenerationRecord, ...]: ...

    def all_generation_ids(self) -> tuple[str, ...]: ...

    def all_pointer_generation_ids(self) -> tuple[str, ...]: ...

    def retire_generation(self, generation_id: str, *, now_us: int | None = None) -> int: ...

    def delete_retired_generations(
        self,
        tenant_id: str,
        *,
        keep: int = 2,
        older_than_us: int | None = None,
    ) -> tuple[str, ...]: ...

    def pointer(self, tenant_id: str) -> VectorCurrentPointer | None: ...

    def current_epoch(self, tenant_id: str) -> int: ...

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: VectorGenerationRecord,
        expected_epoch: int,
        now_us: int | None = None,
    ) -> VectorCurrentPointer: ...

    def pointer_info(self, tenant_id: str) -> dict[str, object]: ...


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


class ProfileSurface(Protocol):
    def subjects_citing_claim(
        self, tenant_id: str, generation_id: str, claim_id: str
    ) -> tuple[ProfileSubjectKey, ...]: ...

    """Repository surface for the profile projection (ADR-0016 §2)."""

    def projection_state(self) -> str: ...

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None: ...

    def reset_projection(self, *, now_us: int | None = None) -> None: ...

    def insert_generation(
        self,
        *,
        tenant_id: str,
        builder_version: int,
        source_watermark: int,
        tombstone_watermark: int,
        subject_count: int,
        field_count: int,
        content_checksum: str,
        agent_watermarks: dict[str, int],
        generation_id: str | None = None,
        now_us: int | None = None,
    ) -> ProfileGenerationRecord: ...

    def get_generation(self, generation_id: str) -> ProfileGenerationRecord: ...

    def generations_for_tenant(self, tenant_id: str) -> tuple[ProfileGenerationRecord, ...]: ...

    def retire_generation(self, generation_id: str, *, now_us: int | None = None) -> int: ...

    def delete_retired_generations(
        self,
        tenant_id: str,
        *,
        keep: int = ...,
        older_than_us: int | None = ...,
    ) -> tuple[str, ...]: ...

    def bump_generation_watermark(
        self, tenant_id: str, generation_id: str, agent_id: str, watermark: int
    ) -> None: ...

    def subjects_for_generation(
        self, tenant_id: str, generation_id: str
    ) -> tuple[ProfileSubjectRecord, ...]: ...

    def fields_for_subject(
        self, tenant_id: str, generation_id: str, subject: ProfileSubjectKey
    ) -> tuple[ProfileFieldRecord, ...]: ...

    def field_count(self, tenant_id: str, generation_id: str) -> int: ...

    def subject_count(self, tenant_id: str, generation_id: str) -> int: ...

    def insert_fields(
        self,
        tenant_id: str,
        generation_id: str,
        drafts: Sequence[ProfileFieldDraft],
        *,
        now_us: int | None = None,
    ) -> int: ...

    def delete_subject_rows(
        self, tenant_id: str, generation_id: str, subject: ProfileSubjectKey
    ) -> int: ...

    def recompute_generation_manifest(self, tenant_id: str, generation_id: str) -> None: ...

    def pointer(self, tenant_id: str) -> ProfileCurrentPointer | None: ...

    def current_epoch(self, tenant_id: str) -> int: ...

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: ProfileGenerationRecord,
        expected_epoch: int,
        now_us: int | None = None,
    ) -> ProfileCurrentPointer: ...

    def pointer_info(self, tenant_id: str) -> dict[str, object]: ...


class GraphSurface(Protocol):
    """Repository surface for the relation graph projection (ADR-0016 §3)."""

    def projection_state(self) -> str: ...

    def set_projection_state(self, state: str, *, now_us: int | None = None) -> None: ...

    def reset_projection(self, *, now_us: int | None = None) -> None: ...

    def insert_generation(
        self,
        *,
        tenant_id: str,
        builder_version: int,
        source_watermark: int,
        tombstone_watermark: int,
        node_count: int,
        edge_count: int,
        content_checksum: str,
        agent_watermarks: dict[str, int],
        generation_id: str | None = None,
        now_us: int | None = None,
    ) -> GraphGenerationRecord: ...

    def get_generation(self, generation_id: str) -> GraphGenerationRecord: ...

    def generations_for_tenant(self, tenant_id: str) -> tuple[GraphGenerationRecord, ...]: ...

    def retire_generation(self, generation_id: str, *, now_us: int | None = None) -> int: ...

    def delete_retired_generations(
        self,
        tenant_id: str,
        *,
        keep: int = ...,
        older_than_us: int | None = ...,
    ) -> tuple[str, ...]: ...

    def bump_generation_watermark(
        self, tenant_id: str, generation_id: str, agent_id: str, watermark: int
    ) -> None: ...

    def node_count(self, tenant_id: str, generation_id: str) -> int: ...

    def edge_count(self, tenant_id: str, generation_id: str) -> int: ...

    def node_exists(
        self, tenant_id: str, generation_id: str, node_id: str, node_kind: str
    ) -> bool: ...

    def edges_for_source(
        self,
        tenant_id: str,
        generation_id: str,
        node_id: str,
        *,
        node_kind: str,
        limit: int | None = ...,
    ) -> tuple[GraphEdgeRecord, ...]: ...

    def edges_for_resource(
        self, tenant_id: str, generation_id: str, resource_type: str, resource_id: str
    ) -> tuple[GraphEdgeRecord, ...]: ...

    def edges_for_entity(
        self, tenant_id: str, generation_id: str, entity_id: str
    ) -> tuple[GraphEdgeRecord, ...]: ...

    def all_edges(self, tenant_id: str, generation_id: str) -> tuple[GraphEdgeRecord, ...]: ...

    def insert_edges(
        self,
        tenant_id: str,
        generation_id: str,
        drafts: Sequence[GraphEdgeDraft],
        *,
        node_status: str | Mapping[str, str] = "canonical",
        now_us: int | None = None,
    ) -> int: ...

    def delete_resource_edges(
        self, tenant_id: str, generation_id: str, resource_type: str, resource_id: str
    ) -> int: ...

    def delete_entity_edges(self, tenant_id: str, generation_id: str, entity_id: str) -> int: ...

    def prune_orphan_nodes(self, tenant_id: str, generation_id: str) -> int: ...

    def recompute_generation_manifest(self, tenant_id: str, generation_id: str) -> None: ...

    def pointer(self, tenant_id: str) -> GraphCurrentPointer | None: ...

    def current_epoch(self, tenant_id: str) -> int: ...

    def switch_pointer(
        self,
        *,
        tenant_id: str,
        generation: GraphGenerationRecord,
        expected_epoch: int,
        now_us: int | None = None,
    ) -> GraphCurrentPointer: ...

    def pointer_info(self, tenant_id: str) -> dict[str, object]: ...


class PersonaSurface(Protocol):
    def current(self, agent_id: str) -> PersonaRecord: ...
    def integrity_problems(self) -> tuple[str, ...]: ...
    def by_revision(self, agent_id: str, revision: int) -> PersonaRecord: ...
    def history(self, agent_id: str, *, limit: int = 100) -> tuple[PersonaRecord, ...]: ...
    def current_policy(self, agent_id: str) -> PersonaPolicy: ...
    def replace_policy(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        expected_revision: int,
        config: Mapping[str, object],
        created_by: str,
        reason_code: str,
    ) -> PersonaPolicy: ...
    def publish(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        expected_revision: int,
        core_json: str,
        traits_json: str,
        narrative_json: str,
        digest: str,
        policy_id: str,
        source_refs_json: str,
        change_reason: str,
        created_by: str,
        source: str,
    ) -> PersonaRecord: ...
    def current_state(self, agent_id: str) -> PersonaState | None: ...
    def put_state(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        expected_revision: int,
        state_json: str,
        baseline_json: str,
        source_refs_json: str,
        started_us: int,
        expires_us: int,
        created_by: str,
    ) -> PersonaState: ...
    def due_states(self, *, now_us: int, limit: int = 100) -> tuple[PersonaState, ...]: ...
    def insert_proposal(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        base_revision: int,
        target_fields: Sequence[str],
        patch_json: str,
        field_deltas_json: str,
        evidence_refs_json: str,
        confidence: float,
        generator: str,
        generator_version: str,
        policy_evaluation_json: str,
        expires_us: int,
        actor: str,
    ) -> PersonaProposal: ...
    def proposal(self, proposal_id: str) -> PersonaProposal: ...
    def proposals(self, agent_id: str, *, limit: int = 100) -> tuple[PersonaProposal, ...]: ...
    def published_delta_total(self, agent_id: str, *, since_us: int) -> float: ...
    def last_proposal_publication_us(self, agent_id: str) -> int | None: ...
    def transition_proposal(
        self,
        proposal_id: str,
        *,
        expected_status: PersonaProposalStatus,
        target: PersonaProposalStatus,
        actor: str,
        reason_code: str,
        published_revision_id: str | None = None,
    ) -> PersonaProposal: ...


class ReflectionSurface(Protocol):
    """Fixed-window reflection, credential and resumable-event storage surface."""

    def observations_at_watermark(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        source_watermark: int,
        window_start_us: int,
        window_end_us: int,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        limit: int,
    ) -> tuple[StoredObservation, ...]: ...
    def observation_fingerprint(self, observation_id: str) -> str: ...
    def find_window(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        scope: Mapping[str, str | None],
        topic_key: str,
        window_start_us: int,
        window_end_us: int,
        source_watermark: int,
        builder_version: str,
    ) -> ConsolidationWindow | None: ...
    def insert_window(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        scope: Mapping[str, str | None],
        topic_key: str,
        window_start_us: int,
        window_end_us: int,
        source_watermark: int,
        observations: Sequence[StoredObservation],
        source_fingerprint: str,
        builder_version: str,
    ) -> ConsolidationWindow: ...
    def get_window(self, window_id: str) -> ConsolidationWindow: ...
    def mark_window(
        self,
        window_id: str,
        *,
        status: str,
        episode_id: str | None = None,
        sealed_us: int | None = None,
    ) -> int: ...
    def find_run(self, tenant_id: str, fingerprint: str) -> ReflectionRecord | None: ...
    def get_run(self, reflection_id: str) -> ReflectionRecord: ...
    def insert_run(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        window_id: str,
        fingerprint: str,
        source_watermark: int,
        versions: VersionSet,
        commit_mode: str,
        replay_of: str | None = None,
    ) -> ReflectionRecord: ...
    def finish_run(
        self,
        reflection_id: str,
        *,
        status: str,
        candidate_count: int,
        rejected_count: int,
        diff: Mapping[str, object],
        provider_outcome_id: str | None,
    ) -> int: ...
    def insert_evidence(self, reflection_id: str, candidate: Candidate) -> None: ...
    def insert_candidate(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        reflection_id: str,
        candidate: Candidate,
        decision: str,
        reject_reason: str | None = None,
        canonical_resource_type: str | None = None,
        canonical_resource_id: str | None = None,
    ) -> CandidateRecord: ...
    def insert_reject(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        reflection_id: str,
        raw: Mapping[str, object],
        reason: str,
    ) -> str: ...
    def candidates_for_run(self, reflection_id: str) -> tuple[CandidateRecord, ...]: ...
    def update_candidate_decision(
        self,
        candidate_id: str,
        *,
        decision: str,
        reject_reason: str | None = None,
        canonical_resource_type: str | None = None,
        canonical_resource_id: str | None = None,
    ) -> int: ...
    def insert_provider_outcome(
        self,
        *,
        tenant_id: str,
        agent_id: str | None,
        job_kind: str,
        provider_kind: ProviderKind,
        model_id: str,
        prompt_version: str,
        provider_schema_version: str,
        outcome: ProviderOutcome,
    ) -> str: ...
    def provider_circuit_state(
        self, tenant_id: str, provider_kind: ProviderKind
    ) -> Mapping[str, object] | None: ...
    def reserve_provider_probe(
        self, tenant_id: str, provider_kind: ProviderKind, *, now_us: int
    ) -> bool: ...
    def update_provider_circuit(
        self,
        tenant_id: str,
        provider_kind: ProviderKind,
        *,
        state: str,
        consecutive_failures: int,
        opened_until_us: int | None,
        probe_in_flight: bool,
        now_us: int,
    ) -> None: ...
    def charge_provider_budget(
        self,
        tenant_id: str,
        provider_kind: ProviderKind,
        *,
        budget_day: int,
        amount_microunits: int,
        limit_microunits: int,
        now_us: int,
    ) -> bool: ...
    def refund_provider_budget(
        self,
        tenant_id: str,
        provider_kind: ProviderKind,
        *,
        budget_day: int,
        amount_microunits: int,
        now_us: int,
    ) -> None: ...
    def provider_budget_spent(
        self, tenant_id: str, provider_kind: ProviderKind, *, budget_day: int
    ) -> int: ...
    def credential_by_digest(
        self, token_sha256: str, *, now_us: int
    ) -> CredentialRecord | None: ...
    def credential(self, credential_id: str) -> CredentialRecord | None: ...
    def credentials(
        self, tenant_id: str, *, limit: int = 201, after: tuple[int, str] | None = None
    ) -> tuple[CredentialRecord, ...]: ...
    def save_credential_metadata(
        self, record: CredentialRecord, *, expected_revision: int
    ) -> None: ...
    def touch_credential(self, credential_id: str, *, now_us: int) -> None: ...
    def revoke_credential(self, credential_id: str, *, now_us: int) -> int: ...
    def insert_credential(
        self,
        *,
        token_sha256: str,
        tenant_id: str,
        app_instance_id: str,
        plane: str,
        agent_ids: Sequence[str],
        space_group_ids: Sequence[str],
        space_ids: Sequence[str],
        entity_ids: Sequence[str],
        capabilities: Sequence[str],
        data_purposes: Sequence[str],
        expires_us: int,
        rotated_from_id: str | None = None,
    ) -> CredentialRecord: ...
    def events_after(
        self,
        *,
        tenant_id: str,
        after_cursor: int,
        agent_ids: Sequence[str],
        space_group_ids: Sequence[str],
        space_ids: Sequence[str],
        limit: int,
    ) -> tuple[ServiceEvent, ...]: ...
    def append_event(
        self,
        *,
        tenant_id: str,
        event_type: str,
        resource_refs: Sequence[Mapping[str, object]],
        source_watermark: int,
        occurred_us: int,
        agent_id: str | None = None,
        space_group_id: str | None = None,
        space_id: str | None = None,
        event_id: str | None = None,
    ) -> ServiceEvent: ...


class Transaction(Protocol):
    """Repository surface available inside one unit of work."""

    @property
    def identities(self) -> IdentitySurface: ...

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

    @property
    def episodes(self) -> EpisodeSurface: ...

    @property
    def claims(self) -> ClaimSurface: ...

    @property
    def relations(self) -> RelationSurface: ...

    @property
    def artifacts(self) -> ArtifactSurface: ...

    @property
    def retention(self) -> RetentionSurface: ...

    @property
    def fts(self) -> FtsSurface: ...

    @property
    def usage(self) -> RecallUsageSurface: ...

    @property
    def vector(self) -> VectorSurface: ...

    @property
    def profile(self) -> ProfileSurface: ...

    @property
    def graph(self) -> GraphSurface: ...

    @property
    def personas(self) -> PersonaSurface: ...

    @property
    def reflection(self) -> ReflectionSurface: ...

    @property
    def console(self) -> ConsoleRepository: ...

    @property
    def console_reads(self) -> ConsoleReadRepository: ...

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
    def list_space_groups(self, tenant_id: str) -> tuple[SpaceGroup, ...]: ...
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
    def list_audit_events(
        self, tenant_id: str, *, after_us: int = 0, limit: int = 100
    ) -> tuple[AuditEvent, ...]: ...
    def advance_watermark(
        self,
        tenant_id: str,
        agent_id: str,
        entries: Sequence[tuple[str, str, int]],
    ) -> int: ...
    def watermark(self, tenant_id: str, agent_id: str) -> WatermarkState | None: ...

    def tenant_watermarks(self, tenant_id: str) -> dict[str, int]: ...
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
