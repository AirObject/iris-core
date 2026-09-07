"""Phase 5 Forget + Retention + Legal Hold integration tests.

Tombstone selectors, protected resources, legal holds, the evidence-loss
cascade, the deletion ledger, watermark monotonicity, and the audit
leakage discipline (references/hashes/counts only, never content).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.domain.errors import AccessDeniedError, NotFoundError
from iris_memory_core.domain.retention import (
    ForgetSelector,
    ForgetSelectorKind,
    LegalHoldActiveError,
    ProtectedResourceError,
    RetentionAction,
)
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.memory.test_memory_claims import _Ctx


@pytest.fixture
def fctx(
    clocked_store: Store,
    mutable_clock: MutableClock,
    phase5_claims: ClaimService,
    phase5_forget: ForgetService,
    phase5_retention: RetentionService,
) -> dict[str, Any]:
    ctx = _Ctx(clocked_store, mutable_clock, phase5_claims)
    ctx.remember("base-claim", predicate="about")
    return {
        "ctx": ctx,
        "store": clocked_store,
        "clock": mutable_clock,
        "claims": phase5_claims,
        "forget": phase5_forget,
        "retention": phase5_retention,
    }


def _resource(resource_type: str, resource_id: str) -> ForgetSelector:
    return ForgetSelector(
        kind=ForgetSelectorKind.RESOURCE,
        resource_type=resource_type,
        resource_id=resource_id,
    )


class TestResourceSelector:
    def test_forget_claim_erases_and_hides_everywhere(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        claim = ctx.remember("f1")
        result = forget.forget(
            ctx.access,
            _resource("claim", claim.claim_id),
            reason="user erasure",
            idempotency_key="fg-f1",
        )
        assert result.erased_count == 1
        assert result.tombstone_seq_hi >= result.tombstone_seq_lo >= 1
        with pytest.raises(NotFoundError):
            ctx.claims.get(ctx.access, claim.claim_id)
        remaining = ctx.claims.search(ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity)
        assert all(view.claim.id != claim.claim_id for view in remaining)
        # Content is scrubbed even if the row is read directly.
        with fctx["store"].read() as tx:
            rows = (
                tx.raw()
                .execute(
                    "SELECT canonical_text, value_json FROM claim_revisions WHERE claim_id = ?",
                    (claim.claim_id,),
                )
                .fetchall()
            )
        assert all(row["canonical_text"] == "<erased>" for row in rows)

    def test_remember_after_forget_creates_a_new_row_not_a_resurrection(
        self, fctx: dict[str, Any]
    ) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        first = ctx.remember("f2")
        forget.forget(
            ctx.access, _resource("claim", first.claim_id), reason="r", idempotency_key="fg-f2"
        )
        again = ctx.remember("f2-reborn")
        assert again.claim_id != first.claim_id
        view = ctx.claims.get(ctx.access, again.claim_id)
        assert view is not None
        assert view is not None and view.claim.status == "active"

    def test_evidence_loss_cascade_retracts_active_claims(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        observation = ctx.observe("f3-obs")
        claim = ctx.claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="said",
            value="hello",
            canonical_text="Bob said hello",
            subject_entity_id=ctx.entity,
            evidence=[
                {"source_type": "observation", "source_id": observation, "relation": "supports"}
            ],
            idempotency_key="f3-claim",
        )
        forget.forget(
            ctx.access, _resource("observation", observation), reason="r", idempotency_key="fg-f3"
        )
        view = ctx.claims.get(ctx.access, claim.claim_id)
        assert view is not None
        assert view.claim.status == "retracted"
        assert view.claim.evidence_count == 0
        remaining = ctx.claims.search(ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity)
        assert all(item.claim.id != claim.claim_id for item in remaining)

    def test_tombstone_watermark_monotone(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        with fctx["store"].read() as tx:
            before = tx.tombstone_watermark()
        a = ctx.remember("f4a", predicate="likes-a")
        b = ctx.remember("f4b", predicate="likes-b")
        forget.forget(
            ctx.access, _resource("claim", a.claim_id), reason="r", idempotency_key="fg-f4a"
        )
        forget.forget(
            ctx.access, _resource("claim", b.claim_id), reason="r", idempotency_key="fg-f4b"
        )
        with fctx["store"].read() as tx:
            after = tx.tombstone_watermark()
        assert after >= before + 2

    def test_audit_and_ledger_never_copy_content(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        claim = ctx.remember("f5", canonical_text="SECRET CANARY TEXT f5")
        forget.forget(
            ctx.access,
            _resource("claim", claim.claim_id),
            reason="canary",
            idempotency_key="fg-f5",
        )
        with fctx["store"].read() as tx:
            audits = (
                tx.raw()
                .execute(
                    "SELECT details FROM audit_events "
                    "WHERE resource_type = 'claim' AND resource_id = ?",
                    (claim.claim_id,),
                )
                .fetchall()
            )
            ledger = (
                tx.raw()
                .execute("SELECT selector_json, reason_code FROM forget_requests")
                .fetchall()
            )
            # Outbox payloads for the invalidation stream carry ResourceRefs
            # and watermarks only — never content snapshots (ADR-0013 §7).
            payloads = (
                tx.raw()
                .execute("SELECT payload FROM outbox_jobs WHERE job_kind = 'memory.invalidated'")
                .fetchall()
            )
        assert payloads
        for row in audits:
            assert "SECRET CANARY" not in row["details"]
        for row in ledger:
            assert "SECRET CANARY" not in row["selector_json"]
        for row in payloads:
            assert "SECRET CANARY" not in row["payload"]
            decoded = json.loads(row["payload"])
            assert set(decoded) <= {
                "version",
                "resources",
                "tombstone_watermark",
                "erase_content",
            }
            for item in decoded["resources"]:
                assert set(item) == {"resource_type", "resource_id"}


class TestProtectedAndHolds:
    def test_pinned_note_is_protected(
        self, fctx: dict[str, Any], phase5_notes: NoteService
    ) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        note = phase5_notes.create(
            ctx.access,
            agent_id=ctx.agent,
            kind="important",
            title="keep me",
            body="",
            idempotency_key="pn-1",
        )
        phase5_notes.transition(
            ctx.access,
            note.note_id,
            "pin",
            expected_revision=note.revision,
            reason="pinned",
            idempotency_key="pn-2",
        )
        with pytest.raises(ProtectedResourceError):
            forget.forget(
                ctx.access, _resource("note", note.note_id), reason="r", idempotency_key="fg-pn"
            )

    def test_unfulfilled_promise_note_is_protected(
        self, fctx: dict[str, Any], phase5_notes: NoteService
    ) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        note = phase5_notes.create(
            ctx.access,
            agent_id=ctx.agent,
            kind="promise",
            title="send the report",
            body="",
            idempotency_key="pn-3",
        )
        with pytest.raises(ProtectedResourceError):
            forget.forget(
                ctx.access, _resource("note", note.note_id), reason="r", idempotency_key="fg-pn3"
            )

    def test_security_claim_is_protected(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        claim = ctx.claims.remember(
            ctx.admin_access,
            agent_id=ctx.agent,
            predicate="likes",
            value={"drink": "tea"},
            canonical_text="Bob likes tea",
            subject_entity_id=ctx.entity,
            privacy_labels=["restricted"],
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": ctx.observe("obs-sec1"),
                    "relation": "supports",
                }
            ],
            idempotency_key="idem-sec1",
        )
        with pytest.raises(ProtectedResourceError):
            forget.forget(
                ctx.access, _resource("claim", claim.claim_id), reason="r", idempotency_key="fg-sec"
            )

    def test_legal_hold_blocks_resource_forget_until_released(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        retention: RetentionService = fctx["retention"]
        claim = ctx.remember("hold1", space_id=ctx.space, session_id="sess-1")
        retention.create_legal_hold(ctx.admin_access, space_id=ctx.space, reason="litigation hold")
        with pytest.raises(LegalHoldActiveError):
            forget.forget(
                ctx.access,
                _resource("claim", claim.claim_id),
                reason="r",
                idempotency_key="fg-hold",
            )
        holds = retention.active_holds(ctx.admin_access)
        assert len(holds) == 1
        released = retention.release_legal_hold(ctx.admin_access, holds[0].id, reason="case closed")
        assert released.released_us is not None
        result = forget.forget(
            ctx.access, _resource("claim", claim.claim_id), reason="r", idempotency_key="fg-hold2"
        )
        assert result.erased_count == 1

    def test_bulk_selector_skips_and_counts_held(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        retention: RetentionService = fctx["retention"]
        held = ctx.remember("bulk-h", space_id=ctx.space, session_id="sess-1")
        free = ctx.remember("bulk-f")
        retention.create_legal_hold(ctx.admin_access, space_id=ctx.space, reason="hold")
        result = forget.forget(
            ctx.admin_access,
            ForgetSelector(
                kind=ForgetSelectorKind.SUBJECT_PREDICATE,
                agent_id=ctx.agent,
                subject_entity_id=ctx.entity,
            ),
            reason="bulk",
            idempotency_key="fg-bulk",
        )
        assert result.held_skipped == 1
        assert result.erased_count >= 1
        # held claim survives; the free one is gone
        assert ctx.claims.get(ctx.access, held.claim_id) is not None
        with pytest.raises(NotFoundError):
            ctx.claims.get(ctx.access, free.claim_id)

    def test_admin_selectors_require_admin(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        with pytest.raises(AccessDeniedError):
            forget.forget(
                ctx.access,
                ForgetSelector(kind=ForgetSelectorKind.SPACE, space_id=ctx.space),
                reason="r",
            )


class TestSelectors:
    def test_session_selector_forgets_all_session_memory(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        session_claim = ctx.remember("ss1", space_id=ctx.space, session_id="sess-1")
        agent_claim = ctx.remember("ss2")
        result = forget.forget(
            ctx.admin_access,
            ForgetSelector(
                kind=ForgetSelectorKind.SESSION, space_id=ctx.space, session_id="sess-1"
            ),
            reason="session purge",
        )
        assert result.erased_count >= 1
        with pytest.raises(NotFoundError):
            ctx.claims.get(ctx.admin_access, session_claim.claim_id)
        # Agent-level memory survives a session purge.
        assert ctx.claims.get(ctx.access, agent_claim.claim_id) is not None

    def test_data_request_removes_subject_across_agents_data(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        claims = [ctx.remember(f"dr{i}", canonical_text=f"secret {i}") for i in range(3)]
        result = forget.forget(
            ctx.admin_access,
            ForgetSelector(kind=ForgetSelectorKind.DATA_REQUEST, subject_entity_id=ctx.entity),
            reason="gdpr erasure",
        )
        assert result.erased_count >= 3
        for claim in claims:
            with pytest.raises(NotFoundError):
                ctx.claims.get(ctx.admin_access, claim.claim_id)
        ledger = forget.export_deletion_ledger(ctx.admin_access)
        assert len(ledger) >= 1
        assert all("secret" not in request.selector_json for request in ledger)

    def test_deletion_ledger_export_requires_admin(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        forget: ForgetService = fctx["forget"]
        with pytest.raises(AccessDeniedError):
            forget.export_deletion_ledger(ctx.access)


class TestRetention:
    def test_decay_lowers_accessibility_but_keeps_content(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        retention: RetentionService = fctx["retention"]
        claim = ctx.remember("decay1", accessibility=0.9)
        retention.set_policy(
            ctx.admin_access,
            resource_type="claim",
            action=RetentionAction.DECAY.value,
            threshold_days=1,
            reason="policy",
        )
        # Age the claim past the threshold.
        with fctx["store"].write() as tx:
            tx.raw().execute(
                "UPDATE claims SET updated_us = updated_us - ? WHERE id = ?",
                (2 * 86_400_000_000, claim.claim_id),
            )
        with fctx["store"].write() as tx:
            report = retention.retention_sweep(tx, tenant_id="t1")
        assert report.decayed == 1
        view = ctx.claims.get(ctx.access, claim.claim_id)
        assert view is not None
        assert view.claim.accessibility < 0.9
        assert view.revision.canonical_text == "Bob likes tea"
        assert view.claim.status == "active"

    def test_archive_removes_from_current_reads(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        retention: RetentionService = fctx["retention"]
        claim = ctx.remember("arch1")
        retention.set_policy(
            ctx.admin_access,
            resource_type="claim",
            action=RetentionAction.ARCHIVE.value,
            threshold_days=1,
            reason="policy",
        )
        with fctx["store"].write() as tx:
            tx.raw().execute(
                "UPDATE claims SET updated_us = updated_us - ? WHERE id = ?",
                (2 * 86_400_000_000, claim.claim_id),
            )
        with fctx["store"].write() as tx:
            report = retention.retention_sweep(tx, tenant_id="t1")
        assert report.archived == 1
        view = ctx.claims.get(ctx.access, claim.claim_id)
        assert view is not None
        assert view.claim.status == "archived"
        remaining = ctx.claims.search(ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity)
        assert all(item.claim.id != claim.claim_id for item in remaining)

    def test_retention_delete_uses_the_forget_machine(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        retention: RetentionService = fctx["retention"]
        claim = ctx.remember("del1", canonical_text="RETENTION DELETE CANARY")
        retention.set_policy(
            ctx.admin_access,
            resource_type="claim",
            action=RetentionAction.DELETE.value,
            threshold_days=1,
            privacy_label="agent:a1",
            reason="policy",
        )
        # The policy privacy filter requires the claim carry the label.
        with fctx["store"].write() as tx:
            tx.raw().execute(
                "UPDATE claims SET updated_us = updated_us - ? WHERE id = ?",
                (2 * 86_400_000_000, claim.claim_id),
            )
            tx.raw().execute(
                "UPDATE claim_revisions SET privacy_labels = ? WHERE claim_id = ? AND revision = "
                "(SELECT current_revision FROM claims WHERE id = ?)",
                (json.dumps(["agent:a1"]), claim.claim_id, claim.claim_id),
            )
        with fctx["store"].write() as tx:
            report = retention.retention_sweep(tx, tenant_id="t1")
        assert report.deleted == 1
        with pytest.raises(NotFoundError):
            ctx.claims.get(ctx.access, claim.claim_id)
        with fctx["store"].read() as tx:
            ledger = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM forget_requests WHERE reason_code = 'retention_policy'"
                )
                .fetchone()[0]
            )
        assert ledger >= 1

    def test_retention_delete_requires_privacy_scoped_policy(self, fctx: dict[str, Any]) -> None:
        """DB constraint: a delete policy must name a privacy label — bulk
        unscoped deletion is not expressible (migration 0006)."""
        from iris_memory_core.domain.errors import ConflictError

        with pytest.raises(ConflictError):
            fctx["retention"].set_policy(
                fctx["ctx"].admin_access,
                resource_type="claim",
                action="delete",
                threshold_days=1,
                reason="policy",
            )

    def test_retention_never_deletes_protected_notes(
        self, fctx: dict[str, Any], phase5_notes: NoteService
    ) -> None:
        ctx: _Ctx = fctx["ctx"]
        retention: RetentionService = fctx["retention"]
        agent_label = f"agent:{ctx.agent}"
        pinned = phase5_notes.create(
            ctx.access,
            agent_id=ctx.agent,
            kind="important",
            title="pinned",
            body="",
            privacy_labels=[agent_label],
            idempotency_key="rn-1",
        )
        phase5_notes.transition(
            ctx.access,
            pinned.note_id,
            "pin",
            expected_revision=pinned.revision,
            reason="pin",
            idempotency_key="rn-2",
        )
        promise = phase5_notes.create(
            ctx.access,
            agent_id=ctx.agent,
            kind="promise",
            title="promise",
            body="",
            privacy_labels=[agent_label],
            idempotency_key="rn-3",
        )
        retention.set_policy(
            ctx.admin_access,
            resource_type="note",
            action="delete",
            threshold_days=1,
            privacy_label=agent_label,
            reason="policy",
        )
        with fctx["store"].write() as tx:
            tx.raw().execute(
                "UPDATE notes SET updated_us = updated_us - ? WHERE id IN (?, ?)",
                (2 * 86_400_000_000, pinned.note_id, promise.note_id),
            )
            report = retention.retention_sweep(tx, tenant_id="t1")
        assert report.protected_skipped >= 2
        assert report.deleted == 0

    def test_legal_hold_blocks_retention_archive(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        retention: RetentionService = fctx["retention"]
        claim = ctx.remember("hold-ret", space_id=ctx.space, session_id="sess-1")
        retention.create_legal_hold(ctx.admin_access, space_id=ctx.space, reason="hold")
        retention.set_policy(
            ctx.admin_access,
            resource_type="claim",
            action="archive",
            threshold_days=1,
            reason="policy",
        )
        with fctx["store"].write() as tx:
            tx.raw().execute(
                "UPDATE claims SET updated_us = updated_us - ? WHERE id = ?",
                (2 * 86_400_000_000, claim.claim_id),
            )
            report = retention.retention_sweep(tx, tenant_id="t1")
        assert report.held_skipped == 1
        view = ctx.claims.get(ctx.access, claim.claim_id)
        assert view is not None
        assert view.claim.status == "active"

    def test_policy_versions_advance_on_updates(self, fctx: dict[str, Any]) -> None:
        ctx: _Ctx = fctx["ctx"]
        retention: RetentionService = fctx["retention"]
        first = retention.set_policy(
            ctx.admin_access, resource_type="claim", action="decay", threshold_days=10, reason="r"
        )
        second = retention.set_policy(
            ctx.admin_access, resource_type="claim", action="decay", threshold_days=5, reason="r"
        )
        assert second.policy_version == first.policy_version + 1
        assert second.threshold_days == 5
