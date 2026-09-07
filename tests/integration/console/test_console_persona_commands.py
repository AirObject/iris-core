"""Managed Persona publication uses real authority and immutable revisions."""

import json
from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.console.personas import ConsolePersonaCommands
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    InvalidRequestError,
    RevisionMismatchError,
)
from iris_memory_core.domain.surface import SurfaceMode
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import invalidate, principal_for
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def ready(world: dict[str, Any]) -> Any:
    principal = principal_for(world)
    return world["security"].reauth(principal, world["token"])


def publish(world: dict[str, Any], principal: Any, **overrides: Any) -> Any:
    arguments: dict[str, Any] = dict(
        operation="persona.publish",
        expected_revision=1,
        expected_policy_revision=1,
        fields={"core": {"name": "Iris"}, "traits": {"style": "warm"}, "narrative": {}},
        source_refs=[],
        reason="operator_request",
        idempotency_key="persona-publication",
    )
    arguments.update(overrides)
    return ConsolePersonaCommands(world["security"]).mutate(principal, world["agent"], **arguments)


def test_publish_and_rollback_in_required_mode_preserve_history_and_replay(world: Any) -> None:
    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="managed Persona test"
    )
    principal = ready(world)
    with store.read() as tx:
        original = tx.personas.current(world["agent"])
    second = publish(world, principal)
    assert second.revision == 2
    assert publish(world, principal) == second
    with store.read() as tx:
        current = tx.personas.current(world["agent"])
        assert current.created_by == "console:" + principal.key.id
        assert json.loads(current.traits) == {"style": "warm"}
        assert (
            tx.raw()
            .execute("SELECT source FROM persona_revisions WHERE id=?", (current.id,))
            .fetchone()[0]
            == "console"
        )
        assert tx.personas.by_revision(world["agent"], 1).core == original.core
    third = publish(
        world,
        principal,
        operation="persona.rollback",
        expected_revision=2,
        fields={},
        target_revision=1,
        idempotency_key="persona-rollback",
    )
    assert third.revision == 3 and third.content_hash == original.content_hash
    assert publish(world, principal) == second
    with store.read() as tx:
        current = tx.personas.current(world["agent"])
        assert (current.core, current.traits, current.narrative) == (
            original.core,
            original.traits,
            original.narrative,
        )
        assert tx.personas.by_revision(world["agent"], 2).content_hash == second.content_hash
        assert len(tx.personas.history(world["agent"])) == 3
    with pytest.raises(RevisionMismatchError):
        publish(world, principal, idempotency_key="stale-current")


