"""Real protected snapshots, existing Outbox fencing and internal prerequisite checks."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from queue import Queue
from threading import Event
from typing import Any

import pytest

from iris_memory_core.application.console.backup_operations import BackupOperations
from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.domain.console import Selector
from iris_memory_core.domain.console_operations import OperationProblem, TrustedBackupPayload
from iris_memory_core.domain.errors import AccessDeniedError, LeaseFencedError, NotFoundError
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from iris_memory_core.storage.admin_archives import AdminArchiveService
from iris_memory_core.storage.backup import BackupService, verify_database_invariants
from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_commands import invalidate, principal_for
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


@pytest.fixture
def backup(world: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    store = world["store"]
    archives = AdminArchiveService(
        store, backup_root=tmp_path / "custody", export_root=tmp_path / "exports"
    )
    world["app"].state.archives = archives
    principal = world["security"].reauth(principal_for(world), world["token"])
    service = BackupOperations(world["security"], archives)
    return {
        **world,
        "archives": archives,
        "service": service,
        "principal": principal,
        "outbox": OutboxService(store, store.clock),
        "root": tmp_path / "custody",
    }


def accept(backup: dict[str, Any], key: str = "backup-one") -> Any:
    return backup["service"].create(
        backup["principal"], reason="operator_request", idempotency_key=key
    )


def claim(backup: dict[str, Any], owner: str = "first") -> Any:
    return backup["outbox"].claim(owner, kinds=frozenset({"console.trusted_backup"})).jobs[0]


def test_http_accepts_then_real_worker_verifies_without_exposing_instance_files(
    backup: dict[str, Any],
) -> None:
    client, csrf = signed_in(backup["app"], backup["token"])
    with client:
        assert (
            client.post(
                "/v1/backups", json={"reason_code": "operator_request"}, headers=headers(csrf)
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/auth/reauth", json={"key": backup["token"]}, headers=headers(csrf)
            ).status_code
            == 200
        )
        response = client.post(
            "/v1/backups",
            json={"reason_code": "operator_request"},
            headers=headers(csrf, key="558e7de8-0e9d-412a-8662-3d0e54b5c1dd"),
        )
        assert response.status_code == 202, response.text
        validate_response("ConsoleOperationEnvelope", response.json())
        identifier = response.json()["data"]["id"]
        assert response.json()["data"]["status"] == "queued"
        store = backup["store"]
        worker = OutboxWorker(
            backup["outbox"],
            phase14_handlers(store, store.clock, store.ids, archives=backup["archives"]),
        )
        assert worker.run_once()["completed"] == 1
        response = client.get("/v1/operations/" + identifier)
        validate_response("ConsoleOperationEnvelope", response.json())
        assert response.json()["data"]["status"] == "completed", response.text
        assert response.json()["data"]["result_ref"] is None
        result, reason = backup["service"].prerequisite(backup["tenant"], identifier)
        assert reason is None and result.result_ref and result.manifest_hash
        for private in (
            result.result_ref,
            result.manifest_hash,
            str(backup["root"]),
            "manifest_path",
            "download",
        ):
            assert private not in response.text
        listing = client.get("/v1/operations?kind=trusted_backup")
        assert [row["id"] for row in listing.json()["data"]] == [identifier]
        assert client.get("/v1/operations?kind=import").status_code == 400
        for value in (
            {"kind": "arbitrary", "reason_code": "operator_request"},
            {"path": "/tmp", "reason_code": "operator_request"},
        ):
            assert client.post("/v1/backups", json=value, headers=headers(csrf)).status_code == 400
        replay = client.post(
            "/v1/backups",
            json={"reason_code": "operator_request"},
            headers=headers(csrf, key="558e7de8-0e9d-412a-8662-3d0e54b5c1dd"),
        )
        assert replay.json()["data"] == response.json()["data"]


def test_unverified_unavailable_and_cross_tenant_results_block(backup: dict[str, Any]) -> None:
    operation = accept(backup)
    assert backup["service"].prerequisite(backup["tenant"], operation.id) == (
        None,
        "backup_not_verified",
    )
    unavailable = BackupOperations(backup["security"], None)
    job = claim(backup)
    assert backup["outbox"].execute(job, unavailable.work, owner="first") == "completed"
    current = ConsoleOperations(backup["security"]).detail(backup["principal"], operation.id)
    assert current.status == "blocked" and current.blocked_reason == "backup_unavailable"
    assert current.backup == TrustedBackupPayload()
    client, _ = signed_in(backup["app"], backup["token"])
    with client:
        problems = client.get(f"/v1/operations/{operation.id}/problems")
        assert problems.status_code == 200
        validate_response("ConsoleOperationProblemPage", problems.json())
        assert problems.json()["data"][0]["code"] == "backup_unavailable"
    assert unavailable.prerequisite(backup["tenant"], operation.id) == (None, "backup_unavailable")
    assert backup["service"].prerequisite("another-tenant", operation.id) == (
        None,
        "backup_not_verified",
    )


def test_limited_backup_operator_needs_no_forget_grant_and_cannot_read_another_owner(
    backup: dict[str, Any],
) -> None:
    security = backup["security"]
    grant = replace(
        backup["owner"].grant,
        permissions=frozenset({"backups.write"}),
        space_selector=Selector("ids", frozenset({backup["spaces"][0]})),
    )
    _, token = security.issue_offline(
        tenant_id=backup["tenant"],
        label="Scoped backup operator",
        description="Backup only",
        template="maintainer",
        grant=grant,
        expires_us=backup["owner"].expires_us,
    )
    principal, _ = security.login(token, client_digest="1" * 64)
    principal = security.reauth(principal, token)
    operation = backup["service"].create(
        principal, reason="operator_request", idempotency_key="limited"
    )
    operations = ConsoleOperations(security)
    assert [row.id for row in operations.list_owned(principal)] == [operation.id]
    with pytest.raises(AccessDeniedError):
        operations.list_owned(principal, kind="memory_forget")
    with pytest.raises(NotFoundError):
        operations.detail(backup["principal"], operation.id)
    assert (
        backup["outbox"].execute(claim(backup), backup["service"].work, owner="first")
        == "completed"
    )
    current = operations.detail(principal, operation.id)
    assert current.status == "completed" and current.forget is None
    assert current.backup is not None
    assert current.backup.result_ref is not None
    assert operations.metadata(current, key_id=principal.key.id)["result_ref"] is None
    assert (backup["root"] / "trusted" / current.backup.result_ref).stat().st_mode & 0o077 == 0


def test_backup_verification_rejects_forged_completed_state_without_receipt(
    backup: dict[str, Any], tmp_path: Path
) -> None:
    operation = accept(backup)
    with backup["store"].write() as tx:
        # Simulate an externally corrupted database, bypassing the normal completion guard.
        tx.raw().execute("DROP TRIGGER console_backup_completion")
        tx.raw().execute(
            "UPDATE console_operations SET status='completed',processed=total WHERE id=?",
            (operation.id,),
        )
    service = BackupService(backup["store"])
    snapshot = tmp_path / "invalid-receipt"
    service.create_backup(snapshot)
    # Legacy file verification proves byte integrity; trusted operations also require invariants.
    assert service.verify_backup(snapshot).ok
    assert "backup operation status and verified receipt disagree" in verify_database_invariants(
        snapshot / "canonical.sqlite3"
    )
    with pytest.raises(RuntimeError, match="backup verification failed"):
        backup["archives"].create_verified("dffb7f18-bd6d-49ee-a248-eb5a879c38bb")
    assert backup["service"].prerequisite(backup["tenant"], operation.id) == (
        None,
        "backup_not_verified",
    )


@pytest.mark.parametrize(
    "change", ["permission", "epoch", "session-expired", "revoked", "reauth-expired"]
)
def test_authority_is_rechecked_after_snapshot_preparation(
    backup: dict[str, Any], change: str
) -> None:
    operation = accept(backup)
    job = claim(backup)
    commit = backup["service"].work(job)
    if change == "reauth-expired":
        # Persist an expired reauthentication without invalidating this job's lease.
        with backup["store"].write() as tx:
            session = tx.console.session(backup["principal"].session.id)
            tx.console.save_session(
                replace(session, reauth_until_us=backup["store"].clock.now_us())
            )
    else:
        invalidate(backup, backup["principal"], change)
    assert backup["outbox"].execute(job, lambda job: commit, owner="first") == "completed"
    with backup["store"].read() as tx:
        current = tx.console_operations.get(backup["tenant"], operation.id)
        assert current.status == "blocked" and current.blocked_reason == "authority_changed"
        assert current.backup.result_ref is None


def test_two_worker_takeover_fences_old_completed_snapshot(backup: dict[str, Any]) -> None:
    operation = accept(backup)
    first = claim(backup)
    prepared = backup["service"].work(first)
    backup["store"].clock.advance(30_000_001)
    second = claim(backup, "second")
    assert first.id == second.id and second.lease_generation > first.lease_generation
    assert backup["outbox"].execute(second, backup["service"].work, owner="second") == "completed"
    result = backup["service"].prerequisite(backup["tenant"], operation.id)
    with pytest.raises(LeaseFencedError):
        backup["outbox"].execute(first, lambda job: prepared, owner="first")
    assert backup["service"].prerequisite(backup["tenant"], operation.id) == result
    assert len(list((backup["root"] / "trusted").glob("*/manifest.json"))) == 2
    assert result[1] is None


def test_two_actual_workers_overlap_preparation_and_only_takeover_can_publish(
    backup: dict[str, Any], monkeypatch: Any
) -> None:
    operation = accept(backup)
    store = backup["store"]
    ready, release = Event(), Event()
    real = backup["archives"].create_verified
    references: list[str] = []

    def prepare(reference: str) -> Any:
        result = real(reference)
        references.append(reference)
        if len(references) == 1:
            ready.set()
            assert release.wait(20), "takeover did not release old worker"
        return result

    monkeypatch.setattr(backup["archives"], "create_verified", prepare)
    handlers = {"console.trusted_backup": backup["service"].work}
    first = OutboxWorker(OutboxService(store, store.clock), handlers, owner="worker-first")
    second = OutboxWorker(OutboxService(store, store.clock), handlers, owner="worker-second")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first.run_once)
        try:
            assert ready.wait(20), "first worker did not prepare its real backup"
            store.clock.advance(30_000_001)
            assert second.run_once()["completed"] == 1
        finally:
            release.set()
        assert future.result(timeout=20)["fenced"] == 1
    result, reason = backup["service"].prerequisite(backup["tenant"], operation.id)
    assert reason is None and result.result_ref == references[1]


def test_long_backup_renews_real_lease_past_original_expiry(
    backup: dict[str, Any], monkeypatch: Any
) -> None:
    operation = accept(backup)
    store = backup["store"]
    outbox: OutboxService = backup["outbox"]
    renewed: Queue[bool] = Queue()
    real_heartbeat = outbox.heartbeat
    real_backup = backup["archives"].create_verified

    def heartbeat(job: Any, *, owner: str) -> bool:
        outcome = real_heartbeat(job, owner=owner)
        renewed.put(outcome)
        return outcome

    def prepare(reference: str) -> Any:
        for _ in range(4):
            store.clock.advance(10_000_000)
            assert renewed.get(timeout=2), "live lease was not renewed"
        return real_backup(reference)

    monkeypatch.setattr(OutboxService, "worker_heartbeat_seconds", property(lambda self: 0.01))
    monkeypatch.setattr(outbox, "heartbeat", heartbeat)
    monkeypatch.setattr(backup["archives"], "create_verified", prepare)
    worker = OutboxWorker(outbox, {"console.trusted_backup": backup["service"].work})
    assert worker.run_once()["completed"] == 1
    assert backup["service"].prerequisite(backup["tenant"], operation.id)[1] is None


@pytest.mark.parametrize("prepared", [False, True])
def test_cancel_prevents_late_publication(backup: dict[str, Any], prepared: bool) -> None:
    operation = accept(backup)
    job = claim(backup)
    commit = backup["service"].work(job) if prepared else None
    operations = ConsoleOperations(backup["security"])
    cancelled = operations.cancel(
        backup["principal"], operation.id, reason="operator_request", idempotency_key="stop"
    )
    assert cancelled.status == "cancelled"
    assert (
        backup["outbox"].execute(
            job, (lambda job: commit) if prepared else backup["service"].work, owner="first"
        )
        == "completed"
    )
    assert operations.detail(backup["principal"], operation.id) == cancelled
    assert backup["service"].prerequisite(backup["tenant"], operation.id) == (
        None,
        "backup_not_verified",
    )


def test_retry_uses_existing_outbox_and_never_publishes_failed_attempt(
    backup: dict[str, Any], monkeypatch: Any
) -> None:
    operation = accept(backup)
    real = backup["archives"].create_verified

    def fail(reference: str) -> Any:
        raise OSError("injected unavailable filesystem")

    with monkeypatch.context() as patch:
        patch.setattr(backup["archives"], "create_verified", fail)
        assert (
            backup["outbox"].execute(claim(backup), backup["service"].work, owner="first")
            == "retryable"
        )
    assert (
        backup["service"].prerequisite(backup["tenant"], operation.id)[1] == "backup_not_verified"
    )
    backup["store"].clock.advance(3_000_000)
    assert backup["archives"].create_verified == real
    assert (
        backup["outbox"].execute(claim(backup), backup["service"].work, owner="first")
        == "completed"
    )
    assert backup["service"].prerequisite(backup["tenant"], operation.id)[1] is None


@pytest.mark.parametrize("damage", ["manifest", "catalog-digest", "unverified-adapter"])
def test_bad_digest_and_unverified_adapter_cannot_issue_trusted_receipt(
    backup: dict[str, Any], damage: str, monkeypatch: Any
) -> None:
    operation = accept(backup)
    if damage == "unverified-adapter":
        monkeypatch.setattr(
            backup["archives"], "create_verified", lambda reference: TrustedBackupPayload()
        )
        assert (
            backup["outbox"].execute(claim(backup), backup["service"].work, owner="first") == "dead"
        )
        with backup["store"].read() as tx:
            assert tx.console_operations.get(backup["tenant"], operation.id).status == "failed"
        assert (
            backup["service"].prerequisite(backup["tenant"], operation.id)[1]
            == "backup_not_verified"
        )
        return
    assert (
        backup["outbox"].execute(claim(backup), backup["service"].work, owner="first")
        == "completed"
    )
    result, _ = backup["service"].prerequisite(backup["tenant"], operation.id)
    if damage == "manifest":
        (backup["root"] / "trusted" / result.result_ref / "manifest.json").write_text("{}")
    else:
        with backup["store"].write() as tx:
            tx.raw().execute(
                "UPDATE console_operation_backups SET manifest_hash=? WHERE operation_id=?",
                ("0" * 64, operation.id),
            )
    assert backup["service"].prerequisite(backup["tenant"], operation.id) == (
        None,
        "backup_verification_failed",
    )


def test_backup_does_not_inherit_forget_500_bound(backup: dict[str, Any]) -> None:
    operation = accept(backup)
    with backup["store"].write() as tx:
        tx.raw().execute("UPDATE console_operations SET total=1000000 WHERE id=?", (operation.id,))
        tx.console_operations.add_problem(
            OperationProblem(operation.id, 9000, "execution_failed", operation.created_us)
        )
    assert (
        backup["outbox"].execute(claim(backup), backup["service"].work, owner="first")
        == "completed"
    )
    assert (
        ConsoleOperations(backup["security"]).detail(backup["principal"], operation.id).processed
        == 1000000
    )
    client, _ = signed_in(backup["app"], backup["token"])
    with client:
        problems = client.get(f"/v1/operations/{operation.id}/problems")
        assert problems.status_code == 200
        validate_response("ConsoleOperationProblemPage", problems.json())
        assert problems.json()["data"][0]["input_index"] == 9000


def test_restore_invalidates_external_references_and_queued_backup_intent(
    backup: dict[str, Any], tmp_path: Path
) -> None:
    complete = accept(backup)
    assert (
        backup["outbox"].execute(claim(backup), backup["service"].work, owner="first")
        == "completed"
    )
    queued = accept(backup, "queued-backup")
    recovery = BackupService(backup["store"])
    snapshot = tmp_path / "restore-source"
    recovery.create_backup(snapshot)
    restored = tmp_path / "restored"
    result = recovery.restore_backup(snapshot, restored)
    assert result.check.ok, result.check.problems
    import sqlite3

    from iris_memory_core.storage.console_operation_restore import reset_operations_for_restore
    from iris_memory_core.storage.console_operations import ConsoleOperationRepository

    for _ in range(2):
        reset_operations_for_restore(restored / "canonical.sqlite3")
        with sqlite3.connect(restored / "canonical.sqlite3") as connection:
            connection.row_factory = sqlite3.Row
            repository = ConsoleOperationRepository(connection)
            for identifier in (complete.id, queued.id):
                operation = repository.get(backup["tenant"], identifier)
                assert operation is not None
                assert (
                    operation.status == "blocked"
                    and operation.blocked_reason == "restore_requires_review"
                )
                assert (
                    operation.backup == TrustedBackupPayload() and operation.current_job_id is None
                )
                assert operation.processed == 0
