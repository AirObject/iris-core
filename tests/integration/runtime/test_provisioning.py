"""Provisioning: bootstrap persona, space groups, bindings and scope reads."""

from __future__ import annotations

import pytest

from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    IdempotencyKeyReusedError,
    IdempotencyUnavailableError,
    NotFoundError,
    ReasonRequiredError,
    RevisionMismatchError,
    ScopeViolationError,
)
from iris_memory_core.domain.scope import Scope
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for


def test_agent_creation_writes_bootstrap_persona_pointer_watermark_and_audit(
    store: Store,
    provisioning: ProvisioningService,
    admin_access: AccessContext,
) -> None:
    agent = provisioning.create_agent(admin_access, "Iris")
    assert agent.persona_current_revision_id is not None
    with store.read() as tx:
        persona = tx.get_persona_revision(agent.persona_current_revision_id)
        watermark = tx.watermark(admin_access.tenant_id, agent.id)
        audits = (
            tx.raw()
            .execute(
                "SELECT action FROM audit_events WHERE resource_id = ? ORDER BY action",
                (agent.id,),
            )
            .fetchall()
        )
    assert persona.status == "published"
    assert persona.source == "bootstrap"
    assert persona.revision == 1
    assert len(persona.content_hash) == 64
    assert watermark is not None and watermark.current_seq >= 1
    assert {row[0] for row in audits} == {"agent.created"}
    # Locked: content is the documented stable default.
    assert persona.core and persona.traits == "[]" and persona.narrative == ""


def test_create_agent_requires_admin(store: Store, provisioning: ProvisioningService) -> None:
    access = access_for("tenant-a")
    with pytest.raises(AccessDeniedError):
        provisioning.create_agent(access, "Iris")


