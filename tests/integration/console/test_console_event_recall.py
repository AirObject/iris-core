"""Dismissal invalidates stale pending-event Recall envelopes without rewriting them."""

from typing import Any

import pytest

from iris_memory_core.application.console.events import ConsoleEventCommands
from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.recall import RecallService, StructuredRecallOrchestrator
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.errors import ConflictError
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import principal_for
from tests.integration.console.test_console_reads import world as world_fixture
from tests.integration.memory.test_task_deletion_storage import seed

auth = auth_fixture
world = world_fixture


@pytest.mark.parametrize("window", ["replay", "publication"])
def test_dismissed_pending_event_is_not_republished_in_cached_recall(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, window: str
) -> None:
    task, _, _, identifier = seed(world)
    store = world["store"]
    clock = store.clock
    events = CognitiveEventService(store, clock)
    orchestrator = StructuredRecallOrchestrator(
        store,
        RecentContextService(store, clock),
        StateService(store, clock),
        FocusService(store, clock),
        clock=clock,
        events=events,
    )
    recall = RecallService(orchestrator, store, clock)
    req = recall.build_request(
        request_id="pending-event-recall",
        agent_id=world["agent"],
        space_id=world["spaces"][0],
        purpose="reply",
        topic="event",
        token_budget=1000,
        deadline_at_us=clock.now_us() + 30_000_000,
    )
    commands = ConsoleEventCommands(world["security"])
    principal = principal_for(world)

    def dismiss() -> None:
        commands.dismiss(
            principal,
            identifier,
            expected_revision=1,
            reason="operator_request",
            idempotency_key="recall-dismiss",
        )

    if window == "replay":
        first = recall.recall(world["access"], req)
        assert first.pending_event_ids == (identifier,)
        with store.read() as tx:
            row = tx.usage.get_request(world["tenant"], req.request_id)
            assert row is not None
            saved = row["response_json"]
        # New matches can push the saved ID out of a fresh LIMIT 50 query;
        # the original bounded set remains valid and must not be reselected.
        with store.write() as tx:
            for _ in range(51):
                events.create_internal(
                    tx,
                    tenant_id=world["tenant"],
                    agent_id=world["agent"],
                    space_group_id=None,
                    space_id=world["spaces"][0],
                    session_id=None,
                    kind="later.match",
                    object_type="task",
                    object_id=task,
                    occurrence_id=None,
                    scheduled_at_us=0,
                    deliver_after_us=0,
                    now_us=clock.now_us(),
                )
        assert recall.recall(world["access"], req) == first
        dismiss()
    else:
        original = orchestrator.recall

        def collect_then_dismiss(*args: Any, **kwargs: Any) -> Any:
            value = original(*args, **kwargs)
            assert value.pending_event_ids == (identifier,)
            dismiss()
            return value

        monkeypatch.setattr(orchestrator, "recall", collect_then_dismiss)
    with pytest.raises(ConflictError, match="no longer valid"):
        recall.recall(world["access"], req)
    with store.read() as tx:
        current = tx.usage.get_request(world["tenant"], req.request_id)
        if window == "replay":
            assert current is not None
            assert current["response_json"] == saved
        else:
            assert current is None
