"""Phase 5 review round 5 regressions for restore and Forget commit boundaries."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import iris_memory_core.storage.backup as backup_module
from iris_memory_core.application.artifacts import ArtifactService
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.jobs.handlers import memory_invalidated_handler
from iris_memory_core.storage.backup import BackupService, verify_database_invariants
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.test_phase5_claims import _Ctx
from tests.integration.test_phase5_review_round4 import _downgrade_snapshot_to_round2


def _resource(resource_type: str, resource_id: str) -> ForgetSelector:
    return ForgetSelector(
        kind=ForgetSelectorKind.RESOURCE,
        resource_type=resource_type,
        resource_id=resource_id,
    )


@pytest.fixture
def rctx(
    clocked_store: Store,
    mutable_clock: MutableClock,
    phase5_idempotency: IdempotencyManager,
) -> tuple[_Ctx, dict[str, Any]]:
    claims = ClaimService(clocked_store, mutable_clock, idempotency=phase5_idempotency)
    ctx = _Ctx(clocked_store, mutable_clock, claims)
    return ctx, {
        "artifacts": ArtifactService(clocked_store, mutable_clock, idempotency=phase5_idempotency),
        "forget": ForgetService(clocked_store, mutable_clock, idempotency=phase5_idempotency),
    }


def test_same_microsecond_independent_forgets_get_distinct_invalidation_jobs(
    rctx: tuple[_Ctx, dict[str, Any]],
) -> None:
    """The outbox identity must be as fine as the Forget ledger identity."""
    ctx, services = rctx
    forget: ForgetService = services["forget"]
    ctx.clock.set(1_700_005_000_000_000)
    first_claim = ctx.remember("r51-a", predicate="r51", value={"n": 1}, canonical_text="first")
    selector = ForgetSelector(
        kind=ForgetSelectorKind.SUBJECT_PREDICATE,
        agent_id=ctx.agent,
        subject_entity_id=ctx.entity,
        predicate="r51",
    )
    first = forget.forget(ctx.access, selector, reason="first", idempotency_key="r51-forget-first")
    second_claim = ctx.remember("r51-b", predicate="r51", value={"n": 2}, canonical_text="second")
    second = forget.forget(
        ctx.access, selector, reason="second", idempotency_key="r51-forget-second"
    )
    assert first.erased_count == second.erased_count == 1
    assert first.request_id != second.request_id
    with ctx.store.read() as tx:
        assert tx.claims.get(first_claim.claim_id).status == "tombstoned"
        assert tx.claims.get(second_claim.claim_id).status == "tombstoned"
        rows = (
            tx.raw()
            .execute(
                "SELECT dedupe_key FROM outbox_jobs WHERE job_kind = 'memory.invalidated' "
                "ORDER BY dedupe_key"
            )
            .fetchall()
        )
    keys = {str(row[0]) for row in rows}
    assert any(first.request_id in key for key in keys)
    assert any(second.request_id in key for key in keys)


def test_local_blob_survives_a_late_forget_transaction_failure(
    rctx: tuple[_Ctx, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rolled-back Forget must not leave an active Artifact without bytes."""
    ctx, services = rctx
    artifacts: ArtifactService = services["artifacts"]
    forget: ForgetService = services["forget"]
    created = artifacts.ingest_local_blob(
        ctx.access,
        agent_id=ctx.agent,
        content=b"must survive rollback",
        media_type="application/octet-stream",
        idempotency_key="r52-create",
    )
    with ctx.store.read() as tx:
        record = tx.artifacts.get(created.artifact_id)
    blob = ctx.store.artifact_root / record.locator
    assert blob.is_file()

    def fail_after_erasure(*_args: object, **_kwargs: object) -> None:
        raise ConflictError("forced late Forget failure")

    monkeypatch.setattr(forget, "_enqueue_invalidations", fail_after_erasure)
    with pytest.raises(ConflictError, match="forced late Forget failure"):
        forget.forget(
            ctx.access,
            _resource("artifact", created.artifact_id),
            reason="rollback probe",
            idempotency_key="r52-forget",
        )
    with ctx.store.read() as tx:
        current = tx.artifacts.get(created.artifact_id)
        assert current.status == "active"
        assert not tx.is_tombstoned("t1", "artifact", created.artifact_id)
    assert blob.is_file()
    read = artifacts.read(ctx.access, created.artifact_id)
    assert read is not None and read.content == b"must survive rollback"


