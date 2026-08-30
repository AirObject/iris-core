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


class Clock(Protocol):
    def now(self) -> datetime: ...

    def now_us(self) -> int: ...


class SystemClock:
    """Wall clock in UTC; microseconds come from the same reading (§4.2)."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_us(self) -> int:
        return time.time_ns() // 1000


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


class Transaction(Protocol):
    """Repository surface available inside one unit of work."""

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
