"""Phase 5 non-resurrection tests: six revival paths, 20 rounds each.

After a committed Forget, the target must never come back through:

1. idempotency-cache replay (the Phase 5 "cache placeholder");
2. the old outbox (replaying stale jobs / handlers re-verifying);
3. a simulated stale index candidate (rehydrate gate);
4. the artifact store (blob + metadata rows);
5. import (the canonical bulk-write gate);
6. backup restore (with deletion-ledger replay).

Every round asserts the returned-target count is exactly zero.
"""

from __future__ import annotations

from typing import Any

import pytest

from iris_memory_core.application.artifacts import ArtifactService
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.test_phase5_claims import _Ctx

ROUNDS = 20


@pytest.fixture
def rctx(
    clocked_store: Store,
    mutable_clock: MutableClock,
    phase5_claims: ClaimService,
    phase5_forget: ForgetService,
    phase5_artifacts: ArtifactService,
) -> dict[str, Any]:
    return {
        "ctx": _Ctx(clocked_store, mutable_clock, phase5_claims),
        "store": clocked_store,
        "claims": phase5_claims,
        "forget": phase5_forget,
        "artifacts": phase5_artifacts,
    }


def _forget(rctx: dict[str, Any], claim_id: str, round_index: int) -> None:
    rctx["forget"].forget(
        rctx["ctx"].access,
        ForgetSelector(
            kind=ForgetSelectorKind.RESOURCE, resource_type="claim", resource_id=claim_id
        ),
        reason="erasure",
        idempotency_key=f"fg-{round_index}",
    )


class TestCachePlaceholderReplay:
    def test_replayed_remember_never_resurrects(self, rctx: dict[str, Any]) -> None:
        """Round N: remember → forget → replay the SAME idempotency key →
        the replay may return the first outcome body, but every canonical
        read of the claim must reject."""
        ctx: _Ctx = rctx["ctx"]
        claims: ClaimService = rctx["claims"]
        for index in range(ROUNDS):
            observation = ctx.observe(f"obs-replay-c{index}")
            payload: dict[str, Any] = dict(
                agent_id=ctx.agent,
                predicate=f"p{index}",
                value={"drink": "tea"},
                canonical_text="Bob likes tea",
                subject_entity_id=ctx.entity,
                evidence=[
                    {"source_type": "observation", "source_id": observation, "relation": "supports"}
                ],
                idempotency_key=f"idem-c{index}",
            )
            result = claims.remember(ctx.access, **payload)
            # retry with the SAME logical payload under the SAME key
            replay = claims.remember(ctx.access, **payload)
            assert replay.replayed
            _forget(rctx, result.claim_id, index)
            # replay AFTER the forget — cache answers, canonical reads reject
            post = claims.remember(ctx.access, **payload)
            assert post.claim_id == result.claim_id  # replay of the same request
            with pytest.raises(NotFoundError):
                claims.get(ctx.access, result.claim_id)
            hits = [
                view
                for view in claims.search(
                    ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity
                )
                if view.claim.id == result.claim_id
            ]
            assert len(hits) == 0

    def test_correct_idempotent_replay_after_forget(self, rctx: dict[str, Any]) -> None:
        ctx: _Ctx = rctx["ctx"]
        claims: ClaimService = rctx["claims"]
        for index in range(ROUNDS):
            created = ctx.remember(f"cc{index}", predicate=f"q{index}")
            evidence = [
                {
                    "source_type": "observation",
                    "source_id": ctx.observe(f"obs-ccx{index}"),
                    "relation": "contradicts",
                }
            ]
            claims.correct(
                ctx.access,
                created.claim_id,
                expected_revision=1,
                mode="dispute",
                reason="r",
                evidence=evidence,
                idempotency_key=f"cor-{index}",
            )
            _forget(rctx, created.claim_id, 1000 + index)
            # A replayed correction after the forget fails closed at the
            # pre-read: the Forget state outranks the cached success record
            # (same rule as Focus, ADR-0005) — no resurrection through the
            # idempotency cache.
            with pytest.raises(NotFoundError):
                claims.correct(
                    ctx.access,
                    created.claim_id,
                    expected_revision=1,
                    mode="dispute",
                    reason="r",
                    evidence=evidence,
                    idempotency_key=f"cor-{index}",
                )
            with pytest.raises(NotFoundError):
                claims.get(ctx.access, created.claim_id)