def test_invalidation_handler_finishes_blob_cleanup_after_a_post_commit_crash(
    rctx: tuple[_Ctx, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed outbox row durably closes the commit-to-unlink window."""
    ctx, services = rctx
    artifacts: ArtifactService = services["artifacts"]
    forget: ForgetService = services["forget"]
    created = artifacts.ingest_local_blob(
        ctx.access,
        agent_id=ctx.agent,
        content=b"worker cleanup",
        media_type="application/octet-stream",
        idempotency_key="r52-worker-create",
    )
    with ctx.store.read() as tx:
        record = tx.artifacts.get(created.artifact_id)
    blob = ctx.store.artifact_root / record.locator
    assert blob.is_file()

    # Model a process dying after SQLite COMMIT but before the synchronous
    # post-commit unlink.  The outbox row is already durable at that point.
    monkeypatch.setattr(forget, "_cleanup_local_blob_tombstones", lambda *_a, **_kw: None)
    forget.forget(
        ctx.access,
        _resource("artifact", created.artifact_id),
        reason="worker cleanup probe",
        idempotency_key="r52-worker-forget",
    )
    assert blob.is_file()
    with ctx.store.read() as tx:
        row = (
            tx.raw()
            .execute(
                "SELECT id FROM outbox_jobs WHERE job_kind = 'memory.invalidated' "
                "AND payload LIKE ?",
                (f"%{created.artifact_id}%",),
            )
            .fetchone()
        )
        assert row is not None
        job = tx.outbox.get(str(row[0]))
    commit = memory_invalidated_handler()(job)
    with ctx.store.write() as tx:
        commit(tx)
    assert not blob.exists()


def test_tombstone_only_artifact_keeps_blob_through_sync_and_worker_cleanup(
    rctx: tuple[_Ctx, dict[str, Any]],
) -> None:
    """Durable cleanup must preserve the explicit tombstone-only mode."""
    ctx, services = rctx
    artifacts: ArtifactService = services["artifacts"]
    forget: ForgetService = services["forget"]
    created = artifacts.ingest_local_blob(
        ctx.access,
        agent_id=ctx.agent,
        content=b"retained by policy",
        media_type="application/octet-stream",
        idempotency_key="r52-retain-create",
    )
    with ctx.store.read() as tx:
        record = tx.artifacts.get(created.artifact_id)
    blob = ctx.store.artifact_root / record.locator
    forget.forget(
        ctx.access,
        _resource("artifact", created.artifact_id),
        reason="legal retention",
        erase_content=False,
        idempotency_key="r52-retain-forget",
    )
    assert blob.is_file()
    with ctx.store.read() as tx:
        row = (
            tx.raw()
            .execute(
                "SELECT id, payload FROM outbox_jobs WHERE job_kind = 'memory.invalidated' "
                "AND payload LIKE ?",
                (f"%{created.artifact_id}%",),
            )
            .fetchone()
        )
        assert row is not None
        assert json.loads(str(row["payload"]))["erase_content"] is False
        job = tx.outbox.get(str(row["id"]))
    commit = memory_invalidated_handler()(job)
    with ctx.store.write() as tx:
        commit(tx)
    assert blob.is_file()


def test_external_ref_forget_scrubs_the_stored_url_and_source_ref(
    rctx: tuple[_Ctx, dict[str, Any]],
) -> None:
    """For external_ref the locator itself is content and must be erased."""
    ctx, services = rctx
    artifacts: ArtifactService = services["artifacts"]
    forget: ForgetService = services["forget"]
    secret_url = "https://example.invalid/private/customer-42"
    created = artifacts.register_external_ref(
        ctx.access,
        agent_id=ctx.agent,
        url=secret_url,
        media_type="text/plain",
        source_ref={"resource_type": "note", "resource_id": "customer-42"},
        idempotency_key="r52-external-create",
    )
    forget.forget(
        ctx.access,
        _resource("artifact", created.artifact_id),
        reason="external reference erasure",
        idempotency_key="r52-external-forget",
    )
    with ctx.store.read() as tx:
        row = (
            tx.raw()
            .execute(
                "SELECT locator, source_ref, status FROM artifacts WHERE id = ?",
                (created.artifact_id,),
            )
            .fetchone()
        )
    assert row is not None
    assert tuple(row) == ("<erased>", "null", "tombstoned")


def test_legacy_restore_upgrades_before_replaying_colliding_requests(tmp_path: Path) -> None:
    """A coarse old UNIQUE key must not reject finer post-backup ledger rows."""
    database = tmp_path / "source.sqlite3"
    MigrationRunner(database).migrate()
    clock = MutableClock()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)), clock=clock
    )
    idem = IdempotencyManager(store)
    ctx = _Ctx(store, clock, ClaimService(store, clock, idempotency=idem))
    forget = ForgetService(store, clock, idempotency=idem)
    selector = ForgetSelector(
        kind=ForgetSelectorKind.SUBJECT_PREDICATE,
        agent_id=ctx.agent,
        subject_entity_id=ctx.entity,
        predicate="r53-empty",
    )
    clock.set(1_700_006_000_000_000)
    forget.forget(ctx.admin_access, selector, reason="before", idempotency_key="r53-before")
    service = BackupService(store)
    backup = tmp_path / "backup"
    service.create_backup(backup)
    _downgrade_snapshot_to_round2(backup)
    forget.forget(ctx.admin_access, selector, reason="after", idempotency_key="r53-after")
    ledger = tuple(forget.export_deletion_ledger(ctx.admin_access))

    target = tmp_path / "restored"
    restored_store = Store(
        SQLiteRuntime(target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)),
        clock=clock,
    )
    report = service.restore_backup(
        backup,
        target,
        deletion_ledger=ledger,
        forget_service=ForgetService(restored_store, clock),
    )
    assert report.check.ok, report.check.problems
    assert not verify_database_invariants(target / "canonical.sqlite3")
    with restored_store.read() as tx:
        rows = (
            tx.raw()
            .execute(
                "SELECT idempotency_key, reason_code FROM forget_requests "
                "WHERE selector_key = ? ORDER BY idempotency_key",
                (selector.selector_key(),),
            )
            .fetchall()
        )
    identities = {(str(row[0]), str(row[1])) for row in rows}
    assert ("r53-before", "before") in identities
    assert ("r53-after", "after") in identities


def test_restore_diff_uses_the_verified_manifest_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation of the source backup after verification cannot suppress replay."""
    database = tmp_path / "source.sqlite3"
    MigrationRunner(database).migrate()
    clock = MutableClock()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)), clock=clock
    )
    idem = IdempotencyManager(store)
    ctx = _Ctx(store, clock, ClaimService(store, clock, idempotency=idem))
    forget = ForgetService(store, clock, idempotency=idem)
    claim = ctx.remember("r54-claim", predicate="r54")
    service = BackupService(store)
    backup = tmp_path / "backup"
    service.create_backup(backup)
    forget.forget(
        ctx.admin_access,
        _resource("claim", claim.claim_id),
        reason="post-backup",
        idempotency_key="r54-forget",
    )
    ledger = tuple(forget.export_deletion_ledger(ctx.admin_access))
    assert len(ledger) == 1
    request = ledger[0]

    real_restore = backup_module.restore_backup

    def mutate_source_after_switch(*args: Any, **kwargs: Any) -> Any:
        report = real_restore(*args, **kwargs)
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["forget_ledger_by_tenant"] = {
            request.tenant_id: {
                "watermark_us": request.created_us,
                "requests": [
                    {
                        "app_instance_id": request.app_instance_id,
                        "selector_key": request.selector_key,
                        "created_us": request.created_us,
                        "idempotency_key": request.idempotency_key,
                        "reason_code": request.reason_code,
                        "erase_content": request.erase_content,
                    }
                ],
            }
        }
        # Deliberately do not refresh checksums: this is an untrusted mutation
        # after the source bytes were copied and authenticated.
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return report

    monkeypatch.setattr(backup_module, "restore_backup", mutate_source_after_switch)
    target = tmp_path / "restored"
    restored_store = Store(
        SQLiteRuntime(target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)),
        clock=clock,
    )
    report = service.restore_backup(
        backup,
        target,
        deletion_ledger=ledger,
        forget_service=ForgetService(restored_store, clock),
    )
    assert report.check.ok, report.check.problems
    with restored_store.read() as tx:
        assert tx.is_tombstoned("t1", "claim", claim.claim_id)
    restored_database = target / "canonical.sqlite3"
    assert not Path(f"{restored_database}-wal").exists()
    assert not Path(f"{restored_database}-shm").exists()
    immutable = sqlite3.connect(
        f"file:{restored_database}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        count = immutable.execute(
            "SELECT COUNT(*) FROM resource_tombstones WHERE resource_id = ?",
            (claim.claim_id,),
        ).fetchone()[0]
    finally:
        immutable.close()
    assert count == 1, "ledger replay must be checkpointed into the switched main database"


