"""Managed event cancellation preserves delivery facts and business plans."""

from typing import Any

import pytest

from iris_memory_core.application.console.events import ConsoleEventCommands
from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.errors import ConflictError, InvalidRequestError, NotFoundError
from iris_memory_core.domain.surface import SurfaceMode
from iris_memory_core.storage.idempotency import IdempotencyManager
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import principal_for
from tests.integration.console.test_console_reads import world as world_fixture
from tests.integration.memory.test_task_deletion_storage import seed

auth = auth_fixture
world = world_fixture


@pytest.mark.parametrize("delivered", [False, True])
def test_dismiss_pending_or_delivered_in_required_mode_without_ack_or_task_completion(
    world: dict[str, Any], delivered: bool
) -> None:
    task, step, _, identifier = seed(world)
    store = world["store"]
    if delivered:
        bundle = CognitiveEventService(store, store.clock).pull(
            world["access"], agent_id=world["agent"]
        )
        assert [event.id for event, _ in bundle.events] == [identifier]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="management event test"
    )
    principal = principal_for(world)
    with store.read() as tx:
        before = tx.events.get(identifier)
        plan, child = tx.tasks.get_task(task), tx.tasks.get_step(step)
    commands = ConsoleEventCommands(world["security"])
    arguments = {
        "expected_revision": before.current_revision,
        "reason": "operator_request",
        "idempotency_key": "dismiss-fixed-event",
    }
    result = commands.dismiss(principal, identifier, **arguments)
    assert result.revision == before.current_revision + 1
    assert commands.dismiss(principal, identifier, **arguments) == result
    with store.read() as tx:
        current = tx.events.get(identifier)
        assert current.status == "cancelled"
        assert current.current_revision == result.revision
        assert current.delivery_attempts == before.delivery_attempts
        assert current.last_delivery_us == before.last_delivery_us
        assert current.ack_id is None and current.acknowledged_us is None
        assert current.delivered_lease_id is None and current.delivered_lease_epoch is None
        assert (
            tx.events.current_revision_row(identifier).created_by == "console:" + principal.key.id
        )
        assert tx.tasks.get_task(task) == plan and tx.tasks.get_step(step) == child
        assert not tx.is_tombstoned(world["tenant"], "cognitive_event", identifier)
    with pytest.raises(ConflictError):
        commands.dismiss(
            principal, identifier, **{**arguments, "idempotency_key": "stale-revision"}
        )
    with pytest.raises(InvalidRequestError):
        commands.dismiss(
            principal,
            identifier,
            expected_revision=result.revision,
            reason="operator_request",
            idempotency_key="already-terminal",
        )


def test_ack_wins_before_dismissal_and_its_receipt_is_unchanged(world: dict[str, Any]) -> None:
    task, step, _, identifier = seed(world)
    store = world["store"]
    events = CognitiveEventService(store, store.clock, idempotency=IdempotencyManager(store))
    bundle = events.pull(world["access"], agent_id=world["agent"])
    [delivered] = [event for event, _ in bundle.events if event.id == identifier]
    receipt = events.ack(world["access"], identifier, idempotency_key="host-accepted")
    principal = principal_for(world)
    commands = ConsoleEventCommands(world["security"])
    with store.read() as tx:
        before = tx.events.get(identifier)
        plan, child = tx.tasks.get_task(task), tx.tasks.get_step(step)
    with pytest.raises(ConflictError):
        commands.dismiss(
            principal,
            identifier,
            expected_revision=delivered.current_revision,
            reason="operator_request",
            idempotency_key="stale-delivery",
        )
    with pytest.raises(InvalidRequestError):
        commands.dismiss(
            principal,
            identifier,
            expected_revision=before.current_revision,
            reason="operator_request",
            idempotency_key="terminal-ack",
        )
    with store.read() as tx:
        assert tx.events.get(identifier) == before
        assert before.ack_id == receipt.ack_id and before.status == "acknowledged"
        assert tx.tasks.get_task(task) == plan and tx.tasks.get_step(step) == child


