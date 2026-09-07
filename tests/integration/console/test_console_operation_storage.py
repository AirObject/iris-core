"""Isolated Operation persistence prototype; no HTTP/Worker admission yet."""

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.domain.console_operations import ConsoleOperation, OperationProblem
from iris_memory_core.domain.errors import ConflictError
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def operation(world: dict[str, Any], identifier: str) -> ConsoleOperation:
    now = world["store"].clock.now_us()
    return ConsoleOperation(
        id=identifier,
        tenant_id=world["tenant"],
        key_id=world["owner"].id,
        key_revision=world["owner"].revision,
        grant_fingerprint=world["owner"].grant.fingerprint,
        session_id="real-session-bound-at-http-admission",
        session_epoch=1,
        preview_id="preview-" + identifier,
        preview_hash="a" * 64,
        kind="memory_forget",
        mode="soft",
        reason_code="operator_request",
        status="queued",
        revision=1,
        processed=0,
        total=51,
        payload_json='{"states":[]}',
        expected_deletion_seq=0,
        holds_version="b" * 64,
        current_job_id=None,
        blocked_reason=None,
        created_us=now,
        updated_us=now,
        started_us=None,
        finished_us=None,
    )


def test_progress_cas_and_transaction_failure_preserve_committed_batch(
    world: dict[str, Any],
) -> None:
    store = world["store"]
    original = operation(world, "operation-1")
    with store.write() as tx:
        tx.console_operations.insert(original)
    running = replace(original, processed=50, status="running", revision=2)
    with store.write() as tx:
        tx.console_operations.advance(running, expected_revision=1)
    with pytest.raises(ConflictError), store.write() as tx:
        tx.console_operations.advance(running, expected_revision=1)
    completed = replace(running, processed=51, status="completed", revision=3)
    with pytest.raises(RuntimeError), store.write() as tx:
        tx.console_operations.advance(completed, expected_revision=2)
        raise RuntimeError("simulate failure before fenced transaction commit")
    with store.read() as tx:
        assert tx.console_operations.get(world["tenant"], original.id) == running
    with store.write() as tx:
        tx.console_operations.advance(completed, expected_revision=2)
    with pytest.raises(ConflictError), store.write() as tx:
        tx.console_operations.advance(
            replace(completed, status="running", revision=4), expected_revision=3
        )


def test_owner_grant_pagination_and_problem_retry_are_bounded(world: dict[str, Any]) -> None:
    original = operation(world, "operation-1")
    store = world["store"]
    with store.write() as tx:
        repository = tx.console_operations
        for index in range(3):
            repository.insert(replace(original, id=f"op-{index}", preview_id=f"preview-{index}"))
        repository.insert(replace(original, id="other-grant", grant_fingerprint="c" * 64))
        problem = OperationProblem("op-0", 50, "preview_stale", original.created_us)
        repository.add_problem(problem)
        repository.add_problem(problem)
    with store.read() as tx:
        repository = tx.console_operations
        rows = repository.list_owned(
            original.tenant_id,
            original.key_id,
            original.grant_fingerprint,
            key_revision=original.key_revision,
            limit=2,
        )
        assert [row.id for row in rows] == ["op-2", "op-1"]
        remaining = repository.list_owned(
            original.tenant_id,
            original.key_id,
            original.grant_fingerprint,
            key_revision=original.key_revision,
            after=(rows[-1].created_us, rows[-1].id),
            limit=2,
        )
        assert [row.id for row in remaining] == ["op-0"]
        assert repository.get("other-tenant", "op-0") is None
        assert repository.problems("op-0") == (problem,)
        assert repository.problems("op-0", after=50) == ()
        with pytest.raises(ValueError):
            repository.list_owned(
                original.tenant_id,
                original.key_id,
                original.grant_fingerprint,
                key_revision=original.key_revision,
                limit=202,
            )