def test_ledger_replay_failure_never_switches_the_previous_target(tmp_path: Path) -> None:
    """Deletion-ledger replay is part of staging preparation, not post-switch work."""
    database = tmp_path / "source.sqlite3"
    MigrationRunner(database).migrate()
    clock = MutableClock()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)), clock=clock
    )
    idem = IdempotencyManager(store)
    ctx = _Ctx(store, clock, ClaimService(store, clock, idempotency=idem))
    forget = ForgetService(store, clock, idempotency=idem)
    service = BackupService(store)
    backup = tmp_path / "backup"
    service.create_backup(backup)
    selector = ForgetSelector(
        kind=ForgetSelectorKind.SUBJECT_PREDICATE,
        agent_id=ctx.agent,
        subject_entity_id=ctx.entity,
        predicate="r55-empty",
    )
    forget.forget(ctx.admin_access, selector, reason="r55", idempotency_key="r55")
    request = forget.export_deletion_ledger(ctx.admin_access)[0]
    malformed = replace(request, selector_json="{")

    target = tmp_path / "existing-target"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("previous target", encoding="utf-8")
    report = service.restore_backup(
        backup,
        target,
        deletion_ledger=(malformed,),
        forget_service=ForgetService(store, clock),
    )
    assert not report.check.ok
    assert any("staging preparation failed" in problem for problem in report.check.problems)
    assert sentinel.read_text(encoding="utf-8") == "previous target"
    assert not (target / "canonical.sqlite3").exists()


