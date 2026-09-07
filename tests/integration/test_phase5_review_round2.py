"""Phase 5 review round 2: regression tests for the eight defects found by
the adversarial review after the first full CI pass.

Each test names the defect it pins (P0-1, P0-2, P1-3 .. P1-8 from the review
report) and fails against the pre-fix code:

- P0-1  deletion-ledger watermark missed same-microsecond forgets on restore
- P0-2  subject_predicate forget crossed the caller's space envelope
- P1-3  task artifact evidence skipped the privacy evaluation
- P1-4  claim-as-evidence skipped agent/scope/privacy/live-status admission
- P1-5  relation evidence lived only in revision JSON (not invalidatable)
- P1-6  artifact content dedup was tenant-global, breaking scope identity
- P1-7  a successful resource forget could not replay its idempotent result
- P1-8  an empty/all-held forget outcome violated the ledger seq CHECK
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.artifacts import ArtifactService
from iris_memory_core.application.episodes import EpisodeService, RelationService
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.domain.errors import AccessDeniedError, InvalidRequestError, NotFoundError
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
    """The full Phase 5 service set over one deterministic store."""
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
        "episodes": EpisodeService(clocked_store, mutable_clock, idempotency=phase5_idempotency),
        "relations": RelationService(clocked_store, mutable_clock, idempotency=phase5_idempotency),
        "artifacts": ArtifactService(clocked_store, mutable_clock, idempotency=phase5_idempotency),
        "forget": forget,
        "retention": RetentionService(clocked_store, mutable_clock, forget=forget),
    }


class TestP0_1LedgerIdentityReplay:
    def test_same_microsecond_forget_around_backup_is_replayed(self, tmp_path: Path) -> None:
        """Two forgets in the SAME microsecond, one before and one after the
        backup: a MAX(created_us) boundary with strict ``>`` misses the
        second one; identity set-difference must replay it."""
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

        before_backup = ctx.remember("p01-before", predicate="before")
        after_backup = ctx.remember("p01-after", predicate="after")

        clock.set(1_700_000_900_000_000)
        forget.forget(
            ctx.admin_access,
            _resource("claim", before_backup.claim_id),
            reason="pre-backup erasure",
            idempotency_key="p01-fg-before",
        )
        service = BackupService(store)
        backup_dir = tmp_path / "backup"
        report = service.create_backup(backup_dir)
        assert report.schema_version == 20
        assert verify_backup(backup_dir).ok
        # No clock advance: the post-backup forget lands on the exact same
        # created_us microsecond as the pre-backup one.
        forget.forget(
            ctx.admin_access,
            _resource("claim", after_backup.claim_id),
            reason="post-backup erasure",
            idempotency_key="p01-fg-after",
        )
        with store.read() as tx:
            stamps = (
                tx.raw()
                .execute("SELECT created_us FROM forget_requests ORDER BY selector_key")
                .fetchall()
            )
        assert len({row[0] for row in stamps}) == 1, "both forgets must share one microsecond"

        identities = service.backup_ledger_identities(backup_dir)
        assert len(identities["t1"]) == 1, "only the pre-backup row is in the manifest"
        assert service.backup_ledger_watermark(backup_dir) == {"t1": 1_700_000_900_000_000}

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
        assert not verify_database_invariants(target_dir / "canonical.sqlite3")
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


class TestP0_2SubjectPredicateSpaceEnvelope:
    def test_narrow_space_caller_cannot_forget_other_space_claims(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        forget: ForgetService = services["forget"]
        claims: ClaimService = services["claims"]
        # Two claims for the same subject+predicate: one in the caller's
        # space, one in the other space of the SAME agent (created by a
        # caller entitled to both).
        obs_in = ctx.observe(
            "p02-obs-in", access=ctx.wide_access, space_id=ctx.space, session_id="sess-1"
        )
        obs_out = ctx.observe(
            "p02-obs-out", access=ctx.wide_access, space_id=ctx.other_space, session_id="sess-2"
        )
        in_space = claims.remember(
            ctx.wide_access,
            agent_id=ctx.agent,
            predicate="envelope",
            value={"v": 1},
            canonical_text="in space",
            subject_entity_id=ctx.entity,
            space_id=ctx.space,
            session_id="sess-1",
            evidence=[{"source_type": "observation", "source_id": obs_in, "relation": "supports"}],
            idempotency_key="p02-in",
        )
        other_space = claims.remember(
            ctx.wide_access,
            agent_id=ctx.agent,
            predicate="envelope",
            value={"v": 2},
            canonical_text="other space",
            subject_entity_id=ctx.entity,
            space_id=ctx.other_space,
            session_id="sess-2",
            evidence=[{"source_type": "observation", "source_id": obs_out, "relation": "supports"}],
            idempotency_key="p02-out",
        )
        selector = ForgetSelector(
            kind=ForgetSelectorKind.SUBJECT_PREDICATE,
            agent_id=ctx.agent,
            subject_entity_id=ctx.entity,
            predicate="envelope",
        )
        # ctx.access holds ONLY space A: fail-closed, nothing is erased.
        with pytest.raises(AccessDeniedError):
            forget.forget(ctx.access, selector, reason="r", idempotency_key="p02-narrow")
        for claim_id in (in_space.claim_id, other_space.claim_id):
            view = claims.get(ctx.wide_access, claim_id)
            assert view is not None and view.claim.status == "active"
        # A caller entitled to both spaces may erase the whole slice.
        result = forget.forget(ctx.wide_access, selector, reason="r", idempotency_key="p02-wide")
        assert result.erased_count == 2


class TestP1_4ClaimEvidenceAdmission:
    def test_cross_agent_claim_evidence_rejected(self, rctx: tuple[_Ctx, dict[str, Any]]) -> None:
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        with ctx.store.write() as tx:
            second_agent = tx.insert_agent("t1", "A1b", actor="test").id
        source = ctx.remember("p04-src")
        other_agent_access = access_for(
            "t1",
            agent_ids=frozenset({second_agent}),
            space_ids=frozenset({ctx.space}),
            consent_entities=frozenset({ctx.entity}),
        )
        observation = ctx.observe("p04-obs")
        with pytest.raises(InvalidRequestError, match="tenant and agent"):
            claims.remember(
                other_agent_access,
                agent_id=second_agent,
                predicate="derived",
                value={"v": 1},
                canonical_text="derived fact",
                subject_entity_id=ctx.entity,
                evidence=[
                    {"source_type": "claim", "source_id": source.claim_id, "relation": "supports"},
                    {
                        "source_type": "observation",
                        "source_id": observation,
                        "relation": "supports",
                    },
                ],
                idempotency_key="p04-cross-agent",
            )

    def test_space_scoped_claim_not_visible_to_space_scoped_citing_claim(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        obs_b = ctx.observe(
            "p04-obs-b", access=ctx.wide_access, space_id=ctx.other_space, session_id="sess-2"
        )
        source = claims.remember(
            ctx.wide_access,
            agent_id=ctx.agent,
            predicate="lives-in-space-b",
            value={"v": 1},
            canonical_text="space B fact",
            subject_entity_id=ctx.entity,
            space_id=ctx.other_space,
            session_id="sess-2",
            evidence=[{"source_type": "observation", "source_id": obs_b, "relation": "supports"}],
            idempotency_key="p04-space-src",
        )
        citing_access = access_for(
            "t1",
            agent_ids=frozenset({ctx.agent}),
            space_ids=frozenset({ctx.space}),
            consent_entities=frozenset({ctx.entity}),
        )
        obs_a = ctx.observe(
            "p04-obs-a", access=ctx.wide_access, space_id=ctx.space, session_id="sess-1"
        )
        with pytest.raises(InvalidRequestError, match="scope envelope"):
            claims.remember(
                citing_access,
                agent_id=ctx.agent,
                predicate="derived2",
                value={"v": 2},
                canonical_text="space A derived",
                subject_entity_id=ctx.entity,
                space_id=ctx.space,
                session_id="sess-1",
                evidence=[
                    {"source_type": "claim", "source_id": source.claim_id, "relation": "supports"},
                    {
                        "source_type": "observation",
                        "source_id": obs_a,
                        "relation": "supports",
                    },
                ],
                idempotency_key="p04-cross-space",
            )

    def test_retracted_claim_is_not_live_evidence(self, rctx: tuple[_Ctx, dict[str, Any]]) -> None:
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        source = ctx.remember("p04-sup", value={"drink": "coffee"})
        correction_observation = ctx.observe("p04-obs-correct")
        claims.correct(
            ctx.access,
            source.claim_id,
            expected_revision=source.revision,
            mode="retract",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": correction_observation,
                    "relation": "corrects",
                }
            ],
            reason="retracted",
            idempotency_key="p04-correct",
        )
        with ctx.store.read() as tx:
            assert tx.claims.get(source.claim_id).status == "retracted"
        observation = ctx.observe("p04-obs3")
        with pytest.raises(InvalidRequestError, match="live claim"):
            claims.remember(
                ctx.access,
                agent_id=ctx.agent,
                predicate="derived3",
                value={"v": 3},
                canonical_text="derived",
                subject_entity_id=ctx.entity,
                evidence=[
                    {"source_type": "claim", "source_id": source.claim_id, "relation": "supports"},
                    {
                        "source_type": "observation",
                        "source_id": observation,
                        "relation": "supports",
                    },
                ],
                idempotency_key="p04-superseded-src",
            )

    def test_subject_private_claim_evidence_requires_consent(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        claims: ClaimService = services["claims"]
        secret = ctx.remember("p04-private-src", privacy_labels=[f"entity:{ctx.entity}:private"])
        no_consent = access_for(
            "t1", agent_ids=frozenset({ctx.agent}), space_ids=frozenset({ctx.space})
        )
        observation = ctx.observe("p04-obs4")
        with pytest.raises(AccessDeniedError):
            claims.remember(
                no_consent,
                agent_id=ctx.agent,
                predicate="derived4",
                value={"v": 4},
                canonical_text="derived",
                subject_entity_id=ctx.entity,
                evidence=[
                    {"source_type": "claim", "source_id": secret.claim_id, "relation": "supports"},
                    {
                        "source_type": "observation",
                        "source_id": observation,
                        "relation": "supports",
                    },
                ],
                idempotency_key="p04-private",
            )


class TestP1_5RelationEvidenceNormalization:
    def test_forgetting_the_only_evidence_retracts_the_relation(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        relations: RelationService = services["relations"]
        forget: ForgetService = services["forget"]
        observation = ctx.observe("p05-obs")
        created = relations.create(
            ctx.access,
            agent_id=ctx.agent,
            source_entity_id=ctx.entity,
            relation_type="works_with",
            target_entity_id=ctx.other_entity,
            evidence=[
                {"source_type": "observation", "source_id": observation, "relation": "supports"}
            ],
            idempotency_key="p05-rel",
        )
        with ctx.store.read() as tx:
            assert tx.relations.valid_evidence_count(created.relation_id) == 1
        forget.forget(
            ctx.access,
            _resource("observation", observation),
            reason="source erasure",
            idempotency_key="p05-fg",
        )
        with ctx.store.read() as tx:
            relation = tx.relations.get(created.relation_id)
            assert relation.status == "retracted"
            assert relation.evidence_count == 0
            assert tx.relations.valid_evidence_count(created.relation_id) == 0
            rows = (
                tx.raw()
                .execute(
                    "SELECT invalidated_us FROM relation_evidence WHERE relation_id = ?",
                    (created.relation_id,),
                )
                .fetchall()
            )
        assert rows and all(row[0] is not None for row in rows)
        # The immutable history: a retraction revision exists and the pointer
        # moved onto it (never an in-place edit).
        with ctx.store.read() as tx:
            history = tx.relations.history(created.relation_id)
        assert [revision.revision for revision in history] == [2, 1]
        assert history[0].status == "retracted"

    def test_relation_with_two_evidence_rows_survives_one_loss(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        relations: RelationService = services["relations"]
        forget: ForgetService = services["forget"]
        first = ctx.observe("p05-obs-a")
        second = ctx.observe("p05-obs-b")
        created = relations.create(
            ctx.access,
            agent_id=ctx.agent,
            source_entity_id=ctx.entity,
            relation_type="knows",
            target_entity_id=ctx.other_entity,
            evidence=[
                {"source_type": "observation", "source_id": first, "relation": "supports"},
                {"source_type": "observation", "source_id": second, "relation": "supports"},
            ],
            idempotency_key="p05-rel2",
        )
        forget.forget(
            ctx.access,
            _resource("observation", first),
            reason="partial erasure",
            idempotency_key="p05-fg2",
        )
        with ctx.store.read() as tx:
            relation = tx.relations.get(created.relation_id)
        assert relation.status == "active"
        assert relation.evidence_count == 1

    def test_dedup_replay_attaches_new_evidence_rows(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        relations: RelationService = services["relations"]
        first = ctx.observe("p05-obs-c")
        second = ctx.observe("p05-obs-d")
        created = relations.create(
            ctx.access,
            agent_id=ctx.agent,
            source_entity_id=ctx.entity,
            relation_type="mentions",
            target_entity_id=ctx.other_entity,
            evidence=[{"source_type": "observation", "source_id": first, "relation": "supports"}],
            idempotency_key="p05-rel3",
        )
        again = relations.create(
            ctx.access,
            agent_id=ctx.agent,
            source_entity_id=ctx.entity,
            relation_type="mentions",
            target_entity_id=ctx.other_entity,
            evidence=[
                {"source_type": "observation", "source_id": first, "relation": "supports"},
                {"source_type": "observation", "source_id": second, "relation": "supports"},
            ],
            idempotency_key="p05-rel3-dedup",
        )
        assert again.deduped and again.relation_id == created.relation_id
        with ctx.store.read() as tx:
            assert tx.relations.valid_evidence_count(created.relation_id) == 2
            assert tx.relations.get(created.relation_id).evidence_count == 2


class TestP1_6ArtifactScopeDedup:
    def test_same_content_in_two_scopes_is_two_artifacts(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        artifacts: ArtifactService = services["artifacts"]
        payload = b"p06 identical bytes"
        agent_level = artifacts.ingest_inline(
            ctx.access,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            idempotency_key="p06-agent",
        )
        other_space_access = access_for(
            "t1", agent_ids=frozenset({ctx.agent}), space_ids=frozenset({ctx.other_space})
        )
        # Pre-fix: this dedup-hit the agent-level artifact (different scope)
        # and then failed authorization because the caller cannot read it.
        space_b = artifacts.ingest_inline(
            other_space_access,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            space_id=ctx.other_space,
            idempotency_key="p06-space-b",
        )
        assert space_b.artifact_id != agent_level.artifact_id
        assert not space_b.deduped
        with ctx.store.read() as tx:
            stored = tx.artifacts.get(space_b.artifact_id)
        assert stored.space_id == ctx.other_space
        # Re-ingesting the SAME scope dedups onto that scope's own artifact.
        again = artifacts.ingest_inline(
            other_space_access,
            agent_id=ctx.agent,
            content=payload,
            media_type="text/plain",
            space_id=ctx.other_space,
            idempotency_key="p06-space-b-2",
        )
        assert again.deduped and again.artifact_id == space_b.artifact_id


class TestP1_7ForgetIdempotentReplay:
    def test_resource_forget_replays_its_own_success(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        forget: ForgetService = services["forget"]
        claim = ctx.remember("p07")
        first = forget.forget(
            ctx.access,
            _resource("claim", claim.claim_id),
            reason="r",
            idempotency_key="p07-key",
        )
        assert first.erased_count == 1
        # Pre-fix: the tombstone pre-check raised NotFound before the cache.
        replay = forget.forget(
            ctx.access,
            _resource("claim", claim.claim_id),
            reason="r",
            idempotency_key="p07-key",
        )
        assert replay.replayed
        assert replay.request_id == first.request_id
        assert replay.erased_count == first.erased_count
        # A DIFFERENT key on the same resource is a new request and the
        # tombstone check answers it fail-closed — at ANY instant, including
        # the same microsecond (round 3 keeps distinct-key requests distinct).
        rctx[1]["clock"].advance(1)
        with pytest.raises(NotFoundError):
            forget.forget(
                ctx.access,
                _resource("claim", claim.claim_id),
                reason="r",
                idempotency_key="p07-other-key",
            )

    def test_artifact_forget_keeps_row_replays_and_reads_none(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        artifacts: ArtifactService = services["artifacts"]
        forget: ForgetService = services["forget"]
        blob = artifacts.ingest_local_blob(
            ctx.access,
            agent_id=ctx.agent,
            content=b"p07 blob bytes",
            media_type="application/octet-stream",
            idempotency_key="p07-blob",
        )
        first = forget.forget(
            ctx.access,
            _resource("artifact", blob.artifact_id),
            reason="erasure",
            idempotency_key="p07-art-key",
        )
        assert first.erased_count == 1
        replay = forget.forget(
            ctx.access,
            _resource("artifact", blob.artifact_id),
            reason="erasure",
            idempotency_key="p07-art-key",
        )
        assert replay.replayed and replay.request_id == first.request_id
        # The row survives (sealed) for scope identity and audit linkage, but
        # every read surface returns nothing and the bytes are gone.
        assert artifacts.read(ctx.access, blob.artifact_id) is None
        assert artifacts.get(ctx.access, blob.artifact_id) is None
        with ctx.store.read() as tx:
            row = (
                tx.raw()
                .execute("SELECT status, locator FROM artifacts WHERE id = ?", (blob.artifact_id,))
                .fetchone()
            )
        assert row is not None and row[0] == "tombstoned"
        blob_path = ctx.store.artifact_root / str(row[1])
        assert not blob_path.exists()

    def test_inline_artifact_forget_scrubs_content_bytes(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        artifacts: ArtifactService = services["artifacts"]
        forget: ForgetService = services["forget"]
        inline = artifacts.ingest_inline(
            ctx.access,
            agent_id=ctx.agent,
            content=b"p07 inline bytes",
            media_type="text/plain",
            idempotency_key="p07-inline",
        )
        forget.forget(
            ctx.access,
            _resource("artifact", inline.artifact_id),
            reason="erasure",
            idempotency_key="p07-inline-fg",
        )
        with ctx.store.read() as tx:
            row = (
                tx.raw()
                .execute(
                    "SELECT status, content FROM artifacts WHERE id = ?", (inline.artifact_id,)
                )
                .fetchone()
            )
        assert row is not None and row[0] == "tombstoned" and row[1] is None


class TestP1_8EmptyOutcomeLedger:
    def test_subject_predicate_with_no_targets_writes_a_ledger_row(
        self, rctx: tuple[_Ctx, dict[str, Any]]
    ) -> None:
        ctx, services = rctx
        forget: ForgetService = services["forget"]
        # Pre-fix: [seq_before+1, seq_before] violated the CHECK and the whole
        # request died with ConflictError; a zero-target outcome is legitimate
        # and must be auditable.
        result = forget.forget(
            ctx.access,
            ForgetSelector(
                kind=ForgetSelectorKind.SUBJECT_PREDICATE,
                agent_id=ctx.agent,
                subject_entity_id=ctx.other_entity,
                predicate="never-seen",
            ),
            reason="nothing to erase",
            idempotency_key="p08-empty",
        )
        assert result.target_count == 0 and result.erased_count == 0
        assert result.tombstone_seq_lo == result.tombstone_seq_hi
        with ctx.store.read() as tx:
            rows = (
                tx.raw()
                .execute(
                    "SELECT target_count, erased_count, tombstone_seq_lo, tombstone_seq_hi "
                    "FROM forget_requests WHERE id = ?",
                    (result.request_id,),
                )
                .fetchall()
            )
        assert rows and rows[0][0] == 0 and rows[0][2] <= rows[0][3]

    def test_all_held_session_forget_reports_skips(self, rctx: tuple[_Ctx, dict[str, Any]]) -> None:
        ctx, services = rctx
        forget: ForgetService = services["forget"]
        retention: RetentionService = services["retention"]
        claims: ClaimService = services["claims"]
        held_observation = ctx.observe(
            "p08-held-obs", access=ctx.wide_access, space_id=ctx.space, session_id="sess-1"
        )
        claims.remember(
            ctx.wide_access,
            agent_id=ctx.agent,
            predicate="held",
            value={"v": 1},
            canonical_text="held claim",
            subject_entity_id=ctx.entity,
            space_id=ctx.space,
            session_id="sess-1",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": held_observation,
                    "relation": "supports",
                }
            ],
            idempotency_key="p08-held",
        )
        retention.create_legal_hold(ctx.admin_access, space_id=ctx.space, reason="hold")
        result = forget.forget(
            ctx.admin_access,
            ForgetSelector(
                kind=ForgetSelectorKind.SESSION, space_id=ctx.space, session_id="sess-1"
            ),
            reason="session erasure under hold",
        )
        assert result.erased_count == 0 and result.held_skipped == 2
        assert result.tombstone_seq_lo == result.tombstone_seq_hi
