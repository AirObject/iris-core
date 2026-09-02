"""Phase 5 claim integration tests: Remember/Correct/Search/history.

Covers the negative matrix (no-evidence, unknown entity, cross tenant/agent/
space/session refs, bad revisions, privacy violations), the bi-temporal
boundary suite for Correct (current/history/disputed/superseded views), and
the identity views (at-time subject vs current subject after redirects).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.artifacts import ArtifactService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    HistoryUnavailableError,
    InvalidRequestError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.domain.memory import EvidenceRequiredError, SubjectAmbiguousError
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for


class _Ctx:
    def __init__(self, store: Store, clock: MutableClock, claims: ClaimService) -> None:
        self.store = store
        self.clock = clock
        self.claims = claims
        with store.write() as tx:
            tx.insert_tenant("t1", status="active")
            self.agent = tx.insert_agent("t1", "A1", actor="test").id
            tx.insert_tenant("t2", status="active")
            self.other_agent = tx.insert_agent("t2", "A2", actor="test").id
            self.space = tx.insert_space("t1", "chat_group").id
            self.other_space = tx.insert_space("t1", "direct").id
            tx.raw().execute(
                "INSERT INTO sessions (id, tenant_id, space_id, status, started_us) "
                "VALUES (?, 't1', ?, 'open', 1)",
                ("sess-1", self.space),
            )
            tx.raw().execute(
                "INSERT INTO sessions (id, tenant_id, space_id, status, started_us) "
                "VALUES (?, 't1', ?, 'open', 1)",
                ("sess-2", self.other_space),
            )
            self.entity = tx.identities.insert_entity(
                "t1", EntityKind.PERSON, display_name="Bob"
            ).id
            self.other_entity = tx.identities.insert_entity(
                "t1", EntityKind.PERSON, display_name="Ann"
            ).id
            self.t2_entity = tx.identities.insert_entity(
                "t2", EntityKind.PERSON, display_name="Zed"
            ).id
        self.access = access_for(
            "t1",
            agent_ids=frozenset({self.agent}),
            space_ids=frozenset({self.space}),
            consent_entities=frozenset({self.entity}),
        )
        self.wide_access = access_for(
            "t1",
            agent_ids=frozenset({self.agent}),
            space_ids=frozenset({self.space, self.other_space}),
            consent_entities=frozenset({self.entity}),
        )
        self.admin_access = AccessContext(
            tenant_id="t1",
            app_instance_id="admin-1",
            admin=True,
            agent_ids=frozenset({self.agent}),
            allowed_space_ids=frozenset({self.space, self.other_space}),
        )
        self.other_tenant_access = access_for("t2", agent_ids=frozenset({self.other_agent}))

    def observe(self, key: str, *, access: AccessContext | None = None, **overrides: Any) -> str:
        from iris_memory_core.application.observation import ObservationService

        service = ObservationService(self.store, gauge=_gauge())
        record: dict[str, Any] = {
            "agent_id": self.agent,
            "role": "user",
            "kind": "message.text",
            "idempotency_key": key,
            "occurred_us": self.clock.now_us(),
            "committed_us": self.clock.now_us(),
            "content": "evidence text",
        }
        record.update(overrides)
        outcome = service.observe_batch(access or self.access, [record])
        assert len(outcome.accepted_observation_ids) == 1
        return outcome.accepted_observation_ids[0]

    def remember(self, key: str, **overrides: Any) -> Any:
        payload: dict[str, Any] = {
            "agent_id": self.agent,
            "predicate": "likes",
            "value": {"drink": "tea"},
            "canonical_text": "Bob likes tea",
            "subject_entity_id": self.entity,
        }
        payload.update(overrides)
        evidence = payload.pop("evidence", None)
        if evidence is None:
            observation = self.observe(f"obs-{key}")
            evidence = [
                {"source_type": "observation", "source_id": observation, "relation": "supports"}
            ]
        return self.claims.remember(
            self.access,
            evidence=evidence,
            idempotency_key=f"idem-{key}",
            **payload,
        )


def _gauge() -> Any:
    """A generous gauge whose limits never trip under test volumes."""
    from iris_memory_core.application.backpressure import (
        BackpressureConfig,
        BackpressureGauge,
        FixedDiskProbe,
    )

    return BackpressureGauge(
        BackpressureConfig(
            soft_disk_free_bytes=10**12,
            hard_disk_free_bytes=10**11,
            retry_base_delay_us=1_000,
            retry_max_delay_us=10_000,
        ),
        probe=FixedDiskProbe(10**13),
        database_path=Path("."),
    )


@pytest.fixture
def ctx(clocked_store: Store, mutable_clock: MutableClock, phase5_claims: ClaimService) -> _Ctx:
    return _Ctx(clocked_store, mutable_clock, phase5_claims)


class TestRemember:
    def test_remember_creates_active_claim_with_evidence(self, ctx: _Ctx) -> None:
        result = ctx.remember("r1")
        assert result.revision == 1 and not result.deduped
        view = ctx.claims.get(ctx.access, result.claim_id)
        assert view is not None
        assert view is not None
        assert view.claim.status == "active"
        assert view.claim.evidence_count == 1
        assert view.current_subject_entity_id == ctx.entity

    def test_exact_dedup_attaches_evidence_instead_of_a_second_row(self, ctx: _Ctx) -> None:
        first = ctx.remember("d1")
        observation = ctx.observe("obs-d2")
        second = ctx.claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="likes",
            value={"drink": "tea"},
            canonical_text="Bob likes tea",
            subject_entity_id=ctx.entity,
            evidence=[
                {"source_type": "observation", "source_id": observation, "relation": "supports"}
            ],
            idempotency_key="idem-d2",
        )
        assert second.deduped and second.claim_id == first.claim_id
        view = ctx.claims.get(ctx.access, first.claim_id)
        assert view is not None
        assert view.claim.evidence_count == 2

    def test_similar_but_distinct_claims_never_merge(self, ctx: _Ctx) -> None:
        """Different subject/predicate/scope → separate rows; the dedup key
        is exactly (tenant, agent, subject, predicate, value, scope), so a
        different VALIDITY WINDOW of the same fact is the SAME logical claim
        (§13.2 — corrected via revisions, never a second row)."""
        a = ctx.remember("s1")
        b = ctx.remember("s2", subject_entity_id=ctx.other_entity)
        c = ctx.remember("s3", predicate="prefers")
        d = ctx.remember("s4", valid_from_us=1, valid_until_us=2)
        e = ctx.remember("s5", space_id=ctx.space, session_id="sess-1")
        ids = {a.claim_id, b.claim_id, c.claim_id, e.claim_id}
        assert len(ids) == 4
        assert d.deduped and d.claim_id == a.claim_id

    def test_no_evidence_is_rejected(self, ctx: _Ctx) -> None:
        with pytest.raises(EvidenceRequiredError):
            ctx.claims.remember(
                ctx.access,
                agent_id=ctx.agent,
                predicate="likes",
                value={"drink": "tea"},
                canonical_text="t",
                subject_entity_id=ctx.entity,
                evidence=[],
                idempotency_key="no-evidence",
            )

    def test_unknown_subject_entity_rejected(self, ctx: _Ctx) -> None:
        with pytest.raises(NotFoundError):
            ctx.remember("u1", subject_entity_id="missing-entity")

    def test_cross_tenant_subject_rejected(self, ctx: _Ctx) -> None:
        with pytest.raises(AccessDeniedError):
            ctx.remember("u2", subject_entity_id=ctx.t2_entity)

    def test_subject_omission_is_ambiguous(self, ctx: _Ctx) -> None:
        with pytest.raises(SubjectAmbiguousError):
            ctx.remember("u3", subject_entity_id=None)
        with pytest.raises(SubjectAmbiguousError):
            ctx.remember("u4", subject_entity_id=ctx.entity, subject_is_self=True)

    def test_self_subject_completion_is_deterministic(self, ctx: _Ctx) -> None:
        a = ctx.remember("self1", subject_entity_id=None, subject_is_self=True)
        b = ctx.remember("self2", subject_entity_id=None, subject_is_self=True)
        view = ctx.claims.get(ctx.access, a.claim_id)
        assert view is not None
        assert view is not None
        assert view.claim.subject_entity_id == view.current_subject_entity_id
        with ctx.store.read() as tx:
            links = tx.links_for_source(
                "t1", "agent", ctx.agent, target_type="entity", relation="self_entity"
            )
        assert len(links) == 1
        assert view.claim.subject_entity_id == links[0].target_id
        # b used the same self entity
        view_b = ctx.claims.get(ctx.access, b.claim_id)
        assert view_b is not None
        assert view_b.claim.subject_entity_id == view.claim.subject_entity_id

    def test_evidence_unknown_observation_rejected(self, ctx: _Ctx) -> None:
        with pytest.raises(NotFoundError):
            ctx.claims.remember(
                ctx.access,
                agent_id=ctx.agent,
                predicate="p",
                value="v",
                canonical_text="t",
                subject_entity_id=ctx.entity,
                evidence=[
                    {"source_type": "observation", "source_id": "missing", "relation": "supports"}
                ],
                idempotency_key="bad-obs",
            )

    def test_evidence_partial_observation_rejected(self, ctx: _Ctx) -> None:
        partial = ctx.observe(
            "partial-obs",
            effect_state="partial",
            effect_proof={"confirmed_range": [0, 10]},
        )
        with pytest.raises(InvalidRequestError):
            ctx.claims.remember(
                ctx.access,
                agent_id=ctx.agent,
                predicate="p",
                value="v",
                canonical_text="t",
                subject_entity_id=ctx.entity,
                evidence=[
                    {"source_type": "observation", "source_id": partial, "relation": "supports"}
                ],
                idempotency_key="partial-obs",
            )

    def test_evidence_stale_source_revision_rejected(self, ctx: _Ctx) -> None:
        observation = ctx.observe("stale-obs")
        with pytest.raises(RevisionMismatchError):
            ctx.claims.remember(
                ctx.access,
                agent_id=ctx.agent,
                predicate="p",
                value="v",
                canonical_text="t",
                subject_entity_id=ctx.entity,
                evidence=[
                    {
                        "source_type": "observation",
                        "source_id": observation,
                        "source_revision": 99,
                        "relation": "supports",
                    }
                ],
                idempotency_key="stale-obs",
            )

    def test_cross_space_session_evidence_rejected(self, ctx: _Ctx) -> None:
        """A session-scoped claim in space A cannot cite space B's evidence."""
        observation = ctx.observe(
            "space-obs",
            access=ctx.wide_access,
            space_id=ctx.other_space,
            session_id="sess-2",
        )
        with pytest.raises(InvalidRequestError):
            ctx.claims.remember(
                ctx.access,
                agent_id=ctx.agent,
                predicate="p",
                value="v",
                canonical_text="t",
                subject_entity_id=ctx.entity,
                space_id=ctx.space,
                session_id="sess-1",
                evidence=[
                    {"source_type": "observation", "source_id": observation, "relation": "supports"}
                ],
                idempotency_key="cross-space",
            )

    def test_privacy_outside_access_context_rejected(self, ctx: _Ctx) -> None:
        with pytest.raises(AccessDeniedError):
            ctx.remember("priv1", privacy_labels=["restricted"])

    def test_subject_private_claim_hidden_without_consent(self, ctx: _Ctx) -> None:
        secret = ctx.remember("priv2", privacy_labels=[f"entity:{ctx.entity}:private"])
        # The creator (with consent) sees it.
        assert ctx.claims.get(ctx.access, secret.claim_id) is not None
        no_consent = access_for(
            "t1", agent_ids=frozenset({ctx.agent}), space_ids=frozenset({ctx.space})
        )
        with pytest.raises(AccessDeniedError):
            ctx.claims.get(no_consent, secret.claim_id)
        assert ctx.claims.search(no_consent, agent_id=ctx.agent, subject_entity_id=ctx.entity) == []

    def test_procedure_claim_rejects_executable_payload(self, ctx: _Ctx) -> None:
        from iris_memory_core.domain.memory import UnsafeProcedureClaimError

        with pytest.raises(UnsafeProcedureClaimError):
            ctx.remember(
                "proc1",
                category="procedure",
                value={"steps": [{"action": "run", "shell": "rm -rf /"}]},
            )
        with pytest.raises(UnsafeProcedureClaimError):
            ctx.remember("proc2", category="procedure", value={"sql": "SELECT 1"})

    def test_procedure_claim_accepts_declarative_steps(self, ctx: _Ctx) -> None:
        ok = ctx.remember(
            "proc3",
            category="procedure",
            value={"name": "greet", "steps": [{"action": "say", "description": "greet warmly"}]},
        )
        assert ok.revision == 1

    def test_idempotent_replay_returns_first_outcome(self, ctx: _Ctx) -> None:
        first_observation = ctx.observe("obs-idem")
        first = ctx.claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="likes",
            value={"drink": "tea"},
            canonical_text="Bob likes tea",
            subject_entity_id=ctx.entity,
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": first_observation,
                    "relation": "supports",
                }
            ],
            idempotency_key="same-key",
        )
        replay = ctx.claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="likes",
            value={"drink": "tea"},
            canonical_text="Bob likes tea",
            subject_entity_id=ctx.entity,
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": first_observation,
                    "relation": "supports",
                }
            ],
            idempotency_key="same-key",
        )
        assert replay.replayed and replay.claim_id == first.claim_id
        from iris_memory_core.domain.errors import IdempotencyKeyReusedError

        with pytest.raises(IdempotencyKeyReusedError):
            ctx.claims.remember(
                ctx.access,
                agent_id=ctx.agent,
                predicate="different",
                value="v",
                canonical_text="t",
                subject_entity_id=ctx.entity,
                evidence=[
                    {
                        "source_type": "observation",
                        "source_id": ctx.observe("obs-idem3"),
                        "relation": "supports",
                    }
                ],
                idempotency_key="same-key",
            )


