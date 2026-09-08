"""Finite backfill Operations execute through real Outbox lease fences."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.console.statistics_operations import KIND, StatisticsOperations
from iris_memory_core.application.console.statistics_rollup import STAGES
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.domain.errors import AccessDeniedError, LeaseFencedError
from iris_memory_core.domain.statistics import HOUR_US, bucket_start
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import principal_for
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


@pytest.fixture
def backfill(world: dict[str, Any]) -> dict[str, Any]:
    principal = world["security"].reauth(principal_for(world), world["token"])
    now = world["store"].clock.now_us()
    return {
        **world,
        "principal": principal,
        "service": StatisticsOperations(world["security"]),
        "outbox": OutboxService(world["store"], world["store"].clock),
        "lower": bucket_start(now - HOUR_US, "hour"),
        "upper": bucket_start(now, "hour") + HOUR_US,
    }


def accept(backfill: dict[str, Any], key: str = "one") -> Any:
    return backfill["service"].create(
        backfill["principal"],
        lower=backfill["lower"],
        upper=backfill["upper"],
        reason="operator_request",
        idempotency_key=key,
    )


def worker(backfill: dict[str, Any]) -> OutboxWorker:
    store = backfill["store"]
    handlers = phase14_handlers(store, store.clock, store.ids)
    return OutboxWorker(backfill["outbox"], {KIND: handlers[KIND]})


def test_backfill_receipt_replay_and_interrupted_batches_publish_once(
    backfill: dict[str, Any],
) -> None:
    operation = accept(backfill)
    assert accept(backfill).id == operation.id
    assert operation.statistics is not None
    first_worker = worker(backfill)
    for _ in range(4):
        assert first_worker.run_once()["completed"] == 1
    with backfill["store"].read() as tx:
        current = tx.console_operations.get(backfill["tenant"], operation.id)
        assert current.processed == 4 and current.status == "running"
        assert tx.statistics.current_build(backfill["tenant"]) is None
    restarted = worker(backfill)
    for _ in range(len(STAGES) + 10):
        restarted.run_once()
        with backfill["store"].read() as tx:
            current = tx.console_operations.get(backfill["tenant"], operation.id)
        if current.status == "completed":
            break
    assert current.processed == current.total == len(STAGES)
    assert accept(backfill).status == "completed"
    assert restarted.run_once()["claimed"] == 0
    with backfill["store"].read() as tx:
        assert (
            tx.statistics.current_build(backfill["tenant"])["id"] == operation.statistics.build_id
        )
        assert tx.raw().execute("PRAGMA foreign_key_check").fetchall() == []


def test_cancel_discards_unpublished_projection_and_queued_retry_cannot_publish(
    backfill: dict[str, Any],
) -> None:
    operation = accept(backfill)
    running = worker(backfill)
    assert running.run_once()["completed"] == 1
    cancelled = ConsoleOperations(backfill["security"]).cancel(
        backfill["principal"], operation.id, reason="operator_request", idempotency_key="cancel"
    )
    assert cancelled.status == "cancelled_partial"
    running.run_once()
    with backfill["store"].read() as tx:
        assert tx.statistics.current_build(backfill["tenant"]) is None
        assert (
            tx.statistics.build(backfill["tenant"], operation.statistics.build_id)["state"]
            == "cancelled"
        )
    assert accept(backfill, "new-after-cancel").id != operation.id


def test_changed_grant_blocks_work_without_counting_hidden_rows(backfill: dict[str, Any]) -> None:
    operation = accept(backfill)
    with backfill["store"].write() as tx:
        key = tx.console.key(backfill["principal"].key.id)
        tx.console.save_key(replace(key, revision=key.revision + 1), expected_revision=key.revision)
    assert worker(backfill).run_once()["completed"] == 1
    with backfill["store"].read() as tx:
        result = tx.console_operations.get(backfill["tenant"], operation.id)
        assert result.status == "blocked" and result.blocked_reason == "authority_changed"
        assert result.processed == 0 and tx.statistics.current_build(backfill["tenant"]) is None


def test_recent_reauthentication_required_at_acceptance(backfill: dict[str, Any]) -> None:
    principal = principal_for(backfill)
    with pytest.raises(AccessDeniedError):
        backfill["service"].create(
            principal,
            lower=backfill["lower"],
            upper=backfill["upper"],
            reason="operator_request",
            idempotency_key="unauth",
        )


def test_expired_worker_cannot_advance_statistics_cursor(backfill: dict[str, Any]) -> None:
    operation = accept(backfill)
    stale = backfill["outbox"].claim("old", kinds=frozenset({KIND})).jobs[0]
    backfill["store"].clock.advance(120_000_000)
    replacement = backfill["outbox"].claim("new", kinds=frozenset({KIND})).jobs[0]
    with pytest.raises(LeaseFencedError):
        backfill["outbox"].execute(stale, backfill["service"].work, owner="old")
    assert (
        backfill["outbox"].execute(replacement, backfill["service"].work, owner="new")
        == "completed"
    )
    with backfill["store"].read() as tx:
        assert tx.console_operations.get(backfill["tenant"], operation.id).processed == 1


def test_real_worker_heartbeat_is_persistent_and_visible_only_to_current_owner(
    backfill: dict[str, Any],
) -> None:
    from iris_memory_core.application.console.statistics import Statistics

    accept(backfill)
    claimed = backfill["outbox"].claim("heartbeat-test", kinds=frozenset({KIND})).jobs[0]
    service = Statistics(backfill["security"])
    result = service.query(backfill["principal"], "pipeline", metric_id="pipeline.heartbeat_us")
    assert result["data"][0]["value"] == "0"
    backfill["store"].clock.advance(1_000_000)
    assert backfill["outbox"].heartbeat(claimed, owner="heartbeat-test")
    result = service.query(backfill["principal"], "pipeline", metric_id="pipeline.heartbeat_us")
    assert result["data"][0]["value"] == "0"
    backfill["store"].clock.advance(1_000_000)
    result = service.query(backfill["principal"], "pipeline", metric_id="pipeline.heartbeat_us")
    assert result["data"][0]["value"] == "1000000"


def test_existing_scheduler_fires_statistics_rollup_and_replay_is_idempotent(
    backfill: dict[str, Any],
) -> None:
    from iris_memory_core.application.scheduler import SchedulerService
    from iris_memory_core.domain.access import AccessContext

    store = backfill["store"]
    scheduler = SchedulerService(store, store.clock)
    schedule = scheduler.create_schedule(
        AccessContext(tenant_id=backfill["tenant"], app_instance_id="stats-schedule", admin=True),
        agent_id=None,
        job_kind=KIND,
        spec={"kind": "interval", "every_seconds": 3600},
        reason="test",
    )
    store.clock.advance(schedule.next_tick_at_us - store.clock.now_us() + 1)
    reports = scheduler.advance()
    assert sum(report.fired for report in reports) == 1
    running = worker(backfill)
    for _ in range(len(STAGES) + 10):
        running.run_once()
    with store.read() as tx:
        published = tx.statistics.current_build(backfill["tenant"])
        assert published is not None
        assert published["to_us"] - published["from_us"] == 24 * HOUR_US
    assert all(report.fired == 0 for report in scheduler.advance())
    assert running.run_once()["claimed"] == 0