def test_recent_reauth_is_required_before_execution_and_replay(world: Any) -> None:
    principal = principal_for(world)
    with pytest.raises(AccessDeniedError):
        publish(world, principal)
    principal = world["security"].reauth(principal, world["token"])
    second = publish(world, principal)
    world["store"].clock.advance(301_000_000)
    with pytest.raises(AccessDeniedError):
        publish(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == second.revision


@pytest.mark.parametrize(
    "change", ["revoked", "permission", "epoch", "session-expired", "key-expired"]
)
def test_cached_receipt_rechecks_authority_after_cache_lookup(
    world: Any, monkeypatch: Any, change: str
) -> None:
    principal = ready(world)
    second = publish(world, principal)
    runner = world["security"].idempotency
    original = runner.run

    def replay_then_invalidate(**values: Any) -> Any:
        result = original(**values)
        assert result.replayed
        invalidate(world, principal, change)
        return result

    monkeypatch.setattr(runner, "run", replay_then_invalidate)
    with pytest.raises(AccessDeniedError):
        publish(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == second.revision


def test_policy_revision_and_content_validation_precede_writes(world: Any) -> None:
    principal = ready(world)
    with pytest.raises(RevisionMismatchError):
        publish(world, principal, expected_policy_revision=2)
    with pytest.raises(InvalidRequestError):
        publish(
            world,
            principal,
            fields={"core": [], "traits": {}, "narrative": {}},
            idempotency_key="invalid-layer",
        )
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1


def test_late_notification_failure_rolls_back_pointer_history_and_idempotency(
    world: Any, monkeypatch: Any
) -> None:
    from iris_memory_core.application.persona import PersonaService

    principal = ready(world)
    with monkeypatch.context() as patch:

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("notification failure")

        patch.setattr(PersonaService, "_notification", fail)
        with pytest.raises(RuntimeError, match="notification failure"):
            publish(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1
        assert len(tx.personas.history(world["agent"])) == 1
    assert publish(world, principal).revision == 2


def test_concurrent_managed_publications_have_one_revision_winner(world: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor

    principal = ready(world)

    def attempt(number: int) -> str:
        try:
            publish(world, principal, idempotency_key=f"racing-persona-{number}")
            return "published"
        except RevisionMismatchError:
            return "stale"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(attempt, range(12)))
    assert outcomes.count("published") == 1 and outcomes.count("stale") == 11
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 2
        assert len(tx.personas.history(world["agent"])) == 2


@pytest.mark.parametrize("restricted", [False, True])
def test_only_explicit_parent_wide_publish_grant_can_write(world: Any, restricted: bool) -> None:
    from iris_memory_core.domain.console import Selector
    from tests.integration.console.test_console_reads import grant_for

    grant = grant_for(world, permissions=frozenset({"memory.read", "persona.publish"}))
    if not restricted:
        grant = replace(
            grant,
            permissions=frozenset({"memory.read", "memory.write"}),
            space_selector=Selector("all"),
        )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="publisher",
        description="grant test",
        template="maintainer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    principal, _ = world["security"].login(token, client_digest="0" * 64)
    principal = world["security"].reauth(principal, token)
    with pytest.raises(AccessDeniedError):
        publish(world, principal)
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1


def state_evidence(world: Any) -> Any:
    from iris_memory_core.application.persona import PERSONA_STATE_CAPABILITY, PersonaService
    from tests.integration.memory.test_task_deletion_storage import seed

    task, _, _, _ = seed(world)
    service = PersonaService(world["store"], world["store"].clock)
    state = service.update_state(
        replace(world["access"], capabilities=frozenset({PERSONA_STATE_CAPABILITY})),
        world["agent"],
        expected_revision=0,
        state={"energy": 0.5},
        baseline={},
        ttl_us=600_000_000,
        source_refs=[{"resource_type": "task", "resource_id": task}],
    )
    return state, task


def test_rollback_retains_state_and_transitive_evidence_provenance(world: Any) -> None:
    from iris_memory_core.application.console.reads import ResourceReader
    from iris_memory_core.application.console.resources import ResourceRef

    state, task = state_evidence(world)
    principal = ready(world)
    refs = [{"resource_type": "persona_state", "resource_id": state.id, "revision": state.revision}]
    publish(world, principal, source_refs=refs)
    publish(world, principal, expected_revision=2, idempotency_key="newer-persona")
    rolled = publish(
        world,
        principal,
        operation="persona.rollback",
        expected_revision=3,
        fields={},
        target_revision=2,
        idempotency_key="rollback-with-evidence",
    )
    with world["store"].read() as tx:
        assert json.loads(tx.personas.current(world["agent"]).source_refs) == refs
        assert (
            ResourceReader(tx, principal, world["store"].clock.now_us()).get(
                ResourceRef("persona_revision", rolled.resource_id)
            )
            is not None
        )
    # Today's revoked access to an original source also hides its copied Persona.
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
        limited = replace(principal, key=tx.console.key(key.id))
    with world["store"].read() as tx:
        assert (
            ResourceReader(tx, limited, world["store"].clock.now_us()).get(
                ResourceRef("persona_revision", rolled.resource_id)
            )
            is None
        )


@pytest.mark.parametrize("change", ["expired", "stale-state", "hidden-source", "revoked-target"])
def test_rollback_rejects_changed_evidence_without_changing_current(
    world: Any, change: str
) -> None:
    from iris_memory_core.domain.errors import NotFoundError, PersonaPolicyDeniedError

    state, task = state_evidence(world)
    principal = ready(world)
    publish(
        world, principal, source_refs=[{"resource_type": "persona_state", "resource_id": state.id}]
    )
    publish(world, principal, expected_revision=2, idempotency_key="independent-newer-persona")
    with world["store"].write() as tx:
        if change == "revoked-target":
            tx.raw().execute(
                "UPDATE persona_revision_metadata SET lifecycle_status='revoked' "
                "WHERE revision_id=(SELECT id FROM persona_revisions "
                "WHERE agent_id=? AND revision=2)",
                (world["agent"],),
            )
        elif change == "expired":
            tx.raw().execute(
                "UPDATE persona_states SET expires_us=started_us+1 WHERE id=?", (state.id,)
            )
        elif change == "stale-state":
            tx.personas.put_state(
                tenant_id=world["tenant"],
                agent_id=world["agent"],
                expected_revision=1,
                state_json="{}",
                baseline_json="{}",
                source_refs_json="[]",
                started_us=world["store"].clock.now_us(),
                expires_us=state.expires_us,
                created_by="test-new-state",
            )
        else:
            tx.raw().execute(
                "UPDATE task_revisions SET privacy_labels='[\"restricted\"]' WHERE task_id=?",
                (task,),
            )
            key = tx.console.key(principal.key.id)
            tx.console.save_key(
                replace(
                    key, revision=key.revision + 1, grant=replace(key.grant, allow_restricted=False)
                ),
                expected_revision=key.revision,
            )
            principal = replace(principal, key=tx.console.key(key.id))
    world["store"].clock.advance(2)
    with pytest.raises((NotFoundError, PersonaPolicyDeniedError)):
        publish(
            world,
            principal,
            operation="persona.rollback",
            expected_revision=3,
            fields={},
            target_revision=2,
            idempotency_key="invalid-evidence-rollback",
        )
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 3