class TestCorrectBiTemporal:
    def _claim(self, ctx: _Ctx, key: str) -> Any:
        return ctx.remember(key)

    def test_supersede_preserves_old_revision_and_stamps_system_time(self, ctx: _Ctx) -> None:
        ctx.clock.set(1_000)
        created = self._claim(ctx, "c1")
        ctx.clock.set(2_000)
        corrected = ctx.claims.correct(
            ctx.access,
            created.claim_id,
            expected_revision=1,
            mode="supersede",
            value={"drink": "coffee"},
            canonical_text="Bob likes coffee",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": ctx.observe("obs-c1b"),
                    "relation": "supports",
                }
            ],
            idempotency_key="cor-1",
        )
        assert (corrected.revision, corrected.previous_revision) == (2, 1)
        history = ctx.claims.history(ctx.access, created.claim_id)
        assert [revision.revision for revision in history] == [2, 1]
        assert history[1].canonical_text == "Bob likes tea"
        assert history[1].recorded_at_us == 1_000
        assert history[1].superseded_at_us == 2_000
        assert history[0].superseded_at_us is None
        # Current read shows the correction only.
        view = ctx.claims.get(ctx.access, created.claim_id)
        assert view is not None
        assert view.revision.canonical_text == "Bob likes coffee"

    def test_as_of_reconstruction_boundary(self, ctx: _Ctx) -> None:
        ctx.clock.set(1_000)
        created = self._claim(ctx, "c2")
        ctx.clock.set(2_000)
        ctx.claims.correct(
            ctx.access,
            created.claim_id,
            expected_revision=1,
            mode="supersede",
            value={"drink": "coffee"},
            canonical_text="Bob likes coffee",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": ctx.observe("obs-c2b"),
                    "relation": "supports",
                }
            ],
            idempotency_key="cor-2",
        )
        before = ctx.claims.revision_at(ctx.access, created.claim_id, as_of_us=999)
        assert before is None
        at_creation = ctx.claims.revision_at(ctx.access, created.claim_id, as_of_us=1_000)
        assert at_creation is not None and at_creation.canonical_text == "Bob likes tea"
        at_correction = ctx.claims.revision_at(ctx.access, created.claim_id, as_of_us=2_000)
        assert at_correction is not None and at_correction.canonical_text == "Bob likes coffee"
        after = ctx.claims.revision_at(ctx.access, created.claim_id, as_of_us=100_000)
        assert after is not None and after.canonical_text == "Bob likes coffee"

    def test_as_of_search_sees_the_historical_state(self, ctx: _Ctx) -> None:
        ctx.clock.set(1_000)
        created = self._claim(ctx, "c3")
        ctx.clock.set(2_000)
        ctx.claims.correct(
            ctx.access,
            created.claim_id,
            expected_revision=1,
            mode="supersede",
            value="v2",
            canonical_text="Bob likes coffee",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": ctx.observe("obs-c3b"),
                    "relation": "supports",
                }
            ],
            idempotency_key="cor-3",
        )
        old = ctx.claims.search(
            ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity, as_of_us=1_500
        )
        assert [view.revision.canonical_text for view in old] == ["Bob likes tea"]
        new = ctx.claims.search(
            ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity, as_of_us=2_500
        )
        assert [view.revision.canonical_text for view in new] == ["Bob likes coffee"]

    def test_history_unavailable_after_retention_prune(self, ctx: _Ctx) -> None:
        ctx.clock.set(10_000)
        created = self._claim(ctx, "c4")
        boundary = ctx.clock.now_us() + 5_000
        with ctx.store.write() as tx:
            tx.claims.set_history_available_from(created.claim_id, boundary)
        with pytest.raises(HistoryUnavailableError):
            ctx.claims.revision_at(ctx.access, created.claim_id, as_of_us=boundary - 1)
        # At/after the boundary the answer is available and honest.
        available = ctx.claims.revision_at(ctx.access, created.claim_id, as_of_us=boundary)
        assert available is not None

    def test_dispute_marks_disputed_with_contradicts_evidence(self, ctx: _Ctx) -> None:
        created = self._claim(ctx, "c5")
        disputed = ctx.claims.correct(
            ctx.access,
            created.claim_id,
            expected_revision=1,
            mode="dispute",
            reason="contradicted by newer statement",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": ctx.observe("obs-c5b"),
                    "relation": "contradicts",
                }
            ],
            idempotency_key="cor-5",
        )
        view = ctx.claims.get(ctx.access, created.claim_id)
        assert view is not None
        assert view.claim.status == "disputed"
        assert disputed.revision == 2
        evidence = ctx.claims.evidence(ctx.access, created.claim_id)
        assert any(item.relation == "contradicts" for item in evidence)

    def test_retract_removes_from_current_reads_but_keeps_history(self, ctx: _Ctx) -> None:
        created = self._claim(ctx, "c6")
        ctx.claims.correct(
            ctx.access,
            created.claim_id,
            expected_revision=1,
            mode="retract",
            reason="mistake",
            idempotency_key="cor-6",
        )
        view = ctx.claims.get(ctx.access, created.claim_id)
        assert view is not None
        assert view.claim.status == "retracted"
        assert ctx.claims.search(ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity) == []
        assert len(ctx.claims.history(ctx.access, created.claim_id)) == 2

    def test_expected_revision_cas(self, ctx: _Ctx) -> None:
        created = self._claim(ctx, "c7")
        with pytest.raises(RevisionMismatchError):
            ctx.claims.correct(
                ctx.access,
                created.claim_id,
                expected_revision=99,
                mode="dispute",
                reason="r",
                evidence=[
                    {
                        "source_type": "observation",
                        "source_id": ctx.observe("obs-c7b"),
                        "relation": "contradicts",
                    }
                ],
                idempotency_key="cor-7",
            )

    def test_low_authority_cannot_supersede_higher(self, ctx: _Ctx) -> None:
        created = ctx.remember("c8", source_authority="admin_confirmed")
        with pytest.raises(AccessDeniedError):
            ctx.claims.correct(
                ctx.access,
                created.claim_id,
                expected_revision=1,
                mode="supersede",
                value="v",
                canonical_text="t",
                source_authority="agent_inference",
                evidence=[
                    {
                        "source_type": "observation",
                        "source_id": ctx.observe("obs-c8b"),
                        "relation": "supports",
                    }
                ],
                idempotency_key="cor-8",
            )
        # ...but a dispute is always available.
        ctx.claims.correct(
            ctx.access,
            created.claim_id,
            expected_revision=1,
            mode="dispute",
            reason="r",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": ctx.observe("obs-c8c"),
                    "relation": "contradicts",
                }
            ],
            idempotency_key="cor-8b",
        )

    def test_as_of_sees_claim_that_was_active_before_retraction(self, ctx: _Ctx) -> None:
        """The historical status filter applies to the RECONSTRUCTED
        revision, not the current pointer (ADR-0013 §1)."""
        ctx.clock.set(1_000)
        created = self._claim(ctx, "c9")
        ctx.claims.correct(
            ctx.access,
            created.claim_id,
            expected_revision=1,
            mode="retract",
            reason="mistake",
            idempotency_key="cor-9",
        )
        assert ctx.claims.search(ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity) == []
        then = ctx.claims.search(
            ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity, as_of_us=1_000
        )
        assert [view.revision.canonical_text for view in then] == ["Bob likes tea"]

    def test_valid_time_window_filters_search(self, ctx: _Ctx) -> None:
        ctx.remember("v1", valid_from_us=1_000, valid_until_us=2_000)
        ctx.clock.set(1_500)
        assert (
            len(ctx.claims.search(ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity))
            == 1
        )
        ctx.clock.set(3_000)
        assert ctx.claims.search(ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity) == []
        assert (
            len(
                ctx.claims.search(
                    ctx.access,
                    agent_id=ctx.agent,
                    subject_entity_id=ctx.entity,
                    valid_at_us=1_500,
                )
            )
            == 1
        )


