"""Verified restore preserves published, rejected and pending Proposal history."""

from typing import Any

from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_persona_commands import ready
from tests.integration.console.test_console_persona_proposals import command, prepare
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def test_verified_restore_retains_proposal_status_events_and_publication(
    world: Any, tmp_path: Any
) -> None:
    from iris_memory_core.storage.backup import BackupService
    from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
    from iris_memory_core.storage.uow import Store

    writer, refs = prepare(world)
    proposals = [
        command(world, writer, evidence_refs=refs, idempotency_key=f"proposal-backup-{number}")
        for number in range(3)
    ]
    reviewer = ready(world)
    published = command(
        world,
        reviewer,
        operation="persona.proposal.approve",
        proposal_id=proposals[0].resource_id,
        fields={},
        idempotency_key="publish-before-backup",
    )
    command(
        world,
        reviewer,
        operation="persona.proposal.reject",
        proposal_id=proposals[1].resource_id,
        fields={},
        expected_policy_revision=None,
        idempotency_key="reject-before-backup",
    )
    with world["store"].read() as tx:
        current = tx.personas.current(world["agent"])
        history = tx.personas.history(world["agent"])
        records = [tx.personas.proposal(item.resource_id) for item in proposals]
        events = [
            tuple(row)
            for row in tx.raw().execute("SELECT * FROM persona_proposal_events ORDER BY id")
        ]
    backup = BackupService(world["store"])
    source, target = tmp_path / "proposals-backup", tmp_path / "restored"
    backup.create_backup(source)
    result = backup.restore_backup(source, target)
    assert result.check.ok, result.check.problems
    restored = Store(
        SQLiteRuntime(target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),))
    )
    with restored.read() as tx:
        assert tx.personas.current(world["agent"]) == current
        assert tx.personas.history(world["agent"]) == history
        restored_proposals = [tx.personas.proposal(item.resource_id) for item in proposals]
        assert restored_proposals == records
        assert [record.status.value for record in restored_proposals] == [
            "published",
            "rejected",
            "proposed",
        ]
        assert (
            restored_proposals[0].published_revision_id
            == published.published_revision_id
            == current.id
        )
        assert [
            tuple(row)
            for row in tx.raw().execute("SELECT * FROM persona_proposal_events ORDER BY id")
        ] == events
