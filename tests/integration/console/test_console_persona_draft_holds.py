"""Retained draft sources obey current Hold, authority and transaction fences."""

import json
from typing import Any

import pytest

from iris_memory_core.application.console.persona_drafts import ConsolePersonaDraftCommands
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.domain.errors import NotReadyError, PersonaPolicyDeniedError
from iris_memory_core.domain.retention import LegalHoldActiveError
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_persona_commands import ready, state_evidence
from tests.integration.console.test_console_persona_drafts import command
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def test_current_source_hold_blocks_discard_and_replacement_until_release(world: Any) -> None:
    state, task = state_evidence(world)
    principal = ready(world)
    draft = command(
        world, principal, source_refs=[{"resource_type": "persona_state", "resource_id": state.id}]
    )
    service = RetentionService(
        world["store"],
        world["store"].clock,
        forget=ForgetService(world["store"], world["store"].clock),
    )
    with world["store"].read() as tx:
        original = tx.persona_drafts.get(world["tenant"], world["agent"], draft.resource_id)
        source = tx.tasks.get_task(task)
    hold = service.create_legal_hold(
        world["access"],
        space_id=source.space_id,
        session_id=source.session_id,
        agent_id=world["agent"],
        reason="operator_request",
    )
    commands = ConsolePersonaDraftCommands(world["security"])
    view = commands.context(principal, world["agent"], draft.resource_id)
    assert view.available_actions == ("publish",)
    for operation in ("update", "discard"):
        with pytest.raises(LegalHoldActiveError):
            command(
                world,
                principal,
                operation="persona.draft." + operation,
                draft_id=draft.resource_id,
                expected_revision=1,
                fields=json.loads(original.fields_json) if operation == "update" else {},
                base_revision=1 if operation == "update" else None,
                policy_revision=1 if operation == "update" else None,
                source_refs=[],
                idempotency_key="held-" + operation,
            )
    with world["store"].read() as tx:
        assert tx.persona_drafts.get(world["tenant"], world["agent"], draft.resource_id) == original
        assert not tx.is_tombstoned(world["tenant"], "persona_draft", draft.resource_id)
        assert not tx.raw().execute("SELECT 1 FROM forget_requests").fetchone()
    service.release_legal_hold(world["access"], hold.id, reason="operator_request")
    assert (
        "discard"
        in commands.context(principal, world["agent"], draft.resource_id).available_actions
    )
    result = command(
        world,
        principal,
        operation="persona.draft.discard",
        draft_id=draft.resource_id,
        expected_revision=1,
        fields={},
        base_revision=None,
        policy_revision=None,
        idempotency_key="held-discard",
    )
    assert result.status == "discarded"


def test_draft_publish_revalidates_expired_current_evidence(world: Any) -> None:
    state, _ = state_evidence(world)
    principal = ready(world)
    draft = command(
        world, principal, source_refs=[{"resource_type": "persona_state", "resource_id": state.id}]
    )
    world["store"].clock.advance(601_000_000)
    principal = world["security"].reauth(principal, world["token"])
    with pytest.raises(PersonaPolicyDeniedError):
        command(
            world,
            principal,
            operation="persona.draft.publish",
            draft_id=draft.resource_id,
            expected_revision=1,
            fields={},
            idempotency_key="expired-evidence",
        )
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1
        assert (
            tx.persona_drafts.get(world["tenant"], world["agent"], draft.resource_id).status
            == "draft"
        )


def test_hold_query_budget_fails_closed_before_any_scrub(world: Any, monkeypatch: Any) -> None:
    from iris_memory_core.storage.memory import RetentionRepository

    principal = ready(world)
    draft = command(world, principal)
    monkeypatch.setattr(RetentionRepository, "active_holds", lambda *args, **kwargs: [None] * 501)
    with pytest.raises(NotReadyError):
        command(
            world,
            principal,
            operation="persona.draft.discard",
            draft_id=draft.resource_id,
            expected_revision=1,
            fields={},
            base_revision=None,
            policy_revision=None,
            idempotency_key="hold-budget",
        )
    with world["store"].read() as tx:
        assert (
            tx.persona_drafts.get(world["tenant"], world["agent"], draft.resource_id).status
            == "draft"
        )
