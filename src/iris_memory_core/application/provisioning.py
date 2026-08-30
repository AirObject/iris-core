"""Tenant, agent, space-group and space provisioning (§5, Phase 1.2).

Management-plane operations (space groups and their bindings) require an admin
AccessContext and a reason code. Reads enforce the scope null semantics and
privacy labels through the domain rules — never by trusting the body.

Every write path accepts an idempotency key; replays return the first outcome
snapshot, never a re-read of the (since possibly mutated) aggregate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar, cast

from iris_memory_core.application.ports import IdempotencyRunner, Transaction, UnitOfWork
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    IdempotencyUnavailableError,
    NotFoundError,
    require_reason,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.model import (
    Agent,
    Session,
    Space,
    SpaceGroup,
    SpaceGroupBinding,
    Tenant,
    record_restore,
    snapshot_json,
)
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.scope import Scope, scope_allows

_T = TypeVar("_T")

ADMIN_MANAGEMENT_ACTIONS = frozenset(
    {
        "create_agent",
        "create_space_group",
        "update_space_group",
        "bind_space",
        "unbind_space",
    }
)


def _require_admin(action: str, access: AccessContext) -> None:
    if not access.admin:
        raise AccessDeniedError(
            f"{action} belongs to the management plane and requires admin access"
        )


def _same_tenant(access: AccessContext, tenant_id: str) -> None:
    if access.tenant_id != tenant_id:
        raise AccessDeniedError("cross-tenant access is denied")


def _first_outcome(replayed: bool, body: str, record_type: type[_T], fresh: _T | None) -> _T:
    if not replayed:
        assert fresh is not None
        return fresh
    return cast(_T, record_restore(record_type, json.loads(body)))


class ProvisioningService:
    def __init__(self, uow: UnitOfWork, idempotency: IdempotencyRunner | None = None) -> None:
        self._uow = uow
        self._idempotency = idempotency

    def _run_operation(
        self,
        access: AccessContext,
        *,
        operation: str,
        idempotency_key: str | None,
        payload: dict[str, object],
        op: Callable[[Transaction], tuple[str, str, list[str]]],
    ) -> tuple[str, str, bool]:
        if idempotency_key is not None:
            if self._idempotency is None:
                raise IdempotencyUnavailableError(
                    "idempotency key supplied but no idempotency runner is configured"
                )
            result = self._idempotency.run(
                tenant_id=access.tenant_id,
                app_instance_id=access.app_instance_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint(operation, payload),
                execute=op,
            )
            return result.code, result.body, result.replayed
        with self._uow.write() as tx:
            code, body, _refs = op(tx)
        return code, body, False

    # -- bootstrap -----------------------------------------------------------

    def create_tenant(
        self, tenant_id: str, *, status: str = "active", idempotency_key: str | None = None
    ) -> Tenant:
        """Bootstrap tenant creation; idempotent by primary key and, when a
        runner is wired, by explicit key under the synthetic ``bootstrap``
        app instance."""

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            tenant = tx.insert_tenant(tenant_id, status=status)
            tx.audit(
                tenant_id=tenant_id,
                actor="bootstrap",
                action="tenant.created",
                resource_type="tenant",
                resource_id=tenant_id,
                reason_code="bootstrap",
            )
            return "created", snapshot_json(tenant), [tenant.id]

        bootstrap = AccessContext(tenant_id=tenant_id, app_instance_id="bootstrap", admin=True)
        _code, body, replayed = self._run_operation(
            bootstrap,
            operation="create_tenant",
            idempotency_key=idempotency_key,
            payload={"tenant_id": tenant_id, "status": status},
            op=op,
        )
        if replayed:
            return _first_outcome(replayed, body, Tenant, None)
        with self._uow.read() as tx:
            return tx.get_tenant(tenant_id)

    def create_agent(
        self,
        access: AccessContext,
        display_name: str,
        *,
        idempotency_key: str | None = None,
    ) -> Agent:
        """Create an agent with its locked bootstrap persona and watermark (ADR-0008)."""
        _require_admin("create_agent", access)
        tenant_id = access.tenant_id
        fresh: list[Agent] = []

        def _create(tx: Transaction) -> tuple[str, str, list[str]]:
            agent = tx.insert_agent(tenant_id, display_name, actor="admin")
            tx.advance_watermark(
                tenant_id,
                agent.id,
                (
                    ("agent", agent.id, 1),
                    ("persona_revision", agent.persona_current_revision_id or "", 1),
                ),
            )
            fresh.append(agent)
            return "created", snapshot_json(agent), [agent.id]

        _code, body, replayed = self._run_operation(
            access,
            operation="create_agent",
            idempotency_key=idempotency_key,
            payload={"tenant_id": tenant_id, "display_name": display_name},
            op=_create,
        )
        return _first_outcome(replayed, body, Agent, fresh[0] if fresh else None)

    # -- space groups (management plane) ---------------------------------------

    def create_space_group(
        self,
        access: AccessContext,
        name: str,
        *,
        description: str = "",
        reason: str,
        idempotency_key: str | None = None,
    ) -> SpaceGroup:
        _require_admin("create_space_group", access)
        reason_code = require_reason(reason)
        fresh: list[SpaceGroup] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            group = tx.insert_space_group(
                access.tenant_id, name, description, actor="admin", reason_code=reason_code
            )
            fresh.append(group)
            return "created", snapshot_json(group), [group.id]

        _code, body, replayed = self._run_operation(
            access,
            operation="create_space_group",
            idempotency_key=idempotency_key,
            payload={"name": name, "description": description, "reason": reason},
            op=op,
        )
        return _first_outcome(replayed, body, SpaceGroup, fresh[0] if fresh else None)

    def update_space_group(
        self,
        access: AccessContext,
        space_group_id: str,
        *,
        name: str,
        description: str = "",
        expected_revision: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> SpaceGroup:
        _require_admin("update_space_group", access)
        reason_code = require_reason(reason)
        fresh: list[SpaceGroup] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            existing = tx.get_space_group(space_group_id)
            _same_tenant(access, existing.tenant_id)
            updated = tx.update_space_group(
                space_group_id,
                name=name,
                description=description,
                expected_revision=expected_revision,
                actor="admin",
                reason_code=reason_code,
            )
            fresh.append(updated)
            return "updated", snapshot_json(updated), [space_group_id]

        _code, body, replayed = self._run_operation(
            access,
            operation="update_space_group",
            idempotency_key=idempotency_key,
            payload={
                "space_group_id": space_group_id,
                "name": name,
                "description": description,
                "expected_revision": expected_revision,
                "reason": reason,
            },
            op=op,
        )
        return _first_outcome(replayed, body, SpaceGroup, fresh[0] if fresh else None)

    def bind_space_to_group(
        self,
        access: AccessContext,
        space_id: str,
        space_group_id: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> SpaceGroupBinding:
        _require_admin("bind_space", access)
        reason_code = require_reason(reason)
        fresh: list[SpaceGroupBinding] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            space = tx.get_space(space_id)
            _same_tenant(access, space.tenant_id)
            binding = tx.bind_space_to_group(
                access.tenant_id,
                space_id,
                space_group_id,
                expected_revision=expected_revision,
                actor="admin",
                reason_code=reason_code,
            )
            if space.agent_id is not None:
                tx.advance_watermark(
                    access.tenant_id,
                    space.agent_id,
                    (("space", space_id, expected_revision + 1),),
                )
            assert binding is not None
            fresh.append(binding)
            return "bound", snapshot_json(binding), [binding.id]

        _code, body, replayed = self._run_operation(
            access,
            operation="bind_space_to_group",
            idempotency_key=idempotency_key,
            payload={
                "space_id": space_id,
                "space_group_id": space_group_id,
                "expected_revision": expected_revision,
                "reason": reason,
            },
            op=op,
        )
        return _first_outcome(replayed, body, SpaceGroupBinding, fresh[0] if fresh else None)

    def unbind_space_from_group(
        self,
        access: AccessContext,
        space_id: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> SpaceGroupBinding:
        _require_admin("unbind_space", access)
        reason_code = require_reason(reason)
        fresh: list[SpaceGroupBinding] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            space = tx.get_space(space_id)
            _same_tenant(access, space.tenant_id)
            historical = tx.unbind_space_from_group(
                access.tenant_id,
                space_id,
                expected_revision=expected_revision,
                actor="admin",
                reason_code=reason_code,
            )
            if space.agent_id is not None:
                tx.advance_watermark(
                    access.tenant_id,
                    space.agent_id,
                    (("space", space_id, expected_revision + 1),),
                )
            assert historical is not None
            fresh.append(historical)
            return "unbound", snapshot_json(historical), [space_id]

        _code, body, replayed = self._run_operation(
            access,
            operation="unbind_space_from_group",
            idempotency_key=idempotency_key,
            payload={
                "space_id": space_id,
                "expected_revision": expected_revision,
                "reason": reason,
            },
            op=op,
        )
        return _first_outcome(replayed, body, SpaceGroupBinding, fresh[0] if fresh else None)

    # -- spaces and sessions -----------------------------------------------------

    def create_space(
        self,
        access: AccessContext,
        kind: str,
        *,
        agent_id: str | None = None,
        space_group_id: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> Space:
        """Create a space; the agent dimension must be inside the caller's grant.

        Creating a space already bound to a group is a management-plane
        operation: it requires admin, a reason, the group to be granted, and
        writes the binding history row in the same transaction (never a bare
        ``space_group_id`` column write).
        """
        _same_tenant(access, access.tenant_id)
        if agent_id is not None and agent_id not in access.agent_ids:
            raise AccessDeniedError("agent_id outside the caller's allowed set")
        reason_code: str | None = None
        if space_group_id is not None:
            _require_admin("bind_space", access)
            reason_code = require_reason(reason)
            if space_group_id not in access.allowed_space_group_ids:
                raise AccessDeniedError("space_group_id outside the caller's allowed set")
        fresh: list[Space] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            space = tx.insert_space(access.tenant_id, kind, agent_id=agent_id, actor="app")
            if agent_id is not None:
                tx.advance_watermark(access.tenant_id, agent_id, (("space", space.id, 1),))
            if space_group_id is not None:
                tx.bind_space_to_group(
                    access.tenant_id,
                    space.id,
                    space_group_id,
                    expected_revision=1,
                    actor="admin",
                    reason_code=reason_code or "provisioning",
                )
                if agent_id is not None:
                    tx.advance_watermark(access.tenant_id, agent_id, (("space", space.id, 2),))
            # The snapshot must carry the FINAL state of the tx (bound group,
            # revision 2), not the intermediate insert.
            final = tx.get_space(space.id)
            fresh.append(final)
            return "created", snapshot_json(final), [space.id]

        _code, body, replayed = self._run_operation(
            access,
            operation="create_space",
            idempotency_key=idempotency_key,
            payload={
                "kind": kind,
                "agent_id": agent_id,
                "space_group_id": space_group_id,
                "reason": reason,
            },
            op=op,
        )
        return _first_outcome(replayed, body, Space, fresh[0] if fresh else None)

    def create_session(
        self,
        access: AccessContext,
        space_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> Session:
        fresh: list[Session] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            space = tx.get_space(space_id)
            _same_tenant(access, space.tenant_id)
            self._authorize_space(access, space)
            session = tx.insert_session(access.tenant_id, space_id, actor="app")
            if space.agent_id is not None:
                tx.advance_watermark(
                    access.tenant_id, space.agent_id, (("session", session.id, 1),)
                )
            fresh.append(session)
            return "created", snapshot_json(session), [session.id]

        _code, body, replayed = self._run_operation(
            access,
            operation="create_session",
            idempotency_key=idempotency_key,
            payload={"space_id": space_id},
            op=op,
        )
        return _first_outcome(replayed, body, Session, fresh[0] if fresh else None)

    # -- scope-enforced reads ------------------------------------------------------

    @staticmethod
    def _authorize_space(access: AccessContext, space: Space) -> None:
        if (
            space.space_group_id is not None
            and space.space_group_id not in access.allowed_space_group_ids
        ):
            raise AccessDeniedError("space group not granted to this caller")
        if space.id not in access.allowed_space_ids:
            raise AccessDeniedError("space not granted to this caller")

    def get_agent(self, access: AccessContext, scope: Scope, agent_id: str) -> Agent:
        access.authorize_scope(scope)
        with self._uow.read() as tx:
            try:
                agent = tx.get_agent(agent_id)
            except NotFoundError:
                raise
            _same_tenant(access, agent.tenant_id)
            data_scope = Scope(tenant_id=agent.tenant_id, agent_id=agent.id)
            if not scope_allows(data_scope, scope):
                raise NotFoundError("agent not visible at the requested scope")
            return agent

    def get_agent_persona(self, access: AccessContext, scope: Scope, agent_id: str) -> str:
        """Current published persona revision id; agents are never Ready without it."""
        agent = self.get_agent(access, scope, agent_id)
        if agent.persona_current_revision_id is None:
            raise NotFoundError("agent has no published persona")
        with self._uow.read() as tx:
            return tx.get_persona_revision(agent.persona_current_revision_id).content_hash

    def get_space(self, access: AccessContext, scope: Scope, space_id: str) -> Space:
        access.authorize_scope(scope)
        with self._uow.read() as tx:
            space = tx.get_space(space_id)
        _same_tenant(access, space.tenant_id)
        data_scope = Scope(
            tenant_id=space.tenant_id,
            agent_id=space.agent_id,
            space_group_id=space.space_group_id,
            space_id=space.id,
        )
        if not scope_allows(data_scope, scope):
            raise NotFoundError("space not visible at the requested scope")
        return space

    def get_space_group(
        self, access: AccessContext, scope: Scope, space_group_id: str
    ) -> SpaceGroup:
        access.authorize_scope(scope)
        with self._uow.read() as tx:
            group = tx.get_space_group(space_group_id)
        _same_tenant(access, group.tenant_id)
        data_scope = Scope(tenant_id=group.tenant_id, space_group_id=group.id)
        if not (
            scope_allows(data_scope, scope) and evaluate_privacy((), data_scope, scope, access)
        ):
            raise NotFoundError("space group not visible at the requested scope")
        return group

    def group_binding_history(
        self,
        access: AccessContext,
        scope: Scope,
        space_group_id: str,
    ) -> tuple[SpaceGroupBinding, ...]:
        self.get_space_group(access, scope, space_group_id)
        with self._uow.read() as tx:
            return tx.list_group_bindings(space_group_id)

    def get_session(self, access: AccessContext, scope: Scope, session_id: str) -> Session:
        access.authorize_scope(scope)
        with self._uow.read() as tx:
            session = tx.get_session(session_id)
            space = tx.get_space(session.space_id)
        _same_tenant(access, session.tenant_id)
        data_scope = Scope(
            tenant_id=session.tenant_id,
            agent_id=space.agent_id,
            space_group_id=space.space_group_id,
            space_id=session.space_id,
            session_id=session.id,
        )
        if not scope_allows(data_scope, scope):
            raise NotFoundError("session not visible at the requested scope")
        return session
