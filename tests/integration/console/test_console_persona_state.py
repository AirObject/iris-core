"""Managed PersonaState revisions, immutable baseline expiry and authority."""

import json
from typing import Any

import pytest

from iris_memory_core.application.console.persona_states import ConsolePersonaStateCommands
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.errors import InvalidTransitionError, RevisionMismatchError
from iris_memory_core.domain.surface import SurfaceMode
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import principal_for
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def edit(world: Any, principal: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = dict(
        operation="persona.state.update",
        expected_revision=0,
        fields={
            "state": {"energy": 0.7, "focus": ["release"]},
            "baseline": {"energy": 0.2},
            "ttl_us": 1000,
        },
        source_refs=[],
        reason="operator_request",
        idempotency_key="managed-state",
    )
    values.update(overrides)
    return ConsolePersonaStateCommands(world["security"]).mutate(
        principal, world["agent"], **values
    )


def test_managed_state_in_required_mode_has_independent_cas_and_preserves_persona(
    world: Any,
) -> None:
    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="Persona State management"
    )
    principal = principal_for(world)
    commands = ConsolePersonaStateCommands(world["security"])
    initial = commands.current(principal, world["agent"])
    assert initial.record is None and initial.expected_revision == 0
    assert initial.available_actions == ("update",)
    with store.read() as tx:
        persona = tx.personas.current(world["agent"])
    first = edit(world, principal)
    assert first.revision == 1
    assert edit(world, principal) == first
    with store.read() as tx:
        state = tx.personas.current_state(world["agent"])
        assert state.created_by == "console:" + principal.key.id
        assert json.loads(state.state_json)["energy"] == 0.7
        assert tx.personas.current(world["agent"]) == persona
    with pytest.raises(RevisionMismatchError):
        edit(world, principal, idempotency_key="stale-state")
    with pytest.raises(InvalidTransitionError):
        edit(
            world,
            principal,
            operation="persona.state.clear",
            expected_revision=1,
            fields={},
            idempotency_key="premature-clear",
        )
    store.clock.advance(1001)
    assert commands.current(principal, world["agent"]).available_actions == ("update", "clear")
    cleared = edit(
        world,
        principal,
        operation="persona.state.clear",
        expected_revision=1,
        fields={},
        idempotency_key="expired-clear",
    )
    assert cleared.revision == 2 and cleared.resource_id != first.resource_id
    assert edit(world, principal) == first
    with store.read() as tx:
        assert json.loads(tx.personas.current_state(world["agent"]).state_json) == {"energy": 0.2}
        assert tx.personas.current(world["agent"]) == persona
        assert (
            tx.raw()
            .execute("SELECT COUNT(*) FROM persona_states WHERE agent_id=?", (world["agent"],))
            .fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("ttl", [0, -1, True, 604_800_000_001])
def test_state_invalid_ttl_is_rejected_without_any_state_write(world: Any, ttl: Any) -> None:
    from iris_memory_core.domain.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError):
        edit(world, principal_for(world), fields={"state": {}, "ttl_us": ttl})
    with world["store"].read() as tx:
        assert tx.personas.current_state(world["agent"]) is None


def test_managed_state_expiry_jobs_fence_older_state_and_manual_clear(world: Any) -> None:
    from iris_memory_core.application.outbox import OutboxService
    from iris_memory_core.jobs.worker import OutboxWorker, phase9_handlers

    store = world["store"]
    principal = principal_for(world)
    edit(world, principal)
    second = edit(
        world,
        principal,
        expected_revision=1,
        fields={"state": {"energy": 0.6}, "baseline": {"energy": 0.1}, "ttl_us": 2000},
        idempotency_key="newer-managed-state",
    )
    worker = OutboxWorker(
        OutboxService(store, store.clock), phase9_handlers(store.clock), owner="state-command-test"
    )
    store.clock.advance(1000)
    worker.run_once()
    with store.read() as tx:
        assert tx.personas.current_state(world["agent"]).id == second.resource_id
    store.clock.advance(1001)
    cleared = edit(
        world,
        principal,
        operation="persona.state.clear",
        expected_revision=2,
        fields={},
        idempotency_key="clear-before-worker",
    )
    worker.run_once()
    with store.read() as tx:
        state = tx.personas.current_state(world["agent"])
        assert state.id == cleared.resource_id and state.revision == 3
        assert json.loads(state.state_json) == {"energy": 0.1}
        assert state.source_refs == "[]"