def test_failed_revision_cas_rolls_back_inserted_history_and_retries(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from iris_memory_core.storage.plans import CognitiveEventRepository

    _, _, _, identifier = seed(world)
    store = world["store"]
    principal = principal_for(world)
    commands = ConsoleEventCommands(world["security"])
    with store.read() as tx:
        before = tx.events.get(identifier)
        history = tx.events.history(identifier)
    with monkeypatch.context() as patch:
        patch.setattr(CognitiveEventRepository, "advance_pointer", lambda *args, **kwargs: 0)
        with pytest.raises(ConflictError):
            commands.dismiss(
                principal,
                identifier,
                expected_revision=1,
                reason="operator_request",
                idempotency_key="fenced-transition",
            )
    with store.read() as tx:
        assert tx.events.get(identifier) == before
        assert tx.events.history(identifier) == history
    assert (
        commands.dismiss(
            principal,
            identifier,
            expected_revision=1,
            reason="operator_request",
            idempotency_key="fenced-transition",
        ).revision
        == 2
    )


def test_hidden_parent_object_prevents_event_dismissal(world: dict[str, Any]) -> None:
    _, step, _, identifier = seed(world)
    store = world["store"]
    with store.write() as tx:
        tx.raw().execute(
            "UPDATE task_step_revisions SET privacy_labels='[\"restricted\"]' WHERE step_id=?",
            (step,),
        )
    with pytest.raises(NotFoundError):
        ConsoleEventCommands(world["security"]).dismiss(
            principal_for(world, limited=True),
            identifier,
            expected_revision=1,
            reason="operator_request",
            idempotency_key="hidden-parent",
        )
    with store.read() as tx:
        assert tx.events.get(identifier).status == "pending"


def test_expired_event_is_terminal_and_preserves_expiry_history(world: dict[str, Any]) -> None:
    _, _, _, identifier = seed(world)
    store = world["store"]
    with store.read() as tx:
        event = tx.events.get(identifier)
        assert event.expires_us is not None
        deadline = event.expires_us
    store.clock.advance(deadline - store.clock.now_us() + 1)
    events = CognitiveEventService(store, store.clock)
    with store.write() as tx:
        assert events.expire_sweep_in_tx(tx, agent_id_scope=(world["tenant"], world["agent"])) == 1
    with store.read() as tx:
        before = tx.events.get(identifier)
        history = tx.events.history(identifier)
        assert before.status == "expired"
    with pytest.raises(InvalidRequestError):
        ConsoleEventCommands(world["security"]).dismiss(
            principal_for(world, limited=True),
            identifier,
            expected_revision=before.current_revision,
            reason="operator_request",
            idempotency_key="expired-dismiss",
        )
    with store.read() as tx:
        assert tx.events.get(identifier) == before
        assert tx.events.history(identifier) == history


@pytest.mark.parametrize(
    "change", ["revoked", "permission", "epoch", "session-expired", "key-expired"]
)
def test_event_receipt_replay_rechecks_current_authority(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from iris_memory_core.domain.errors import AccessDeniedError
    from tests.integration.console.test_console_commands import invalidate

    _, _, _, identifier = seed(world)
    principal = principal_for(world)
    commands = ConsoleEventCommands(world["security"])
    arguments: dict[str, Any] = dict(
        expected_revision=1, reason="operator_request", idempotency_key="event-replay"
    )
    first = commands.dismiss(principal, identifier, **arguments)
    runner = world["security"].idempotency
    original = runner.run

    def replay_then_invalidate(**values: Any) -> Any:
        result = original(**values)
        assert result.replayed
        invalidate(world, principal, change)
        return result

    monkeypatch.setattr(runner, "run", replay_then_invalidate)
    with pytest.raises(AccessDeniedError):
        commands.dismiss(principal, identifier, **arguments)
    with world["store"].read() as tx:
        assert tx.events.get(identifier).current_revision == first.revision


def test_dismissal_removes_pending_recall_ids_and_is_a_snapshot_business_state(
    world: dict[str, Any], tmp_path: Any
) -> None:
    from iris_memory_core.storage.backup import BackupService
    from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
    from iris_memory_core.storage.uow import Store

    task, _, _, identifier = seed(world)
    store = world["store"]
    events = CognitiveEventService(store, store.clock)
    with store.read() as tx:
        assert events.pending_event_ids_for_request_scope(
            tx,
            world["access"],
            agent_id=world["agent"],
            space_id=world["spaces"][0],
            session_id=None,
        ) == (identifier,)
        before = tx.tasks.get_task(task)
        watermark = tx.watermark(world["tenant"], world["agent"])
    ConsoleEventCommands(world["security"]).dismiss(
        principal_for(world),
        identifier,
        expected_revision=1,
        reason="operator_request",
        idempotency_key="snapshot-dismiss",
    )
    with store.read() as tx:
        assert (
            events.pending_event_ids_for_request_scope(
                tx,
                world["access"],
                agent_id=world["agent"],
                space_id=world["spaces"][0],
                session_id=None,
            )
            == ()
        )
        assert tx.watermark(world["tenant"], world["agent"]) != watermark
        assert (
            tx.raw()
            .execute(
                "SELECT COUNT(*) FROM outbox_jobs WHERE aggregate_id=? "
                "AND job_kind='cognitive_event.changed'",
                (identifier,),
            )
            .fetchone()[0]
            > 0
        )
        assert (
            tx.raw()
            .execute(
                "SELECT COUNT(*) FROM audit_events WHERE resource_id=? "
                "AND action='cognitive_event.cancelled'",
                (identifier,),
            )
            .fetchone()[0]
            == 1
        )
        history = tx.events.history(identifier)
    backup = BackupService(store)
    source, target = tmp_path / "dismissed", tmp_path / "restored"
    backup.create_backup(source)
    result = backup.restore_backup(source, target)
    assert result.check.ok, result.check.problems
    restored = Store(
        SQLiteRuntime(target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),))
    )
    with restored.read() as tx:
        assert tx.events.get(identifier).status == "cancelled"
        assert tx.events.history(identifier) == history
        assert tx.tasks.get_task(task) == before
        assert not tx.is_tombstoned(world["tenant"], "cognitive_event", identifier)
