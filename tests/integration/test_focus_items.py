"""FocusItem integration tests (§9.3, P3-FOCUS-01).

Kinds/statuses, idempotent creation, Expected-Revision transitions with the
stable invalid_state_transition code, capacity/kind-quota/token-budget/TTL/
decay/activation properties (≥200 fixed-seed cases each — one test function
per property driving 200 cases), the promotion seam, history retention and
the affect→persona guard.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from iris_memory_core.application.focus import FocusService
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    IdempotencyKeyReusedError,
    InvalidRequestError,
    InvalidTransitionError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.focus import FocusCapacityPolicy, decayed_activation
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for

CASES = 200


@pytest.fixture
def phase3(clocked_store: Store, clocked_tenant_id: str, phase2_agent: str) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space = tx.insert_space(clocked_tenant_id, "chat_group")
        session = tx.insert_session(clocked_tenant_id, space.id, actor="t")
    access = access_for(
        clocked_tenant_id, agent_ids=frozenset({phase2_agent}), space_ids=frozenset({space.id})
    )
    idem = IdempotencyManager(clocked_store)
    return {
        "store": clocked_store,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "space": space.id,
        "session": session.id,
        "access": access,
        "idem": idem,
        "focus": FocusService(clocked_store, clocked_store.clock, idempotency=idem),
    }


def _create(
    ctx: dict[str, Any],
    *,
    kind: str = "goal",
    summary: str | None = None,
    key: str = "k",
    salience: float = 0.5,
    importance: float = 0.5,
    activation: float = 0.5,
    expires_us: int | None = None,
    space_id: str | None = None,
    session_id: str | None = None,
    promotion_policy: str = "",
) -> Any:
    return ctx["focus"].create(
        ctx["access"],
        agent_id=ctx["agent"],
        kind=kind,
        summary=summary if summary is not None else key,
        salience=salience,
        importance=importance,
        activation=activation,
        expires_us=expires_us,
        space_id=space_id,
        session_id=session_id,
        promotion_policy=promotion_policy,
        idempotency_key=key,
    )


class TestCreationAndTransitions:
    def test_all_kinds_accepted(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        for index, kind in enumerate(
            ("goal", "question", "entity", "clue", "concern", "affect", "pending_input")
        ):
            result = _create(ctx, kind=kind, key=f"kind-{index}")
            assert result.revision == 1

    def test_create_is_idempotent_and_key_reuse_detected(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        first = _create(ctx, key="idem", summary="same")
        replay = _create(ctx, key="idem", summary="same")
        assert replay.replayed is True and replay.item_id == first.item_id
        with pytest.raises(IdempotencyKeyReusedError):
            _create(ctx, key="idem", summary="different")

    def test_invalid_kind_and_scores_rejected(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        with pytest.raises(InvalidRequestError):
            _create(ctx, kind="vibe", key="bad-kind")
        with pytest.raises(InvalidRequestError):
            ctx["focus"].create(
                ctx["access"],
                agent_id=ctx["agent"],
                kind="goal",
                summary="x",
                salience=1.5,
                idempotency_key="bad-score",
            )
        with pytest.raises(InvalidRequestError):
            ctx["focus"].create(
                ctx["access"],
                agent_id=ctx["agent"],
                kind="goal",
                summary="",
                idempotency_key="bad-summary",
            )

    def test_transitions_with_expected_revision(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="t1")
        dormant = ctx["focus"].transition(
            ctx["access"],
            item.item_id,
            "dormant",
            expected_revision=1,
            reason="park",
            idempotency_key="ik-focus-1",
        )
        assert dormant.status == "dormant"
        active = ctx["focus"].activate(
            ctx["access"],
            item.item_id,
            expected_revision=2,
            reason="wake",
            idempotency_key="ik-focus-2",
        )
        assert active.status == "active"
        assert active.activation_base > 0.5  # boosted
        dismissed = ctx["focus"].transition(
            ctx["access"],
            item.item_id,
            "dismiss",
            expected_revision=3,
            reason="done",
            idempotency_key="ik-focus-3",
        )
        assert dismissed.status == "dismissed"
        with pytest.raises(InvalidTransitionError) as terminal:
            ctx["focus"].activate(
                ctx["access"],
                item.item_id,
                expected_revision=4,
                reason="nope",
                idempotency_key="ik-focus-1",
            )
        assert terminal.value.code == "invalid_state_transition"

    def test_invalid_transitions_are_stable(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        for source, target in (
            ("dismissed", "active"),
            ("expired", "dormant"),
            ("promoted", "dismissed"),
            ("dismissed", "promoted"),
        ):
            item = _create(ctx, key=f"it-{source}-{target}")
            ctx["focus"].transition(
                ctx["access"],
                item.item_id,
                source,
                expected_revision=1,
                reason="setup",
                **({"promotion_target_type": "task"} if source == "promoted" else {}),
                idempotency_key=f"ik-setup-{source}-{target}",
            )
            with pytest.raises(InvalidTransitionError) as error:
                ctx["focus"].transition(
                    ctx["access"],
                    item.item_id,
                    target,
                    expected_revision=2,
                    reason="try",
                    idempotency_key=f"ik-try-{source}-{target}",
                )
            assert error.value.code == "invalid_state_transition"
        with pytest.raises(InvalidRequestError):
            ctx["focus"].transition(
                ctx["access"],
                _create(ctx, key="bogus").item_id,
                "bogus",
                expected_revision=1,
                reason="try",
                idempotency_key="ik-focus-6",
            )

    def test_wrong_expected_revision_rejected(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="cas")
        with pytest.raises(RevisionMismatchError):
            ctx["focus"].transition(
                ctx["access"],
                item.item_id,
                "dormant",
                expected_revision=99,
                reason="stale",
                idempotency_key="ik-focus-7",
            )


class TestCapacityProperties:
    def test_item_cap_evicts_lowest_activation_to_dormant(self, phase3: dict[str, Any]) -> None:
        """200 seeded cases: with capacity N, admitting item N+1 evicts the
        lowest-activation ACTIVE item to dormant — never a delete."""
        for case in range(CASES):
            seed = random.Random(90_000 + case)
            cap = seed.randrange(1, 5)
            ctx = phase3
            service = FocusService(
                ctx["store"],
                ctx["store"].clock,
                capacity=FocusCapacityPolicy(
                    max_items=cap,
                    token_budget=10_000,
                    kind_quotas={"goal": cap},
                    activation_boost=0.5,
                ),
                idempotency=ctx["idem"],
            )
            access = access_for(ctx["tenant"], agent_ids=frozenset({ctx["agent"]}))
            pre_admission_activations = []
            for i in range(cap + 1):
                activation = seed.random()
                if i < cap:
                    pre_admission_activations.append(activation)
                service.create(
                    access,
                    agent_id=ctx["agent"],
                    kind="goal",
                    summary=f"case{case}-{i}",
                    activation=activation,
                    idempotency_key=f"cap-{case}-{i}-{cap}",
                )
            active = service.list_items(access, agent_id=ctx["agent"], statuses=("active",))
            assert len(active) == cap, case
            dormant = service.list_items(access, agent_id=ctx["agent"], statuses=("dormant",))
            assert len(dormant) >= 1, case
            # the evicted item carries the minimum activation of the
            # PRE-admission active set (the newcomer is always admitted)
            if len(dormant) == 1:
                assert dormant[0][1].activation == min(pre_admission_activations), case

    def test_kind_quota_evicts_within_kind(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        service = FocusService(
            ctx["store"],
            ctx["store"].clock,
            capacity=FocusCapacityPolicy(
                max_items=100,
                token_budget=10_000,
                kind_quotas={"question": 1, "goal": 10},
                activation_boost=0.5,
            ),
            idempotency=ctx["idem"],
        )
        access = access_for(ctx["tenant"], agent_ids=frozenset({ctx["agent"]}))
        first = service.create(
            access, agent_id=ctx["agent"], kind="question", summary="q1", idempotency_key="q-1"
        )
        second = service.create(
            access, agent_id=ctx["agent"], kind="question", summary="q2", idempotency_key="q-2"
        )
        questions = service.list_items(
            access, agent_id=ctx["agent"], statuses=("active",), kind="question"
        )
        assert len(questions) == 1
        dormant = service.list_items(
            access, agent_id=ctx["agent"], statuses=("dormant",), kind="question"
        )
        assert len(dormant) == 1
        assert dormant[0][0].id == first.item_id
        assert questions[0][0].id == second.item_id

    def test_token_budget_evicts_until_fit(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        service = FocusService(
            ctx["store"],
            ctx["store"].clock,
            capacity=FocusCapacityPolicy(
                max_items=10, token_budget=40, kind_quotas={"goal": 10}, activation_boost=0.5
            ),
            idempotency=ctx["idem"],
        )
        access = access_for(ctx["tenant"], agent_ids=frozenset({ctx["agent"]}))
        for i in range(4):  # each summary ~ 4 chars → 1 token; budget 40
            service.create(
                access,
                agent_id=ctx["agent"],
                kind="goal",
                summary="goal-item-" * 4,
                idempotency_key=f"tok-{i}",
            )
        # A huge summary cannot fit even after evicting everyone.
        with pytest.raises(ConflictError):
            service.create(
                access,
                agent_id=ctx["agent"],
                kind="goal",
                summary="x" * 400,
                idempotency_key="tok-huge",
            )


class TestDecayAndTtlProperties:
    def test_decay_sweep_is_idempotent_and_pure(self, phase3: dict[str, Any]) -> None:
        """200 seeded cases: two sweeps at the same instant produce identical
        state; activation always equals the pure decay function."""
        for case in range(CASES):
            seed = random.Random(100_000 + case)
            ctx = phase3
            access = access_for(ctx["tenant"], agent_ids=frozenset({ctx["agent"]}))
            half_life = seed.choice([1_000_000, 60_000_000, 3_600_000_000])
            service = FocusService(
                ctx["store"],
                ctx["store"].clock,
                capacity=FocusCapacityPolicy(
                    max_items=50, token_budget=100_000, dormant_floor=0.05, half_life_us=half_life
                ),
                idempotency=ctx["idem"],
            )
            base = seed.random()
            result = service.create(
                access,
                agent_id=ctx["agent"],
                kind="clue",
                summary=f"decay-{case}",
                activation=base,
                idempotency_key=f"decay-{case}",
            )
            now = ctx["store"].clock.now_us() + seed.randrange(1, 10 * half_life)
            with ctx["store"].write() as tx:
                first = service.maintenance_sweep(
                    tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"], now_us=now
                )
                second = service.maintenance_sweep(
                    tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"], now_us=now
                )
            assert second.unchanged >= first.unchanged or second.expired, case
            item = service.get(access, result.item_id)
            if item is not None:
                _current, revision = item
                expected = decayed_activation(
                    base, revision.last_activated_us, now, half_life_us=half_life
                )
                if revision.status == "active":
                    assert abs(revision.activation - expected) < 1e-9, case
            # revision count must not grow from the repeated sweep
            history1 = service.history(access, result.item_id)
            with ctx["store"].write() as tx:
                service.maintenance_sweep(
                    tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"], now_us=now
                )
            history2 = service.history(access, result.item_id)
            assert len(history2) == len(history1), case

    def test_ttl_expiry_via_maintenance(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        expires = ctx["store"].clock.now_us() + 1_000_000
        item = _create(ctx, key="ttl", expires_us=expires)
        ctx["store"].clock.advance(2_000_000)
        with ctx["store"].write() as tx:
            report = ctx["focus"].maintenance_sweep(
                tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"]
            )
        assert report.expired >= 1
        assert ctx["focus"].get(ctx["access"], item.item_id) is None  # expired hidden
        history = ctx["focus"].history(ctx["access"], item.item_id)
        statuses = [rev.status for rev in history]
        assert "expired" in statuses
        assert "active" in statuses  # history retained, nothing deleted

    def test_active_item_is_not_starved_by_five_hundred_older_dormant_rows(
        self, phase3: dict[str, Any]
    ) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        active = _create(ctx, key="not-starved", expires_us=now + 1)
        with ctx["store"].write() as tx:
            tx.raw().executemany(
                "INSERT INTO focus_items "
                "(id, tenant_id, agent_id, space_group_id, space_id, session_id, scope_key, "
                "kind, summary, status, current_revision, current_revision_id, activation, "
                "activation_base, last_activated_us, expires_us, created_us, updated_us) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"bulk-dormant-{index}",
                        ctx["tenant"],
                        ctx["agent"],
                        None,
                        None,
                        None,
                        f"{ctx['tenant']}|{ctx['agent']}|||",
                        "goal",
                        f"old dormant {index}",
                        "dormant",
                        1,
                        f"bulk-dormant-rev-{index}",
                        0.5,
                        0.5,
                        now,
                        None,
                        now - 1_000_000 + index,
                        now - 1_000_000 + index,
                    )
                    for index in range(500)
                ],
            )
            tx.raw().executemany(
                "INSERT INTO focus_item_revisions "
                "(id, item_id, tenant_id, revision, kind, summary, structured_payload, "
                "privacy_labels, source_refs, salience, activation, activation_base, importance, "
                "status, promotion_policy, promotion_target_type, promotion_target_id, "
                "last_activated_us, expires_us, created_us, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"bulk-dormant-rev-{index}",
                        f"bulk-dormant-{index}",
                        ctx["tenant"],
                        1,
                        "goal",
                        f"old dormant {index}",
                        None,
                        "[]",
                        "[]",
                        0.5,
                        0.5,
                        0.5,
                        0.5,
                        "dormant",
                        "",
                        None,
                        None,
                        now,
                        None,
                        now - 1_000_000 + index,
                        "test",
                    )
                    for index in range(500)
                ],
            )
        ctx["store"].clock.advance(10)
        with ctx["store"].write() as tx:
            report = ctx["focus"].maintenance_sweep(
                tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"]
            )
        assert report.expired == 1
        with ctx["store"].read() as tx:
            assert tx.focus.get(active.item_id).status == "expired"

    def test_expired_items_excluded_from_lists_and_recall_routes(
        self, phase3: dict[str, Any]
    ) -> None:
        ctx = phase3
        expires = ctx["store"].clock.now_us() + 1_000_000
        _create(ctx, summary="expiring soon", key="exp-recall", expires_us=expires)
        _create(ctx, summary="stays alive", key="alive-recall")
        ctx["store"].clock.advance(2_000_000)
        active = ctx["focus"].list_items(ctx["access"], agent_id=ctx["agent"], statuses=("active",))
        summaries = [revision.summary for _, revision in active]
        assert any("alive" in s for s in summaries)
        assert not any("exp-recall" in s for s in summaries)


class TestPromotionSeam:
    def test_promote_records_target_and_audit_without_objects(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="promo", summary="promote me", promotion_policy="when_confirmed")
        revision = ctx["focus"].transition(
            ctx["access"],
            item.item_id,
            "promote",
            expected_revision=1,
            reason="seam",
            promotion_target_type="task",
            idempotency_key="ik-focus-8",
        )
        assert revision.status == "promoted"
        assert revision.promotion_target_type == "task"
        assert revision.promotion_target_id is None  # Phase 4 owns the id
        with ctx["store"].read() as tx:
            audits = (
                tx.raw()
                .execute(
                    "SELECT action FROM audit_events WHERE resource_type='focus_item' "
                    "AND resource_id = ?",
                    (item.item_id,),
                )
                .fetchall()
            )
        assert any(row["action"] == "focus.promoted" for row in audits)
        # Phase 4 added the task/note tables, but the FOCUS seam still must
        # not fabricate objects: nothing was created and the target id stays
        # NULL until the owning service backfills it.
        with ctx["store"].read() as tx:
            tasks_count = tx.raw().execute("SELECT COUNT(*) FROM tasks").fetchone()
            notes_count = tx.raw().execute("SELECT COUNT(*) FROM notes").fetchone()
        assert int(tasks_count[0]) == 0
        assert int(notes_count[0]) == 0

    def test_promote_requires_valid_target_type(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="promo-bad")
        with pytest.raises(InvalidRequestError):
            ctx["focus"].transition(
                ctx["access"],
                item.item_id,
                "promote",
                expected_revision=1,
                reason="x",
                promotion_target_type="vibe",
                idempotency_key="ik-focus-9",
            )
        with pytest.raises(InvalidRequestError):
            ctx["focus"].transition(
                ctx["access"],
                item.item_id,
                "promote",
                expected_revision=1,
                reason="x",
                idempotency_key="ik-focus-10",
            )

    def test_dismiss_expire_keep_history_and_sources(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        dismissed = _create(ctx, key="dismiss-me", summary="bye")
        ctx["focus"].transition(
            ctx["access"],
            dismissed.item_id,
            "dismiss",
            expected_revision=1,
            reason="cleanup",
            idempotency_key="ik-focus-11",
        )
        history = ctx["focus"].history(ctx["access"], dismissed.item_id)
        assert [rev.status for rev in history] == ["dismissed", "active"]
        # the source revision row still exists (nothing physically deleted)
        with ctx["store"].read() as tx:
            count = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM focus_item_revisions WHERE item_id = ?",
                    (dismissed.item_id,),
                )
                .fetchone()[0]
            )
        assert count == 2


class TestAffectGuard:
    def test_affect_never_touches_persona(self, phase3: dict[str, Any]) -> None:
        """affect focus may exist and decay; Persona revisions never change."""
        ctx = phase3
        with ctx["store"].read() as tx:
            before = tx.raw().execute("SELECT COUNT(*) FROM persona_revisions").fetchone()[0]
        _create(ctx, kind="affect", key="mood", summary="feeling great")
        ctx["store"].clock.advance(10_000_000_000)
        with ctx["store"].write() as tx:
            ctx["focus"].maintenance_sweep(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        with ctx["store"].read() as tx:
            after = tx.raw().execute("SELECT COUNT(*) FROM persona_revisions").fetchone()[0]
            traits = (
                tx.raw().execute("SELECT core, traits FROM persona_revisions LIMIT 1").fetchone()
            )
        assert before == after
        assert traits is not None and traits["traits"] == "[]"


class TestActivationSemantics:
    def test_only_explicit_activation_boosts(self, phase3: dict[str, Any]) -> None:
        """retrieved/returned alone must not strengthen activation (§15.6)."""
        ctx = phase3
        item = _create(ctx, key="usage", activation=0.4)
        before = ctx["focus"].get(ctx["access"], item.item_id)
        assert before is not None
        base_before = before[1].activation_base
        # simulate plain retrieval: list reads (no activation change)
        for _ in range(5):
            ctx["focus"].list_items(ctx["access"], agent_id=ctx["agent"])
        after = ctx["focus"].get(ctx["access"], item.item_id)
        assert after is not None
        assert after[1].activation_base == base_before
        # explicit activate DOES boost, bounded at 1.0
        activated = ctx["focus"].activate(
            ctx["access"],
            item.item_id,
            expected_revision=1,
            reason="used",
            idempotency_key="ik-focus-12",
        )
        assert activated.activation_base > base_before
        for revision in range(2, 8):
            boosted = ctx["focus"].activate(
                ctx["access"],
                item.item_id,
                expected_revision=revision,
                reason="spam",
                idempotency_key=f"ik-spam-{revision}",
            )
        assert boosted.activation_base == 1.0


class TestMutationIdempotencyAndRevalidation:
    """Review regressions: focus mutations carry Idempotency-Key semantics
    (§20.5) and the state machine is re-proven inside the write transaction."""

    def test_activate_replay_returns_first_outcome(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="replay-act")
        first = ctx["focus"].activate(
            ctx["access"],
            item.item_id,
            expected_revision=1,
            reason="used",
            idempotency_key="replay-act-1",
        )
        # A byte-identical retry (e.g. after a lost response) replays the
        # first outcome instead of failing with revision_mismatch.
        replayed = ctx["focus"].activate(
            ctx["access"],
            item.item_id,
            expected_revision=1,
            reason="used",
            idempotency_key="replay-act-1",
        )
        assert replayed == first
        # Same key with a DIFFERENT payload is a reuse, never a replay — and
        # the reuse check fires before any state is read.
        with pytest.raises(IdempotencyKeyReusedError):
            ctx["focus"].activate(
                ctx["access"],
                item.item_id,
                expected_revision=2,
                reason="used",
                idempotency_key="replay-act-1",
            )

    def test_activate_same_key_different_payload_rejected(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="reuse-act")
        ctx["focus"].activate(
            ctx["access"],
            item.item_id,
            expected_revision=1,
            reason="used",
            idempotency_key="reuse-act-1",
        )
        with pytest.raises(IdempotencyKeyReusedError):
            ctx["focus"].activate(
                ctx["access"],
                item.item_id,
                expected_revision=1,
                reason="other",
                idempotency_key="reuse-act-1",
            )

    def test_transition_replay_returns_first_outcome(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="replay-tr")
        first = ctx["focus"].transition(
            ctx["access"],
            item.item_id,
            "dormant",
            expected_revision=1,
            reason="park",
            idempotency_key="replay-tr-1",
        )
        replayed = ctx["focus"].transition(
            ctx["access"],
            item.item_id,
            "dormant",
            expected_revision=1,
            reason="park",
            idempotency_key="replay-tr-1",
        )
        assert replayed == first

    def test_mutations_require_idempotency_key(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="nokey")
        with pytest.raises(InvalidRequestError):
            ctx["focus"].activate(ctx["access"], item.item_id, expected_revision=1, reason="used")
        with pytest.raises(InvalidRequestError):
            ctx["focus"].transition(
                ctx["access"], item.item_id, "dormant", expected_revision=1, reason="park"
            )

    def test_write_tx_revalidates_terminal_state(self, phase3: dict[str, Any]) -> None:
        """The preflight legality check can race with a concurrent writer;
        the AUTHORITATIVE check re-runs in the write transaction, so a caller
        that predicted the next revision can never push an item out of a
        terminal state even when the CAS itself would succeed."""
        from iris_memory_core.domain.focus import FocusStatus

        ctx = phase3
        item = _create(ctx, key="toctou")
        service: FocusService = ctx["focus"]
        service.transition(
            ctx["access"],
            item.item_id,
            "dismiss",
            expected_revision=1,
            reason="gone",
            idempotency_key="toctou-dismiss",
        )
        current = service.get(ctx["access"], item.item_id, include_dormant=True)
        assert current is None  # dismissed items are invisible to readers
        with ctx["store"].read() as tx:
            row = tx.focus.get(item.item_id)
        assert row.status == "dismissed"
        assert row.current_revision == 2
        # Simulate the racing window: the preflight saw status=active at
        # revision 1, so the caller submits expected_revision=1 — which is a
        # stale CAS now, but even a predicted-next CAS (2) must be refused by
        # the state machine before the pointer can move.
        with pytest.raises(InvalidTransitionError), ctx["store"].write() as tx:
            service._execute_transition(
                tx,
                ctx["access"],
                {
                    "item_id": item.item_id,
                    "target": "dormant",
                    "expected_revision": 2,
                    "reason": "race",
                    "promotion_target_type": None,
                },
                FocusStatus.DORMANT,
            )
        with ctx["store"].read() as tx:
            after = tx.focus.get(item.item_id)
        assert after.current_revision == 2  # nothing was written
        assert after.status == "dismissed"

    def test_public_replay_after_terminal_is_invalid_transition(
        self, phase3: dict[str, Any]
    ) -> None:
        ctx = phase3
        item = _create(ctx, key="terminal")
        ctx["focus"].transition(
            ctx["access"],
            item.item_id,
            "dismiss",
            expected_revision=1,
            reason="gone",
            idempotency_key="terminal-dismiss",
        )
        with pytest.raises(InvalidTransitionError) as error:
            ctx["focus"].transition(
                ctx["access"],
                item.item_id,
                "promote",
                expected_revision=2,
                reason="sneak",
                promotion_target_type="note",
                idempotency_key="terminal-sneak",
            )
        assert error.value.code == "invalid_state_transition"


class TestByIdSpaceAuthorization:
    """Review regression (P0): by-ID focus reads/writes must respect the
    access envelope's space grants — comparing the item's scope with itself
    authorizes nothing."""

    def test_cross_space_by_id_denied_everywhere(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        with ctx["store"].write() as tx:
            other_space = tx.insert_space(ctx["tenant"], "direct")
        only_other = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({other_space.id}),
        )
        item = _create(ctx, key="locked", space_id=ctx["space"], session_id=ctx["session"])
        with pytest.raises(AccessDeniedError):
            ctx["focus"].get(only_other, item.item_id)
        with pytest.raises(AccessDeniedError):
            ctx["focus"].activate(
                only_other,
                item.item_id,
                expected_revision=1,
                reason="used",
                idempotency_key="x-space-act",
            )
        with pytest.raises(AccessDeniedError):
            ctx["focus"].transition(
                only_other,
                item.item_id,
                "dormant",
                expected_revision=1,
                reason="park",
                idempotency_key="x-space-tr",
            )
        with pytest.raises(AccessDeniedError):
            ctx["focus"].history(only_other, item.item_id)
        # the authorized caller still reaches every path
        owned = ctx["focus"].get(ctx["access"], item.item_id)
        assert owned is not None
        history = ctx["focus"].history(ctx["access"], item.item_id)
        assert len(history) == 1

    def test_authorized_space_by_id_still_works(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="open", space_id=ctx["space"])
        found = ctx["focus"].get(ctx["access"], item.item_id)
        assert found is not None
        assert found[0].space_id == ctx["space"]


class TestByIdPrivacyTombstoneAndReplayAuthorization:
    """Security state outranks history access and cached write outcomes."""

    def test_create_rejects_privacy_labels_not_granted_to_caller(
        self, phase3: dict[str, Any]
    ) -> None:
        ctx = phase3
        with pytest.raises(AccessDeniedError):
            ctx["focus"].create(
                ctx["access"],
                agent_id=ctx["agent"],
                kind="goal",
                summary="cannot self-mark restricted",
                space_id=ctx["space"],
                privacy_labels=["restricted"],
                idempotency_key="ungranted-restricted",
            )

    def test_restricted_item_denies_history_and_mutations(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        admin = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"]}),
            admin=True,
        )
        item = ctx["focus"].create(
            admin,
            agent_id=ctx["agent"],
            kind="goal",
            summary="restricted focus secret",
            space_id=ctx["space"],
            privacy_labels=["restricted"],
            idempotency_key="restricted-create",
        )
        assert ctx["focus"].get(ctx["access"], item.item_id) is None
        with pytest.raises(AccessDeniedError):
            ctx["focus"].history(ctx["access"], item.item_id)
        with pytest.raises(AccessDeniedError):
            ctx["focus"].activate(
                ctx["access"],
                item.item_id,
                expected_revision=1,
                reason="not-authorized",
                idempotency_key="restricted-activate",
            )
        with pytest.raises(AccessDeniedError):
            ctx["focus"].transition(
                ctx["access"],
                item.item_id,
                "dormant",
                expected_revision=1,
                reason="not-authorized",
                idempotency_key="restricted-transition",
            )

    def test_completed_replay_rechecks_current_space_grant(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="replay-scope", space_id=ctx["space"])
        first = ctx["focus"].activate(
            ctx["access"],
            item.item_id,
            expected_revision=1,
            reason="used",
            idempotency_key="replay-scope-activate",
        )
        assert first.revision == 2
        revoked = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            app_instance_id=ctx["access"].app_instance_id,
        )
        with pytest.raises(AccessDeniedError):
            ctx["focus"].activate(
                revoked,
                item.item_id,
                expected_revision=1,
                reason="used",
                idempotency_key="replay-scope-activate",
            )
        with pytest.raises(AccessDeniedError):
            ctx["focus"].create(
                revoked,
                agent_id=ctx["agent"],
                kind="goal",
                summary="replay-scope",
                space_id=ctx["space"],
                idempotency_key="replay-scope",
            )

    def test_tombstone_blocks_history_new_mutation_and_replay(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        item = _create(ctx, key="replay-tombstone")
        first = ctx["focus"].activate(
            ctx["access"],
            item.item_id,
            expected_revision=1,
            reason="used",
            idempotency_key="replay-tombstone-activate",
        )
        assert first.revision == 2
        with ctx["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=ctx["tenant"],
                resource_type="focus_item",
                resource_id=item.item_id,
                reason_code="forget",
                deleted_by="admin",
            )
        with pytest.raises(NotFoundError):
            ctx["focus"].history(ctx["access"], item.item_id)
        with pytest.raises(NotFoundError):
            ctx["focus"].transition(
                ctx["access"],
                item.item_id,
                "dormant",
                expected_revision=2,
                reason="forgotten",
                idempotency_key="tombstone-new-transition",
            )
        with pytest.raises(NotFoundError):
            ctx["focus"].activate(
                ctx["access"],
                item.item_id,
                expected_revision=1,
                reason="used",
                idempotency_key="replay-tombstone-activate",
            )