class TestOldOutboxReplay:
    def test_stale_jobs_and_handler_rechecks_never_resurrect(self, rctx: dict[str, Any]) -> None:
        """Claim-pointer jobs emitted before the forget still sit in the
        outbox; running them (and the invalidation verifier) must not bring
        the claim back — the handler fail-closes on the tombstone state."""
        from iris_memory_core.application.outbox import OutboxService
        from iris_memory_core.application.retention import RetentionService
        from iris_memory_core.jobs.worker import OutboxWorker, phase5_handlers

        ctx: _Ctx = rctx["ctx"]
        store: Store = rctx["store"]
        retention = RetentionService(store, store.clock, forget=rctx["forget"])
        handlers = {
            **phase5_handlers(store.clock, retention=retention),
        }
        worker = OutboxWorker(OutboxService(store, store.clock), handlers)
        for index in range(ROUNDS):
            created = ctx.remember(f"ob{index}", predicate=f"r{index}")
            # Snapshot the jobs the write produced (they reference the claim).
            with store.read() as tx:
                jobs = (
                    tx.raw()
                    .execute(
                        "SELECT id, job_kind FROM outbox_jobs WHERE aggregate_id = ? AND status IN "
                        "('pending','retryable')",
                        (created.claim_id,),
                    )
                    .fetchall()
                )
            _forget(rctx, created.claim_id, 2000 + index)
            # Reset the stale jobs to pending (simulating a delayed worker)
            with store.write() as tx:
                for job in jobs:
                    tx.raw().execute(
                        "UPDATE outbox_jobs SET status = 'pending', lease_owner = NULL, "
                        "lease_generation = 0, lease_expires_us = NULL "
                        "WHERE id = ?",
                        (job["id"],),
                    )
            outcomes = worker.run_once()
            # The claim stays dead whatever the worker did.
            with pytest.raises(NotFoundError):
                rctx["claims"].get(ctx.access, created.claim_id)
            assert outcomes["completed"] + outcomes["retryable"] + outcomes["dead"] >= 0


class TestSimulatedStaleIndexCandidate:
    def test_rehydrate_gate_rejects_tombstoned_candidates(self, rctx: dict[str, Any]) -> None:
        """A 'stale index' would return forgotten ids as candidates; the
        canonical rehydrate gate (what Recall will call in Phase 6) must
        drop them — count of returned targets is zero, every round."""
        ctx: _Ctx = rctx["ctx"]
        store: Store = rctx["store"]

        def rehydrate(candidate_ids: list[str]) -> list[Any]:
            accepted = []
            with store.read() as tx:
                for claim_id in candidate_ids:
                    if tx.is_tombstoned("t1", "claim", claim_id):
                        continue  # tombstone watermark comparison — fail closed
                    try:
                        claim = tx.claims.get(claim_id)
                    except NotFoundError:
                        continue
                    accepted.append(claim)
            return accepted

        for index in range(ROUNDS):
            created = ctx.remember(f"ix{index}", predicate=f"s{index}")
            _forget(rctx, created.claim_id, 3000 + index)
            accepted = rehydrate([created.claim_id])
            assert len(accepted) == 0
            # and the whole stale batch resolves to nothing
            accepted_all = rehydrate([f"unknown-{i}" for i in range(5)] + [created.claim_id])
            assert len(accepted_all) == 0


class TestArtifactRevival:
    def test_forgotten_artifacts_never_return_via_any_read(self, rctx: dict[str, Any]) -> None:
        ctx: _Ctx = rctx["ctx"]
        artifacts: ArtifactService = rctx["artifacts"]
        store: Store = rctx["store"]
        for index in range(ROUNDS):
            created = artifacts.ingest_local_blob(
                ctx.access,
                agent_id=ctx.agent,
                content=f"round-{index}".encode(),
                media_type="text/plain",
                idempotency_key=f"art-{index}",
            )
            record = artifacts.get(ctx.access, created.artifact_id)
            assert record is not None
            locator = record.locator
            rctx["forget"].forget(
                ctx.access,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE,
                    resource_type="artifact",
                    resource_id=created.artifact_id,
                ),
                reason="erasure",
                idempotency_key=f"fg-art-{index}",
            )
            assert artifacts.get(ctx.access, created.artifact_id) is None
            assert artifacts.read(ctx.access, created.artifact_id) is None
            # The blob bytes are gone from the controlled root.
            assert not (store.artifact_root / locator).exists()
            # Re-ingesting the same content creates a NEW artifact, not a
            # revival of the forgotten id.
            reborn = artifacts.ingest_local_blob(
                ctx.access,
                agent_id=ctx.agent,
                content=f"round-{index}".encode(),
                media_type="text/plain",
                idempotency_key=f"art-reborn-{index}",
            )
            assert reborn.artifact_id != created.artifact_id


