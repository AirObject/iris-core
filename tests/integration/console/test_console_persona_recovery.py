"""Managed Persona revisions drive Recall validity and survive verified restore."""

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.recall import RecallService, StructuredRecallOrchestrator
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.errors import ConflictError
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_persona_commands import publish, ready
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def test_publish_and_rollback_invalidate_old_recall_without_rewriting_usage(world: Any) -> None:
    store = world["store"]
    clock = store.clock
    orchestrator = StructuredRecallOrchestrator(
        store,
        RecentContextService(store, clock),
        StateService(store, clock),
        FocusService(store, clock),
        clock=clock,
    )
    recall = RecallService(orchestrator, store, clock)
    request = recall.build_request(
        request_id="original-persona",
        agent_id=world["agent"],
        space_id=world["spaces"][0],
        purpose="reply",
        topic="persona",
        token_budget=1000,
        deadline_at_us=clock.now_us() + 30_000_000,
    )
    first = recall.recall(world["access"], request)
    assert first.persona_revision == 1
    with store.read() as tx:
        original_usage = tx.usage.get_request(world["tenant"], request.request_id)["response_json"]
    principal = ready(world)
    second = publish(world, principal)
    with pytest.raises(ConflictError):
        recall.recall(world["access"], request)
    second_request = replace(request, request_id="second-persona")
    second_result = recall.recall(world["access"], second_request)
    assert (second_result.persona_revision, second_result.persona_content_hash) == (
        2,
        second.content_hash,
    )
    third = publish(
        world,
        principal,
        operation="persona.rollback",
        fields={},
        target_revision=1,
        expected_revision=2,
        idempotency_key="recall-rollback",
    )
    assert third.content_hash == first.persona_content_hash
    # Identical content after rollback is a new publication identity.
    for stale in (request, second_request):
        with pytest.raises(ConflictError):
            recall.recall(world["access"], stale)
    fresh = recall.recall(world["access"], replace(request, request_id="third-persona"))
    assert fresh.persona_revision == 3 and fresh.persona_content_hash == first.persona_content_hash
    with store.read() as tx:
        assert (
            tx.usage.get_request(world["tenant"], request.request_id)["response_json"]
            == original_usage
        )


def test_snapshot_restores_managed_persona_pointer_and_immutable_content(
    world: Any, tmp_path: Any
) -> None:
    from iris_memory_core.storage.backup import BackupService
    from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
    from iris_memory_core.storage.uow import Store

    principal = ready(world)
    second = publish(world, principal)
    third = publish(
        world,
        principal,
        operation="persona.rollback",
        fields={},
        target_revision=1,
        expected_revision=2,
        idempotency_key="backup-rollback",
    )
    with world["store"].read() as tx:
        current = tx.personas.current(world["agent"])
        history = tx.personas.history(world["agent"])
    backup = BackupService(world["store"])
    source, target = tmp_path / "published", tmp_path / "restored"
    backup.create_backup(source)
    restored_result = backup.restore_backup(source, target)
    assert restored_result.check.ok, restored_result.check.problems
    restored = Store(
        SQLiteRuntime(target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),))
    )
    with restored.read() as tx:
        assert tx.personas.current(world["agent"]) == current
        assert tx.personas.history(world["agent"]) == history
        assert current.id == third.resource_id and current.revision == 3
        assert tx.personas.by_revision(world["agent"], 2).id == second.resource_id
