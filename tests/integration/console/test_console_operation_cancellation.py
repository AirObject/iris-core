"""Cancellation changes durable management intent and preserves prior deletion facts."""

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.domain.errors import NotFoundError
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import invalidate, principal_for
from tests.integration.console.test_console_operation_storage import operation
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


@pytest.mark.parametrize("processed", [0, 50])
def test_cancellation_preserves_prior_facts_and_replays_once(
    world: dict[str, Any], processed: int
) -> None:
    principal = principal_for(world)
    service = ConsoleOperations(world["security"])
    original = replace(
        operation(world, "cancel-me"),
        processed=processed,
        forget=replace(
            operation(world, "cancel-me").forget_payload,
            payload_json='{"private_target":"never_return_this"}',
        ),
    )
    store = world["store"]
    with store.write() as tx:
        tx.console_operations.insert(original)
        marker = tx.record_tombstone(
            tenant_id=world["tenant"],
            resource_type="note",
            resource_id=world["ids"]["a"],
            reason_code="earlier_committed_batch",
            deleted_by="console:" + principal.key.id,
        )
    cancelled = service.cancel(
        principal, original.id, reason="operator_request", idempotency_key="cancel-1"
    )
    assert cancelled.status == ("cancelled_partial" if processed else "cancelled")
    assert cancelled.processed == processed and cancelled.payload_json == "{}"
    assert (
        service.cancel(
            principal, original.id, reason="operator_request", idempotency_key="cancel-1"
        )
        == cancelled
    )
    assert (
        service.cancel(
            principal, original.id, reason="operator_request", idempotency_key="cancel-2"
        )
        == cancelled
    )
    metadata = service.metadata(cancelled, key_id=principal.key.id)
    assert metadata["progress"]["processed"] == str(processed)
    assert not metadata["cancellable"]
    assert "never_return_this" not in str(metadata)
    assert "session_id" not in metadata and "current_job_id" not in metadata
    with store.read() as tx:
        assert tx.is_tombstoned(world["tenant"], "note", world["ids"]["a"])
        assert tx.tombstone_watermark() == marker.tombstone_seq
        assert not tx.is_tombstoned(world["tenant"], "note", world["ids"]["b"])


def test_changed_key_revision_hides_old_operation_before_limit(world: dict[str, Any]) -> None:
    principal = principal_for(world)
    service = ConsoleOperations(world["security"])
    original = operation(world, "old-revision")
    with world["store"].write() as tx:
        tx.console_operations.insert(original)
    invalidate(world, principal, "revision")
    assert service.list_owned(principal) == ()
    with pytest.raises(NotFoundError):
        service.detail(principal, original.id)
    with pytest.raises(NotFoundError):
        service.cancel(
            principal, original.id, reason="operator_request", idempotency_key="old-authority"
        )
