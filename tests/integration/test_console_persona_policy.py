"""Policy authority, independent revisions and stale Proposal review fencing."""

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.console.persona_policy import ConsolePersonaPolicyCommands
from iris_memory_core.domain.errors import AccessDeniedError, RevisionMismatchError
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_commands import invalidate, principal_for
from tests.integration.test_console_persona_commands import ready
from tests.integration.test_console_persona_proposals import command, prepare
from tests.integration.test_console_reads import world as world_fixture
from tests.integration.test_phase9_persona import _bounded_auto_config

auth = auth_fixture
world = world_fixture


def replace_policy(world: Any, principal: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = dict(
        expected_revision=1,
        config=_bounded_auto_config(mode="manual"),
        reason="operator_request",
        idempotency_key="policy-replacement",
    )
    values.update(overrides)
    return ConsolePersonaPolicyCommands(world["security"]).replace(
        principal, world["agent"], **values
    )


def test_policy_revision_is_independent_and_does_not_rewrite_persona(world: Any) -> None:
    principal = ready(world)
    with world["store"].read() as tx:
        persona = tx.personas.current(world["agent"])
        original = tx.personas.current_policy(world["agent"])
    second = replace_policy(world, principal)
    assert second.revision == 2 and second.resource_id != original.id
    assert replace_policy(world, principal) == second
    with world["store"].read() as tx:
        current = tx.personas.current_policy(world["agent"])
        assert current.created_by == "console:" + principal.key.id
        assert current.reason_code == "operator_request"
        assert tx.personas.current(world["agent"]) == persona
        old = (
            tx.raw()
            .execute("SELECT content_hash,status FROM persona_policies WHERE id=?", (original.id,))
            .fetchone()
        )
        assert tuple(old) == (original.content_hash, "superseded")
    with pytest.raises(RevisionMismatchError):
        replace_policy(world, principal, idempotency_key="stale-policy")


def test_policy_replacement_fences_open_proposal_review_form(world: Any) -> None:
    writer, refs = prepare(world)
    proposal = command(world, writer, evidence_refs=refs)
    principal = ready(world)
    replace_policy(world, principal, expected_revision=2)
    review = dict(operation="persona.proposal.approve", proposal_id=proposal.resource_id, fields={})
    with pytest.raises(RevisionMismatchError):
        command(world, principal, idempotency_key="stale-reviewed-policy", **review)
    with world["store"].read() as tx:
        assert tx.personas.proposal(proposal.resource_id).status.value == "proposed"
        assert tx.personas.current(world["agent"]).revision == 1
    approved = command(
        world, principal, expected_policy_revision=3, idempotency_key="review-new-policy", **review
    )
    assert approved.status == "published"


def test_policy_requires_recent_reauth_on_write_and_replay(world: Any) -> None:
    principal = principal_for(world)
    with pytest.raises(AccessDeniedError):
        replace_policy(world, principal)
    principal = world["security"].reauth(principal, world["token"])
    second = replace_policy(world, principal)
    world["store"].clock.advance(301_000_000)
    with pytest.raises(AccessDeniedError):
        replace_policy(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current_policy(world["agent"]).id == second.resource_id


def test_policy_late_failure_restores_old_current_and_can_retry(
    world: Any, monkeypatch: Any
) -> None:
    from iris_memory_core.storage.uow import Transaction

    principal = ready(world)
    with monkeypatch.context() as patch:

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("watermark unavailable")

        patch.setattr(Transaction, "advance_watermark", fail)
        with pytest.raises(RuntimeError, match="watermark unavailable"):
            replace_policy(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current_policy(world["agent"]).revision == 1
        assert (
            tx.raw()
            .execute("SELECT COUNT(*) FROM persona_policies WHERE agent_id=?", (world["agent"],))
            .fetchone()[0]
            == 1
        )
        assert (
            tx.raw()
            .execute("SELECT COUNT(*) FROM audit_events WHERE action='persona.policy_replaced'")
            .fetchone()[0]
            == 0
        )
    assert replace_policy(world, principal).revision == 2


@pytest.mark.parametrize(
    "change", ["revoked", "permission", "epoch", "session-expired", "key-expired"]
)
def test_cached_policy_receipt_rechecks_authority_after_lookup(
    world: Any, monkeypatch: Any, change: str
) -> None:
    principal = ready(world)
    second = replace_policy(world, principal)
    runner = world["security"].idempotency
    original = runner.run

    def replay_then_invalidate(**values: Any) -> Any:
        result = original(**values)
        assert result.replayed
        invalidate(world, principal, change)
        return result

    monkeypatch.setattr(runner, "run", replay_then_invalidate)
    with pytest.raises(AccessDeniedError):
        replace_policy(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current_policy(world["agent"]).id == second.resource_id


@pytest.mark.parametrize("restricted", [False, True])
def test_policy_requires_parent_wide_publish_grant(world: Any, restricted: bool) -> None:
    from iris_memory_core.domain.console import Selector
    from tests.integration.test_console_reads import grant_for

    grant = grant_for(world, permissions=frozenset({"memory.read", "persona.publish"}))
    if not restricted:
        grant = replace(
            grant,
            permissions=frozenset({"memory.read", "memory.write"}),
            space_selector=Selector("all"),
        )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="policy authority test",
        description="parent-wide permission boundary",
        template="maintainer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    principal, _ = world["security"].login(token, client_digest="0" * 64)
    principal = world["security"].reauth(principal, token)
    with pytest.raises(AccessDeniedError):
        replace_policy(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current_policy(world["agent"]).revision == 1