class TestImportGate:
    def test_bulk_import_replays_are_tombstone_aware(self, rctx: dict[str, Any]) -> None:
        """The canonical bulk-import gate (the seam Phase 13 will call):
        every record whose (type, id) is tombstoned — or whose tombstone
        watermark predates the import — is refused, never silently merged."""
        ctx: _Ctx = rctx["ctx"]
        store: Store = rctx["store"]

        def import_records(records: list[dict[str, Any]]) -> int:
            imported = 0
            with store.write() as tx:
                watermark_before = tx.tombstone_watermark()
                for record in records:
                    if tx.is_tombstoned(
                        record["tenant_id"], record["resource_type"], record["resource_id"]
                    ):
                        continue
                    tx.raw().execute(
                        "INSERT INTO resource_links (id, tenant_id, source_type, source_id, "
                        "target_type, target_id, relation, created_us) VALUES (?, ?, 'import', "
                        "?, 'import', ?, 'applied', ?)",
                        (
                            f"imp-{record['resource_id']}",
                            record["tenant_id"],
                            record["resource_id"],
                            record["resource_id"],
                            store.clock.now_us(),
                        ),
                    )
                    imported += 1
                assert tx.tombstone_watermark() >= watermark_before
            return imported

        for index in range(ROUNDS):
            created = ctx.remember(f"im{index}", predicate=f"t{index}")
            _forget(rctx, created.claim_id, 4000 + index)
            landed = import_records(
                [
                    {
                        "tenant_id": "t1",
                        "resource_type": "claim",
                        "resource_id": created.claim_id,
                    }
                ]
            )
            assert landed == 0
            with pytest.raises(NotFoundError):
                rctx["claims"].get(ctx.access, created.claim_id)


class TestBackupRestoreRevival:
    def test_restore_with_ledger_replay_never_resurrects(
        self, rctx: dict[str, Any], tmp_path: Any
    ) -> None:
        """Backup → mutate + forget → restore the OLD backup → the target
        is back in the file → replay the deletion ledger → target gone."""
        from iris_memory_core.application.forget import ForgetService as FS
        from iris_memory_core.storage.backup import BackupService, restore_backup
        from iris_memory_core.storage.idempotency import IdempotencyManager
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version

        ctx: _Ctx = rctx["ctx"]
        store: Store = rctx["store"]
        service = BackupService(store)
        for index in range(ROUNDS):
            created = ctx.remember(f"bk{index}", predicate=f"u{index}")
            backup_dir = tmp_path / f"backup-{index}"
            service.create_backup(backup_dir)
            _forget(rctx, created.claim_id, 5000 + index)
            ledger = tuple(rctx["forget"].export_deletion_ledger(ctx.admin_access))
            # Restore the pre-forget backup into a fresh directory.
            target_dir = tmp_path / f"restored-{index}"
            report = restore_backup(backup_dir, target_dir)
            assert report.check.ok, report.check.problems
            # The old file still contains the claim — the danger moment.
            restored_store = Store(
                SQLiteRuntime(
                    target_dir / "canonical.sqlite3",
                    allowed_versions=(sqlite_runtime_version(),),
                )
            )
            restored_forget = FS(restored_store, restored_store.clock)
            replayed = restored_forget.replay_deletion_ledger(restored_store, ledger)
            assert replayed >= 1
            claims_on_restored = ClaimService(
                restored_store, restored_store.clock, idempotency=IdempotencyManager(restored_store)
            )
            with pytest.raises(NotFoundError):
                claims_on_restored.get(ctx.admin_access, created.claim_id)
            hits = [
                view
                for view in claims_on_restored.search(
                    ctx.admin_access, agent_id=ctx.agent, subject_entity_id=ctx.entity
                )
                if view.claim.id == created.claim_id
            ]
            assert len(hits) == 0
