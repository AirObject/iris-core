"""Management command authorization, replay and transaction boundary tests.

The callback below is a transaction probe; domain command routes remain a
separate integration slice and are not claimed by this executor test.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.resources import ResourceRef
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    IdempotencyKeyReusedError,
    NotFoundError,
)
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_reads import grant_for
from tests.integration.console.test_console_reads import world as read_world_fixture

auth = auth_fixture
world = read_world_fixture


def principal_for(world: dict[str, Any], *, limited: bool = False) -> OperatorPrincipal:
    token = world["token"]
    if limited:
        _, token = world["security"].issue_offline(
            tenant_id=world["tenant"],
            label="writer",
            description="command fixture",
            template="maintainer",
            grant=grant_for(world, permissions=frozenset({"memory.read", "memory.write"})),
            expires_us=world["store"].clock.now_us() + 3_600_000_000,
        )
    principal, _ = world["security"].login(token, client_digest="0" * 64)
    assert isinstance(principal, OperatorPrincipal)
    return principal


def target_for(world: dict[str, Any], name: str = "a") -> CommandTarget:
    with world["store"].read() as tx:
        record = tx.console_reads.get("notes", world["tenant"], world["ids"][name])
    return CommandTarget("note", record.scope, resource_id=record.id)


def probe(world: dict[str, Any], *, fail: bool = False) -> Any:
    def mutate(tx: Any, actor: CommandActor) -> tuple[str, str, list[str]]:
        assert actor.origin == "console"
        tx.audit(
            tenant_id=world["tenant"],
            actor=actor.audit_actor,
            action="test.command.write",
            resource_type="note",
            resource_id=world["ids"]["a"],
            reason_code=actor.reason_code,
            details={},
        )
        if fail:
            raise RuntimeError("domain command failed")
        return (
            "note.updated",
            json.dumps({"note_id": world["ids"]["a"]}),
            ["note:" + world["ids"]["a"]],
        )

    return mutate


def count(world: dict[str, Any]) -> int:
    with world["store"].read() as tx:
        return int(
            tx.raw()
            .execute("SELECT COUNT(*) FROM audit_events WHERE action='test.command.write'")
            .fetchone()[0]
        )


def run(world: dict[str, Any], principal: OperatorPrincipal, **overrides: Any) -> Any:
    values: dict[str, Any] = dict(
        operation="note.update",
        target=target_for(world),
        payload={"expected_revision": 1},
        idempotency_key="command-one",
        execute=probe(world),
    )
    values.update(overrides)
    return ConsoleCommandExecutor(world["security"]).run(principal, **values)


def invalidate(world: dict[str, Any], principal: OperatorPrincipal, kind: str) -> None:
    with world["store"].write() as tx:
        key = tx.console.key(principal.key.id)
        session = tx.console.session(principal.session.id)
        now = world["store"].clock.now_us()
        if kind == "epoch":
            tx.console.save_session(replace(session, epoch=session.epoch + 1))
        elif kind == "session-expired":
            tx.console.save_session(replace(session, idle_expires_us=now))
        else:
            changes: dict[str, Any] = {"revision": key.revision + 1}
            if kind == "revoked":
                changes["status"] = "revoked"
                changes["revoked_us"] = now
                changes["revoke_reason"] = "operator_request"
            elif kind == "key-expired":
                changes["expires_us"] = now
            elif kind == "permission":
                changes["grant"] = replace(key.grant, permissions=frozenset({"memory.read"}))
            tx.console.save_key(replace(key, **changes), expected_revision=key.revision)


def test_management_replay_has_one_atomic_mutation(world: dict[str, Any]) -> None:
    principal = principal_for(world, limited=True)
    first, replay = run(world, principal), run(world, principal)
    assert not first.replayed and replay.replayed
    assert first.body == replay.body and count(world) == 1
    with pytest.raises(IdempotencyKeyReusedError):
        run(world, principal, payload={"expected_revision": 2})
    assert count(world) == 1


@pytest.mark.parametrize("name", ["global", "b", "restricted", "custom", "private"])
def test_limited_writer_cannot_modify_visible_parent_or_ungranted_data(
    world: dict[str, Any], name: str
) -> None:
    principal = principal_for(world, limited=True)
    with pytest.raises((AccessDeniedError, NotFoundError)):
        run(world, principal, target=target_for(world, name))
    assert count(world) == 0


def test_hidden_source_ref_is_rejected_before_idempotency(world: dict[str, Any]) -> None:
    principal = principal_for(world, limited=True)
    target = replace(target_for(world), source_refs=(ResourceRef("note", world["ids"]["private"]),))
    with pytest.raises(NotFoundError):
        run(world, principal, target=target)
    assert count(world) == 0


@pytest.mark.parametrize(
    "kind", ["revoked", "permission", "epoch", "session-expired", "key-expired", "revision"]
)
def test_permission_change_before_commit_rolls_back(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    principal = principal_for(world)
    runner = world["security"].idempotency
    original = runner.run

    def race(**values: Any) -> Any:
        invalidate(world, principal, kind)
        return original(**values)

    monkeypatch.setattr(runner, "run", race)
    with pytest.raises(AccessDeniedError):
        run(world, principal)
    assert count(world) == 0


@pytest.mark.parametrize(
    "kind", ["revoked", "permission", "epoch", "session-expired", "key-expired"]
)
def test_cached_result_is_reauthorized_after_loading(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    principal = principal_for(world)
    run(world, principal)
    runner = world["security"].idempotency
    original = runner.run

    def race(**values: Any) -> Any:
        result = original(**values)
        assert result.replayed
        invalidate(world, principal, kind)
        return result

    monkeypatch.setattr(runner, "run", race)
    with pytest.raises(AccessDeniedError):
        run(world, principal)
    assert count(world) == 1


def test_failed_domain_callback_rolls_back_its_mutation(world: dict[str, Any]) -> None:
    principal = principal_for(world)
    with pytest.raises(RuntimeError, match="domain command failed"):
        run(world, principal, execute=probe(world, fail=True))
    assert count(world) == 0
    assert not run(world, principal).replayed
    assert count(world) == 1


def test_real_note_command_in_required_mode_preserves_host_gate(world: dict[str, Any]) -> None:
    from iris_memory_core.application.notes import NoteService
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.errors import LeaseExpiredError
    from iris_memory_core.domain.surface import SurfaceMode

    store = world["store"]
    coordinator = SurfaceCoordinatorService(store, store.clock)
    coordinator.set_mode(world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test")
    service = NoteService(
        store, store.clock, surface=coordinator, idempotency=world["security"].idempotency
    )
    principal = principal_for(world, limited=True)
    target = replace(target_for(world), resource_id=None)
    fields = {"kind": "idea", "title": "真实管理创建", "body": "operator body"}

    def create(tx: Any, actor: CommandActor) -> tuple[str, str, list[str]]:
        return service.create_for_command(tx, actor, fields)

    result = run(
        world, principal, operation="note.create", target=target, payload=fields, execute=create
    )
    replay = run(
        world, principal, operation="note.create", target=target, payload=fields, execute=create
    )
    assert replay.replayed and result.body == replay.body
    identifier = json.loads(result.body)["note_id"]
    with store.read() as tx:
        note = tx.notes.get(identifier)
        revision = tx.notes.current_revision_row(identifier)
        assert note.current_revision == 1
        assert revision.created_by == "console:" + principal.key.id
        assert revision.body == "operator body"
        assert (
            tx.raw()
            .execute(
                "SELECT COUNT(*) FROM outbox_jobs WHERE aggregate_id=? AND job_kind='note.changed'",
                (identifier,),
            )
            .fetchone()[0]
            == 1
        )
    # The separately assembled online service still requires a real proof.
    with pytest.raises(LeaseExpiredError):
        service.create(
            world["access"],
            agent_id=world["agent"],
            space_id=world["spaces"][0],
            kind="idea",
            title="host",
            idempotency_key="host-no-proof",
        )


def test_note_command_cannot_change_actor_scope_at_domain_seam(world: dict[str, Any]) -> None:
    from iris_memory_core.application.notes import NoteService
    from iris_memory_core.domain.scope import Scope

    principal = principal_for(world, limited=True)
    service = NoteService(world["store"], world["store"].clock)
    target = replace(target_for(world), resource_id=None)
    fields = {"kind": "idea", "title": "invalid"}

    def create(tx: Any, actor: CommandActor) -> tuple[str, str, list[str]]:
        widened = replace(actor, scope=Scope(world["tenant"], agent_id=world["agent"]))
        return service.create_for_command(tx, widened, fields)

    with pytest.raises(AccessDeniedError):
        run(
            world, principal, operation="note.create", target=target, payload=fields, execute=create
        )


def test_managed_restricted_label_requires_explicit_grant_without_admin(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.application.console.notes import ConsoleNoteCommands

    security = world["security"]
    _, token = security.issue_offline(
        tenant_id=world["tenant"],
        label="restricted writer",
        description="explicit grant",
        template="maintainer",
        grant=replace(world["owner"].grant, allow_restricted=True),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    principal, _ = security.login(token, client_digest="1" * 64)
    command = ConsoleNoteCommands(security)
    values: dict[str, Any] = dict(
        scope=target_for(world).scope,
        fields={"kind": "idea", "title": "restricted"},
        privacy_labels=["restricted"],
        source_refs=[],
        reason="operator_request",
        idempotency_key="restricted-command",
    )
    record = command.create(principal, **values)
    assert record.privacy_labels == ("restricted",)
    with pytest.raises(AccessDeniedError):
        command.create(principal_for(world), **values)


def test_management_keeps_domain_scope_label_matching(world: dict[str, Any]) -> None:
    from iris_memory_core.application.console.notes import ConsoleNoteCommands
    from iris_memory_core.domain.scope import Scope

    # An all-scope operator may see a qualified label, but creation still has
    # the Note domain's requirement that this qualifier match its stored scope.
    with pytest.raises(AccessDeniedError):
        ConsoleNoteCommands(world["security"]).create(
            principal_for(world),
            scope=Scope(world["tenant"], agent_id=world["agent"]),
            fields={"kind": "idea", "title": "missing label scope"},
            privacy_labels=["space:" + world["spaces"][0]],
            source_refs=[],
            reason="operator_request",
            idempotency_key="mismatched-label",
        )