def test_create_agent_is_idempotent_by_key(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    first = provisioning.create_agent(admin_access, "Iris", idempotency_key="op-1")
    second = provisioning.create_agent(admin_access, "Iris", idempotency_key="op-1")
    assert second.id == first.id
    with pytest.raises(IdempotencyKeyReusedError):
        provisioning.create_agent(admin_access, "Other", idempotency_key="op-1")
    with store.read() as tx:
        count = tx.raw().execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    assert int(count) == 1


def test_space_group_lifecycle_with_expected_revision_and_history(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    group = provisioning.create_space_group(admin_access, "community", reason="ops: setup")
    renamed = provisioning.update_space_group(
        admin_access,
        group.id,
        name="community-v2",
        description="desc",
        expected_revision=1,
        reason="ops: rename",
    )
    assert renamed.revision == 2
    with pytest.raises(RevisionMismatchError):
        provisioning.update_space_group(
            admin_access,
            group.id,
            name="x",
            expected_revision=1,
            reason="ops: stale",
        )
    with store.read() as tx:
        history = (
            tx.raw()
            .execute(
                "SELECT revision, name FROM space_group_revisions WHERE space_group_id = ? "
                "ORDER BY revision",
                (group.id,),
            )
            .fetchall()
        )
    assert [(int(r), str(n)) for r, n in history] == [(1, "community"), (2, "community-v2")]


def test_management_operations_require_reason_and_admin(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    with pytest.raises(ReasonRequiredError):
        provisioning.create_space_group(admin_access, "g", reason="  ")
    user_access = access_for(admin_access.tenant_id)
    with pytest.raises(AccessDeniedError):
        provisioning.create_space_group(user_access, "g", reason="ops")
    with pytest.raises(AccessDeniedError):
        provisioning.update_space_group(
            user_access, "any", name="n", expected_revision=1, reason="ops"
        )


def test_space_group_binding_history_keeps_occurrence_time(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    group = provisioning.create_space_group(admin_access, "community", reason="ops: setup")
    space = provisioning.create_space(admin_access, "chat_group")
    provisioning.bind_space_to_group(
        admin_access, space.id, group.id, expected_revision=1, reason="ops: bind"
    )
    historical = provisioning.unbind_space_from_group(
        admin_access, space.id, expected_revision=2, reason="ops: unbind"
    )
    assert historical is not None and historical.unbound_us is not None
    group_access = access_for(
        admin_access.tenant_id, space_group_ids=frozenset({group.id}), admin=True
    )
    bindings = provisioning.group_binding_history(
        group_access,
        Scope(tenant_id=admin_access.tenant_id, space_group_id=group.id),
        group.id,
    )
    assert len(bindings) == 1
    assert bindings[0].unbound_us is not None  # occurrence-time group membership survives unbinding
    rebound = provisioning.bind_space_to_group(
        admin_access, space.id, group.id, expected_revision=3, reason="ops: rebind"
    )
    assert rebound.unbound_us is None


def test_space_cannot_have_two_active_group_bindings(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    group_a = provisioning.create_space_group(admin_access, "a", reason="ops")
    group_b = provisioning.create_space_group(admin_access, "b", reason="ops")
    space = provisioning.create_space(admin_access, "chat_group")
    provisioning.bind_space_to_group(
        admin_access, space.id, group_a.id, expected_revision=1, reason="ops: bind"
    )
    with pytest.raises(ConflictError):
        provisioning.bind_space_to_group(
            admin_access, space.id, group_b.id, expected_revision=2, reason="ops: conflict"
        )


def test_scope_reads_enforce_null_semantics(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    agent = provisioning.create_agent(admin_access, "Iris")
    creator = access_for(admin_access.tenant_id, agent_ids=frozenset({agent.id}), admin=True)
    space = provisioning.create_space(creator, "chat_group", agent_id=agent.id)
    space_access = access_for(
        admin_access.tenant_id,
        agent_ids=frozenset({agent.id}),
        space_ids=frozenset({space.id}),
    )
    session = provisioning.create_session(space_access, space.id)

    agent_access = access_for(admin_access.tenant_id, agent_ids=frozenset({agent.id}))
    tenant_scope = Scope(tenant_id=admin_access.tenant_id)
    agent_scope = Scope(tenant_id=admin_access.tenant_id, agent_id=agent.id)

    # Tenant-level request cannot read agent-scoped data (request null = only null data).
    with pytest.raises(NotFoundError):
        provisioning.get_agent(agent_access, tenant_scope, agent.id)
    assert provisioning.get_agent(agent_access, agent_scope, agent.id).id == agent.id

    # Session reads need the session dimension (space-granted caller, no session dim).
    space_scope = Scope(tenant_id=admin_access.tenant_id, space_id=space.id)
    with pytest.raises(NotFoundError):
        provisioning.get_session(space_access, space_scope, session.id)
    # An agent-only caller may not even name the space dimension.
    with pytest.raises(ScopeViolationError):
        provisioning.get_session(agent_access, space_scope, session.id)
    session_scope = Scope(
        tenant_id=admin_access.tenant_id,
        agent_id=agent.id,
        space_id=space.id,
        session_id=session.id,
    )
    assert provisioning.get_session(space_access, session_scope, session.id).id == session.id


def test_cross_tenant_and_cross_agent_reads_are_denied(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    agent = provisioning.create_agent(admin_access, "Iris")
    other_tenant = access_for("tenant-b", admin=True)
    with pytest.raises(AccessDeniedError):
        provisioning.get_agent(
            other_tenant,
            Scope(tenant_id="tenant-b", agent_id=agent.id),
            agent.id,
        )
    outsider_agent = access_for(admin_access.tenant_id, agent_ids=frozenset({"someone-else"}))
    with pytest.raises(ScopeViolationError):
        provisioning.get_agent(
            outsider_agent,
            Scope(tenant_id=admin_access.tenant_id, agent_id=agent.id),
            agent.id,
        )


def test_space_group_read_requires_group_scope_dimension(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    group = provisioning.create_space_group(admin_access, "community", reason="ops")
    group_access = access_for(
        admin_access.tenant_id, space_group_ids=frozenset({group.id}), admin=True
    )
    tenant_scope = Scope(tenant_id=admin_access.tenant_id)
    with pytest.raises(NotFoundError):
        provisioning.get_space_group(group_access, tenant_scope, group.id)
    group_scope = Scope(tenant_id=admin_access.tenant_id, space_group_id=group.id)
    assert provisioning.get_space_group(group_access, group_scope, group.id).id == group.id


def test_create_space_blocks_lateral_writes_outside_grants(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext
) -> None:
    """A caller limited to agent A must not create a space bound to agent B."""
    from iris_memory_core.domain.errors import AccessDeniedError

    agent_a = provisioning.create_agent(admin_access, "A")
    agent_b = provisioning.create_agent(admin_access, "B")
    group = provisioning.create_space_group(admin_access, "community", reason="ops")

    agent_a_access = access_for(admin_access.tenant_id, agent_ids=frozenset({agent_a.id}))
    with pytest.raises(AccessDeniedError):
        provisioning.create_space(agent_a_access, "chat_group", agent_id=agent_b.id)
    with pytest.raises(AccessDeniedError):
        provisioning.create_space(agent_a_access, "chat_group", agent_id="not-granted")

    # Group-bound creation requires admin + reason + group grant and writes history.
    user_access = access_for(
        admin_access.tenant_id,
        agent_ids=frozenset({agent_a.id}),
        space_group_ids=frozenset({group.id}),
    )
    with pytest.raises(AccessDeniedError):
        provisioning.create_space(
            user_access, "chat_group", agent_id=agent_a.id, space_group_id=group.id
        )
    admin_group_access = access_for(
        admin_access.tenant_id,
        agent_ids=frozenset({agent_a.id}),
        space_group_ids=frozenset({group.id}),
        admin=True,
    )
    with pytest.raises(ReasonRequiredError):
        provisioning.create_space(
            admin_group_access, "chat_group", agent_id=agent_a.id, space_group_id=group.id
        )
    space = provisioning.create_space(
        admin_group_access,
        "chat_group",
        agent_id=agent_a.id,
        space_group_id=group.id,
        reason="ops: bind at creation",
    )
    assert space.space_group_id == group.id
    assert space.revision == 2  # creation + binding CAS
    bindings = provisioning.group_binding_history(
        admin_group_access,
        Scope(tenant_id=admin_access.tenant_id, space_group_id=group.id),
        group.id,
    )
    assert len(bindings) == 1 and bindings[0].space_id == space.id


def test_create_and_bind_space_advances_watermark_once_with_final_revision(
    store: Store,
    provisioning: ProvisioningService,
    admin_access: AccessContext,
) -> None:
    """One canonical transaction = one watermark bump, recording the space's
    FINAL revision (2 after the group binding), not one bump per write."""
    from tests.conftest import access_for

    agent = provisioning.create_agent(admin_access, "Iris")
    group = provisioning.create_space_group(admin_access, "community", reason="ops: setup")
    scoped = access_for(
        admin_access.tenant_id,
        agent_ids=frozenset({agent.id}),
        space_group_ids=frozenset({group.id}),
        admin=True,
    )
    space = provisioning.create_space(
        scoped, "chat_group", agent_id=agent.id, space_group_id=group.id, reason="ops: bind"
    )
    assert space.revision == 2
    with store.read() as tx:
        state = tx.watermark(admin_access.tenant_id, agent.id)
        rows = (
            tx.raw()
            .execute(
                "SELECT aggregate_type, aggregate_id, aggregate_revision "
                "FROM agent_watermark_entries WHERE tenant_id = ? AND agent_id = ? AND seq = ?",
                (admin_access.tenant_id, agent.id, state.current_seq if state else -1),
            )
            .fetchall()
        )
    # seq 1: agent bootstrap; seq 2: the single create+bind transaction.
    assert state is not None and state.current_seq == 2
    assert [(row[0], row[1], int(row[2])) for row in rows] == [("space", space.id, 2)]


def test_provisioning_writes_accept_idempotency_keys(
    store: Store,
    provisioning: ProvisioningService,
    idempotency: IdempotencyManager,
    admin_access: AccessContext,
) -> None:
    wired = ProvisioningService(store, idempotency)
    group = wired.create_space_group(admin_access, "g", reason="r", idempotency_key="g-1")
    replay_group = wired.create_space_group(admin_access, "g", reason="r", idempotency_key="g-1")
    assert replay_group == group  # first-outcome snapshot, byte-for-byte
    agent = wired.create_agent(admin_access, "A", idempotency_key="a-1")
    replay_agent = wired.create_agent(admin_access, "A", idempotency_key="a-1")
    assert replay_agent.id == agent.id and replay_agent.created_us == agent.created_us
    scoped = access_for(admin_access.tenant_id, agent_ids=frozenset({agent.id}))
    space = wired.create_space(scoped, "direct", agent_id=agent.id, idempotency_key="s-1")
    replay_space = wired.create_space(scoped, "direct", agent_id=agent.id, idempotency_key="s-1")
    assert replay_space.id == space.id and replay_space.created_us == space.created_us
    scoped_space = access_for(
        admin_access.tenant_id, agent_ids=frozenset({agent.id}), space_ids=frozenset({space.id})
    )
    session = wired.create_session(scoped_space, space.id, idempotency_key="sess-1")
    replay_session = wired.create_session(scoped_space, space.id, idempotency_key="sess-1")
    assert replay_session.id == session.id and replay_session.started_us == session.started_us
    with pytest.raises(IdempotencyKeyReusedError):
        wired.create_space(scoped, "local", agent_id=agent.id, idempotency_key="s-1")
    with store.read() as tx:
        counts = (
            tx.raw().execute("SELECT COUNT(*) FROM agents").fetchone()[0],
            tx.raw().execute("SELECT COUNT(*) FROM spaces").fetchone()[0],
            tx.raw().execute("SELECT COUNT(*) FROM space_groups").fetchone()[0],
            tx.raw().execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        )
    assert counts == (1, 1, 1, 1)  # replays executed nothing


def test_provisioning_idempotency_partitions_by_app_instance(
    store: Store, idempotency: IdempotencyManager, admin_access: AccessContext
) -> None:
    import dataclasses

    wired = ProvisioningService(store, idempotency)
    other_app = dataclasses.replace(admin_access, app_instance_id="app-2")
    first = wired.create_agent(admin_access, "A", idempotency_key="shared")
    second = wired.create_agent(other_app, "A", idempotency_key="shared")
    assert first.id != second.id  # same key, different app instance: no collision


def test_provisioning_idempotency_key_without_runner_fails_loudly(
    store: Store, admin_access: AccessContext
) -> None:
    unwired = ProvisioningService(store)
    with pytest.raises(IdempotencyUnavailableError):
        unwired.create_agent(admin_access, "A", idempotency_key="k")
    with pytest.raises(IdempotencyUnavailableError):
        unwired.create_space(admin_access, "direct", idempotency_key="k")


def test_create_tenant_accepts_idempotency_key(
    store: Store, idempotency: IdempotencyManager
) -> None:
    wired = ProvisioningService(store, idempotency)
    tenant = wired.create_tenant("tenant-idem", idempotency_key="t-1")
    replay = wired.create_tenant("tenant-idem", idempotency_key="t-1")
    assert replay == tenant  # bootstrap app instance replays the first outcome
    with store.read() as tx:
        count = (
            tx.raw().execute("SELECT COUNT(*) FROM tenants WHERE id = 'tenant-idem'").fetchone()[0]
        )
    assert int(count) == 1
    unwired = ProvisioningService(store)
    with pytest.raises(IdempotencyUnavailableError):
        unwired.create_tenant("tenant-x", idempotency_key="t-2")