def test_artifact_copy_failure_never_switches_database_or_blob_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Artifact bytes are part of the staged tree and must switch with SQLite."""
    database = tmp_path / "source" / "canonical.sqlite3"
    database.parent.mkdir()
    MigrationRunner(database).migrate()
    clock = MutableClock()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)), clock=clock
    )
    idem = IdempotencyManager(store)
    ctx = _Ctx(store, clock, ClaimService(store, clock, idempotency=idem))
    ArtifactService(store, clock, idempotency=idem).ingest_local_blob(
        ctx.access,
        agent_id=ctx.agent,
        content=b"new backup blob",
        media_type="application/octet-stream",
        idempotency_key="r56-create",
    )
    service = BackupService(store)
    backup = tmp_path / "backup"
    service.create_backup(backup)

    target = tmp_path / "existing-target"
    old_blob = target / "artifacts" / "old" / "payload"
    old_blob.parent.mkdir(parents=True)
    old_blob.write_bytes(b"old live blob")
    sentinel = target / "keep.txt"
    sentinel.write_text("previous target", encoding="utf-8")
    real_copy = backup_module._copy_regular_file

    def fail_on_artifact(source: Path, destination: Path) -> None:
        if backup / "artifacts" in source.parents:
            raise OSError("forced artifact staging failure")
        real_copy(source, destination)

    monkeypatch.setattr(backup_module, "_copy_regular_file", fail_on_artifact)
    report = service.restore_backup(backup, target)
    assert not report.check.ok
    assert any("forced artifact staging failure" in problem for problem in report.check.problems)
    assert sentinel.read_text(encoding="utf-8") == "previous target"
    assert old_blob.read_bytes() == b"old live blob"
    assert not (target.parent / f"{target.name}.restoring").exists()


def test_unexpected_staging_callback_failure_is_reported_and_cleaned(
    tmp_path: Path,
) -> None:
    """An internal preparation bug must not leak staging or replace live data."""
    database = tmp_path / "source.sqlite3"
    MigrationRunner(database).migrate()
    backup = tmp_path / "backup"
    backup_module.write_backup_files(database, backup, "r57-backup")
    target = tmp_path / "existing-target"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("previous target", encoding="utf-8")

    def fail_preparation(_staging: Path) -> None:
        raise RuntimeError("unexpected callback failure")

    report = backup_module.restore_backup(backup, target, prepare_staging=fail_preparation)
    assert not report.check.ok
    assert any("unexpected callback failure" in problem for problem in report.check.problems)
    assert sentinel.read_text(encoding="utf-8") == "previous target"
    assert not (target.parent / f"{target.name}.restoring").exists()


@pytest.mark.parametrize("mutation", ["missing_tree", "extra_blob"])
def test_backup_blob_inventory_is_exact_even_outside_the_payload_checksums(
    tmp_path: Path, mutation: str
) -> None:
    """Artifact files are indirectly authenticated by an exact manifest inventory."""
    database = tmp_path / "source" / "canonical.sqlite3"
    database.parent.mkdir()
    MigrationRunner(database).migrate()
    clock = MutableClock()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)), clock=clock
    )
    idem = IdempotencyManager(store)
    ctx = _Ctx(store, clock, ClaimService(store, clock, idempotency=idem))
    ArtifactService(store, clock, idempotency=idem).ingest_local_blob(
        ctx.access,
        agent_id=ctx.agent,
        content=b"authenticated artifact bytes",
        media_type="application/octet-stream",
        idempotency_key=f"r58-create-{mutation}",
    )
    service = BackupService(store)
    backup = tmp_path / "backup"
    service.create_backup(backup, signing_key=b"r58-test-signing-key-with-32-bytes")
    artifacts_dir = backup / "artifacts"
    if mutation == "missing_tree":
        for path in sorted(artifacts_dir.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        artifacts_dir.rmdir()
    else:
        extra = artifacts_dir / "ff" / "00000000-0000-0000-0000-000000000000"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"not named by the signed manifest")

    check = service.verify_backup(backup, signing_key=b"r58-test-signing-key-with-32-bytes")
    assert not check.ok
    expected = "missing" if mutation == "missing_tree" else "unexpected"
    assert any(expected in problem and "artifact blob" in problem for problem in check.problems)
    target = tmp_path / "restored"
    report = service.restore_backup(
        backup,
        target,
        signing_key=b"r58-test-signing-key-with-32-bytes",
    )
    assert not report.check.ok
    assert not target.exists()
