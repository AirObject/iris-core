"""Actual fenced Outbox execution of a fixed management deletion set."""

from typing import Any

import pytest

from iris_memory_core.application.console.forget import ConsoleForgetCommands
from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_commands import invalidate, principal_for
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def accepted(
    world: dict[str, Any], count: int = 51, *, mode: str = "soft"
) -> tuple[Any, str, list[str]]:
    principal = principal_for(world)
    if mode == "erase":
        principal = world["security"].reauth(principal, world["token"])
    identifiers = [
        world["notes"]
        .create(
            world["access"],
            agent_id=world["agent"],
            space_id=world["spaces"][0],
            kind="idea",
            title=f"Fixed deletion member {index}",
            idempotency_key=f"operation-note-{index}",
        )
        .note_id
        for index in range(count)
    ]
    forget = ConsoleForgetCommands(world["security"])
    preview = forget.preview(
        principal,
        targets=[
            {"resource_type": "note", "id": identifier, "expected_revision": 1}
            for identifier in identifiers
        ],
        mode=mode,
        reason="operator_request",
        idempotency_key="large-preview",
    )
    assert preview["can_commit"]
    result = forget.commit(
        principal,
        preview_id=preview["preview_id"],
        preview_hash=preview["preview_hash"],
        reason="operator_request",
        idempotency_key="accept-operation",
    )
    assert result["operation"]["status"] == "queued"
    assert result["operation"]["progress"]["processed"] == "0"
    assert (
        forget.commit(
            principal,
            preview_id=preview["preview_id"],
            preview_hash=preview["preview_hash"],
            reason="operator_request",
            idempotency_key="accept-operation",
        )
        == result
    )
    return principal, result["operation"]["id"], identifiers


def worker(world: dict[str, Any]) -> OutboxWorker:
    store = world["store"]
    return OutboxWorker(
        OutboxService(store, store.clock),
        phase14_handlers(store, store.clock, store.ids),
        concurrency=1,
    )


@pytest.mark.parametrize(
    "change", ["revoked", "permission", "epoch", "session-expired", "reauth-expired"]
)
def test_authority_expiry_between_batches_preserves_committed_erasure(
    world: dict[str, Any], change: str
) -> None:
    principal, identifier, targets = accepted(world, mode="erase")
    assert worker(world).run_once()["completed"] == 1
    store = world["store"]
    if change == "reauth-expired":
        store.clock.advance(300_000_001)
    else:
        invalidate(world, principal, change)
    assert worker(world).run_once()["completed"] == 1
    with store.read() as tx:
        operation = tx.console_operations.get(world["tenant"], identifier)
        assert operation is not None
        assert operation.status == "blocked" and operation.processed == 50
        assert operation.blocked_reason == "authority_changed"
        assert operation.problems_count == 1
        for target in targets[:50]:
            assert tx.is_tombstoned(world["tenant"], "note", target)
            assert tx.notes.get(target).title == "<erased>"
        assert not tx.is_tombstoned(world["tenant"], "note", targets[-1])
        assert tx.notes.get(targets[-1]).title == "Fixed deletion member 50"
    assert worker(world).run_once()["claimed"] == 0


@pytest.mark.parametrize("count", [51, 500])
def test_fixed_batches_resume_with_a_new_worker_and_finish_once(
    world: dict[str, Any], count: int
) -> None:
    principal, operation_id, identifiers = accepted(world, count)
    operations = ConsoleOperations(world["security"])
    with world["store"].read() as tx:
        assert all(
            not tx.is_tombstoned(world["tenant"], "note", identifier) for identifier in identifiers
        )
    for processed in range(0, count, 50):
        result = worker(world).run_once()  # A fresh process identity for every batch.
        assert result["completed"] == 1, result
        current = operations.detail(principal, operation_id)
        assert current.processed == min(processed + 50, count)
    current = operations.detail(principal, operation_id)
    assert current.status == "completed" and current.payload_json == "{}"
    with world["store"].read() as tx:
        assert all(
            tx.is_tombstoned(world["tenant"], "note", identifier) for identifier in identifiers
        )
    assert worker(world).run_once()["claimed"] == 0