class TestIdentityViews:
    def test_redirect_changes_current_but_not_at_time_subject(self, ctx: _Ctx) -> None:
        created = ctx.remember("i1")
        with ctx.store.write() as tx:
            tx.identities.insert_entity_redirect(
                "t1",
                ctx.entity,
                ctx.other_entity,
                actor="admin",
                reason_code="merge",
            )
        view = ctx.claims.get(ctx.access, created.claim_id)
        assert view is not None
        assert view.claim.subject_entity_id == ctx.entity  # at-time identity
        assert view.current_subject_entity_id == ctx.other_entity  # current identity
        # Nothing was written back into the revision.
        assert view.revision.subject_entity_id == ctx.entity

    def test_search_respects_scope_envelope(self, ctx: _Ctx) -> None:
        session_claim = ctx.remember("sc1", space_id=ctx.space, session_id="sess-1")
        # The other space needs its own authorized creator.
        other_observation = ctx.observe(
            "obs-sc2", access=ctx.wide_access, space_id=ctx.other_space, session_id="sess-2"
        )
        other_session_claim = ctx.claims.remember(
            ctx.wide_access,
            agent_id=ctx.agent,
            predicate="likes",
            value={"drink": "tea"},
            canonical_text="Bob likes tea",
            subject_entity_id=ctx.entity,
            space_id=ctx.other_space,
            session_id="sess-2",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": other_observation,
                    "relation": "supports",
                }
            ],
            idempotency_key="idem-sc2",
        )
        agent_claim = ctx.remember("sc3")
        session_view = ctx.claims.search(
            ctx.access, agent_id=ctx.agent, space_id=ctx.space, session_id="sess-1"
        )
        assert {view.claim.id for view in session_view} == {
            session_claim.claim_id,
            agent_claim.claim_id,  # agent-level data is visible downward
        }
        assert other_session_claim.claim_id not in {view.claim.id for view in session_view}


