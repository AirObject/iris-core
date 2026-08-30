"""SQLite repositories implementing the application ``Transaction`` port.

Every method runs inside the caller's short transaction; revision bumps use
compare-and-set so optimistic concurrency is enforced by the database itself.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from iris_memory_core.application.ports import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import (
    ConflictError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.hashing import content_hash
from iris_memory_core.domain.identity import (
    BindingMethod,
    BindingState,
    EntityKind,
    EntityState,
    ExternalIdentityKey,
    FieldAuthority,
    MergeOutcome,
    decide_attribute_merge,
    transition_binding,
)
from iris_memory_core.domain.model import (
    BOOTSTRAP_PERSONA_CORE,
    BOOTSTRAP_PERSONA_NARRATIVE,
    BOOTSTRAP_PERSONA_REVISION,
    BOOTSTRAP_PERSONA_SOURCE,
    BOOTSTRAP_PERSONA_STATUS,
    BOOTSTRAP_PERSONA_TRAITS,
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

TOMBSTONE_RESOURCE_TYPES = frozenset(
    {"tenant", "agent", "space_group", "space", "session", "entity", "external_identity", "binding"}
)

#: ADR-0005: a committed tombstone excludes the resource from every canonical
#: read — including collection and resolution queries, not just point reads.
#: Redirect edges whose SOURCE entity is tombstoned drop out of resolution; an
#: edge whose TARGET is tombstoned stays so resolution reaches the deleted
#: terminal and the caller's tombstone guard nulls it (a tombstoned terminal
#: resolves to nothing — it must not silently resolve to the pre-merge entity).
_NOT_TOMBSTONED_BINDING = (
    "NOT EXISTS (SELECT 1 FROM resource_tombstones t WHERE t.tenant_id = bindings.tenant_id "
    "AND t.resource_type = 'binding' AND t.resource_id = bindings.id)"
)
_NOT_TOMBSTONED_REDIRECT = (
    "NOT EXISTS (SELECT 1 FROM resource_tombstones t "
    "WHERE t.tenant_id = entity_redirects.tenant_id "
    "AND t.resource_type = 'entity' AND t.resource_id = entity_redirects.from_entity_id)"
)


def rfc3339(us: int) -> str:
    moment = datetime.fromtimestamp(us / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _one(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
    cursor = connection.execute(sql, params)
    row = cursor.fetchone()
    return row if row is not None else None


def _require(
    connection: sqlite3.Connection, sql: str, params: tuple[Any, ...], what: str
) -> sqlite3.Row:
    row = _one(connection, sql, params)
    if row is None:
        raise NotFoundError(f"{what} not found")
    return row


def _cas_bump(
    connection: sqlite3.Connection,
    table: str,
    row_id: str,
    expected_revision: int,
    assignments: dict[str, Any],
    now_us: int,
) -> int:
    """Compare-and-set revision bump; raises the stable mismatch error on conflict."""
    sets = ", ".join(f"{column} = ?" for column in assignments)
    values = list(assignments.values())
    cursor = connection.execute(
        f"UPDATE {table} SET {sets}, revision = revision + 1, updated_us = ? "
        f"WHERE id = ? AND revision = ?",
        (*values, now_us, row_id, expected_revision),
    )
    if cursor.rowcount == 1:
        return expected_revision + 1
    row = _one(connection, f"SELECT revision FROM {table} WHERE id = ?", (row_id,))
    if row is None:
        raise NotFoundError(f"{table} {row_id} not found")
    raise RevisionMismatchError(table, row_id, expected_revision, int(row["revision"]))


class SpaceRepository:
    """Tenants, agents, space groups, spaces, sessions and group bindings."""

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- tenants ----------------------------------------------------------

    def insert_tenant(self, tenant_id: str, *, status: str) -> Tenant:
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO tenants (id, status, created_us, created_at) VALUES (?, ?, ?, ?)",
            (tenant_id, status, now_us, rfc3339(now_us)),
        )
        return Tenant(id=tenant_id, status=status, created_us=now_us)

    def get_tenant(self, tenant_id: str) -> Tenant:
        row = _require(
            self._connection, "SELECT * FROM tenants WHERE id = ?", (tenant_id,), "tenant"
        )
        return Tenant(
            id=str(row["id"]), status=str(row["status"]), created_us=int(row["created_us"])
        )

    # -- agents and bootstrap persona --------------------------------------

    def insert_agent(self, tenant_id: str, display_name: str, *, actor: str) -> Agent:
        self.get_tenant(tenant_id)
        agent_id = str(self._ids.new())
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO agents (id, tenant_id, display_name, status, created_us, created_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (agent_id, tenant_id, display_name, now_us, rfc3339(now_us)),
        )
        persona = self._insert_bootstrap_persona(tenant_id, agent_id, now_us)
        self._connection.execute(
            "UPDATE agents SET persona_current_revision_id = ? WHERE id = ?",
            (persona.id, agent_id),
        )
        self._connection.execute(
            "INSERT INTO agent_watermarks (tenant_id, agent_id, current_seq, updated_us) "
            "VALUES (?, ?, 0, ?)",
            (tenant_id, agent_id, now_us),
        )
        self.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="agent.created",
            resource_type="agent",
            resource_id=agent_id,
            reason_code="provisioning",
            details={"display_name": display_name},
        )
        self.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="persona.bootstrap_published",
            resource_type="persona_revision",
            resource_id=persona.id,
            reason_code="provisioning",
            details={"status": persona.status, "source": persona.source},
            revision=persona.revision,
        )
        return Agent(
            id=agent_id,
            tenant_id=tenant_id,
            display_name=display_name,
            status="active",
            persona_current_revision_id=persona.id,
            created_us=now_us,
        )

    def _insert_bootstrap_persona(
        self, tenant_id: str, agent_id: str, now_us: int
    ) -> PersonaRevision:
        revision_id = str(self._ids.new())
        digest = content_hash(
            {
                "core": BOOTSTRAP_PERSONA_CORE,
                "traits": BOOTSTRAP_PERSONA_TRAITS,
                "narrative": BOOTSTRAP_PERSONA_NARRATIVE,
            }
        )
        self._connection.execute(
            "INSERT INTO persona_revisions (id, tenant_id, agent_id, revision, core, traits, "
            "narrative, content_hash, status, source, created_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                tenant_id,
                agent_id,
                BOOTSTRAP_PERSONA_REVISION,
                BOOTSTRAP_PERSONA_CORE,
                BOOTSTRAP_PERSONA_TRAITS,
                BOOTSTRAP_PERSONA_NARRATIVE,
                digest,
                BOOTSTRAP_PERSONA_STATUS,
                BOOTSTRAP_PERSONA_SOURCE,
                now_us,
            ),
        )
        return PersonaRevision(
            id=revision_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            revision=BOOTSTRAP_PERSONA_REVISION,
            core=BOOTSTRAP_PERSONA_CORE,
            traits=BOOTSTRAP_PERSONA_TRAITS,
            narrative=BOOTSTRAP_PERSONA_NARRATIVE,
            content_hash=digest,
            status=BOOTSTRAP_PERSONA_STATUS,
            source=BOOTSTRAP_PERSONA_SOURCE,
            created_us=now_us,
        )

    def get_agent(self, agent_id: str) -> Agent:
        row = _require(self._connection, "SELECT * FROM agents WHERE id = ?", (agent_id,), "agent")
        return Agent(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            display_name=str(row["display_name"]),
            status=str(row["status"]),
            persona_current_revision_id=(
                str(row["persona_current_revision_id"])
                if row["persona_current_revision_id"] is not None
                else None
            ),
            created_us=int(row["created_us"]),
        )

    def get_persona_revision(self, revision_id: str) -> PersonaRevision:
        row = _require(
            self._connection,
            "SELECT * FROM persona_revisions WHERE id = ?",
            (revision_id,),
            "persona revision",
        )
        return PersonaRevision(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            agent_id=str(row["agent_id"]),
            revision=int(row["revision"]),
            core=str(row["core"]),
            traits=str(row["traits"]),
            narrative=str(row["narrative"]),
            content_hash=str(row["content_hash"]),
            status=str(row["status"]),
            source=str(row["source"]),
            created_us=int(row["created_us"]),
        )

    # -- space groups -------------------------------------------------------

    def insert_space_group(
        self,
        tenant_id: str,
        name: str,
        description: str,
        *,
        actor: str,
        reason_code: str,
    ) -> SpaceGroup:
        self.get_tenant(tenant_id)
        group_id = str(self._ids.new())
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO space_groups (id, tenant_id, name, description, status, revision, "
            "created_us, updated_us) VALUES (?, ?, ?, ?, 'active', 1, ?, ?)",
            (group_id, tenant_id, name, description, now_us, now_us),
        )
        self._connection.execute(
            "INSERT INTO space_group_revisions (id, space_group_id, revision, name, description, "
            "changed_by, reason_code, content_hash, created_us) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (
                str(self._ids.new()),
                group_id,
                name,
                description,
                actor,
                reason_code,
                content_hash({"name": name, "description": description}),
                now_us,
            ),
        )
        self.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="space_group.created",
            resource_type="space_group",
            resource_id=group_id,
            reason_code=reason_code,
            details={"name": name},
            revision=1,
        )
        return self.get_space_group(group_id)

    def get_space_group(self, space_group_id: str) -> SpaceGroup:
        row = _require(
            self._connection,
            "SELECT * FROM space_groups WHERE id = ?",
            (space_group_id,),
            "space group",
        )
        return SpaceGroup(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            name=str(row["name"]),
            description=str(row["description"]),
            primary_space_id=(
                str(row["primary_space_id"]) if row["primary_space_id"] is not None else None
            ),
            status=str(row["status"]),
            revision=int(row["revision"]),
            created_us=int(row["created_us"]),
            updated_us=int(row["updated_us"]),
        )

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
        self.get_space_group(space_group_id)
        now_us = self._clock.now_us()
        new_revision = _cas_bump(
            self._connection,
            "space_groups",
            space_group_id,
            expected_revision,
            {"name": name, "description": description},
            now_us,
        )
        self._connection.execute(
            "INSERT INTO space_group_revisions (id, space_group_id, revision, name, description, "
            "changed_by, reason_code, content_hash, created_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(self._ids.new()),
                space_group_id,
                new_revision,
                name,
                description,
                actor,
                reason_code,
                content_hash({"name": name, "description": description}),
                now_us,
            ),
        )
        self.audit(
            tenant_id=self.get_space_group(space_group_id).tenant_id,
            actor=actor,
            action="space_group.updated",
            resource_type="space_group",
            resource_id=space_group_id,
            reason_code=reason_code,
            details={"name": name},
            revision=new_revision,
        )
        return self.get_space_group(space_group_id)

    # -- spaces and sessions --------------------------------------------------

    def insert_space(
        self,
        tenant_id: str,
        kind: str,
        *,
        agent_id: str | None = None,
        actor: str = "system",
    ) -> Space:
        """Create a standalone space.

        Group membership is only established through ``bind_space_to_group``:
        it is a management-plane operation with reason, expected revision and
        binding history. This method never writes ``space_group_id``.
        """
        self.get_tenant(tenant_id)
        if agent_id is not None:
            agent = self.get_agent(agent_id)
            if agent.tenant_id != tenant_id:
                raise ConflictError("agent belongs to another tenant")
        space_id = str(self._ids.new())
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO spaces (id, tenant_id, agent_id, space_group_id, kind, status, revision, "
            "created_us, updated_us) VALUES (?, ?, ?, NULL, ?, 'active', 1, ?, ?)",
            (space_id, tenant_id, agent_id, kind, now_us, now_us),
        )
        self.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="space.created",
            resource_type="space",
            resource_id=space_id,
            reason_code="provisioning",
            details={"kind": kind, "agent_id": agent_id},
            revision=1,
        )
        return self.get_space(space_id)

    def get_space(self, space_id: str) -> Space:
        row = _require(self._connection, "SELECT * FROM spaces WHERE id = ?", (space_id,), "space")
        return Space(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            agent_id=str(row["agent_id"]) if row["agent_id"] is not None else None,
            space_group_id=str(row["space_group_id"])
            if row["space_group_id"] is not None
            else None,
            kind=str(row["kind"]),
            status=str(row["status"]),
            revision=int(row["revision"]),
            created_us=int(row["created_us"]),
            updated_us=int(row["updated_us"]),
        )

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
        space = self.get_space(space_id)
        group = self.get_space_group(space_group_id)
        if space.tenant_id != tenant_id or group.tenant_id != tenant_id:
            raise ConflictError("space and group must belong to the tenant")
        if self.get_active_group_binding(space_id) is not None:
            raise ConflictError("space already has an active group binding")
        now_us = self._clock.now_us()
        _cas_bump(
            self._connection,
            "spaces",
            space_id,
            expected_revision,
            {"space_group_id": space_group_id},
            now_us,
        )
        binding_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO space_group_bindings (id, tenant_id, space_id, space_group_id, bound_us, "
            "bound_by, reason_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (binding_id, tenant_id, space_id, space_group_id, now_us, actor, reason_code),
        )
        self.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="space_group.bound",
            resource_type="space",
            resource_id=space_id,
            reason_code=reason_code,
            details={"space_group_id": space_group_id},
            revision=expected_revision + 1,
        )
        return self.get_active_group_binding(space_id)  # type: ignore[return-value]

    def unbind_space_from_group(
        self,
        tenant_id: str,
        space_id: str,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> SpaceGroupBinding | None:
        space = self.get_space(space_id)
        if space.tenant_id != tenant_id:
            raise ConflictError("space belongs to another tenant")
        active = self.get_active_group_binding(space_id)
        if active is None:
            raise ConflictError("space has no active group binding")
        now_us = self._clock.now_us()
        _cas_bump(
            self._connection,
            "spaces",
            space_id,
            expected_revision,
            {"space_group_id": None},
            now_us,
        )
        self._connection.execute(
            "UPDATE space_group_bindings SET unbound_us = ?, unbound_by = ?, "
            "unbound_reason_code = ? WHERE id = ? AND unbound_us IS NULL",
            (now_us, actor, reason_code, active.id),
        )
        self.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="space_group.unbound",
            resource_type="space",
            resource_id=space_id,
            reason_code=reason_code,
            details={"space_group_id": active.space_group_id},
            revision=expected_revision + 1,
        )
        return self.get_group_binding(active.id)

    def get_active_group_binding(self, space_id: str) -> SpaceGroupBinding | None:
        row = _one(
            self._connection,
            "SELECT * FROM space_group_bindings WHERE space_id = ? AND unbound_us IS NULL",
            (space_id,),
        )
        return self._binding_row(row) if row is not None else None

    def get_group_binding(self, binding_id: str) -> SpaceGroupBinding | None:
        row = _one(
            self._connection,
            "SELECT * FROM space_group_bindings WHERE id = ?",
            (binding_id,),
        )
        return self._binding_row(row) if row is not None else None

    @staticmethod
    def _binding_row(row: sqlite3.Row) -> SpaceGroupBinding:
        return SpaceGroupBinding(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            space_id=str(row["space_id"]),
            space_group_id=str(row["space_group_id"]),
            bound_us=int(row["bound_us"]),
            unbound_us=int(row["unbound_us"]) if row["unbound_us"] is not None else None,
            bound_by=str(row["bound_by"]),
            reason_code=str(row["reason_code"]),
        )

    def list_group_bindings(self, space_group_id: str) -> tuple[SpaceGroupBinding, ...]:
        rows = self._connection.execute(
            "SELECT * FROM space_group_bindings WHERE space_group_id = ? ORDER BY bound_us",
            (space_group_id,),
        ).fetchall()
        return tuple(self._binding_row(row) for row in rows)

    def insert_session(self, tenant_id: str, space_id: str, *, actor: str) -> Session:
        space = self.get_space(space_id)
        if space.tenant_id != tenant_id:
            raise ConflictError("space belongs to another tenant")
        session_id = str(self._ids.new())
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO sessions (id, tenant_id, space_id, status, started_us) "
            "VALUES (?, ?, ?, 'open', ?)",
            (session_id, tenant_id, space_id, now_us),
        )
        self.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="session.created",
            resource_type="session",
            resource_id=session_id,
            reason_code="provisioning",
            details={"space_id": space_id},
        )
        return Session(
            id=session_id,
            tenant_id=tenant_id,
            space_id=space_id,
            status="open",
            started_us=now_us,
            ended_us=None,
        )

    def get_session(self, session_id: str) -> Session:
        row = _require(
            self._connection, "SELECT * FROM sessions WHERE id = ?", (session_id,), "session"
        )
        return Session(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            space_id=str(row["space_id"]),
            status=str(row["status"]),
            started_us=int(row["started_us"]),
            ended_us=int(row["ended_us"]) if row["ended_us"] is not None else None,
        )

    # -- audit passthrough so provisioning writes audit in the same tx --------

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
        ledger = LedgerRepository(self._connection, self._clock, self._ids)
        return ledger.audit(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            reason_code=reason_code,
            details=details,
            revision=revision,
        )


class IdentityRepository:
    """Entities, external identities, bindings, redirects and attributes."""

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def _entity_row(self, row: sqlite3.Row) -> Entity:
        labels = json.loads(str(row["privacy_labels"]))
        return Entity(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            kind=EntityKind(str(row["kind"])),
            state=EntityState(str(row["state"])),
            display_name=str(row["display_name"]),
            privacy_labels=tuple(str(label) for label in labels),
            revision=int(row["revision"]),
            created_us=int(row["created_us"]),
            updated_us=int(row["updated_us"]),
        )

    def insert_entity(
        self,
        tenant_id: str,
        kind: EntityKind,
        *,
        display_name: str = "",
        privacy_labels: Sequence[str] = (),
        actor: str = "system",
    ) -> Entity:
        entity_id = str(self._ids.new())
        now_us = self._clock.now_us()
        labels_json = json.dumps(list(privacy_labels), ensure_ascii=False, sort_keys=True)
        self._connection.execute(
            "INSERT INTO entities (id, tenant_id, kind, state, display_name, privacy_labels, "
            "revision, created_us, updated_us) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                entity_id,
                tenant_id,
                kind.value,
                EntityState.PROVISIONAL.value,
                display_name,
                labels_json,
                now_us,
                now_us,
            ),
        )
        self._connection.execute(
            "INSERT INTO entity_revisions (id, entity_id, revision, kind, state, display_name, "
            "privacy_labels, changed_by, reason_code, content_hash, created_us) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'created', ?, ?)",
            (
                str(self._ids.new()),
                entity_id,
                kind.value,
                EntityState.PROVISIONAL.value,
                display_name,
                labels_json,
                actor,
                content_hash(
                    {
                        "kind": kind.value,
                        "state": EntityState.PROVISIONAL.value,
                        "display_name": display_name,
                        "privacy_labels": list(privacy_labels),
                    }
                ),
                now_us,
            ),
        )
        return self._entity_row(
            _require(
                self._connection, "SELECT * FROM entities WHERE id = ?", (entity_id,), "entity"
            )
        )

    def get_entity(self, entity_id: str) -> Entity:
        row = _require(
            self._connection, "SELECT * FROM entities WHERE id = ?", (entity_id,), "entity"
        )
        return self._entity_row(row)

    def update_entity_state(
        self,
        entity_id: str,
        state: EntityState,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> Entity:
        current = self.get_entity(entity_id)
        now_us = self._clock.now_us()
        new_revision = _cas_bump(
            self._connection,
            "entities",
            entity_id,
            expected_revision,
            {"state": state.value},
            now_us,
        )
        labels_json = json.dumps(list(current.privacy_labels), ensure_ascii=False, sort_keys=True)
        self._connection.execute(
            "INSERT INTO entity_revisions (id, entity_id, revision, kind, state, display_name, "
            "privacy_labels, changed_by, reason_code, content_hash, created_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(self._ids.new()),
                entity_id,
                new_revision,
                current.kind.value,
                state.value,
                current.display_name,
                labels_json,
                actor,
                reason_code,
                content_hash(
                    {
                        "kind": current.kind.value,
                        "state": state.value,
                        "display_name": current.display_name,
                        "privacy_labels": list(current.privacy_labels),
                    }
                ),
                now_us,
            ),
        )
        return self.get_entity(entity_id)

    def insert_external_identity(
        self, key: ExternalIdentityKey, *, entity_id: str | None
    ) -> ExternalIdentity:
        existing = self.find_external_identity(key)
        if existing is not None:
            raise ConflictError(
                "external identity already registered",
                details={"external_identity_id": existing.id},
            )
        identity_id = str(self._ids.new())
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO external_identities (id, tenant_id, provider, realm, external_id, "
            "entity_id, created_us, updated_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identity_id,
                key.tenant_id,
                key.provider,
                key.realm,
                key.external_id,
                entity_id,
                now_us,
                now_us,
            ),
        )
        return self.get_external_identity(identity_id)

    def get_external_identity(self, external_identity_id: str) -> ExternalIdentity:
        row = _require(
            self._connection,
            "SELECT * FROM external_identities WHERE id = ?",
            (external_identity_id,),
            "external identity",
        )
        return self._identity_row(row)

    def find_external_identity(self, key: ExternalIdentityKey) -> ExternalIdentity | None:
        row = _one(
            self._connection,
            "SELECT * FROM external_identities WHERE tenant_id = ? AND provider = ? "
            "AND realm = ? AND external_id = ?",
            (key.tenant_id, key.provider, key.realm, key.external_id),
        )
        return self._identity_row(row) if row is not None else None

    @staticmethod
    def _identity_row(row: sqlite3.Row) -> ExternalIdentity:
        return ExternalIdentity(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            provider=str(row["provider"]),
            realm=str(row["realm"]),
            external_id=str(row["external_id"]),
            entity_id=str(row["entity_id"]) if row["entity_id"] is not None else None,
            created_us=int(row["created_us"]),
            updated_us=int(row["updated_us"]),
        )

    def _binding_row(self, row: sqlite3.Row) -> Binding:
        return Binding(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            external_identity_id=str(row["external_identity_id"]),
            entity_id=str(row["entity_id"]),
            state=BindingState(str(row["state"])),
            method=BindingMethod(str(row["method"])),
            confidence=float(row["confidence"]),
            proof_digest=str(row["proof_digest"]),
            valid_from_us=int(row["valid_from_us"]),
            valid_until_us=int(row["valid_until_us"])
            if row["valid_until_us"] is not None
            else None,
            revision=int(row["revision"]),
            created_us=int(row["created_us"]),
            updated_us=int(row["updated_us"]),
            created_by=str(row["created_by"]),
            confirmed_by=str(row["confirmed_by"]) if row["confirmed_by"] is not None else None,
            revoked_by=str(row["revoked_by"]) if row["revoked_by"] is not None else None,
        )

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
        identity = self.get_external_identity(external_identity_id)
        entity = self.get_entity(entity_id)
        if identity.tenant_id != tenant_id or entity.tenant_id != tenant_id:
            raise ConflictError("identity and entity must belong to the tenant")
        # Rival proposals are allowed to coexist; the one-verified-per-identity
        # invariant is enforced on transition (partial unique index + service).
        binding_id = str(self._ids.new())
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO bindings (id, tenant_id, external_identity_id, entity_id, state, method, "
            "confidence, proof_digest, valid_from_us, revision, created_us, updated_us, "
            "created_by) "
            "VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                binding_id,
                tenant_id,
                external_identity_id,
                entity_id,
                method.value,
                confidence,
                proof_digest,
                now_us,
                now_us,
                now_us,
                actor,
            ),
        )
        self._connection.execute(
            "INSERT INTO binding_revisions (id, binding_id, revision, state, changed_by, "
            "reason_code, created_us) VALUES (?, ?, 1, 'proposed', ?, ?, ?)",
            (str(self._ids.new()), binding_id, actor, reason_code, now_us),
        )
        return self._binding_row(
            _require(
                self._connection, "SELECT * FROM bindings WHERE id = ?", (binding_id,), "binding"
            )
        )

    def get_binding(self, binding_id: str) -> Binding:
        row = _require(
            self._connection, "SELECT * FROM bindings WHERE id = ?", (binding_id,), "binding"
        )
        return self._binding_row(row)

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
        current = self.get_binding(binding_id)
        transition_binding(current.state, target)
        now_us = self._clock.now_us()
        assignments: dict[str, Any] = {"state": target.value}
        if target is BindingState.VERIFIED:
            assignments["confirmed_by"] = actor
            assignments["confirmed_us"] = now_us
        if target is BindingState.REVOKED:
            assignments["revoked_by"] = actor
            assignments["revoked_us"] = now_us
            assignments["valid_until_us"] = now_us
        try:
            new_revision = _cas_bump(
                self._connection, "bindings", binding_id, expected_revision, assignments, now_us
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                "another verified binding exists for this external identity",
                details={"binding_id": binding_id, "target": target.value},
            ) from error
        self._connection.execute(
            "INSERT INTO binding_revisions (id, binding_id, revision, state, changed_by, "
            "reason_code, note, created_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(self._ids.new()),
                binding_id,
                new_revision,
                target.value,
                actor,
                reason_code,
                note,
                now_us,
            ),
        )
        return self.get_binding(binding_id)

    def binding_state_events(self, binding_id: str) -> tuple[tuple[int, BindingState], ...]:
        rows = self._connection.execute(
            "SELECT created_us, state FROM binding_revisions WHERE binding_id = ? "
            "ORDER BY revision",
            (binding_id,),
        ).fetchall()
        return tuple((int(row["created_us"]), BindingState(str(row["state"]))) for row in rows)

    def verified_binding_for(self, external_identity_id: str) -> Binding | None:
        row = _one(
            self._connection,
            "SELECT * FROM bindings WHERE external_identity_id = ? AND state = 'verified' "
            f"AND {_NOT_TOMBSTONED_BINDING}",
            (external_identity_id,),
        )
        return self._binding_row(row) if row is not None else None

    def bindings_for(self, external_identity_id: str) -> tuple[Binding, ...]:
        rows = self._connection.execute(
            "SELECT * FROM bindings WHERE external_identity_id = ? "
            f"AND {_NOT_TOMBSTONED_BINDING} ORDER BY created_us",
            (external_identity_id,),
        ).fetchall()
        return tuple(self._binding_row(row) for row in rows)

    def insert_entity_redirect(
        self,
        tenant_id: str,
        from_entity_id: str,
        to_entity_id: str,
        *,
        actor: str,
        reason_code: str,
    ) -> EntityRedirect:
        source = self.get_entity(from_entity_id)
        target = self.get_entity(to_entity_id)
        if source.tenant_id != tenant_id or target.tenant_id != tenant_id:
            raise ConflictError("redirect endpoints must belong to the tenant")
        redirect_id = str(self._ids.new())
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO entity_redirects (id, tenant_id, from_entity_id, to_entity_id, "
            "reason_code, created_by, created_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (redirect_id, tenant_id, from_entity_id, to_entity_id, reason_code, actor, now_us),
        )
        return EntityRedirect(
            id=redirect_id,
            tenant_id=tenant_id,
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            reason_code=reason_code,
            created_by=actor,
            created_us=now_us,
        )

    def get_entity_redirect(self, from_entity_id: str) -> EntityRedirect | None:
        row = _one(
            self._connection,
            "SELECT * FROM entity_redirects WHERE from_entity_id = ? "
            f"AND {_NOT_TOMBSTONED_REDIRECT}",
            (from_entity_id,),
        )
        if row is None:
            return None
        return EntityRedirect(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            from_entity_id=str(row["from_entity_id"]),
            to_entity_id=str(row["to_entity_id"]),
            reason_code=str(row["reason_code"]),
            created_by=str(row["created_by"]),
            created_us=int(row["created_us"]),
        )

    def redirect_map(self, tenant_id: str) -> dict[str, str]:
        rows = self._connection.execute(
            "SELECT from_entity_id, to_entity_id FROM entity_redirects "
            f"WHERE tenant_id = ? AND {_NOT_TOMBSTONED_REDIRECT}",
            (tenant_id,),
        ).fetchall()
        return {str(row["from_entity_id"]): str(row["to_entity_id"]) for row in rows}

    def redirects_active_at(self, tenant_id: str, at_us: int) -> dict[str, str]:
        rows = self._connection.execute(
            "SELECT from_entity_id, to_entity_id FROM entity_redirects "
            f"WHERE tenant_id = ? AND created_us <= ? AND {_NOT_TOMBSTONED_REDIRECT}",
            (tenant_id, at_us),
        ).fetchall()
        return {str(row["from_entity_id"]): str(row["to_entity_id"]) for row in rows}

    def _attribute_row(self, row: sqlite3.Row) -> IdentityAttribute:
        return IdentityAttribute(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            entity_id=str(row["entity_id"]),
            field=str(row["field"]),
            value=str(row["value"]),
            authority=FieldAuthority(str(row["authority"])),
            source_ref=str(row["source_ref"]),
            effective_us=int(row["effective_us"]),
            recorded_us=int(row["recorded_us"]),
            superseded_us=int(row["superseded_us"]) if row["superseded_us"] is not None else None,
            status=str(row["status"]),
        )

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
        entity = self.get_entity(entity_id)
        if entity.tenant_id != tenant_id:
            raise ConflictError("entity belongs to another tenant")
        now_us = self._clock.now_us()
        effective = now_us if effective_us is None else effective_us
        current = self.current_identity_attribute(tenant_id, entity_id, field)
        if current is None:
            decision = None
        else:
            decision = decide_attribute_merge(current.authority, current.value, authority, value)
        if (
            current is not None
            and decision is not None
            and decision.outcome is MergeOutcome.IGNORED
        ):
            return AttributeWrite(
                current=current, outcome=MergeOutcome.IGNORED.value, changed=False
            )
        if (
            current is not None
            and decision is not None
            and decision.outcome is MergeOutcome.SUPERSEDE
        ):
            self._connection.execute(
                "UPDATE identity_attributes SET status = 'superseded', superseded_us = ? "
                "WHERE id = ? AND status = 'current'",
                (now_us, current.id),
            )
        status = (
            "current"
            if (decision is None or decision.outcome is MergeOutcome.SUPERSEDE)
            else "conflict"
        )
        attribute_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO identity_attributes (id, tenant_id, entity_id, field, value, authority, "
            "source_ref, effective_us, recorded_us, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attribute_id,
                tenant_id,
                entity_id,
                field,
                value,
                authority.value,
                source_ref,
                effective,
                now_us,
                status,
            ),
        )
        return AttributeWrite(
            current=self._attribute_row(
                _require(
                    self._connection,
                    "SELECT * FROM identity_attributes WHERE id = ?",
                    (attribute_id,),
                    "identity attribute",
                )
            ),
            outcome=(
                MergeOutcome.SUPERSEDE.value if status == "current" else MergeOutcome.COEXIST.value
            ),
            changed=True,
        )

    def current_identity_attribute(
        self, tenant_id: str, entity_id: str, field: str
    ) -> IdentityAttribute | None:
        row = _one(
            self._connection,
            "SELECT * FROM identity_attributes WHERE entity_id = ? AND field = ? "
            "AND status = 'current'",
            (entity_id, field),
        )
        return self._attribute_row(row) if row is not None else None

    def conflicted_attributes(self, entity_id: str) -> tuple[IdentityAttribute, ...]:
        rows = self._connection.execute(
            "SELECT * FROM identity_attributes WHERE entity_id = ? AND status = 'conflict' "
            "ORDER BY recorded_us",
            (entity_id,),
        ).fetchall()
        return tuple(self._attribute_row(row) for row in rows)


class LedgerRepository:
    """Audit events, watermarks, tombstones and resource links."""

    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

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
        event_id = str(self._ids.new())
        now_us = self._clock.now_us()
        payload = json.dumps(details or {}, ensure_ascii=False, sort_keys=True, default=str)
        self._connection.execute(
            "INSERT INTO audit_events (id, tenant_id, actor, action, resource_type, resource_id, "
            "revision, reason_code, details, created_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                tenant_id,
                actor,
                action,
                resource_type,
                resource_id,
                revision,
                reason_code,
                payload,
                now_us,
            ),
        )
        return AuditEvent(
            id=event_id,
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            revision=revision,
            reason_code=reason_code,
            details=details or {},
            created_us=now_us,
        )

    def advance_watermark(
        self,
        tenant_id: str,
        agent_id: str,
        entries: Sequence[tuple[str, str, int]],
    ) -> int:
        now_us = self._clock.now_us()
        cursor = self._connection.execute(
            "INSERT INTO agent_watermarks (tenant_id, agent_id, current_seq, updated_us) "
            "VALUES (?, ?, 1, ?) ON CONFLICT (tenant_id, agent_id) DO UPDATE SET "
            "current_seq = current_seq + 1, updated_us = excluded.updated_us "
            "RETURNING current_seq",
            (tenant_id, agent_id, now_us),
        )
        seq = int(cursor.fetchone()[0])
        for aggregate_type, aggregate_id, aggregate_revision in entries:
            self._connection.execute(
                "INSERT INTO agent_watermark_entries (tenant_id, agent_id, seq, aggregate_type, "
                "aggregate_id, aggregate_revision, recorded_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    agent_id,
                    seq,
                    aggregate_type,
                    aggregate_id,
                    aggregate_revision,
                    now_us,
                ),
            )
        return seq

    def watermark(self, tenant_id: str, agent_id: str) -> WatermarkState | None:
        row = _one(
            self._connection,
            "SELECT * FROM agent_watermarks WHERE tenant_id = ? AND agent_id = ?",
            (tenant_id, agent_id),
        )
        if row is None:
            return None
        return WatermarkState(
            tenant_id=tenant_id,
            agent_id=agent_id,
            current_seq=int(row["current_seq"]),
            updated_us=int(row["updated_us"]),
        )

    def record_tombstone(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        reason_code: str,
        deleted_by: str,
    ) -> Tombstone:
        now_us = self._clock.now_us()
        tombstone_id = str(self._ids.new())
        seq_row = self._connection.execute(
            "SELECT COALESCE(MAX(tombstone_seq), 0) + 1 FROM resource_tombstones"
        ).fetchone()
        seq = int(seq_row[0]) if seq_row is not None else 1
        self._connection.execute(
            "INSERT INTO resource_tombstones (id, tenant_id, resource_type, resource_id, "
            "reason_code, deleted_by, created_us, tombstone_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tombstone_id,
                tenant_id,
                resource_type,
                resource_id,
                reason_code,
                deleted_by,
                now_us,
                seq,
            ),
        )
        return Tombstone(
            id=tombstone_id,
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            reason_code=reason_code,
            deleted_by=deleted_by,
            created_us=now_us,
            tombstone_seq=seq,
        )

    def is_tombstoned(self, tenant_id: str, resource_type: str, resource_id: str) -> bool:
        row = _one(
            self._connection,
            "SELECT 1 FROM resource_tombstones WHERE tenant_id = ? AND resource_type = ? "
            "AND resource_id = ?",
            (tenant_id, resource_type, resource_id),
        )
        return row is not None

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
        now_us = self._clock.now_us()
        link_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO resource_links (id, tenant_id, source_type, source_id, target_type, "
                "target_id, relation, created_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    link_id,
                    tenant_id,
                    source_type,
                    source_id,
                    target_type,
                    target_id,
                    relation,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError:
            row = _one(
                self._connection,
                "SELECT * FROM resource_links WHERE tenant_id = ? AND source_type = ? AND "
                "source_id = ? AND target_type = ? AND target_id = ? AND relation = ?",
                (tenant_id, source_type, source_id, target_type, target_id, relation),
            )
            assert row is not None
            link_id = str(row["id"])
            now_us = int(row["created_us"])
        return ResourceLink(
            id=link_id,
            tenant_id=tenant_id,
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relation=relation,
            created_us=now_us,
        )

    def tombstone_watermark(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(tombstone_seq), 0) FROM resource_tombstones"
        ).fetchone()
        return int(row[0]) if row is not None else 0
