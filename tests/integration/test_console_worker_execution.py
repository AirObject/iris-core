"""Management transaction reuse never manufactures background privileges."""

from typing import Any

import pytest

from iris_memory_core.application.console.execution_context import WorkerExecutionContext
from iris_memory_core.application.console.forget import ConsoleForgetCommands
from iris_memory_core.domain.errors import AccessDeniedError
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_commands import invalidate, principal_for
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


@pytest.mark.parametrize(
    "change", [None, "revoked", "permission", "epoch", "session-expired", "reauth-expired"]
)
def test_worker_execution_context_rechecks_actual_operator_session(
    world: dict[str, Any], change: str | None
) -> None:
    principal = principal_for(world)
    principal = world["security"].reauth(principal, world["token"])
    store = world["store"]
    commands = ConsoleForgetCommands(
        WorkerExecutionContext(store, store.clock, world["security"].ids)
    )
    identifier = world["ids"]["a"]
    with store.read() as tx:
        states, _ = commands._inspect(
            tx, principal, [{"resource_type": "note", "id": identifier, "expected_revision": 1}]
        )
    if change == "reauth-expired":
        store.clock.advance(300_000_001)
    elif change:
        invalidate(world, principal, change)

    def execute() -> None:
        with store.write() as tx:
            commands.apply_verified_batch(
                tx,
                principal,
                preview_id="durable-operation-preview",
                mode="erase",
                reason="operator_request",
                states=states,
            )

    if change is None:
        execute()
    else:
        with pytest.raises(AccessDeniedError):
            execute()
    with store.read() as tx:
        assert tx.is_tombstoned(world["tenant"], "note", identifier) is (change is None)
        if change is not None:
            assert tx.notes.get(identifier).title != "<erased>"
