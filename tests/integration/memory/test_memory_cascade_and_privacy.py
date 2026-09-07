"""Phase 5 review round 3: regression tests for the second-order defects
found by the adversarial review after the round-2 fixes.

Each test names the defect it pins (R3-1 .. R3-6 from the review report) and
fails against the pre-fix code:

- R3-1  a legacy int-format deletion-ledger manifest failed restore
        verification before the whole-ledger replay fallback could run
- R3-2  observation evidence skipped the privacy evaluation (claims,
        relations, and task/step completion share the gap)
- R3-3  artifact content dedup ignored privacy_labels — a restricted ingest
        aliased a public row
- R3-4  the claim evidence-death cascade was not a transitive closure, and
        explicit ``correct(retract)`` never cascaded at all
- R3-5  two independent forgets in the same microsecond were merged into the
        first request's ledger row
- R3-6  a tombstone-only claim forget in the claim's creation microsecond
        violated the ``superseded_at_us > recorded_at_us`` CHECK
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.artifacts import ArtifactService
from iris_memory_core.application.episodes import RelationService
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.domain.errors import AccessDeniedError
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.storage.backup import BackupService, verify_backup
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.memory.test_memory_claims import _Ctx


def _resource(resource_type: str, resource_id: str) -> ForgetSelector:
    return ForgetSelector(
        kind=ForgetSelectorKind.RESOURCE, resource_type=resource_type, resource_id=resource_id
    )


@pytest.fixture
def rctx(
    clocked_store: Store,
    mutable_clock: MutableClock,
    phase5_idempotency: IdempotencyManager,
) -> tuple[_Ctx, dict[str, Any]]:
    ctx = _Ctx(
        clocked_store,
        mutable_clock,
        ClaimService(clocked_store, mutable_clock, idempotency=phase5_idempotency),
    )
    forget = ForgetService(clocked_store, mutable_clock, idempotency=phase5_idempotency)
    return ctx, {
        "store": clocked_store,
        "clock": mutable_clock,
        "claims": ctx.claims,
        "relations": RelationService(clocked_store, mutable_clock, idempotency=phase5_idempotency),
        "artifacts": ArtifactService(clocked_store, mutable_clock, idempotency=phase5_idempotency),
        "forget": forget,
    }


def _rewrite_legacy_manifest(backup_dir: Path, watermark: int) -> None:
    """Turn a new-format manifest into a legacy int-format one (what an
    older build wrote) and refresh checksums.txt so the directory is a
    well-formed OLD backup, not a corrupted new one."""
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tenants = sorted(manifest.get("forget_ledger_by_tenant", {}))
    manifest["forget_ledger_by_tenant"] = {tenant: watermark for tenant in tenants}
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksums = backup_dir / "checksums.txt"
    lines = []
    for line in checksums.read_text(encoding="utf-8").splitlines():
        _, _, name = line.partition("  ")
        if name.strip() == "manifest.json":
            digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            lines.append(f"{digest}  manifest.json")
        else:
            lines.append(line)
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestR3_1LegacyLedgerManifest:
    def test_legacy_int_manifest_verifies_and_restores(self, tmp_path: Path) -> None:
        """Pre-fix, ``_reconcile_manifest`` compared the legacy int payload
        against the new dict structure, verify failed, and the documented
        whole-ledger replay fallback was unreachable."""
        home = tmp_path / "home"
        home.mkdir()
        database = home / "canonical.sqlite3"
        MigrationRunner(database).migrate()
        clock = MutableClock()
        store = Store(
            SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)), clock=clock
        )
        idem = IdempotencyManager(store)
        ctx = _Ctx(store, clock, ClaimService(store, clock, idempotency=idem))
        forget = ForgetService(store, clock, idempotency=idem)

        before_backup = ctx.remember("r31-before", predicate="before")
        after_backup = ctx.remember("r31-after", predicate="after")
        clock.set(1_700_001_000_000_000)
        forget.forget(
            ctx.admin_access,
            _resource("claim", before_backup.claim_id),
            reason="pre-backup erasure",
            idempotency_key="r31-fg-before",
        )
        service = BackupService(store)
        backup_dir = tmp_path / "backup"
        service.create_backup(backup_dir)
        _rewrite_legacy_manifest(backup_dir, 1_700_001_000_000_000)

        check = verify_backup(backup_dir)
        assert check.ok, check.problems
        # A legacy manifest carries no identities: restore replays the whole
        # supplied ledger (idempotent) rather than set-diffing against nothing.
        assert service.backup_ledger_identities(backup_dir) == {}
        assert service.backup_ledger_watermark(backup_dir) == {"t1": 1_700_001_000_000_000}

        forget.forget(
            ctx.admin_access,
            _resource("claim", after_backup.claim_id),
            reason="post-backup erasure",
            idempotency_key="r31-fg-after",
        )
        ledger = tuple(forget.export_deletion_ledger(ctx.admin_access))
        assert len(ledger) == 2
        target_dir = tmp_path / "restored"
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
        assert restore.check.ok, restore.check.problems
        connection = sqlite3.connect(target_dir / "canonical.sqlite3")
        try:
            for claim_id in (before_backup.claim_id, after_backup.claim_id):
                tombstoned = connection.execute(
                    "SELECT COUNT(*) FROM resource_tombstones WHERE resource_id = ?",
                    (claim_id,),
                ).fetchone()[0]
                assert tombstoned == 1, f"claim {claim_id} resurrected from the backup"
        finally:
            connection.close()

    def test_legacy_manifest_with_wrong_watermark_still_fails(self, tmp_path: Path) -> None:
        """Legacy tolerance verifies the watermark it carries — a wrong one
        is a real mismatch, not a format quirk to wave through."""
        home = tmp_path / "home"
        home.mkdir()
        database = home / "canonical.sqlite3"
        MigrationRunner(database).migrate()
        clock = MutableClock()
        store = Store(
            SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)), clock=clock
        )
        idem = IdempotencyManager(store)
        ctx = _Ctx(store, clock, ClaimService(store, clock, idempotency=idem))
        forget = ForgetService(store, clock, idempotency=idem)
        claim = ctx.remember("r31b", predicate="claim")
        clock.set(1_700_002_000_000_000)
        forget.forget(
            ctx.admin_access,
            _resource("claim", claim.claim_id),
            reason="erasure",
            idempotency_key="r31b-fg",
        )
        service = BackupService(store)
        backup_dir = tmp_path / "backup"
        service.create_backup(backup_dir)
        _rewrite_legacy_manifest(backup_dir, 1_700_002_000_000_000 - 7)
        check = verify_backup(backup_dir)
        assert not check.ok
        assert any("deletion-ledger" in problem for problem in check.problems)


class TestR3_2ObservationEvidencePrivacy:
    def test_non_admin_cannot_cite_restricted_observation(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        restricted_obs = ctx.observe("r32-obs", privacy_labels=["restricted"])
        with pytest.raises(AccessDeniedError, match="observation evidence privacy"):
            claims.remember(
                ctx.access,
                agent_id=ctx.agent,
                predicate="derived-from-restricted",
                value={"v": 1},
                canonical_text="derived from restricted observation",
                subject_entity_id=ctx.entity,
                evidence=[
                    {
                        "source_type": "observation",
                        "source_id": restricted_obs,
                        "relation": "supports",
                    }
                ],
                idempotency_key="r32-denied",
            )
        # The only grant that sees ``restricted`` — admin — may cite it.
        created = claims.remember(
            ctx.admin_access,
            agent_id=ctx.agent,
            predicate="derived-from-restricted",
            value={"v": 1},
            canonical_text="derived from restricted observation",
            subject_entity_id=ctx.entity,
            evidence=[
                {"source_type": "observation", "source_id": restricted_obs, "relation": "supports"}
            ],
            idempotency_key="r32-allowed",
        )
        assert created.claim_id

    def test_relation_evidence_observation_privacy(self, rctx: tuple[_Ctx, dict[str, Any]]) -> None:
        """Relations share the claim evidence validator (ADR-0013 §3): the
        observation's own labels decide, not the relation's."""
        ctx, services = rctx
        relations: RelationService = services["relations"]
        restricted_obs = ctx.observe("r32-rel-obs", privacy_labels=["restricted"])
        with pytest.raises(AccessDeniedError, match="observation evidence privacy"):
            relations.create(
                ctx.access,
                agent_id=ctx.agent,
                source_entity_id=ctx.entity,
                relation_type="knows",
                target_entity_id=ctx.other_entity,
                evidence=[
                    {
                        "source_type": "observation",
                        "source_id": restricted_obs,
                        "relation": "supports",
                    }
                ],
                idempotency_key="r32-rel-denied",
            )


class TestR3_3ArtifactDedupPrivacy:
    def test_restricted_ingest_never_aliases_a_public_artifact(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        artifacts: ArtifactService = services["artifacts"]
        payload = b"r33 identical bytes"
        public = artifacts.ingest_inline(
            ctx.access,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            idempotency_key="r33-public",
        )
        # Pre-fix: this dedup-hit the PUBLIC row and handed its id back for
        # restricted content. Restricted ingest requires admin.
        restricted = artifacts.ingest_inline(
            ctx.admin_access,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            privacy_labels=["restricted"],
            idempotency_key="r33-restricted",
        )
        assert restricted.artifact_id != public.artifact_id
        assert not restricted.deduped
        with ctx.store.read() as tx:
            stored = tx.artifacts.get(restricted.artifact_id)
        assert stored.privacy_labels == ("restricted",)
        # The ordinary caller can read the public copy but NOT the restricted
        # one — under either id.
        assert artifacts.read(ctx.access, public.artifact_id) is not None
        with pytest.raises(AccessDeniedError):
            artifacts.read(ctx.access, restricted.artifact_id)
        # Same content + same labels still dedups onto the restricted row.
        again = artifacts.ingest_inline(
            ctx.admin_access,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            privacy_labels=["restricted"],
            idempotency_key="r33-restricted-2",
        )
        assert again.deduped and again.artifact_id == restricted.artifact_id


class TestR3_4TransitiveEvidenceCascade:
    def test_forget_cascade_is_transitive(self, rctx: tuple[_Ctx, dict[str, Any]]) -> None:
        """A -> B -> C evidence chain: forgetting A's evidence must retract
        B AND then C (the retraction of B kills C's evidence too). Pre-fix C
        stayed active with a dead evidence row."""
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        forget: ForgetService = services["forget"]
        upstream = ctx.remember("r34-a", predicate="chain")
        middle = claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="chain-derived",
            value={"v": 1},
            canonical_text="cites upstream",
            subject_entity_id=ctx.entity,
            evidence=[
                {"source_type": "claim", "source_id": upstream.claim_id, "relation": "supports"}
            ],
            idempotency_key="r34-b",
        )
        downstream = claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="chain-derived-2",
            value={"v": 2},
            canonical_text="cites middle",
            subject_entity_id=ctx.entity,
            evidence=[
                {"source_type": "claim", "source_id": middle.claim_id, "relation": "supports"}
            ],
            idempotency_key="r34-c",
        )
        forget.forget(
            ctx.access,
            _resource("claim", upstream.claim_id),
            reason="top of the chain goes",
            idempotency_key="r34-fg",
        )
        with ctx.store.read() as tx:
            up = tx.claims.get(upstream.claim_id)
            mid = tx.claims.get(middle.claim_id)
            down = tx.claims.get(downstream.claim_id)
            dead_rows = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM claim_evidence "
                    "WHERE invalidated_us IS NOT NULL AND source_type = 'claim'"
                )
                .fetchone()[0]
            )
        assert up.status == "tombstoned"
        assert mid.status == "retracted" and mid.evidence_count == 0
        assert down.status == "retracted" and down.evidence_count == 0
        assert dead_rows == 2, "evidence rows citing A and citing B both die"

    def test_correct_retract_cascades_to_citing_claims(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        """Explicit ``correct(retract)`` is a death of the claim as evidence:
        citing evidence dies and the closure retracts what it leaves
        evidence-less — exactly like a forget."""
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        source = ctx.remember("r34-src", predicate="explicit")
        citing = claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="explicit-derived",
            value={"v": 1},
            canonical_text="cites source",
            subject_entity_id=ctx.entity,
            evidence=[
                {"source_type": "claim", "source_id": source.claim_id, "relation": "supports"}
            ],
            idempotency_key="r34-citing",
        )
        transitive = claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="explicit-derived-2",
            value={"v": 2},
            canonical_text="cites the citing claim",
            subject_entity_id=ctx.entity,
            evidence=[
                {"source_type": "claim", "source_id": citing.claim_id, "relation": "supports"}
            ],
            idempotency_key="r34-transitive",
        )
        claims.correct(
            ctx.access,
            source.claim_id,
            expected_revision=source.revision,
            mode="retract",
            reason="withdrawn by the author",
            idempotency_key="r34-retract",
        )
        with ctx.store.read() as tx:
            src = tx.claims.get(source.claim_id)
            cit = tx.claims.get(citing.claim_id)
            tra = tx.claims.get(transitive.claim_id)
        assert src.status == "retracted"
        assert cit.status == "retracted" and cit.evidence_count == 0
        assert tra.status == "retracted" and tra.evidence_count == 0

    def test_correct_retract_cascades_to_relations(self, rctx: tuple[_Ctx, dict[str, Any]]) -> None:
        """A relation whose only normalized evidence is the retracted claim
        falls with it (relation_evidence dies with its source)."""
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        relations: RelationService = services["relations"]
        source = ctx.remember("r34-rel-src", predicate="relation-source")
        relation = relations.create(
            ctx.access,
            agent_id=ctx.agent,
            source_entity_id=ctx.entity,
            relation_type="knows",
            target_entity_id=ctx.other_entity,
            evidence=[
                {"source_type": "claim", "source_id": source.claim_id, "relation": "supports"}
            ],
            idempotency_key="r34-relation",
        )
        claims.correct(
            ctx.access,
            source.claim_id,
            expected_revision=source.revision,
            mode="retract",
            reason="withdrawn by the author",
            idempotency_key="r34-retract-rel",
        )
        with ctx.store.read() as tx:
            current = tx.relations.get(relation.relation_id)
        assert current.status == "retracted"
        assert current.evidence_count == 0


class TestR3_5DistinctSameInstantForgets:
    def test_same_instant_different_key_is_its_own_request(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        """Pre-fix the ledger identity was (selector, instant): the second
        forget returned the first request's id and result without executing.
        With the full identity tuple it is a distinct request."""
        ctx, services = rctx
        forget: ForgetService = services["forget"]
        ctx.remember("r35-1", predicate="p35", value={"n": 1}, canonical_text="first claim")
        ctx.remember("r35-2", predicate="p35", value={"n": 2}, canonical_text="second claim")
        selector = ForgetSelector(
            kind=ForgetSelectorKind.SUBJECT_PREDICATE,
            agent_id=ctx.agent,
            subject_entity_id=ctx.entity,
        )
        first = forget.forget(ctx.access, selector, reason="r35", idempotency_key="r35-k1")
        assert first.erased_count == 2
        # Same microsecond, DIFFERENT logical request (key): its own row, its
        # own (empty) outcome — claims are already tombstoned.
        second = forget.forget(ctx.access, selector, reason="r35", idempotency_key="r35-k2")
        assert second.request_id != first.request_id
        assert not second.replayed
        assert second.erased_count == 0
        with ctx.store.read() as tx:
            rows = tx.raw().execute("SELECT COUNT(*) FROM forget_requests").fetchone()[0]
        assert rows == 2
        # A TRUE replay of the first request still returns its own result.
        replay = forget.forget(ctx.access, selector, reason="r35", idempotency_key="r35-k1")
        assert replay.replayed
        assert replay.request_id == first.request_id
        assert replay.erased_count == 2

    def test_management_plane_reason_and_mode_distinguish(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        """No-key management-plane forgets in one microsecond: reason and
        erasure mode are identity components — three distinct rows."""
        ctx, services = rctx
        forget: ForgetService = services["forget"]
        obs = ctx.observe(
            "r35-mp-obs", access=ctx.wide_access, space_id=ctx.space, session_id="sess-1"
        )
        ctx.claims.remember(
            ctx.wide_access,
            agent_id=ctx.agent,
            predicate="mp-bulk",
            value={"v": 1},
            canonical_text="session claim",
            subject_entity_id=ctx.entity,
            space_id=ctx.space,
            session_id="sess-1",
            evidence=[{"source_type": "observation", "source_id": obs, "relation": "supports"}],
            idempotency_key="r35-mp-claim",
        )
        selector = ForgetSelector(
            kind=ForgetSelectorKind.SESSION, space_id=ctx.space, session_id="sess-1"
        )
        first = forget.forget(ctx.admin_access, selector, reason="bulk-a", erase_content=True)
        second = forget.forget(ctx.admin_access, selector, reason="bulk-b", erase_content=True)
        third = forget.forget(ctx.admin_access, selector, reason="bulk-a", erase_content=False)
        ids = {first.request_id, second.request_id, third.request_id}
        assert len(ids) == 3
        with ctx.store.read() as tx:
            rows = (
                tx.raw()
                .execute("SELECT idempotency_key, reason_code, erase_content FROM forget_requests")
                .fetchall()
            )
        assert len(rows) == 3
        assert {row[1] for row in rows} == {"bulk-a", "bulk-b"}
        assert sorted(row[2] for row in rows) == [0, 1, 1]


class TestR3_6SameMicrosecondTombstoneOnlyForget:
    def test_tombstone_only_forget_clamps_the_supersede_stamp(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        """A claim forgotten in its own creation microsecond with
        erase_content=False: the system-time stamp clamps to
        recorded_at_us + 1 — zero-length intervals are not representable
        (pre-fix: CHECK constraint failure aborted the transaction)."""
        ctx, services = rctx
        forget: ForgetService = services["forget"]
        claim = ctx.remember("r36")  # frozen clock: creation == forget instant
        result = forget.forget(
            ctx.access,
            _resource("claim", claim.claim_id),
            reason="tombstone-only",
            erase_content=False,
            idempotency_key="r36-key",
        )
        assert result.erased_count == 1
        with ctx.store.read() as tx:
            row = tx.claims.get(claim.claim_id)
        assert row.status == "tombstoned"
        assert row.superseded_at_us is not None
        assert row.superseded_at_us > row.recorded_at_us
