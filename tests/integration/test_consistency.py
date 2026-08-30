"""Consistency kernel: revisions, watermarks, tombstones, audit, idempotency."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from iris_memory_core.application.ports import Transaction
from iris_memory_core.domain.errors import (
    IdempotencyInProgressError,
    IdempotencyKeyReusedError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store


def test_revision_cas_bumps_or_fails_with_stable_error(store: Store, tenant_id: str) -> None:
    with store.write() as tx:
        group = tx.insert_space_group(tenant_id, "g1", "", actor="admin", reason_code="setup")
        updated = tx.update_space_group(
            group.id,
            name="g2",
            description="",
            expected_revision=1,
            actor="admin",
            reason_code="rename",
        )
    assert updated.revision == 2

    with pytest.raises(RevisionMismatchError) as exc_info, store.write() as tx:
        tx.update_space_group(
            group.id,
            name="g3",
            description="",
            expected_revision=1,  # stale
            actor="admin",
            reason_code="rename",
        )
    assert exc_info.value.code == "revision_mismatch"
    assert exc_info.value.details["current_revision"] == 2

    with pytest.raises(NotFoundError), store.write() as tx:
        tx.update_space_group(
            "missing-id",
            name="x",
            description="",
            expected_revision=1,
            actor="admin",
            reason_code="rename",
        )


def test_watermark_is_monotonic_with_entries(store: Store, tenant_id: str) -> None:
    with store.write() as tx:
        agent = tx.insert_agent(tenant_id, "agent-1", actor="admin")
        tx.advance_watermark(tenant_id, agent.id, (("agent", agent.id, 1),))
    # One transaction advances exactly once, even for several aggregates; the
    # entry records each aggregate's FINAL revision in that transaction.
    with store.write() as tx:
        projected = tx.advance_watermark(tenant_id, agent.id, (("note", "n1", 1),))
        tx.advance_watermark(
            tenant_id,
            agent.id,
            (
                ("note", "n1", 4),
                ("note", "n2", 2),
            ),
        )
    with store.write() as tx:
        next_seq = tx.advance_watermark(tenant_id, agent.id, (("note", "n3", 1),))
    assert projected < next_seq
    with store.read() as tx:
        state = tx.watermark(tenant_id, agent.id)
    assert state is not None and state.current_seq == next_seq
    with store.read() as tx:
        rows = (
            tx.raw()
            .execute(
                "SELECT seq, aggregate_type, aggregate_id, aggregate_revision "
                "FROM agent_watermark_entries WHERE tenant_id = ? AND agent_id = ? "
                "ORDER BY seq, aggregate_id",
                (tenant_id, agent.id),
            )
            .fetchall()
        )
    # seq 1: agent bootstrap; seq 2: collapsed tx (n1 final revision 4, n2); seq 3.
    assert len(rows) == 4
    seq2 = {(row[1], row[2]): int(row[3]) for row in rows if int(row[0]) == 2}
    assert seq2 == {("note", "n1"): 4, ("note", "n2"): 2}


def test_tombstones_exclude_canonical_reads(store: Store, tenant_id: str) -> None:
    with store.write() as tx:
        agent = tx.insert_agent(tenant_id, "doomed", actor="admin")
        tx.record_tombstone(
            tenant_id=tenant_id,
            resource_type="agent",
            resource_id=agent.id,
            reason_code="gdpr_erasure",
            deleted_by="admin",
        )
        first_seq = tx.tombstone_watermark()
        second = tx.record_tombstone(
            tenant_id=tenant_id,
            resource_type="agent",
            resource_id="other",
            reason_code="gdpr_erasure",
            deleted_by="admin",
        )
    assert second.tombstone_seq == first_seq + 1
    with pytest.raises(NotFoundError), store.read() as tx:
        tx.get_agent(agent.id)
    with store.read() as tx:
        assert tx.is_tombstoned(tenant_id, "agent", agent.id) is True


def test_audit_rows_are_immutable_and_queryable(store: Store, tenant_id: str) -> None:
    with store.write() as tx:
        event = tx.audit(
            tenant_id=tenant_id,
            actor="admin",
            action="probe.action",
            resource_type="entity",
            resource_id="e1",
            reason_code="test",
            details={"sensitive": False},
        )
    with store.read() as tx:
        row = (
            tx.raw()
            .execute(
                "SELECT actor, action, reason_code, details FROM audit_events WHERE id = ?",
                (event.id,),
            )
            .fetchone()
        )
    assert tuple(row) == ("admin", "probe.action", "test", json.dumps({"sensitive": False}))


def test_idempotency_replays_same_payload_and_rejects_different_payload(
    store: Store, idempotency: IdempotencyManager, tenant_id: str
) -> None:
    def create_group(tx: Transaction) -> tuple[str, str, list[str]]:
        group = tx.insert_space_group(tenant_id, "idem", "", actor="admin", reason_code="setup")
        return "created", group.id, [group.id]

    fingerprint = request_fingerprint("create_group", {"name": "idem"})
    first = idempotency.run(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="create_group",
        idempotency_key="key-1",
        request_fingerprint=fingerprint,
        execute=create_group,
    )
    assert first.replayed is False

    replay = idempotency.run(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="create_group",
        idempotency_key="key-1",
        request_fingerprint=fingerprint,
        execute=create_group,
    )
    assert replay.replayed is True
    assert replay.resource_refs == first.resource_refs
    assert replay.body == first.body

    other = request_fingerprint("create_group", {"name": "different"})
    with pytest.raises(IdempotencyKeyReusedError):
        idempotency.run(
            tenant_id=tenant_id,
            app_instance_id="app-1",
            operation="create_group",
            idempotency_key="key-1",
            request_fingerprint=other,
            execute=create_group,
        )
    # Exactly one group row exists despite three executions.
    with store.read() as tx:
        rows = (
            tx.raw()
            .execute("SELECT COUNT(*) FROM space_groups WHERE tenant_id = ?", (tenant_id,))
            .fetchone()
        )
    assert int(rows[0]) == 1


def test_idempotency_in_progress_is_blocked_until_lease_expires(
    store: Store, idempotency: IdempotencyManager, tenant_id: str
) -> None:
    record = idempotency.begin(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="slow_op",
        idempotency_key="key-2",
        request_fingerprint=request_fingerprint("slow_op", {"x": 1}),
    )
    assert record.status == "in_progress"
    with pytest.raises(IdempotencyInProgressError):
        idempotency.begin(
            tenant_id=tenant_id,
            app_instance_id="app-1",
            operation="slow_op",
            idempotency_key="key-2",
            request_fingerprint=request_fingerprint("slow_op", {"x": 1}),
        )


def test_idempotency_crash_recovery_distinguishes_outcomes(
    store: Store, idempotency: IdempotencyManager, tenant_id: str
) -> None:
    fingerprint = request_fingerprint("op", {"a": 1})

    # Crash before the business transaction: no outcome row.
    idempotency.begin(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="op",
        idempotency_key="crash-1",
        request_fingerprint=fingerprint,
    )
    _expire(store, "crash-1")
    recovered = idempotency.recover_expired()
    assert any(item.idempotency_key == "crash-1" for item in recovered)
    record = idempotency.load(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="op",
        idempotency_key="crash-1",
    )
    assert record is not None and record.status == "failed_replayable"

    # Crash after the business commit: the outcome row replays.
    idempotency.begin(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="op",
        idempotency_key="crash-2",
        request_fingerprint=fingerprint,
    )
    transaction_ref = "txn-committed"
    with store.write() as tx:
        # Simulate the split state: outcome committed, record still in_progress.
        tx.raw().execute(
            "INSERT INTO idempotency_outcomes (transaction_ref, result_code, result_body, "
            "resource_refs, completed_us) VALUES (?, ?, ?, ?, ?)",
            (transaction_ref, "created", "ref-1", '["ref-1"]', store.clock.now_us()),
        )
        tx.raw().execute(
            "UPDATE idempotency_records SET transaction_ref = ? WHERE idempotency_key = ?",
            (transaction_ref, "crash-2"),
        )
    _expire(store, "crash-2")
    replay = idempotency.run(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="op",
        idempotency_key="crash-2",
        request_fingerprint=fingerprint,
        execute=lambda tx: ("created", "should-not-run", ["should-not-run"]),
    )
    assert replay.replayed is True
    assert replay.body == "ref-1"


def _counting_execute(calls: list[int]) -> Callable[[Transaction], tuple[str, str, list[str]]]:
    def execute(tx: Transaction) -> tuple[str, str, list[str]]:
        del tx
        calls.append(1)
        return "created", "side-effect", ["ref"]

    return execute


def test_expired_lease_grants_execution_to_exactly_one_worker(
    store: Store, idempotency: IdempotencyManager, tenant_id: str
) -> None:
    """Two workers racing an expired lease: one executes, one is fenced out."""
    import threading

    fingerprint = request_fingerprint("contended_op", {"n": 1})
    idempotency.begin(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="contended_op",
        idempotency_key="race-1",
        request_fingerprint=fingerprint,
    )
    _expire(store, "race-1")

    calls: list[int] = []
    outcomes: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        try:
            result = idempotency.run(
                tenant_id=tenant_id,
                app_instance_id="app-1",
                operation="contended_op",
                idempotency_key="race-1",
                request_fingerprint=fingerprint,
                execute=_counting_execute(calls),
            )
            with lock:
                outcomes.append(result)
        except BaseException as error:
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1, "side effect must execute exactly once"
    completed = [item for item in outcomes if getattr(item, "replayed", None) is False]
    replayed = [item for item in outcomes if getattr(item, "replayed", None) is True]
    blocked = [item for item in errors if isinstance(item, IdempotencyInProgressError)]
    assert len(completed) == 1
    assert len(replayed) + len(blocked) == 1, (
        "the losing worker must replay the winner or be blocked, never execute"
    )
    with store.read() as tx:
        side_effects = tx.raw().execute("SELECT COUNT(*) FROM idempotency_outcomes").fetchone()[0]
        records = (
            tx.raw()
            .execute(
                "SELECT status, response_body FROM idempotency_records "
                "WHERE idempotency_key = 'race-1'"
            )
            .fetchall()
        )
    assert int(side_effects) == 1
    assert records[0][0] == "completed"
    assert records[0][1] == "side-effect"


def _expire(store: Store, key: str) -> None:
    with store.write() as tx:
        tx.raw().execute(
            "UPDATE idempotency_records SET expires_us = ? WHERE idempotency_key = ?",
            (store.clock.now_us() - 1, key),
        )


def test_stale_recovery_snapshot_cannot_steal_a_fresh_lease(
    store: Store, idempotency: IdempotencyManager, tenant_id: str
) -> None:
    """Directed interleaving of the reviewer's double-execution repro.

    Both workers read the SAME expired snapshot. Worker A recovers and claims
    a fresh lease; worker B's recovery — still holding the pre-claim snapshot —
    must match zero rows (owner_token + expires_us CAS) and therefore can
    neither demote A's lease nor execute the business callback a second time.
    """
    fingerprint = request_fingerprint("stale_op", {"n": 1})
    idempotency.begin(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="stale_op",
        idempotency_key="stale-1",
        request_fingerprint=fingerprint,
    )
    _expire(store, "stale-1")
    stale = idempotency.load(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="stale_op",
        idempotency_key="stale-1",
    )
    assert stale is not None and stale.status == "in_progress"

    # Worker A: recover the expired lease and claim it with a fresh owner.
    recovered = idempotency._recover(stale)
    assert recovered is not None and recovered.status == "failed_replayable"
    claimed = idempotency._claim(recovered, store.clock.now_us(), "owner-A")
    assert claimed.status == "in_progress"
    assert claimed.owner_token == "owner-A"
    assert claimed.expires_us > stale.expires_us

    # Worker B: same stale snapshot. Its recovery must be a no-op, not a
    # demotion of A's brand-new lease.
    assert idempotency._recover(stale) is None

    fresh = idempotency.load(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="stale_op",
        idempotency_key="stale-1",
    )
    assert fresh is not None
    assert fresh.status == "in_progress"
    assert fresh.owner_token == "owner-A"  # A's lease survived B's recovery

    # B's begin() now observes the live lease and is blocked — B never runs
    # the operation, so side effects happen exactly once (A's execution).
    with pytest.raises(IdempotencyInProgressError):
        idempotency.begin(
            tenant_id=tenant_id,
            app_instance_id="app-1",
            operation="stale_op",
            idempotency_key="stale-1",
            request_fingerprint=fingerprint,
        )


def test_idempotency_records_partition_by_app_instance(
    store: Store, idempotency: IdempotencyManager, tenant_id: str
) -> None:
    """Same tenant + same key under two app instances are independent."""
    calls: list[str] = []

    def make_execute(worker: str) -> Callable[[Transaction], tuple[str, str, list[str]]]:
        def execute(tx: Transaction) -> tuple[str, str, list[str]]:
            del tx
            calls.append(worker)
            return "created", worker, [worker]

        return execute

    for app in ("app-A", "app-B"):
        result = idempotency.run(
            tenant_id=tenant_id,
            app_instance_id=app,
            operation="register",
            idempotency_key="shared-key",
            request_fingerprint=request_fingerprint("register", {"x": 1}),
            execute=make_execute(app),
        )
        assert result.replayed is False
    assert calls == ["app-A", "app-B"]  # both executed; no cross-app collision
    with store.read() as tx:
        records = (
            tx.raw()
            .execute(
                "SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key = 'shared-key'"
            )
            .fetchone()[0]
        )
    assert int(records) == 2


def test_read_units_of_work_reject_watermark_advances(store: Store, tenant_id: str) -> None:
    with store.write() as tx:
        agent = tx.insert_agent(tenant_id, "ro-agent", actor="admin")
    with pytest.raises(RuntimeError), store.read() as tx:
        tx.advance_watermark(tenant_id, agent.id, (("note", "n", 1),))


def test_failed_replayable_record_can_retry_execution(
    store: Store, idempotency: IdempotencyManager, tenant_id: str
) -> None:
    fingerprint = request_fingerprint("retry_op", {"n": 1})
    idempotency.begin(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="retry_op",
        idempotency_key="retry-1",
        request_fingerprint=fingerprint,
    )
    _expire(store, "retry-1")

    calls: list[int] = []

    def execute(tx: Transaction) -> tuple[str, str, list[str]]:
        calls.append(1)
        return "ok", "done", ["r1"]

    result = idempotency.run(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="retry_op",
        idempotency_key="retry-1",
        request_fingerprint=fingerprint,
        execute=execute,
    )
    assert result.replayed is False and result.body == "done"
    assert len(calls) == 1


def test_business_failure_releases_the_idempotency_lease(
    store: Store, idempotency: IdempotencyManager, tenant_id: str
) -> None:
    """A business rejection must not strand the key as a live lease: retries
    re-execute and re-raise the SAME stable error instead of degrading into
    idempotency_in_progress for a full lease term."""
    fingerprint = request_fingerprint("flaky_op", {"n": 1})
    calls: list[str] = []

    def rejecting(tx: Transaction) -> tuple[str, str, list[str]]:
        del tx
        calls.append("reject")
        raise ValueError("business rejection")

    with pytest.raises(ValueError, match="business rejection"):
        idempotency.run(
            tenant_id=tenant_id,
            app_instance_id="app-1",
            operation="flaky_op",
            idempotency_key="flaky-1",
            request_fingerprint=fingerprint,
            execute=rejecting,
        )
    stranded = idempotency.load(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="flaky_op",
        idempotency_key="flaky-1",
    )
    assert stranded is not None and stranded.status == "failed_replayable"

    def succeeding(tx: Transaction) -> tuple[str, str, list[str]]:
        del tx
        calls.append("succeed")
        return "ok", "done", ["r1"]

    result = idempotency.run(
        tenant_id=tenant_id,
        app_instance_id="app-1",
        operation="flaky_op",
        idempotency_key="flaky-1",
        request_fingerprint=fingerprint,
        execute=succeeding,
    )
    assert result.replayed is False and result.body == "done"
    assert calls == ["reject", "succeed"]