def test_new_hold_between_batches_blocks_remaining_fixed_members(world: dict[str, Any]) -> None:
    principal, operation_id, identifiers = accepted(world)
    assert worker(world).run_once()["completed"] == 1
    store = world["store"]
    RetentionService(
        store, store.clock, forget=ForgetService(store, store.clock)
    ).create_legal_hold(world["access"], space_id=world["spaces"][0], reason="stop_later_batches")
    assert worker(world).run_once()["completed"] == 1
    current = ConsoleOperations(world["security"]).detail(principal, operation_id)
    assert current.status == "blocked" and current.processed == 50 and current.problems_count == 1
    with store.read() as tx:
        assert (
            sum(tx.is_tombstoned(world["tenant"], "note", identifier) for identifier in identifiers)
            == 50
        )
    assert worker(world).run_once()["claimed"] == 0


def test_late_batch_error_rolls_back_content_and_records_failed_operation(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from iris_memory_core.domain.errors import InvalidRequestError

    principal, operation_id, identifiers = accepted(world, mode="erase")
    original = ConsoleForgetCommands.apply_verified_batch

    def fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise InvalidRequestError("injected after canonical writes")

    with monkeypatch.context() as patch:
        patch.setattr(ConsoleForgetCommands, "apply_verified_batch", fail)
        outcome = worker(world).run_once()
    assert outcome["dead"] == 1, outcome
    current = ConsoleOperations(world["security"]).detail(principal, operation_id)
    assert current.status == "failed" and current.processed == 0
    assert current.problems_count == 1 and current.payload_json == "{}"
    with world["store"].read() as tx:
        assert all(
            not tx.is_tombstoned(world["tenant"], "note", identifier) for identifier in identifiers
        )
        assert all(tx.notes.get(identifier).title != "<erased>" for identifier in identifiers)
        assert tx.console_operations.problems(operation_id)[0].code == "execution_failed"


def test_lost_fence_rolls_back_progress_and_next_job_then_retries(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from iris_memory_core.storage.spine import OutboxRepository

    principal, operation_id, identifiers = accepted(world)
    store = world["store"]
    with monkeypatch.context() as patch:
        patch.setattr(OutboxRepository, "complete", lambda *a, **kw: 0)
        outcome = worker(world).run_once()
    assert outcome["fenced"] == 1, outcome
    current = ConsoleOperations(world["security"]).detail(principal, operation_id)
    assert current.status == "queued" and current.processed == 0 and current.revision == 1
    with store.read() as tx:
        assert all(
            not tx.is_tombstoned(world["tenant"], "note", identifier) for identifier in identifiers
        )
        expires = tx.outbox.get(current.current_job_id).lease_expires_us
    store.clock.advance(expires - store.clock.now_us() + 1)
    assert worker(world).run_once()["completed"] == 1
    assert worker(world).run_once()["completed"] == 1
    assert (
        ConsoleOperations(world["security"]).detail(principal, operation_id).status == "completed"
    )


def test_cancel_between_real_batches_prevents_queued_continuation(world: dict[str, Any]) -> None:
    principal, operation_id, identifiers = accepted(world)
    assert worker(world).run_once()["completed"] == 1
    cancelled = ConsoleOperations(world["security"]).cancel(
        principal, operation_id, reason="operator_request", idempotency_key="stop-rest"
    )
    assert cancelled.status == "cancelled_partial" and cancelled.processed == 50
    assert worker(world).run_once()["completed"] == 1  # The queued continuation is a no-op.
    with world["store"].read() as tx:
        assert (
            sum(tx.is_tombstoned(world["tenant"], "note", identifier) for identifier in identifiers)
            == 50
        )
    assert worker(world).run_once()["claimed"] == 0
