"""Focus management transactions, including authorization of capacity victims."""

from __future__ import annotations

from typing import Any

import pytest

from iris_memory_core.application.console.focus import ConsoleFocusCommands
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    InvalidRequestError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.focus import FocusCapacityPolicy
from iris_memory_core.domain.scope import Scope
from iris_memory_core.domain.surface import SurfaceMode
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import principal_for
from tests.integration.console.test_console_reads import world as read_world_fixture

auth = auth_fixture
world = read_world_fixture


def create(
    command: ConsoleFocusCommands,
    world: dict[str, Any],
    *,
    limited: bool = False,
    key: str = "new-focus",
) -> Any:
    return command.create(
        principal_for(world, limited=limited),
        scope=Scope(world["tenant"], agent_id=world["agent"], space_id=world["spaces"][0]),
        fields={"kind": "goal", "summary": "managed attention"},
        privacy_labels=[],
        source_refs=[],
        reason="operator_request",
        idempotency_key=key,
    )


def test_focus_management_required_mode_cas_replay_and_terminal_state(
    world: dict[str, Any],
) -> None:
    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test"
    )
    command = ConsoleFocusCommands(world["security"])
    record = create(command, world)
    principal = principal_for(world)
    mutate = dict(principal=principal, item_id=record.id, reason="operator_request")
    result = command.mutate(
        **mutate,
        operation="focus.update",
        expected_revision=1,
        fields={"summary": "corrected", "importance": 0.8},
        idempotency_key="edit-focus",
    )
    assert result.revision == 2 and result.fields["summary"] == "corrected"
    with pytest.raises(RevisionMismatchError):
        command.mutate(
            **mutate,
            operation="focus.update",
            expected_revision=1,
            fields={"summary": "stale"},
            idempotency_key="stale",
        )
    dormant = command.mutate(
        **mutate,
        operation="focus.transition",
        expected_revision=2,
        fields={"target_status": "dormant"},
        idempotency_key="sleep",
    )
    assert dormant.status == "dormant"
    awake = command.mutate(
        **mutate, operation="focus.activate", expected_revision=3, fields={}, idempotency_key="wake"
    )
    assert awake.status == "active"
    replay = command.mutate(
        **mutate,
        operation="focus.update",
        expected_revision=1,
        fields={"summary": "corrected", "importance": 0.8},
        idempotency_key="edit-focus",
    )
    assert replay.fields == result.fields and replay.updated_us == result.updated_us
    dismissed = command.mutate(
        **mutate,
        operation="focus.transition",
        expected_revision=4,
        fields={"target_status": "dismissed"},
        idempotency_key="dismiss",
    )
    assert command.available_actions(principal, dismissed) == ()
    with store.read() as tx:
        assert tx.focus.current_revision_row(record.id).created_by == "console:" + principal.key.id


@pytest.mark.parametrize("victim", ["parent", "other-space", "restricted"])
def test_capacity_cannot_evict_a_resource_outside_the_writer_grant(
    world: dict[str, Any], victim: str
) -> None:
    store = world["store"]
    service = FocusService(store, store.clock, idempotency=world["security"].idempotency)
    existing = service.create(
        world["access"],
        agent_id=world["agent"],
        kind="goal",
        summary="protected scope",
        space_id=None
        if victim == "parent"
        else world["spaces"][1 if victim == "other-space" else 0],
        privacy_labels=["restricted"] if victim == "restricted" else [],
        idempotency_key="victim",
    )
    command = ConsoleFocusCommands(world["security"])
    command.focus._capacity = FocusCapacityPolicy(max_items=1)
    with pytest.raises((AccessDeniedError, NotFoundError)) as caught:
        create(command, world, limited=True)
    # Fail closed with a domain authorization/not-found error, never a partial capacity mutation.
    assert getattr(caught.value, "code", None) in {"access_denied", "not_found"}
    with store.read() as tx:
        current = tx.focus.get(existing.item_id)
        assert current.current_revision == 1 and current.status == "active"
        assert len(tx.focus.active_items(world["tenant"], world["agent"])) == 1


def test_authorized_capacity_eviction_is_atomic_and_does_not_repeat(world: dict[str, Any]) -> None:
    command = ConsoleFocusCommands(world["security"])
    command.focus._capacity = FocusCapacityPolicy(max_items=1)
    first = create(command, world, key="first")
    second = create(command, world, key="second")
    assert create(command, world, key="second").id == second.id
    with world["store"].read() as tx:
        previous = tx.focus.get(first.id)
        assert previous.status == "dormant" and previous.current_revision == 2
        assert tx.focus.current_revision_row(first.id).created_by == "console:" + world["owner"].id
        assert tx.focus.get(second.id).current_revision == 1


def test_unmaterialized_focus_promotion_is_not_a_console_command(world: dict[str, Any]) -> None:
    command = ConsoleFocusCommands(world["security"])
    record = create(command, world)
    with pytest.raises(InvalidRequestError):
        command.mutate(
            principal_for(world),
            record.id,
            operation="focus.transition",
            expected_revision=1,
            fields={"target_status": "promoted"},
            reason="operator_request",
            idempotency_key="no-shell-target",
        )


@pytest.mark.parametrize("operation", ["focus.update", "focus.activate"])
def test_edit_and_reactivation_cannot_evict_hidden_capacity_victim(
    world: dict[str, Any], operation: str
) -> None:
    store = world["store"]
    service = FocusService(store, store.clock, idempotency=world["security"].idempotency)
    items = [
        service.create(
            world["access"],
            agent_id=world["agent"],
            space_id=space,
            kind="goal",
            summary="existing",
            idempotency_key="capacity-" + str(index),
        )
        for index, space in enumerate(world["spaces"])
    ]
    revision = 1
    if operation == "focus.activate":
        service.transition(
            world["access"],
            items[0].item_id,
            "dormant",
            expected_revision=1,
            reason="test",
            idempotency_key="make-dormant",
        )
        revision = 2
    command = ConsoleFocusCommands(world["security"])
    command.focus._capacity = FocusCapacityPolicy(max_items=1)
    with pytest.raises((AccessDeniedError, NotFoundError)):
        command.mutate(
            principal_for(world, limited=True),
            items[0].item_id,
            operation=operation,
            expected_revision=revision,
            fields={"summary": "edited"} if operation == "focus.update" else {},
            reason="operator_request",
            idempotency_key="denied-side-effect",
        )
    with store.read() as tx:
        assert tx.focus.get(items[0].item_id).current_revision == revision
        assert tx.focus.get(items[1].item_id).current_revision == 1
        assert tx.focus.get(items[1].item_id).status == "active"