class TestArtifactAndNoteEvidence:
    def test_artifact_evidence_admitted_through_canonical_validator(
        self, ctx: _Ctx, phase5_artifacts: ArtifactService
    ) -> None:
        from iris_memory_core.storage.idempotency import IdempotencyManager

        artifacts = ArtifactService(
            ctx.store, ctx.store.clock, idempotency=IdempotencyManager(ctx.store)
        )
        artifact = artifacts.ingest_inline(
            ctx.access,
            agent_id=ctx.agent,
            content=b"receipt",
            media_type="text/plain",
            idempotency_key="ev-art",
        )
        claim = ctx.claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="paid",
            value=True,
            canonical_text="Bob paid",
            subject_entity_id=ctx.entity,
            evidence=[
                {
                    "source_type": "artifact",
                    "source_id": artifact.artifact_id,
                    "relation": "supports",
                }
            ],
            idempotency_key="ev-art-claim",
        )
        assert claim.revision == 1

    def test_note_evidence_admitted(self, ctx: _Ctx) -> None:
        from iris_memory_core.application.notes import NoteService
        from iris_memory_core.storage.idempotency import IdempotencyManager

        notes = NoteService(ctx.store, ctx.store.clock, idempotency=IdempotencyManager(ctx.store))
        note = notes.create(
            ctx.access,
            agent_id=ctx.agent,
            kind="important",
            title="Bob paid",
            body="",
            idempotency_key="ev-note",
        )
        claim = ctx.claims.remember(
            ctx.access,
            agent_id=ctx.agent,
            predicate="paid",
            value=True,
            canonical_text="Bob paid",
            subject_entity_id=ctx.entity,
            evidence=[{"source_type": "note", "source_id": note.note_id, "relation": "supports"}],
            idempotency_key="ev-note-claim",
        )
        assert claim.revision == 1

    def test_note_promotion_materializes_a_real_claim(self, ctx: _Ctx, phase5_notes: Any) -> None:
        note = phase5_notes.create(
            ctx.access,
            agent_id=ctx.agent,
            kind="important",
            title="Bob likes tea",
            body="",
            idempotency_key="promo-note",
        )
        transitioned = phase5_notes.transition(
            ctx.access,
            note.note_id,
            "promote",
            expected_revision=note.revision,
            reason="promotion",
            promotion_target_type="claim",
            idempotency_key="promo-act",
        )
        assert transitioned.promotion_target_id is not None
        view = ctx.claims.get(ctx.access, transitioned.promotion_target_id)
        assert view is not None
        assert view is not None and view.claim.status == "active"
        evidence = ctx.claims.evidence(ctx.access, transitioned.promotion_target_id)
        assert any(item.source_type == "note" for item in evidence)
        with ctx.store.read() as tx:
            links = tx.links_for_source("t1", "note", note.note_id, target_type="claim")
        assert any(link.relation == "promoted_to" for link in links)

    def test_note_promotion_materializes_a_real_episode(
        self, ctx: _Ctx, phase5_notes: Any, phase5_episodes: Any
    ) -> None:
        note = phase5_notes.create(
            ctx.access,
            agent_id=ctx.agent,
            kind="observation",
            title="Trip",
            body="We went hiking",
            idempotency_key="promo-note-2",
        )
        transitioned = phase5_notes.transition(
            ctx.access,
            note.note_id,
            "promote",
            expected_revision=note.revision,
            reason="promotion",
            promotion_target_type="episode",
            idempotency_key="promo-act-2",
        )
        assert transitioned.promotion_target_id is not None
        episode, revision = phase5_episodes.get(ctx.access, transitioned.promotion_target_id)
        assert episode.status == "open"
        assert revision.summary == "We went hiking"
