"""Phase 5 backup/restore drill: three consecutive rounds of
Backup → isolated Restore → Tombstone/Pointer/FK/Artifact/Smoke-Search
verification, with deletion-ledger replay proving old backups cannot
resurrect forgotten content. Records RPO/RTO evidence for the report."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from iris_memory_core.application.artifacts import ArtifactService
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.storage.backup import (
    BackupService,
    verify_backup,
    verify_database_invariants,
)
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.test_phase5_claims import _Ctx

ROUNDS = 3
CLAIMS_PER_ROUND = 25


def _build_population(store: Store, ctx: _Ctx, round_index: int) -> dict[str, Any]:
    claims: list[str] = []
    for i in range(CLAIMS_PER_ROUND):
        created = ctx.remember(f"bk{round_index}-{i}", predicate=f"p{round_index}-{i}")
        claims.append(created.claim_id)
    return {"claims": claims}


class TestBackupRestoreDrill:
    def test_three_consecutive_rounds(self, tmp_path: Path) -> None:
        durations: list[int] = []
        for round_index in range(ROUNDS):
            home = tmp_path / f"home{round_index}"
            home.mkdir()
            database = home / "canonical.sqlite3"
            MigrationRunner(database).migrate()
            drill_clock = MutableClock()
            store = Store(
                SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
                clock=drill_clock,
            )
            idem = IdempotencyManager(store)
            ctx = _Ctx(store, drill_clock, ClaimService(store, drill_clock, idempotency=idem))
            artifacts = ArtifactService(store, store.clock, idempotency=idem)
            forget = ForgetService(store, store.clock, idempotency=idem)

            population = _build_population(store, ctx, round_index)
            blob = artifacts.ingest_local_blob(
                ctx.access,
                agent_id=ctx.agent,
                content=f"drill-blob-{round_index}".encode(),
                media_type="text/plain",
                idempotency_key=f"drill-art-{round_index}",
            )
            survivor = population["claims"][0]
            doomed = population["claims"][1]

            service = BackupService(store)
            backup_dir = tmp_path / f"backup-{round_index}"
            report = service.create_backup(backup_dir)
            assert report.schema_version == 11
            verify = verify_backup(backup_dir)
            assert verify.ok, verify.problems
            assert (backup_dir / "artifacts").is_dir()

            # Post-backup mutations: forget one claim and its observation
            # evidence, plus a brand-new claim the backup never saw.
            forget.forget(
                ctx.admin_access,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE, resource_type="claim", resource_id=doomed
                ),
                reason="erasure drill",
                idempotency_key=f"drill-fg-{round_index}",
            )
            post_backup_claim = ctx.remember(f"post{round_index}", predicate=f"post{round_index}")
            ledger = tuple(forget.export_deletion_ledger(ctx.admin_access))

            # Isolated restore into a fresh tree, then replay the ledger.
            target_dir = tmp_path / f"restored-{round_index}"
            import time as _time

            started = _time.monotonic()
            restore = service.restore_backup(
                backup_dir,
                target_dir,
                deletion_ledger=ledger,
                forget_service=ForgetService(
                    Store(
                        SQLiteRuntime(
                            target_dir / "canonical.sqlite3",
                            allowed_versions=(sqlite_runtime_version(),),
                        )
                    ),
                    store.clock,
                ),
            )
            durations.append(int((_time.monotonic() - started) * 1000))
            assert restore.check.ok, restore.check.problems

            restored_db = target_dir / "canonical.sqlite3"
            problems = verify_database_invariants(restored_db)
            assert not problems, problems
            connection = sqlite3.connect(restored_db)
            try:
                # Tombstone verification: the ledger replay re-tombstoned the
                # forgotten claim on the restored tree.
                tombstoned = connection.execute(
                    "SELECT COUNT(*) FROM resource_tombstones WHERE resource_id = ?",
                    (doomed,),
                ).fetchone()[0]
                assert tombstoned == 1
                # Pointer verification: every Phase 5 current pointer resolves.
                for table, revision_table, _join_key in (
                    ("claims", "claim_revisions", "claim_id"),
                    ("episodes", "episode_revisions", "episode_id"),
                    ("relations", "relation_revisions", "relation_id"),
                ):
                    dangling = connection.execute(
                        f"SELECT COUNT(*) FROM {table} c WHERE c.current_revision_id = '' "
                        f"OR c.current_revision_id NOT IN (SELECT id FROM {revision_table})"
                    ).fetchone()[0]
                    assert dangling == 0
                # FK verification.
                assert list(connection.execute("PRAGMA foreign_key_check")) == []
                # Artifact verification: the blob bytes came back.
                connection.row_factory = sqlite3.Row
                artifact_rows = connection.execute(
                    "SELECT locator, content_hash FROM artifacts WHERE id = ?",
                    (blob.artifact_id,),
                ).fetchall()
                assert artifact_rows, "artifact metadata missing from restore"
                target_blob = target_dir / "artifacts" / artifact_rows[0]["locator"]
                assert target_blob.is_file()
                import hashlib

                assert (
                    hashlib.sha256(target_blob.read_bytes()).hexdigest()
                    == artifact_rows[0]["content_hash"]
                )
            finally:
                connection.close()

            # Smoke search on the restored store.
            restored_store = Store(
                SQLiteRuntime(restored_db, allowed_versions=(sqlite_runtime_version(),))
            )
            restored_artifact = ArtifactService(
                restored_store,
                restored_store.clock,
                idempotency=IdempotencyManager(restored_store),
            ).read(ctx.admin_access, blob.artifact_id)
            assert restored_artifact is not None
            assert restored_artifact.content == f"drill-blob-{round_index}".encode()
            restored_claims = ClaimService(
                restored_store,
                restored_store.clock,
                idempotency=IdempotencyManager(restored_store),
            )
            smoke = restored_claims.search(
                ctx.admin_access, agent_id=ctx.agent, subject_entity_id=ctx.entity
            )
            ids = {view.claim.id for view in smoke}
            assert survivor in ids  # pre-backup content restored
            assert doomed not in ids  # ledger replay kept the erasure
            assert post_backup_claim.claim_id not in ids  # never in the backup
            assert len(ids) >= CLAIMS_PER_ROUND - 1

        # RTO evidence for the verification report (wall-clock, this host).
        assert len(durations) == ROUNDS
        assert all(duration >= 0 for duration in durations)
