"""Application ports for transaction; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Protocol

from iris_memory_core.application.console.ports import (
    ConsoleOperationRepository,
    ConsoleReadRepository,
    ConsoleRepository,
)
from iris_memory_core.application.ports.events import CognitiveEventSurface
from iris_memory_core.application.ports.focus import FocusSurface
from iris_memory_core.application.ports.identity import IdentitySurface
from iris_memory_core.application.ports.indexes import (
    FtsSurface,
    GraphSurface,
    ProfileSurface,
    VectorSurface,
)
from iris_memory_core.application.ports.jobs import OutboxSurface, ScheduleSurface
from iris_memory_core.application.ports.memory import (
    ArtifactSurface,
    ClaimSurface,
    EpisodeSurface,
    RelationSurface,
)
from iris_memory_core.application.ports.notes import NoteSurface
from iris_memory_core.application.ports.observation import ObservationSurface
from iris_memory_core.application.ports.observation_context import ObservationContextSurface
from iris_memory_core.application.ports.persona import PersonaSurface
from iris_memory_core.application.ports.persona_drafts import PersonaDraftSurface
from iris_memory_core.application.ports.provider_configs import ProviderConfigRepository
from iris_memory_core.application.ports.recall import RecallUsageSurface, RecentContextSurface
from iris_memory_core.application.ports.reflection import ReflectionSurface
from iris_memory_core.application.ports.retention import RetentionSurface
from iris_memory_core.application.ports.state import StateSurface
from iris_memory_core.application.ports.statistics import StatisticsRepository
from iris_memory_core.application.ports.surface import SurfaceLeaseSurface
from iris_memory_core.application.ports.tasks import TaskSurface
from iris_memory_core.domain.identity import (
    BindingMethod,
    BindingState,
    EntityKind,
    EntityState,
    ExternalIdentityKey,
    FieldAuthority,
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


class Transaction(Protocol):
    """Repository surface available inside one unit of work."""

    @property
    def identities(self) -> IdentitySurface: ...

    @property
    def observation_context(self) -> ObservationContextSurface: ...

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
    def persona_drafts(self) -> PersonaDraftSurface: ...

    @property
    def reflection(self) -> ReflectionSurface: ...

    @property
    def console(self) -> ConsoleRepository: ...

    @property
    def console_operations(self) -> ConsoleOperationRepository: ...

    @property
    def providers(self) -> ProviderConfigRepository: ...

    @property
    def statistics(self) -> StatisticsRepository: ...

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
    def entity_has_self_link(self, tenant_id: str, entity_id: str) -> bool: ...
    def redirect_ancestor_depth(self, tenant_id: str, entity_id: str) -> int: ...
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
    def console_identity_attributes(
        self,
        tenant_id: str,
        entity_id: str,
    ) -> tuple[IdentityAttribute, ...]: ...
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
