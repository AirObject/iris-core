"""Phase 5 review round 4: regression tests for the third round of
second-order adversarial defects.

Each test names the defect it pins (R4-1 .. R4-5 from the review report) and
fails against the pre-fix code:

- R4-1  "legacy backup" tolerance only handled old manifest SHAPES, not a
        real older-Schema-6 snapshot: the readers SELECT the new columns
        (app_instance_id / idempotency_key / erase_content / privacy_key)
        directly and a genuine pre-release backup failed verify/restore with
        ``no such column``
- R4-2  the forget ledger identity omitted the idempotency namespace's
        app_instance_id: two app instances reusing one key in the same
        microsecond collapsed onto the first request's ledger row
- R4-3  privacy_key used a ``\\x1f`` separator join — a label containing the
        separator collided with the two-label set (["custom:a\\x1fcustom:b"]
        vs ["custom:a", "custom:b"])
- R4-4  the evidence-death cascade only processed ACTIVE claims/relations: a
        disputed claim kept its stale denormalized evidence_count and the
        death did not propagate through it
- R4-5  cascade retractions produced new revisions with no claim.changed /
        relation.changed outbox events, so projections could keep serving the
        old active state
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
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.storage.backup import (
    BackupService,
    verify_backup,
    verify_database_invariants,
)
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import MigrationRunner, default_migrations_path
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for
from tests.integration.test_phase5_claims import _Ctx


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


# The REAL older Schema 6 column set (what a pre-release round-2 build had):
# no app_instance_id / idempotency_key / erase_content, and the coarser
# UNIQUE (tenant, selector, created_us) request identity.
_ROUND2_FORGET_REQUESTS_DDL = """
CREATE TABLE forget_requests (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    selector_key TEXT NOT NULL,
    selector_json TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    tombstone_seq_lo INTEGER NOT NULL,
    tombstone_seq_hi INTEGER NOT NULL,
    target_count INTEGER NOT NULL,
    erased_count INTEGER NOT NULL,
    protected_skipped INTEGER NOT NULL,
    held_skipped INTEGER NOT NULL,
    UNIQUE (tenant_id, selector_key, created_us)
) STRICT
"""

_ROUND2_MIGRATION_CHECKSUM = "2c63f2ac96611016f34c210d1731cd2d836e05d04c67699741e5d91ae58b412b"


def _refresh_backup_checksums(backup_dir: Path) -> None:
    """Re-anchor manifest and checksums to the rewritten snapshot bytes — an
    old backup is internally consistent, not a corrupted new one."""
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["canonical.sqlite3"] = hashlib.sha256(
        (backup_dir / "canonical.sqlite3").read_bytes()
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"{hashlib.sha256((backup_dir / name).read_bytes()).hexdigest()}  {name}"
        for name in ("canonical.sqlite3", "manifest.json", "config-fingerprint.json")
    ]
    (backup_dir / "checksums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _downgrade_snapshot_to_round2(backup_dir: Path) -> None:
    """Turn a fresh backup into a FAITHFUL older-build backup: the snapshot's
    tables rebuilt with the older column set AND the manifest rewritten into
    the request-entry shape that build wrote — a real old backup, not a
    corrupted new one."""
    canonical = backup_dir / "canonical.sqlite3"
    connection = sqlite3.connect(canonical)
    try:
        connection.execute("DROP INDEX idx_artifacts_live_content")
        connection.execute("ALTER TABLE artifacts DROP COLUMN privacy_key")
        connection.execute(
            "CREATE UNIQUE INDEX idx_artifacts_live_content ON artifacts "
            "(tenant_id, scope_key, content_hash, storage_kind) WHERE status = 'active'"
        )
        connection.execute("ALTER TABLE forget_requests RENAME TO forget_requests_current")
        connection.execute(_ROUND2_FORGET_REQUESTS_DDL)
        connection.execute(
            "INSERT INTO forget_requests (id, tenant_id, selector_key, selector_json, "
            "reason_code, requested_by, created_us, tombstone_seq_lo, tombstone_seq_hi, "
            "target_count, erased_count, protected_skipped, held_skipped) "
            "SELECT id, tenant_id, selector_key, selector_json, reason_code, requested_by, "
            "created_us, tombstone_seq_lo, tombstone_seq_hi, target_count, erased_count, "
            "protected_skipped, held_skipped FROM forget_requests_current"
        )
        connection.execute("DROP TABLE forget_requests_current")
        connection.execute(
            "CREATE INDEX idx_forget_requests_tenant ON forget_requests (tenant_id, created_us)"
        )
        rows = connection.execute(
            "SELECT tenant_id, selector_key, created_us FROM forget_requests ORDER BY created_us"
        ).fetchall()
        # A real snapshot records the migration bytes that created its old
        # shape, not the checksum of the current test checkout — and a
        # round-2 build predates Phase 6 entirely, so its schema stops at 6.
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 6",
            (_ROUND2_MIGRATION_CHECKSUM,),
        )
        for table in (
            "fts_index",
            "fts_documents",
            "fts_current",
            "fts_generations",
            "fts_projection_state",
            "recall_usage_reports",
            "recall_requests",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        for phase7_table in (
            "vector_projection_state",
            "vector_generations",
            "vector_current",
            "vector_id_map",
            "vector_delta_ledger",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {phase7_table}")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 7")
        connection.commit()
    finally:
        connection.close()
    # The old build's manifest carried only {selector_key, created_us} request
    # entries — the identity columns did not exist for it to record.
    ledger: dict[str, dict[str, Any]] = {}
    for tenant_id, selector_key, created_us in rows:
        entry = ledger.setdefault(str(tenant_id), {"watermark_us": 0, "requests": []})
        entry["watermark_us"] = max(int(entry["watermark_us"]), int(created_us))
        entry["requests"].append({"selector_key": str(selector_key), "created_us": int(created_us)})
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["forget_ledger_by_tenant"] = ledger
    manifest["schema_version"] = 6
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _refresh_backup_checksums(backup_dir)


class TestR4_1RealLegacySchemaBackup:
    def test_round2_schema_backup_verifies_restores_and_replays(self, tmp_path: Path) -> None:
        """Pre-fix, both the backup writer and the verify/restore reader
        SELECTed the new ledger columns directly: a genuine older-Schema-6
        backup died with ``no such column: idempotency_key`` before the
        manifest tolerance could ever matter."""
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

        before_backup = ctx.remember("r41-before", predicate="before")
        after_backup = ctx.remember("r41-after", predicate="after")
        clock.set(1_700_003_000_000_000)
        forget.forget(
            ctx.admin_access,
            _resource("claim", before_backup.claim_id),
            reason="pre-backup erasure",
            idempotency_key="r41-fg-before",
        )
        service = BackupService(store)
        backup_dir = tmp_path / "backup"
        service.create_backup(backup_dir)
        _downgrade_snapshot_to_round2(backup_dir)

        check = verify_backup(backup_dir)
        assert check.ok, check.problems
        # The coarse manifest cannot feed exact set-difference replay: the
        # restore replays the whole supplied ledger instead (idempotent).
        assert service.backup_ledger_identities(backup_dir) == {}
        assert service.backup_ledger_watermark(backup_dir) == {"t1": 1_700_003_000_000_000}

        forget.forget(
            ctx.admin_access,
            _resource("claim", after_backup.claim_id),
            reason="post-backup erasure",
            idempotency_key="r41-fg-after",
        )
        ledger = tuple(forget.export_deletion_ledger(ctx.admin_access))
        assert len(ledger) == 2
        target_dir = tmp_path / "restored"
        restored_store = Store(
            SQLiteRuntime(
                target_dir / "canonical.sqlite3",
                allowed_versions=(sqlite_runtime_version(),),
            )
        )
        restore = service.restore_backup(
            backup_dir,
            target_dir,
            deletion_ledger=ledger,
            forget_service=ForgetService(restored_store, store.clock),
        )
        assert restore.check.ok, restore.check.problems
        # Restore normalizes the authenticated staging copy before switching:
        # the target carries the current columns, full identity, and checksum.
        assert not verify_database_invariants(target_dir / "canonical.sqlite3")
        connection = sqlite3.connect(target_dir / "canonical.sqlite3")
        try:
            for claim_id in (before_backup.claim_id, after_backup.claim_id):
                tombstoned = connection.execute(
                    "SELECT COUNT(*) FROM resource_tombstones WHERE resource_id = ?",
                    (claim_id,),
                ).fetchone()[0]
                assert tombstoned == 1, f"claim {claim_id} resurrected from the backup"
            rows = connection.execute("SELECT COUNT(*) FROM forget_requests").fetchone()[0]
            assert rows == 3, "coarse historical row plus two exact replay identities"
            columns = {row[1] for row in connection.execute("PRAGMA table_info(forget_requests)")}
            assert {"app_instance_id", "idempotency_key", "erase_content"} <= columns
            artifact_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(artifacts)")
            }
            assert "privacy_key" in artifact_columns
            recorded_checksum = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = 6"
            ).fetchone()[0]
        finally:
            connection.close()
        expected_checksum = hashlib.sha256(
            (default_migrations_path() / "0006_phase5_long_term_memory.sql").read_bytes()
        ).hexdigest()
        assert recorded_checksum == expected_checksum
        # The restored Schema 6 snapshot upgrades through the ordinary
        # startup migration (ADR-0014 §9: restore never forward-migrates).
        assert [
            item.version for item in MigrationRunner(target_dir / "canonical.sqlite3").migrate()
        ] == [7, 8]

        # "Can continue serving" includes the repository path that needs the
        # newly backfilled privacy key, not only deletion-ledger replay.
        restored_artifacts = ArtifactService(
            restored_store,
            store.clock,
            idempotency=IdempotencyManager(restored_store),
        )
        created = restored_artifacts.ingest_inline(
            ctx.admin_access,
            agent_id=ctx.agent,
            content=b"post-restore artifact",
            media_type="text/plain",
            idempotency_key="r41-post-restore-artifact",
        )
        assert created.artifact_id


class TestR4_2AppInstanceIdentity:
    def test_same_instant_same_key_different_app_is_distinct(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        """Pre-fix the ledger identity had no app component while the
        idempotency cache namespace did: app-2's forget returned app-1's
        request id with replayed=False and never executed its own semantics."""
        ctx, services = rctx
        forget: ForgetService = services["forget"]
        claim_a = ctx.remember("r42-a", predicate="p42", value={"n": 1}, canonical_text="a")
        claim_b = ctx.remember("r42-b", predicate="p42", value={"n": 2}, canonical_text="b")
        app2 = access_for(
            "t1",
            agent_ids=frozenset({ctx.agent}),
            space_ids=frozenset({ctx.space}),
            consent_entities=frozenset({ctx.entity}),
            app_instance_id="app-2",
        )
        first = forget.forget(
            ctx.access,
            _resource("claim", claim_a.claim_id),
            reason="r42",
            idempotency_key="shared-key",
        )
        second = forget.forget(
            app2,
            _resource("claim", claim_b.claim_id),
            reason="r42",
            idempotency_key="shared-key",
        )
        assert second.request_id != first.request_id
        assert not second.replayed
        assert second.erased_count == 1, "app-2's own claim must actually be erased"
        with ctx.store.read() as tx:
            rows = (
                tx.raw()
                .execute("SELECT app_instance_id, idempotency_key FROM forget_requests")
                .fetchall()
            )
        assert {tuple(row) for row in rows} == {
            ("app-1", "shared-key"),
            ("app-2", "shared-key"),
        }
        # A TRUE replay by app-1 still returns its own original outcome.
        replay = forget.forget(
            ctx.access,
            _resource("claim", claim_a.claim_id),
            reason="r42",
            idempotency_key="shared-key",
        )
        assert replay.replayed
        assert replay.request_id == first.request_id
        assert replay.erased_count == 1


class TestR4_3PrivacyKeyEncoding:
    def test_separator_inside_a_label_never_collides(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        """Pre-fix privacy_key was a \\x1f join: ["custom:a", "custom:b"] and
        the single (legal) label "custom:a\\x1fcustom:b" produced one key, so
        the second ingest dedup-hit the first row and stored an empty label
        set the first caller could then read."""
        ctx, services = rctx
        artifacts: ArtifactService = services["artifacts"]
        # Custom labels are tenant-granted (not admin-implied): one context
        # granted all three labels performs both ingests.
        granter = AccessContext(
            tenant_id="t1",
            app_instance_id="r43-app",
            admin=True,
            agent_ids=frozenset({ctx.agent}),
            allowed_space_ids=frozenset({ctx.space, ctx.other_space}),
            granted_custom_labels=frozenset({"custom:a", "custom:b", "custom:a\x1fcustom:b"}),
        )
        payload = b"r43 identical bytes"
        multi = artifacts.ingest_inline(
            granter,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            privacy_labels=["custom:a", "custom:b"],
            idempotency_key="r43-multi",
        )
        single = artifacts.ingest_inline(
            granter,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            privacy_labels=["custom:a\x1fcustom:b"],
            idempotency_key="r43-single",
        )
        assert single.artifact_id != multi.artifact_id
        assert not single.deduped
        with ctx.store.read() as tx:
            stored_multi = tx.artifacts.get(multi.artifact_id)
            stored_single = tx.artifacts.get(single.artifact_id)
        assert stored_multi.privacy_labels == ("custom:a", "custom:b")
        assert stored_single.privacy_labels == ("custom:a\x1fcustom:b",)
        # Each label set still dedups onto its OWN row only.
        again_multi = artifacts.ingest_inline(
            granter,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            privacy_labels=["custom:b", "custom:a"],
            idempotency_key="r43-multi-2",
        )
        again_single = artifacts.ingest_inline(
            granter,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            privacy_labels=["custom:a\x1fcustom:b"],
            idempotency_key="r43-single-2",
        )
        assert again_multi.deduped and again_multi.artifact_id == multi.artifact_id
        assert again_single.deduped and again_single.artifact_id == single.artifact_id


class TestR4_4DisputedEvidenceCascade:
    def test_disputed_claim_retracts_and_cascade_continues(
        self, rctx: tuple[_Ctx, dict[str, Any]], tmp_path: Path
    ) -> None:
        """Pre-fix the cascade only processed active claims: a disputed claim
        whose evidence died kept status=disputed with a stale
        evidence_count, and the claims citing IT stayed active."""
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        forget: ForgetService = services["forget"]
        upstream = ctx.remember("r44-a", predicate="chain")
        middle = claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="chain-disputed",
            value={"v": 1},
            canonical_text="cites upstream",
            subject_entity_id=ctx.entity,
            evidence=[
                {"source_type": "claim", "source_id": upstream.claim_id, "relation": "supports"}
            ],
            idempotency_key="r44-b",
        )
        contradicting = ctx.observe("r44-obs")
        claims.correct(
            ctx.access,
            middle.claim_id,
            expected_revision=middle.revision,
            mode="dispute",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": contradicting,
                    "relation": "contradicts",
                }
            ],
            reason="challenged",
            idempotency_key="r44-dispute",
        )
        with ctx.store.read() as tx:
            assert tx.claims.get(middle.claim_id).status == "disputed"
        # A disputed claim is admissible live evidence (ADR-0013 §3).
        downstream = claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="chain-disputed-2",
            value={"v": 2},
            canonical_text="cites the disputed middle",
            subject_entity_id=ctx.entity,
            evidence=[
                {"source_type": "claim", "source_id": middle.claim_id, "relation": "supports"}
            ],
            idempotency_key="r44-c",
        )
        # The dispute's own evidence dies first: the disputed claim keeps its
        # one surviving evidence row — recounted (pre-fix the denormalized
        # count stayed stale for disputed claims), still disputed.
        forget.forget(
            ctx.access,
            _resource("observation", contradicting),
            reason="dispute evidence goes",
            idempotency_key="r44-fg-obs",
        )
        with ctx.store.read() as tx:
            mid = tx.claims.get(middle.claim_id)
            down = tx.claims.get(downstream.claim_id)
        assert mid.status == "disputed" and mid.evidence_count == 1
        assert down.status == "active"
        # Now the LAST evidence dies: the disputed claim retracts and the
        # closure continues through it to the downstream claim.
        forget.forget(
            ctx.access,
            _resource("claim", upstream.claim_id),
            reason="top of the chain goes",
            idempotency_key="r44-fg",
        )
        with ctx.store.read() as tx:
            mid = tx.claims.get(middle.claim_id)
            down = tx.claims.get(downstream.claim_id)
        assert mid.status == "retracted" and mid.evidence_count == 0
        assert down.status == "retracted" and down.evidence_count == 0
        # The restore invariants (evidence_count == valid rows) hold — the
        # pre-fix state failed them. Checked on a fresh online backup so the
        # snapshot includes WAL content.
        BackupService(ctx.store).create_backup(tmp_path / "r44-inv")
        assert not verify_database_invariants(tmp_path / "r44-inv" / "canonical.sqlite3")

    def test_disputed_relation_retracts_when_evidence_dies(
        self, rctx: tuple[_Ctx, dict[str, Any]], tmp_path: Path
    ) -> None:
        """The relation-side helper had the same active-only status check
        (no relation dispute surface exists yet, so the state is set directly)."""
        ctx, services = rctx
        relations: RelationService = services["relations"]
        forget: ForgetService = services["forget"]
        source = ctx.remember("r44-rel-src", predicate="relation-source")
        relation = relations.create(
            ctx.access,
            agent_id=ctx.agent,
            source_entity_id=ctx.entity,
            relation_type="knows",
            target_entity_id=ctx.other_entity,
            evidence=[
                {"source_type": "claim", "source_id": source.claim_id, "relation": "supports"}
            ],
            idempotency_key="r44-relation",
        )
        with ctx.store.write() as tx:
            tx.raw().execute(
                "UPDATE relations SET status = 'disputed' WHERE id = ?",
                (relation.relation_id,),
            )
        forget.forget(
            ctx.access,
            _resource("claim", source.claim_id),
            reason="evidence goes",
            idempotency_key="r44-rel-fg",
        )
        with ctx.store.read() as tx:
            current = tx.relations.get(relation.relation_id)
        assert current.status == "retracted"
        assert current.evidence_count == 0
        BackupService(ctx.store).create_backup(tmp_path / "r44-rel-inv")
        assert not verify_database_invariants(tmp_path / "r44-rel-inv" / "canonical.sqlite3")


class TestR4_5CascadeChangeEvents:
    def test_claim_cascade_retractions_emit_claim_changed(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        """Pre-fix the retraction revision only moved the pointer/watermark/
        audit: no claim.changed ever reached the outbox, and memory.invalidated
        structurally cannot carry retracted (non-tombstoned) claims — so
        projections had no input at all for the new state."""
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        forget: ForgetService = services["forget"]
        upstream = ctx.remember("r45-a", predicate="chain")
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
            idempotency_key="r45-b",
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
            idempotency_key="r45-c",
        )
        forget.forget(
            ctx.access,
            _resource("claim", upstream.claim_id),
            reason="top of the chain goes",
            idempotency_key="r45-fg",
        )
        with ctx.store.read() as tx:
            rows = (
                tx.raw()
                .execute(
                    "SELECT job_kind, aggregate_id, source_revision, payload FROM outbox_jobs "
                    "WHERE job_kind IN ('claim.changed', 'memory.invalidated')"
                )
                .fetchall()
            )
        # Creation emitted revision-1 events; the cascade must ALSO emit a
        # revision-2 retraction event for each fallen claim.
        claim_events = {
            (row["aggregate_id"], row["source_revision"]): json.loads(row["payload"])
            for row in rows
            if row["job_kind"] == "claim.changed"
        }
        payload = claim_events[(middle.claim_id, 2)]
        assert payload == {"version": 1, "claim_id": middle.claim_id, "revision": 2}
        assert claim_events[(downstream.claim_id, 2)] == {
            "version": 1,
            "claim_id": downstream.claim_id,
            "revision": 2,
        }
        # memory.invalidated names ONLY tombstoned resources: the retracted
        # cascade members are deliberately absent (they carry no tombstone).
        named: set[tuple[str, str]] = set()
        for row in rows:
            if row["job_kind"] != "memory.invalidated":
                continue
            for item in json.loads(row["payload"])["resources"]:
                named.add((item["resource_type"], item["resource_id"]))
        assert named == {("claim", upstream.claim_id)}

    def test_relation_cascade_retraction_emits_relation_changed(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        relations: RelationService = services["relations"]
        forget: ForgetService = services["forget"]
        source = ctx.remember("r45-rel-src", predicate="relation-source")
        relation = relations.create(
            ctx.access,
            agent_id=ctx.agent,
            source_entity_id=ctx.entity,
            relation_type="knows",
            target_entity_id=ctx.other_entity,
            evidence=[
                {"source_type": "claim", "source_id": source.claim_id, "relation": "supports"}
            ],
            idempotency_key="r45-relation",
        )
        forget.forget(
            ctx.access,
            _resource("claim", source.claim_id),
            reason="evidence goes",
            idempotency_key="r45-rel-fg",
        )
        with ctx.store.read() as tx:
            rows = (
                tx.raw()
                .execute(
                    "SELECT source_revision, payload FROM outbox_jobs "
                    "WHERE job_kind = 'relation.changed' AND aggregate_id = ?",
                    (relation.relation_id,),
                )
                .fetchall()
            )
        # The revision-1 row is the creation event; the cascade's retraction
        # is a revision-2 event alongside it.
        by_revision = {row["source_revision"]: json.loads(row["payload"]) for row in rows}
        assert by_revision[2] == {
            "version": 1,
            "relation_id": relation.relation_id,
            "revision": 2,
        }
