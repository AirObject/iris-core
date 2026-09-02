"""Phase 4 Note integration tests (§10, P4-NOTE-01).

Lifecycle, review sweep semantics (snooze wake, duplicate ASSOCIATION —
never deletion), the task promotion materialization, the claim/episode
seam, retention guards and idempotency/CAS discipline.
"""

from __future__ import annotations

from typing import Any

import pytest

from iris_memory_core.application.notes import NoteService
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    DomainError,
    InvalidRequestError,
    InvalidTransitionError,
    RevisionMismatchError,
)
from iris_memory_core.domain.note import NoteStatus
from tests.conftest import access_for


def _ctx(
    clocked_store: Any,
    clocked_tenant_id: str,
    phase2_agent: str,
    phase4_notes: NoteService,
) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space = tx.insert_space(clocked_tenant_id, "chat_group")
    access = access_for(
        clocked_tenant_id, agent_ids=frozenset({phase2_agent}), space_ids=frozenset({space.id})
    )
    return {
        "store": clocked_store,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "space": space.id,
        "access": access,
        "notes": phase4_notes,
    }


@pytest.fixture
def note_ctx(
    clocked_store: Any,
    clocked_tenant_id: str,
    phase2_agent: str,
    phase4_notes: NoteService,
) -> dict[str, Any]:
    return _ctx(clocked_store, clocked_tenant_id, phase2_agent, phase4_notes)


def _create(ctx: dict[str, Any], key: str, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "agent_id": ctx["agent"],
        "kind": "idea",
        "title": f"note {key}",
        "body": f"body {key}",
    }
    payload.update(overrides)
    return ctx["notes"].create(ctx["access"], idempotency_key=f"note-{key}", **payload)