def test_state_late_enqueue_failure_rolls_back_state_pointer_and_receipt(
    world: Any, monkeypatch: Any
) -> None:
    from iris_memory_core.application.persona import PersonaService

    principal = principal_for(world)
    with monkeypatch.context() as patch:

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("expiry enqueue failed")

        patch.setattr(PersonaService, "_state_expiry_job", fail)
        with pytest.raises(RuntimeError, match="expiry enqueue failed"):
            edit(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current_state(world["agent"]) is None
        assert tx.raw().execute("SELECT COUNT(*) FROM persona_states").fetchone()[0] == 0
        assert (
            tx.raw()
            .execute("SELECT COUNT(*) FROM audit_events WHERE action='persona.state_updated'")
            .fetchone()[0]
            == 0
        )
    assert edit(world, principal).revision == 1


@pytest.mark.parametrize(
    "change", ["revoked", "permission", "epoch", "session-expired", "key-expired"]
)
def test_state_replay_rechecks_actual_authority_after_cache_lookup(
    world: Any, monkeypatch: Any, change: str
) -> None:
    from iris_memory_core.domain.errors import AccessDeniedError
    from tests.integration.console.test_console_commands import invalidate

    principal = principal_for(world)
    first = edit(world, principal)
    runner = world["security"].idempotency
    original = runner.run

    def replay_then_revoke(**values: Any) -> Any:
        outcome = original(**values)
        assert outcome.replayed
        invalidate(world, principal, change)
        return outcome

    monkeypatch.setattr(runner, "run", replay_then_revoke)
    with pytest.raises(AccessDeniedError):
        edit(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current_state(world["agent"]).id == first.resource_id


def test_concurrent_state_creation_has_one_independent_revision_winner(world: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor

    principal = principal_for(world)

    def attempt(number: int) -> str:
        try:
            edit(world, principal, idempotency_key=f"racing-state-{number}")
            return "updated"
        except RevisionMismatchError:
            return "stale"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(attempt, range(12)))
    assert outcomes.count("updated") == 1 and outcomes.count("stale") == 11
    with world["store"].read() as tx:
        assert tx.personas.current_state(world["agent"]).revision == 1
        assert tx.personas.current(world["agent"]).revision == 1


def test_state_mutation_requires_parent_wide_agent_grant(world: Any) -> None:
    from iris_memory_core.domain.errors import AccessDeniedError

    with pytest.raises(AccessDeniedError):
        edit(world, principal_for(world, limited=True))
    with world["store"].read() as tx:
        assert tx.personas.current_state(world["agent"]) is None


@pytest.mark.parametrize("operation", ["update", "clear", "replay", "read"])
def test_state_provenance_is_rechecked_before_read_write_clear_or_replay(
    world: Any, operation: str
) -> None:
    from dataclasses import replace

    from iris_memory_core.domain.errors import AccessDeniedError, NotFoundError
    from tests.integration.memory.test_task_deletion_storage import seed

    task, _, _, _ = seed(world)
    principal = principal_for(world)
    refs = [{"resource_type": "task", "resource_id": task}]
    first = edit(world, principal, source_refs=refs)
    with world["store"].write() as tx:
        tx.raw().execute(
            "UPDATE task_revisions SET privacy_labels='[\"restricted\"]' WHERE task_id=?", (task,)
        )
        key = tx.console.key(principal.key.id)
        tx.console.save_key(
            replace(
                key, revision=key.revision + 1, grant=replace(key.grant, allow_restricted=False)
            ),
            expected_revision=key.revision,
        )
        principal = replace(principal, key=tx.console.key(key.id))
    world["store"].clock.advance(1001)
    with pytest.raises((AccessDeniedError, NotFoundError)):
        if operation == "read":
            ConsolePersonaStateCommands(world["security"]).current(principal, world["agent"])
        elif operation == "replay":
            edit(world, principal, source_refs=refs)
        elif operation == "clear":
            edit(
                world,
                principal,
                operation="persona.state.clear",
                expected_revision=1,
                fields={},
                idempotency_key="hidden-state-clear",
            )
        else:
            edit(world, principal, expected_revision=1, idempotency_key="hidden-state-update")
    with world["store"].read() as tx:
        assert tx.personas.current_state(world["agent"]).id == first.resource_id
