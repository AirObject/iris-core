"""Unit of work: short write transactions with writer gating and busy retry.

Reads go through the same ``Transaction`` facade so tombstone exclusion
(ADR-0005) applies uniformly to every canonical read path.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, suppress
from pathlib import Path

from iris_memory_core.application.ports.clock import (
    Clock,
    IdentifierGenerator,
    SystemClock,
    Uuid7Generator,
)
from iris_memory_core.domain.errors import NotFoundError, OperationalBusyError
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
from iris_memory_core.storage.cognitive import (
    FocusRepository,
    RecentContextRepository,
    StateRepository,
)
from iris_memory_core.storage.console import ConsoleRepository
from iris_memory_core.storage.console_operations import ConsoleOperationRepository
from iris_memory_core.storage.console_reads import ConsoleReadRepository
from iris_memory_core.storage.fts import FtsRepository, RecallUsageRepository
from iris_memory_core.storage.memory import (
    ArtifactRepository,
    ClaimRepository,
    EpisodeRepository,
    RelationRepository,
    RetentionRepository,
)
from iris_memory_core.storage.persona import PersonaRepository
from iris_memory_core.storage.persona_draft import PersonaDraftRepository
from iris_memory_core.storage.plans import (
    CognitiveEventRepository,
    NoteRepository,
    TaskRepository,
)
from iris_memory_core.storage.projection import GraphRepository, ProfileRepository
from iris_memory_core.storage.provider_configs import ProviderConfigRepository
from iris_memory_core.storage.reflection import ReflectionRepository
from iris_memory_core.storage.repositories import (
    IdentityRepository,
    LedgerRepository,
    SpaceRepository,
)
from iris_memory_core.storage.runtime import SQLiteRuntime
from iris_memory_core.storage.spine import (
    ObservationRepository,
    OutboxRepository,
    ScheduleRepository,
    SurfaceRepository,
)
from iris_memory_core.storage.statistics import StatisticsRepository
from iris_memory_core.storage.vector import VectorRepository

BUSY_MESSAGES = ("database is locked", "database table is locked")


def _is_busy(error: sqlite3.Error) -> bool:
    return any(message in str(error) for message in BUSY_MESSAGES)


class Transaction:
    """Facade over the three repositories plus tombstone-guarded reads.

    Agent watermarks advance at most ONCE per write transaction (§7.4): calls
    to :meth:`advance_watermark` accumulate here — keeping the highest revision
    per aggregate — and flush as a single ``current_seq`` bump at commit time,
    so a transaction that creates and then binds a space advances the watermark
    by one and records the space's final revision.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        clock: Clock,
        ids: IdentifierGenerator,
        *,
        writable: bool = True,
        artifact_root: Path | None = None,
        statistics_roots: dict[str, Path] | None = None,
    ) -> None:
        self.spaces = SpaceRepository(connection, clock, ids)
        self.identities = IdentityRepository(connection, clock, ids)
        self.ledger = LedgerRepository(connection, clock, ids)
        self.observations = ObservationRepository(connection, clock, ids)
        self.outbox = OutboxRepository(connection, clock, ids)
        self.schedules = ScheduleRepository(connection, clock, ids)
        self.surfaces = SurfaceRepository(connection, clock, ids)
        self.recent = RecentContextRepository(connection, clock, ids)
        self.states = StateRepository(connection, clock, ids)
        self.focus = FocusRepository(connection, clock, ids)
        self.notes = NoteRepository(connection, clock, ids)
        self.tasks = TaskRepository(connection, clock, ids)
        self.events = CognitiveEventRepository(connection, clock, ids)
        self.episodes = EpisodeRepository(connection, clock, ids)
        self.claims = ClaimRepository(connection, clock, ids)
        self.relations = RelationRepository(connection, clock, ids)
        self.artifacts = ArtifactRepository(connection, clock, ids, artifact_root)
        self.retention = RetentionRepository(connection, clock, ids)
        self.fts = FtsRepository(connection, clock, ids)
        self.usage = RecallUsageRepository(connection, clock, ids)
        self.vector = VectorRepository(connection, clock, ids)
        self.profile = ProfileRepository(connection, clock, ids)
        self.graph = GraphRepository(connection, clock, ids)
        self.personas = PersonaRepository(connection, clock, ids)
        self.persona_drafts = PersonaDraftRepository(connection, clock, ids)
        self.reflection = ReflectionRepository(connection, clock, ids)
        self.console = ConsoleRepository(connection)
        self.console_operations = ConsoleOperationRepository(connection)
        self.providers = ProviderConfigRepository(connection)
        self.statistics = StatisticsRepository(connection, statistics_roots)
        self.console_reads = ConsoleReadRepository(connection, writable=writable)
        self._connection = connection
        self._writable = writable
        self._pending_watermarks: dict[tuple[str, str], dict[tuple[str, str], int]] = {}

    def raw(self) -> sqlite3.Connection:
        """Raw connection for storage-internal infrastructure (idempotency, backup).

        Application services must not call this; it exists for adapters that
        own tables outside the domain repository surface.
        """
        return self._connection

    def _reject_tombstoned(self, tenant_id: str, resource_type: str, resource_id: str) -> None:
        if self.ledger.is_tombstoned(tenant_id, resource_type, resource_id):
            raise NotFoundError(f"{resource_type} {resource_id} is tombstoned")

    # -- provisioning ------------------------------------------------------

    def insert_tenant(self, tenant_id: str, *, status: str) -> Tenant:
        return self.spaces.insert_tenant(tenant_id, status=status)

    def get_tenant(self, tenant_id: str) -> Tenant:
        return self.spaces.get_tenant(tenant_id)

    def insert_agent(self, tenant_id: str, display_name: str, *, actor: str) -> Agent:
        return self.spaces.insert_agent(tenant_id, display_name, actor=actor)

    def get_agent(self, agent_id: str) -> Agent:
        agent = self.spaces.get_agent(agent_id)
        self._reject_tombstoned(agent.tenant_id, "agent", agent.id)
        return agent

    def get_persona_revision(self, revision_id: str) -> PersonaRevision:
        return self.spaces.get_persona_revision(revision_id)

    def insert_space_group(
        self,
        tenant_id: str,
        name: str,
        description: str,
        *,
        actor: str,
        reason_code: str,
    ) -> SpaceGroup:
        return self.spaces.insert_space_group(
            tenant_id, name, description, actor=actor, reason_code=reason_code
        )

    def get_space_group(self, space_group_id: str) -> SpaceGroup:
        group = self.spaces.get_space_group(space_group_id)
        self._reject_tombstoned(group.tenant_id, "space_group", group.id)
        return group

    def list_space_groups(self, tenant_id: str) -> tuple[SpaceGroup, ...]:
        return self.spaces.list_space_groups(tenant_id)

    def update_space_group(
        self,
        space_group_id: str,
        *,
        name: str,
        description: str,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> SpaceGroup:
        return self.spaces.update_space_group(
            space_group_id,
            name=name,
            description=description,
            expected_revision=expected_revision,
            actor=actor,
            reason_code=reason_code,
        )

    def insert_space(
        self,
        tenant_id: str,
        kind: str,
        *,
        agent_id: str | None = None,
        actor: str = "system",
    ) -> Space:
        return self.spaces.insert_space(tenant_id, kind, agent_id=agent_id, actor=actor)

    def get_space(self, space_id: str) -> Space:
        space = self.spaces.get_space(space_id)
        self._reject_tombstoned(space.tenant_id, "space", space.id)
        return space

    def bind_space_to_group(
        self,
        tenant_id: str,
        space_id: str,
        space_group_id: str,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> SpaceGroupBinding:
        return self.spaces.bind_space_to_group(
            tenant_id,
            space_id,
            space_group_id,
            expected_revision=expected_revision,
            actor=actor,
            reason_code=reason_code,
        )

    def unbind_space_from_group(
        self,
        tenant_id: str,
        space_id: str,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> SpaceGroupBinding | None:
        return self.spaces.unbind_space_from_group(
            tenant_id,
            space_id,
            expected_revision=expected_revision,
            actor=actor,
            reason_code=reason_code,
        )

    def get_active_group_binding(self, space_id: str) -> SpaceGroupBinding | None:
        return self.spaces.get_active_group_binding(space_id)

    def list_group_bindings(self, space_group_id: str) -> tuple[SpaceGroupBinding, ...]:
        return self.spaces.list_group_bindings(space_group_id)

    def insert_session(self, tenant_id: str, space_id: str, *, actor: str) -> Session:
        return self.spaces.insert_session(tenant_id, space_id, actor=actor)

    def get_session(self, session_id: str) -> Session:
        session = self.spaces.get_session(session_id)
        self._reject_tombstoned(session.tenant_id, "session", session.id)
        return session

    # -- identity registry ---------------------------------------------------

    def insert_entity(
        self,
        tenant_id: str,
        kind: EntityKind,
        *,
        display_name: str = "",
        privacy_labels: Sequence[str] = (),
        actor: str = "system",
    ) -> Entity:
        return self.identities.insert_entity(
            tenant_id, kind, display_name=display_name, privacy_labels=privacy_labels, actor=actor
        )

    def get_entity(self, entity_id: str) -> Entity:
        entity = self.identities.get_entity(entity_id)
        self._reject_tombstoned(entity.tenant_id, "entity", entity.id)
        return entity

    def update_entity_state(
        self,
        entity_id: str,
        state: EntityState,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> Entity:
        return self.identities.update_entity_state(
            entity_id,
            state,
            expected_revision=expected_revision,
            actor=actor,
            reason_code=reason_code,
        )

    def insert_external_identity(
        self, key: ExternalIdentityKey, *, entity_id: str | None
    ) -> ExternalIdentity:
        return self.identities.insert_external_identity(key, entity_id=entity_id)

    def get_external_identity(self, external_identity_id: str) -> ExternalIdentity:
        identity = self.identities.get_external_identity(external_identity_id)
        self._reject_tombstoned(identity.tenant_id, "external_identity", identity.id)
        return identity

    def find_external_identity(self, key: ExternalIdentityKey) -> ExternalIdentity | None:
        identity = self.identities.find_external_identity(key)
        if identity is not None:
            self._reject_tombstoned(identity.tenant_id, "external_identity", identity.id)
        return identity

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
    ) -> Binding:
        return self.identities.insert_binding(
            tenant_id,
            external_identity_id,
            entity_id,
            method=method,
            confidence=confidence,
            proof_digest=proof_digest,
            actor=actor,
            reason_code=reason_code,
        )

    def get_binding(self, binding_id: str) -> Binding:
        binding = self.identities.get_binding(binding_id)
        self._reject_tombstoned(binding.tenant_id, "binding", binding.id)
        return binding

    def transition_binding(
        self,
        binding_id: str,
        target: BindingState,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
        note: str = "",
    ) -> Binding:
        return self.identities.transition_binding(
            binding_id,
            target,
            expected_revision=expected_revision,
            actor=actor,
            reason_code=reason_code,
            note=note,
        )

    def binding_state_events(self, binding_id: str) -> tuple[tuple[int, BindingState], ...]:
        return self.identities.binding_state_events(binding_id)

    def verified_binding_for(self, external_identity_id: str) -> Binding | None:
        return self.identities.verified_binding_for(external_identity_id)

    def bindings_for(self, external_identity_id: str) -> tuple[Binding, ...]:
        return self.identities.bindings_for(external_identity_id)

    def insert_entity_redirect(
        self,
        tenant_id: str,
        from_entity_id: str,
        to_entity_id: str,
        *,
        actor: str,
        reason_code: str,
    ) -> EntityRedirect:
        return self.identities.insert_entity_redirect(
            tenant_id, from_entity_id, to_entity_id, actor=actor, reason_code=reason_code
        )

    def entity_has_self_link(self, tenant_id: str, entity_id: str) -> bool:
        return self.identities.entity_has_self_link(tenant_id, entity_id)

    def redirect_ancestor_depth(self, tenant_id: str, entity_id: str) -> int:
        return self.identities.redirect_ancestor_depth(tenant_id, entity_id)

    def redirect_map(self, tenant_id: str) -> dict[str, str]:
        return self.identities.redirect_map(tenant_id)

    def get_entity_redirect(self, from_entity_id: str) -> EntityRedirect | None:
        return self.identities.get_entity_redirect(from_entity_id)

    def redirects_active_at(self, tenant_id: str, at_us: int) -> dict[str, str]:
        return self.identities.redirects_active_at(tenant_id, at_us)

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
    ) -> AttributeWrite:
        return self.identities.record_identity_attribute(
            tenant_id,
            entity_id,
            field,
            value,
            authority,
            source_ref,
            actor=actor,
            effective_us=effective_us,
        )

    def console_identity_attributes(
        self,
        tenant_id: str,
        entity_id: str,
    ) -> tuple[IdentityAttribute, ...]:
        return self.identities.console_identity_attributes(tenant_id, entity_id)

    def current_identity_attribute(
        self, tenant_id: str, entity_id: str, field: str
    ) -> IdentityAttribute | None:
        return self.identities.current_identity_attribute(tenant_id, entity_id, field)

    def conflicted_attributes(self, entity_id: str) -> tuple[IdentityAttribute, ...]:
        return self.identities.conflicted_attributes(entity_id)

    # -- consistency ledger ----------------------------------------------------

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
    ) -> AuditEvent:
        return self.ledger.audit(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            reason_code=reason_code,
            details=details,
            revision=revision,
        )

    def list_audit_events(
        self, tenant_id: str, *, after_us: int = 0, limit: int = 100
    ) -> tuple[AuditEvent, ...]:
        return self.ledger.list_audit_events(tenant_id, after_us=after_us, limit=limit)

    def advance_watermark(
        self,
        tenant_id: str,
        agent_id: str,
        entries: Sequence[tuple[str, str, int]],
    ) -> int:
        """Queue one watermark advance; flushed once at the end of this transaction.

        Returns the projected sequence number the flush will assign. Repeated
        calls for the same (tenant, agent) inside one transaction collapse into
        a single advance recording each aggregate's final revision.
        """
        if not self._writable:
            raise RuntimeError("watermarks advance only inside write units of work")
        aggregates = self._pending_watermarks.setdefault((tenant_id, agent_id), {})
        for aggregate_type, aggregate_id, aggregate_revision in entries:
            key = (aggregate_type, aggregate_id)
            aggregates[key] = max(aggregate_revision, aggregates.get(key, 0))
        return self._projected_seq(tenant_id, agent_id)

    def _projected_seq(self, tenant_id: str, agent_id: str) -> int:
        state = self.ledger.watermark(tenant_id, agent_id)
        base = state.current_seq if state is not None else 0
        return base + 1

    def flush_pending_watermarks(self) -> None:
        """Apply the accumulated advances: one seq bump per (tenant, agent)."""
        for (tenant_id, agent_id), aggregates in self._pending_watermarks.items():
            entries = [
                (aggregate_type, aggregate_id, revision)
                for (aggregate_type, aggregate_id), revision in aggregates.items()
            ]
            self.ledger.advance_watermark(tenant_id, agent_id, entries)
        self._pending_watermarks.clear()

    def watermark(self, tenant_id: str, agent_id: str) -> WatermarkState | None:
        return self.ledger.watermark(tenant_id, agent_id)

    def tenant_watermarks(self, tenant_id: str) -> dict[str, int]:
        return self.ledger.tenant_watermarks(tenant_id)

    def record_tombstone(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        reason_code: str,
        deleted_by: str,
    ) -> Tombstone:
        return self.ledger.record_tombstone(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            reason_code=reason_code,
            deleted_by=deleted_by,
        )

    def is_tombstoned(self, tenant_id: str, resource_type: str, resource_id: str) -> bool:
        return self.ledger.is_tombstoned(tenant_id, resource_type, resource_id)

    def insert_resource_link(
        self,
        *,
        tenant_id: str,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
        relation: str,
    ) -> ResourceLink:
        return self.ledger.insert_resource_link(
            tenant_id=tenant_id,
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relation=relation,
        )

    def links_for_source(
        self,
        tenant_id: str,
        source_type: str,
        source_id: str,
        *,
        target_type: str | None = None,
        relation: str | None = None,
    ) -> tuple[ResourceLink, ...]:
        return self.ledger.links_for_source(
            tenant_id, source_type, source_id, target_type=target_type, relation=relation
        )

    def tombstone_watermark(self) -> int:
        return self.ledger.tombstone_watermark()


class _WriteUnitOfWork(AbstractContextManager[Transaction]):
    """owns the writer gate, opens BEGIN IMMEDIATE with busy retry, commits or
    rolls back, and always releases the gate."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._connection: sqlite3.Connection | None = None
        self._transaction: Transaction | None = None
        self._gate_held = False

    def __enter__(self) -> Transaction:
        gate = self._store.runtime.writer_lock
        if not gate.acquire(timeout=30.0):
            raise OperationalBusyError("writer gate acquisition timed out")
        self._gate_held = True
        try:
            self._connection = self._store._connect_with_retry()
        except BaseException:
            self._release()
            raise
        self._transaction = Transaction(
            self._connection,
            self._store.clock,
            self._store.ids,
            writable=True,
            artifact_root=self._store.artifact_root,
            statistics_roots=self._store.statistics_roots,
        )
        return self._transaction

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        connection = self._connection
        try:
            assert connection is not None
            if exc_type is None:
                # Pending watermark advances commit as exactly one bump per
                # (tenant, agent) inside this very transaction.
                if self._transaction is not None:
                    self._transaction.flush_pending_watermarks()
                self._store._commit_with_retry(connection)
        finally:
            if connection is not None and connection.in_transaction:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
            if connection is not None:
                connection.close()
            self._release()

    def _release(self) -> None:
        if self._gate_held:
            self._store.runtime.writer_lock.release()
            self._gate_held = False


class _ReadUnitOfWork(AbstractContextManager[Transaction]):
    def __init__(self, store: Store) -> None:
        self._store = store
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> Transaction:
        self._connection = self._store._ready_connect()
        self._connection.execute("BEGIN DEFERRED")
        return Transaction(
            self._connection,
            self._store.clock,
            self._store.ids,
            writable=False,
            artifact_root=self._store.artifact_root,
            statistics_roots=self._store.statistics_roots,
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        assert self._connection is not None
        try:
            if exc_type is None:
                self._connection.execute("COMMIT")
            elif self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
        finally:
            self._connection.close()


class Store:
    """Application-facing unit of work bound to one SQLite database.

    Every connection the Store opens is schema-Ready-gated: an out-of-window
    (or unmigrated) database is rejected with the stable ``schema_incompatible``
    error before any transaction starts. The application cannot silently run
    against a schema outside its binary compatibility window — including a
    database swapped in underneath it by a restore.
    """

    def __init__(
        self,
        runtime: SQLiteRuntime,
        *,
        clock: Clock | None = None,
        ids: IdentifierGenerator | None = None,
        busy_retry_attempts: int = 8,
        busy_backoff_ms: float = 25.0,
        busy_observer: Callable[[str], None] | None = None,
        verify_schema_window: bool = True,
    ) -> None:
        self.runtime = runtime
        self.clock: Clock = clock or SystemClock()
        self.ids: IdentifierGenerator = ids or Uuid7Generator()
        #: Controlled local blob root for Phase 5 artifacts (§13.6): derived
        #: from the database location, never configurable per request.
        self.artifact_root: Path = runtime.database.parent / "artifacts"
        self.statistics_roots: dict[str, Path] = {}
        self._busy_retry_attempts = busy_retry_attempts
        self._busy_backoff_ms = busy_backoff_ms
        self._busy_observer = busy_observer
        #: Restore's deletion-ledger replay operates on AUTHENTICATED legacy
        #: bytes inside staging: an older-schema snapshot is replayed there
        #: and switched in, and the ordinary startup migration (never the
        #: restore path, ADR-0014 §12-10) brings it forward when the current
        #: binary opens it. Only that path may bypass the window gate.
        self._verify_schema_window = verify_schema_window

    def _note_busy(self, operation_class: str) -> None:
        if self._busy_observer is not None:
            with suppress(Exception):
                self._busy_observer(operation_class)

    def write(self) -> AbstractContextManager[Transaction]:
        """Short write transaction behind the process-wide writer gate.

        Writers never call providers, embed or parse large payloads inside the
        transaction (§20.2); call sites must keep the body minimal.
        """
        return _WriteUnitOfWork(self)

    def read(self) -> AbstractContextManager[Transaction]:
        """One read snapshot per unit of work (BEGIN DEFERRED ... COMMIT).

        Without an explicit transaction, consecutive statements could observe
        different SQLite snapshots; a deferred transaction pins one.
        """
        return _ReadUnitOfWork(self)

    def _ready_connect(self) -> sqlite3.Connection:
        """Open a connection enforcing the schema compatibility window."""
        return self.runtime.connect(verify_schema=self._verify_schema_window)

    def _connect_with_retry(self) -> sqlite3.Connection:
        delay = self._busy_backoff_ms / 1000
        last_error: sqlite3.Error | None = None
        for _ in range(self._busy_retry_attempts):
            connection = self._ready_connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                return connection
            except sqlite3.OperationalError as error:
                connection.close()
                if not _is_busy(error):
                    raise
                self._note_busy("begin_immediate")
                last_error = error
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
        raise OperationalBusyError(
            "database stayed busy beyond the retry budget", details={"cause": str(last_error)}
        )

    def _commit_with_retry(self, connection: sqlite3.Connection) -> None:
        delay = self._busy_backoff_ms / 1000
        for attempt in range(self._busy_retry_attempts):
            try:
                connection.execute("COMMIT")
                return
            except sqlite3.OperationalError as error:
                if not _is_busy(error) or not connection.in_transaction:
                    raise
                self._note_busy("commit")
                if attempt == self._busy_retry_attempts - 1:
                    raise OperationalBusyError(
                        "commit stayed busy beyond the retry budget", details={"cause": str(error)}
                    ) from error
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