class TestNoteLifecycle:
    def test_create_lists_updates_with_revisions(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        created = _create(ctx, "a", importance=0.9)
        assert created.revision == 1 and not created.replayed
        revision = ctx["notes"].update(
            ctx["access"],
            created.note_id,
            expected_revision=1,
            body="edited body",
            idempotency_key="note-a-edit",
        )
        assert revision.revision == 2
        assert revision.body == "edited body"
        listed = ctx["notes"].list_notes(ctx["access"], agent_id=ctx["agent"])
        assert [item[0].id for item in listed] == [created.note_id]
        history = ctx["notes"].history(ctx["access"], created.note_id)
        assert [item.revision for item in history] == [2, 1]

    def test_pin_snooze_archive_reopen_transitions(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        note = _create(ctx, "b")
        clock = ctx["store"].clock
        pinned = ctx["notes"].transition(
            ctx["access"],
            note.note_id,
            "pin",
            expected_revision=1,
            reason="keep",
            idempotency_key="t1",
        )
        assert pinned.status == NoteStatus.PINNED.value
        back = ctx["notes"].transition(
            ctx["access"],
            note.note_id,
            "reopen",
            expected_revision=2,
            reason="back",
            idempotency_key="t2",
        )
        assert back.status == NoteStatus.INBOX.value
        snoozed = ctx["notes"].transition(
            ctx["access"],
            note.note_id,
            "snooze",
            expected_revision=3,
            reason="later",
            snooze_until_us=clock.now_us() + 1_000_000,
            idempotency_key="t3",
        )
        assert snoozed.status == NoteStatus.SNOOZED.value
        # A snoozed note is invisible until its wake time arrives.
        assert ctx["notes"].list_notes(ctx["access"], agent_id=ctx["agent"]) == []
        clock.advance(2_000_000)
        woken = [item for item in ctx["notes"].list_notes(ctx["access"], agent_id=ctx["agent"])]
        assert [item[0].id for item in woken] == [note.note_id]
        # The wake itself is a review-sweep transition (§10.2: snoozed → inbox).
        with ctx["store"].write() as tx:
            ctx["notes"].review_sweep(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        with ctx["store"].read() as tx:
            woken_row = tx.notes.get(note.note_id)
        assert woken_row.status == NoteStatus.INBOX.value
        archived = ctx["notes"].transition(
            ctx["access"],
            note.note_id,
            "archive",
            expected_revision=woken_row.current_revision,
            reason="done",
            idempotency_key="t4",
        )
        assert archived.status == NoteStatus.ARCHIVED.value
        reopened = ctx["notes"].transition(
            ctx["access"],
            note.note_id,
            "reopen",
            expected_revision=archived.revision,
            reason="again",
            idempotency_key="t5",
        )
        assert reopened.status == NoteStatus.INBOX.value

    def test_illegal_transitions_are_stable_errors(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        note = _create(ctx, "c")
        # Legal state move, missing argument: invalid_request (state check first,
        # argument check second — same ordering discipline as Focus).
        with pytest.raises(InvalidRequestError):
            ctx["notes"].transition(
                ctx["access"],
                note.note_id,
                "promote",
                expected_revision=1,
                reason="x",
                idempotency_key="bad1",
            )
        archived = ctx["notes"].transition(
            ctx["access"],
            note.note_id,
            "archive",
            expected_revision=1,
            reason="x",
            idempotency_key="bad2",
        )
        assert archived.status == NoteStatus.ARCHIVED.value
        with pytest.raises(InvalidTransitionError) as captured:
            ctx["notes"].transition(
                ctx["access"],
                note.note_id,
                "snooze",
                expected_revision=2,
                reason="x",
                snooze_until_us=ctx["store"].clock.now_us() + 1,
                idempotency_key="bad3",
            )
        assert captured.value.code == "invalid_state_transition"
        with pytest.raises(InvalidTransitionError):
            ctx["notes"].transition(
                ctx["access"],
                note.note_id,
                "snooze",
                expected_revision=2,
                reason="x",
                snooze_until_us=ctx["store"].clock.now_us() + 1,
                idempotency_key="bad4",
            )

    def test_expected_revision_cas_and_idempotent_replay(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        note = _create(ctx, "d")
        with pytest.raises(RevisionMismatchError):
            ctx["notes"].update(
                ctx["access"],
                note.note_id,
                expected_revision=5,
                body="x",
                idempotency_key="cas1",
            )
        first = ctx["notes"].update(
            ctx["access"],
            note.note_id,
            expected_revision=1,
            body="one",
            idempotency_key="replay-key",
        )
        replay = ctx["notes"].update(
            ctx["access"],
            note.note_id,
            expected_revision=1,
            body="one",
            idempotency_key="replay-key",
        )
        assert replay.revision == first.revision == 2

    def test_create_requires_idempotency_key(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        with pytest.raises(InvalidRequestError):
            ctx["notes"].create(ctx["access"], agent_id=ctx["agent"], kind="idea", title="no key")


class TestNotePromotion:
    def test_promote_to_task_materializes_proposed_task(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        note = _create(ctx, "p1", kind="follow_up")
        revision = ctx["notes"].transition(
            ctx["access"],
            note.note_id,
            "promote",
            expected_revision=1,
            reason="go",
            promotion_target_type="task",
            idempotency_key="promo1",
        )
        assert revision.status == NoteStatus.PROMOTED.value
        assert revision.promotion_target_type == "task"
        assert revision.promotion_target_id is not None
        with ctx["store"].read() as tx:
            task = tx.tasks.get_task(revision.promotion_target_id)
            assert task.status == "proposed"  # §11.5: promotion proposes, never activates
            links = tx.links_for_source(ctx["tenant"], "note", note.note_id, target_type="task")
            assert [link.target_id for link in links] == [task.id]

    def test_promote_to_claim_and_episode_materialize(self, note_ctx: dict[str, Any]) -> None:
        """Phase 5 closes the promotion seam (ADR-0013 §6): claim/episode
        promotions are REAL canonical objects with the note revision as
        evidence, and the note backfills the target id atomically."""
        ctx = note_ctx
        note = _create(ctx, "p2")
        revision = ctx["notes"].transition(
            ctx["access"],
            note.note_id,
            "promote",
            expected_revision=1,
            reason="seam",
            promotion_target_type="claim",
            idempotency_key="promo2",
        )
        assert revision.promotion_target_type == "claim"
        assert revision.promotion_target_id is not None  # Phase 5 owns the id
        with ctx["store"].read() as tx:
            claim = tx.claims.get(revision.promotion_target_id)
            assert claim.status == "active"
            evidence = tx.claims.evidence_for_claim(claim.id)
            assert any(item.source_type == "note" for item in evidence)
            links = tx.links_for_source(ctx["tenant"], "note", note.note_id, target_type="claim")
        assert any(link.relation == "promoted_to" for link in links)

        episode_note = _create(ctx, "p2b")
        episode_revision = ctx["notes"].transition(
            ctx["access"],
            episode_note.note_id,
            "promote",
            expected_revision=1,
            reason="seam",
            promotion_target_type="episode",
            idempotency_key="promo2b",
        )
        assert episode_revision.promotion_target_id is not None
        with ctx["store"].read() as tx:
            episode = tx.episodes.get(episode_revision.promotion_target_id)
        assert episode.status == "open"

    def test_promote_requires_known_target(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        note = _create(ctx, "p3")
        with pytest.raises(InvalidRequestError):
            ctx["notes"].transition(
                ctx["access"],
                note.note_id,
                "promote",
                expected_revision=1,
                reason="x",
                promotion_target_type="vibe",
                idempotency_key="promo3",
            )


class TestNoteReviewSweep:
    def test_snooze_wake_and_duplicate_association(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        clock = ctx["store"].clock
        first = _create(ctx, "dup1", kind="idea", title="same title", body="same body")
        second = _create(ctx, "dup2", kind="idea", title="same title", body="same body")
        snoozed = ctx["notes"].transition(
            ctx["access"],
            second.note_id,
            "snooze",
            expected_revision=1,
            reason="later",
            snooze_until_us=clock.now_us() + 500_000,
            idempotency_key="rev1",
        )
        assert snoozed.status == NoteStatus.SNOOZED.value
        clock.advance(600_000)
        with ctx["store"].write() as tx:
            report = ctx["notes"].review_sweep(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.woken == 1
        with ctx["store"].read() as tx:
            woken = tx.notes.get(second.note_id)
            assert woken.status == NoteStatus.INBOX.value
            links = tx.links_for_source(ctx["tenant"], "note", second.note_id)
            relations = [link.relation for link in links]
        # Duplicates are ASSOCIATED, never deleted (§10.3 step 2).
        assert "possible_duplicate" in relations
        with ctx["store"].read() as tx:
            both = [tx.notes.get(item.note_id) for item in (first, second)]
        assert all(item.status != NoteStatus.TOMBSTONED.value for item in both)

    def test_actionable_review_creates_one_proposed_task_idempotently(
        self, note_ctx: dict[str, Any]
    ) -> None:
        ctx = note_ctx
        clock = ctx["store"].clock
        _create(
            ctx,
            "promise1",
            kind="promise",
            body="I will send the report",
            review_after_us=clock.now_us() - 1,
        )
        with ctx["store"].write() as tx:
            first = ctx["notes"].review_sweep(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert first.promoted_to_task == 1
        with ctx["store"].write() as tx:
            second = ctx["notes"].review_sweep(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        # Replays never promote twice: the note is already promoted.
        assert second.promoted_to_task == 0

    def test_review_extends_still_important_notes(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        clock = ctx["store"].clock
        note = _create(ctx, "keep1", kind="question", review_after_us=clock.now_us() - 1)
        before = ctx["store"].clock.now_us()
        with ctx["store"].write() as tx:
            report = ctx["notes"].review_sweep(
                tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"], now_us=before
            )
        assert report.reviews_extended == 1
        with ctx["store"].read() as tx:
            refreshed = tx.notes.get(note.note_id)
        assert refreshed.status == NoteStatus.INBOX.value
        assert refreshed.review_after_us is not None and refreshed.review_after_us > before

    def test_pinned_notes_are_never_deleted_by_review(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        _create(ctx, "pin1", kind="important")
        note = _create(ctx, "pin2", kind="promise")
        ctx["notes"].transition(
            ctx["access"],
            note.note_id,
            "pin",
            expected_revision=1,
            reason="keep",
            idempotency_key="pinme",
        )
        with ctx["store"].write() as tx:
            ctx["notes"].review_sweep(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        with ctx["store"].read() as tx:
            survivors = tx.notes.list_notes(
                ctx["tenant"], ctx["agent"], statuses=("inbox", "pinned", "snoozed"), limit=10
            )
        assert len(survivors) == 2  # retention guard: nothing auto-deleted


class TestNoteSecurity:
    def test_cross_agent_and_tenant_denied(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        note = _create(ctx, "sec1")
        stranger = access_for("tenant-b", agent_ids=frozenset({"other"}))
        with pytest.raises(AccessDeniedError):
            ctx["notes"].get(stranger, note.note_id)
        with pytest.raises(DomainError):
            ctx["notes"].list_notes(stranger, agent_id=ctx["agent"])

    def test_space_scoped_note_hidden_from_agent_level_list(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        _create(ctx, "space1", space_id=ctx["space"])
        _create(ctx, "agent1")
        agent_level = ctx["notes"].list_notes(ctx["access"], agent_id=ctx["agent"])
        # §5.2: an agent-level request sees only agent-level notes.
        assert [revision.title for _, revision in agent_level] == ["note agent1"]
        space_level = ctx["notes"].list_notes(
            ctx["access"], agent_id=ctx["agent"], space_id=ctx["space"]
        )
        # Stored-null stays visible downward: a space request sees the space
        # note plus agent-level notes (§5.2, same semantics as state lists).
        assert [revision.title for _, revision in space_level] == ["note space1", "note agent1"]

    def test_tombstoned_note_disappears(self, note_ctx: dict[str, Any]) -> None:
        ctx = note_ctx
        note = _create(ctx, "gone1")
        with ctx["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=ctx["tenant"],
                resource_type="note",
                resource_id=note.note_id,
                reason_code="forget",
                deleted_by="test",
            )
        from iris_memory_core.domain.errors import NotFoundError

        with pytest.raises(NotFoundError):
            ctx["notes"].get(ctx["access"], note.note_id)
        assert ctx["notes"].list_notes(ctx["access"], agent_id=ctx["agent"]) == []
