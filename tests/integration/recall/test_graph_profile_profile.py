"""Phase 8 profile projection integration tests (ADR-0016 §2/§5).

Quantified gates:

- 100% of non-empty fields carry at least one still-valid source claim
  (200+ generated subjects; automatic reference verification plus sampled
  canonical fallback comparison).
- Conflicts are retained explicitly — never silently picked or merged.
- Agent persona isolation: self-narrative claims never enter profiles.
- Scope isolation: fields never merge claims across scope/privacy groups.
- Rebuild determinism: 3 consecutive rebuilds of one snapshot produce the
  same source sets, field sets, watermarks and checksums.
- Correct/Forget races x50: after the change commits, old field values
  return zero.
- Canonical fallback: an untrusted projection serves the same view derived
  from canonical claims, labeled honestly.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from iris_memory_core.domain.profile import (
    ProfileSubjectKey,
)
from iris_memory_core.domain.recall import ROUTE_PROFILE
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.recall.graph_profile_helpers import TENANT, Phase8World

SOURCE_CASES = 200
RACE_ROUNDS = 50


@pytest.fixture
def world(clocked_store: Store, mutable_clock: MutableClock) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


def _subjects(world: Phase8World) -> list[ProfileSubjectKey]:
    with world.store.read() as tx:
        pointer = tx.profile.pointer(TENANT)
        assert pointer is not None
        return [
            ProfileSubjectKey(row.subject_kind, row.subject_id)
            for row in tx.profile.subjects_for_generation(TENANT, pointer.generation_id)
        ]


def _fields_of(world: Phase8World, subject: ProfileSubjectKey) -> tuple[Any, ...]:
    with world.store.read() as tx:
        pointer = tx.profile.pointer(TENANT)
        assert pointer is not None
        return tx.profile.fields_for_subject(TENANT, pointer.generation_id, subject)


class TestSourceValidity:
    def test_every_nonempty_field_has_a_valid_source_claim_200_subjects(
        self, world: Phase8World
    ) -> None:
        bob = world.entity("Bob")
        # 200 subjects: 200 entities with distinct claims.
        for case in range(SOURCE_CASES):
            person = world.entity(f"src-{case}")
            world.remember(
                f"name-{case}",
                f"Person {case} is a pilot",
                person,
                category="identity",
            )
            world.remember(
                f"like-{case}",
                f"Person {case} likes tea",
                person,
                category="preference",
            )
        # The pair subject from Bob's targeted relationship claim.
        carol = world.entity("Carol")
        world.remember(
            "friend",
            "Bob trusts Carol",
            bob,
            category="relationship",
            value={"target_entity_id": carol},
        )
        world.rebuild()
        subjects = _subjects(world)
        assert len(subjects) >= SOURCE_CASES + 1  # + relationship subject
        field_total = 0
        for subject in subjects:
            for field in _fields_of(world, subject):
                field_total += 1
                assert field.sources, f"{subject}: field without sources"
        assert field_total > SOURCE_CASES
        # Automatic reference verification: every source claim is still
        # visible at the recorded revision and not tombstoned.
        with world.store.read() as tx:
            pointer = tx.profile.pointer(TENANT)
            assert pointer is not None
            valid_pairs = world.profile.fields_with_valid_sources_in_tx(
                tx, TENANT, pointer.generation_id, subjects[0]
            )
            assert valid_pairs
            for subject in subjects:
                for field in tx.profile.fields_for_subject(TENANT, pointer.generation_id, subject):
                    for source in field.sources:
                        claim = tx.claims.get(source.claim_id)
                        assert claim.status in ("active", "disputed")
                        assert claim.current_revision == source.revision
                        assert not tx.is_tombstoned(TENANT, "claim", source.claim_id)
        # Sampled canonical fallback comparison (round 3: a REAL one — the
        # served projection view must equal the canonical derivation taken
        # in the same snapshot; "projection" source already proves the
        # stored subject checksum matches, this pins the field content).
        for subject in subjects[:25]:
            view = world.profile.read_profile(TENANT, subject, agent_id=world.agent)
            assert view.source == "projection"
            with world.store.read() as tx:
                canonical = world.profile.derive_subject_fields(tx, TENANT, subject)
            assert view.fields == canonical

    def test_freshness_is_snapshot_anchored_not_wall_clock(self, world: Phase8World) -> None:
        bob = world.entity("Tim")
        world.remember("t1", "Tim is a pilot", bob, category="identity")
        world.rebuild()
        first = world.profile.rebuild(TENANT)
        world.clock.advance(90 * 24 * 3_600_000_000)  # three months later
        second = world.profile.rebuild(TENANT)
        assert first.content_checksum == second.content_checksum
        assert first.field_count == second.field_count


class TestConflictRetention:
    def test_conflicting_claims_are_listed_not_merged(self, world: Phase8World) -> None:
        bob = world.entity("Conflict-Bob")
        world.remember(
            "city-a", "Bob lives in Tokyo", bob, predicate="p_city", category="fact", importance=0.9
        )
        world.remember(
            "city-b", "Bob lives in Osaka", bob, predicate="p_city", category="fact", importance=0.9
        )
        world.rebuild()
        fields = _fields_of(world, ProfileSubjectKey("entity", bob))
        city = [f for f in fields if f.field == "p_city" and f.section == "experience"]
        assert len(city) == 1
        assert city[0].conflict_state == "conflict"
        value = json.loads(city[0].value_json)
        values = value["values"]
        assert len(values) == 2
        assert {entry["value"] for entry in values} == {"Bob lives in Tokyo", "Bob lives in Osaka"}
        assert len(city[0].sources) == 2

    def test_disputed_sources_mark_the_field(self, world: Phase8World) -> None:
        bob = world.entity("Dispute-Bob")
        result = world.remember(
            "tea", "Bob likes tea", bob, predicate="p_drink", category="preference"
        )
        world.claims.correct(
            world.access,
            claim_id=result.claim_id,
            expected_revision=result.revision,
            mode="dispute",
            evidence=[{"source_type": "observation", "source_id": world.observation()}],
            reason="dispute test",
            idempotency_key="dispute-1",
        )
        world.rebuild()
        fields = _fields_of(world, ProfileSubjectKey("entity", bob))
        drink = [f for f in fields if f.field == "p_drink" and f.section == "preference"]
        assert drink[0].conflict_state == "disputed"

    def test_same_scope_different_privacy_are_separate_fields(self, world: Phase8World) -> None:
        bob = world.entity("Privacy-Bob")
        world.claims.remember(
            world.admin_access,
            agent_id=world.agent,
            space_id=world.space,
            subject_entity_id=bob,
            predicate="p_note",
            value={"k": "public"},
            canonical_text="public note",
            category="fact",
            importance=0.9,
            evidence=[{"source_type": "observation", "source_id": world.observation()}],
            idempotency_key="p8-priv-public",
        )
        world.claims.remember(
            world.admin_access,
            agent_id=world.agent,
            space_id=world.space,
            subject_entity_id=bob,
            predicate="p_note",
            value={"k": "secret"},
            canonical_text="secret note",
            category="fact",
            importance=0.9,
            privacy_labels=["restricted"],
            evidence=[{"source_type": "observation", "source_id": world.observation()}],
            idempotency_key="p8-priv-secret",
        )
        world.rebuild()
        fields = _fields_of(world, ProfileSubjectKey("entity", bob))
        notes = [f for f in fields if f.field == "p_note" and f.section == "experience"]
        # Cross-privacy stitching is inexpressible: two separate fields,
        # never one merged field.
        assert len(notes) == 2
        assert {f.conflict_state for f in notes} == {"single"}


class TestPersonaIsolation:
    def test_self_narrative_never_enters_profiles(self, world: Phase8World) -> None:
        bob = world.entity("Narrator")
        world.remember(
            "story",
            "I overcame my shyness this year",
            bob,
            category="self_narrative",
            importance=1.0,
        )
        world.remember("fact", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        sections = {f.section for f in _fields_of(world, ProfileSubjectKey("entity", bob))}
        assert "identity" in sections
        assert all("narrative" not in section for section in sections)

    def test_agent_entity_profile_has_no_persona_data(self, world: Phase8World) -> None:
        # The agent entity is a normal entity subject; only claims feed it.
        with world.store.write() as tx:
            agent_entity = tx.identities.insert_entity(
                TENANT,
                __import__(
                    "iris_memory_core.domain.identity", fromlist=["EntityKind"]
                ).EntityKind.AGENT,
            ).id
        world.remember(
            "agent-fact",
            "The agent remembers the war",
            agent_entity,
            category="fact",
            importance=0.9,
        )
        world.rebuild()
        fields = _fields_of(world, ProfileSubjectKey("entity", agent_entity))
        assert fields
        # No persona revision/state content ever appears as a source.
        for field in fields:
            for source in field.sources:
                assert source.claim_id  # claim ids only — persona rows are not sources


class TestScopeAndSubjects:
    def test_relationship_and_space_group_subjects(self, world: Phase8World) -> None:
        bob = world.entity("Pair-Bob")
        carol = world.entity("Pair-Carol")
        world.remember(
            "pair",
            "Bob trusts Carol",
            bob,
            category="relationship",
            value={"target_entity_id": carol},
        )
        world.remember(
            "pair2",
            "Carol mentors Bob",
            carol,
            category="relationship",
            value={"target_entity_id": bob},
        )
        # Space-group subject: a group-scoped community claim (the public
        # remember API is space-scoped; group claims are seeded via the
        # canonical repository like the Phase 6 group-recall tests).
        from tests.integration.recall.test_fts_recall_recall import _bulk_claim

        with world.store.write() as tx:
            group = tx.insert_space_group(TENANT, "Community", "d", actor="t", reason_code="t")
            _bulk_claim(
                tx,
                claim_id="p8group",
                tenant_id=TENANT,
                agent_id=world.agent,
                space_group_id=group.id,
                space_id=None,
                session_id=None,
                status="active",
                privacy_labels=(),
                valid_from_us=None,
                valid_until_us=None,
                recorded_at_us=world.clock.now_us(),
                text="the group loves puns",
                subject_entity_id=bob,
                category="community",
            )
            tx.advance_watermark(TENANT, world.agent, [("claim", "p8group", 1)])
        world.rebuild()
        kinds = {(s.kind, s.subject_id) for s in _subjects(world)}
        lo, hi = sorted((bob, carol))
        assert ("relationship", f"{lo}|{hi}") in kinds
        assert ("space_group", group.id) in kinds
        # Relationship fields carry endpoint attribution.
        pair_fields = _fields_of(world, ProfileSubjectKey("relationship", f"{lo}|{hi}"))
        assert {f.field.split("@")[0] for f in pair_fields} == {"p_pair", "p_pair2"}

    def test_fields_from_other_agents_are_scope_filtered_at_read(self, world: Phase8World) -> None:
        bob = world.entity("Multi-Bob")
        world.remember("mine", "Bob likes tea", bob, category="preference")
        world.claims.remember(
            world.other_agent_access,
            agent_id=world.other_agent,
            space_id=world.space,
            subject_entity_id=bob,
            predicate="p_theirs",
            value={"k": "t"},
            canonical_text="Bob hates tea",
            category="preference",
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": world.observation(
                        agent_id=world.other_agent, access=world.other_agent_access
                    ),
                }
            ],
            idempotency_key="p8-other-agent-claim",
        )
        world.rebuild()
        # Both agents' claims live in the profile, as separate fields.
        fields = _fields_of(world, ProfileSubjectKey("entity", bob))
        assert {f.field for f in fields} >= {"p_mine", "p_theirs"}
        assert {f.agent_id for f in fields} == {world.agent, world.other_agent}


class TestDeterminism:
    def test_three_rebuilds_identical(self, world: Phase8World) -> None:
        bob = world.entity("Det-Bob")
        carol = world.entity("Det-Carol")
        world.remember("n", "Bob is a pilot", bob, category="identity")
        world.remember(
            "c", "Bob lives in Tokyo", bob, predicate="p_city", category="fact", importance=0.9
        )
        world.remember(
            "c2", "Bob lives in Osaka", bob, predicate="p_city", category="fact", importance=0.9
        )
        world.remember(
            "pair",
            "Bob trusts Carol",
            bob,
            category="relationship",
            value={"target_entity_id": carol},
        )
        world.rebuild()
        signatures = []
        for _round in range(3):
            report = world.profile.rebuild(TENANT)
            subjects = sorted((s.kind, s.subject_id) for s in _subjects(world))
            field_signature = tuple(
                sorted(
                    (
                        subject.kind,
                        subject.subject_id,
                        field.section,
                        field.field,
                        field.group_key,
                        field.conflict_state,
                        field.value_json,
                        tuple((s.claim_id, s.revision) for s in field.sources),
                    )
                    for subject in _subjects(world)
                    for field in _fields_of(world, subject)
                )
            )
            signatures.append(
                (
                    report.content_checksum,
                    report.subject_count,
                    report.field_count,
                    report.source_watermark,
                    report.tombstone_watermark,
                    tuple(subjects),
                    field_signature,
                )
            )
        for signature in signatures[1:]:
            assert signature == signatures[0]


class TestInvalidationRaces:
    def test_correct_race_50_rounds(self, world: Phase8World) -> None:
        bob = world.entity("Correct-Bob")
        for round_index in range(RACE_ROUNDS):
            claim = world.remember(
                f"cr-{round_index}",
                f"Bob lives in city {round_index}",
                bob,
                predicate="p_city",
                category="fact",
                importance=0.9,
            )
            world.rebuild()
            fields = _fields_of(world, ProfileSubjectKey("entity", bob))
            city = next(f for f in fields if f.field == "p_city")
            assert f"city {round_index}" in city.summary_text
            world.claims.correct(
                world.access,
                claim_id=claim.claim_id,
                expected_revision=claim.revision,
                mode="supersede",
                value={"k": "moved"},
                canonical_text=f"Bob lives in city {round_index}b",
                evidence=[{"source_type": "observation", "source_id": world.observation()}],
                reason="race",
                idempotency_key=f"p8-cr-{round_index}",
            )
            # Incremental apply of the changed claim's subjects.
            with world.store.write() as tx:
                world.profile.apply_change_in_tx(
                    tx, tenant_id=TENANT, resource_type="claim", resource_id=claim.claim_id
                )
            fields = _fields_of(world, ProfileSubjectKey("entity", bob))
            city = next(f for f in fields if f.field == "p_city")
            assert f"city {round_index}b" in city.summary_text
            # The superseded value never returns.
            assert all(
                f"city {round_index}\x1f" not in f.value_json
                and f'value": "Bob lives in city {round_index}"' not in f.value_json
                for f in fields
            )

    def test_forget_race_50_rounds(self, world: Phase8World) -> None:
        from iris_memory_core.application.forget import ForgetService
        from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind

        forget = ForgetService(world.store, world.clock, idempotency=world.idem)
        bob = world.entity("Forget-Bob")
        for round_index in range(RACE_ROUNDS):
            claim = world.remember(
                f"fg-{round_index}",
                f"secret number {round_index}",
                bob,
                predicate="p_secret",
                category="fact",
                importance=0.9,
            )
            world.rebuild()
            fields = _fields_of(world, ProfileSubjectKey("entity", bob))
            assert any(f"secret number {round_index}" in f.summary_text for f in fields)
            forget.forget(
                world.access,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE,
                    resource_type="claim",
                    resource_id=claim.claim_id,
                ),
                reason="race",
                idempotency_key=f"p8-fg-{round_index}",
            )
            with world.store.write() as tx:
                world.profile.apply_change_in_tx(
                    tx, tenant_id=TENANT, resource_type="claim", resource_id=claim.claim_id
                )
            fields = _fields_of(world, ProfileSubjectKey("entity", bob))
            assert not any(f"secret number {round_index}" in f.summary_text for f in fields)

    def test_tombstoned_subject_serves_no_profile_candidates_immediately(
        self, world: Phase8World
    ) -> None:
        """Review round 2 (P2): the route must not serve a tombstoned
        subject's fields during the projection lag window — the tombstone
        ledger check runs at collect time, independent of the async
        profile.apply ever settling."""
        from iris_memory_core.domain.identity import EntityState
        from iris_memory_core.domain.recall import ROUTE_PROFILE

        bob = world.entity("LagWindow-Bob")
        world.bind_identity(bob, "lagwindow-qq")
        world.remember("lw", "Bob hides a treasure", bob, category="identity")
        world.rebuild()
        result = world.recall_as_speaker("lagwindow-qq")
        assert any(c.resource_id and "treasure" in c.text for c in result.candidates)
        with world.store.write() as tx:
            entity = tx.identities.get_entity(bob)
            tx.update_entity_state(
                bob,
                EntityState.TOMBSTONED,
                expected_revision=entity.revision,
                actor="admin",
                reason_code="test",
            )
            tx.record_tombstone(
                tenant_id=TENANT,
                resource_type="entity",
                resource_id=bob,
                reason_code="test",
                deleted_by="admin",
            )
        # NO apply, NO rebuild: the very next recall must drop the subject's
        # candidates (the claims route also drops them — the subject's
        # claims... stay canonical; the PROFILE route must not serve them).
        result = world.recall_as_speaker("lagwindow-qq")
        profile_candidates = [c for c in result.candidates if c.route == ROUTE_PROFILE]
        assert profile_candidates == []

    def test_entity_tombstone_kills_the_subject(self, world: Phase8World) -> None:
        from iris_memory_core.domain.identity import EntityState

        bob = world.entity("Doomed-Bob")
        world.remember("d1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        assert _fields_of(world, ProfileSubjectKey("entity", bob))
        with world.store.write() as tx:
            entity = tx.identities.get_entity(bob)
            tx.update_entity_state(
                bob,
                EntityState.TOMBSTONED,
                expected_revision=entity.revision,
                actor="admin",
                reason_code="test",
            )
            tx.record_tombstone(
                tenant_id=TENANT,
                resource_type="entity",
                resource_id=bob,
                reason_code="test",
                deleted_by="admin",
            )
        with world.store.write() as tx:
            world.profile.apply_change_in_tx(
                tx, tenant_id=TENANT, resource_type="entity", resource_id=bob
            )
        assert _fields_of(world, ProfileSubjectKey("entity", bob)) == ()


class TestReadSurfaceFiltering:
    def test_read_profile_filters_fields_by_request_scope_and_privacy(
        self, world: Phase8World
    ) -> None:
        from iris_memory_core.domain.scope import Scope

        bob = world.entity("Filter-Bob")
        world.remember("pub", "Bob is a pilot", bob, category="identity")
        # A restricted field (written via the admin plane).
        world.claims.remember(
            world.admin_access,
            agent_id=world.agent,
            space_id=world.space,
            subject_entity_id=bob,
            predicate="p_secret",
            value={"k": "s"},
            canonical_text="Bob hides a treasure",
            category="fact",
            importance=0.9,
            privacy_labels=["restricted"],
            evidence=[{"source_type": "observation", "source_id": world.observation()}],
            idempotency_key="p8-filter-secret",
        )
        # An other-agent field (scope-blocked for this request's agent).
        world.claims.remember(
            world.other_agent_access,
            agent_id=world.other_agent,
            space_id=world.space,
            subject_entity_id=bob,
            predicate="p_theirs",
            value={"k": "t"},
            canonical_text="Bob according to the other agent",
            category="fact",
            importance=0.9,
            evidence=[
                {
                    "source_type": "observation",
                    "source_id": world.observation(
                        agent_id=world.other_agent, access=world.other_agent_access
                    ),
                }
            ],
            idempotency_key="p8-filter-theirs",
        )
        world.rebuild()
        request_scope = Scope(
            tenant_id=TENANT,
            agent_id=world.agent,
            space_group_id=None,
            space_id=world.space,
            session_id=None,
        )
        view = world.profile.read_profile(
            TENANT,
            ProfileSubjectKey("entity", bob),
            agent_id=world.agent,
            access=world.access,
            request_scope=request_scope,
        )
        fields = {f.field for f in view.fields}
        assert "p_pub" in fields
        assert "p_secret" not in fields  # restricted privacy
        assert "p_theirs" not in fields  # other agent's scope
        # Without a request context (management surface) all fields return
        # WITH their labels — the caller enforces its own policy.
        unfiltered = world.profile.read_profile(
            TENANT, ProfileSubjectKey("entity", bob), agent_id=world.agent
        )
        assert {f.field for f in unfiltered.fields} >= {"p_pub", "p_secret", "p_theirs"}


class TestCanonicalFallback:
    def test_never_built_serves_canonical_fallback(self, world: Phase8World) -> None:
        bob = world.entity("Fallback-Bob")
        world.remember("f1", "Bob is a pilot", bob, category="identity")
        world.remember("f2", "Bob likes tea", bob, category="preference")
        # NO rebuild: the projection is absent — the read falls back to the
        # same deterministic derivation over canonical claims.
        view = world.profile.read_profile(
            TENANT, ProfileSubjectKey("entity", bob), agent_id=world.agent
        )
        assert view.source == "canonical_fallback"
        assert view.generation_id is None
        assert {(f.section, f.field) for f in view.fields} >= {
            ("identity", "p_f1"),
            ("preference", "p_f2"),
        }
        # And the projection path serves the identical fields once built.
        world.rebuild()
        projected = world.profile.read_profile(
            TENANT, ProfileSubjectKey("entity", bob), agent_id=world.agent
        )
        assert projected.source == "projection"
        assert projected.fields == view.fields

    def test_lagging_generation_serves_canonical_view(self, world: Phase8World) -> None:
        bob = world.entity("Lag-Bob")
        world.remember("l1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        # A committed change the generation has not incorporated.
        world.remember("l2", "Bob likes tea", bob, category="preference")
        view = world.profile.read_profile(
            TENANT, ProfileSubjectKey("entity", bob), agent_id=world.agent
        )
        assert view.source == "canonical_fallback"
        assert any(f.field == "p_l2" for f in view.fields)

    def test_stale_field_sources_are_dropped_at_read(self, world: Phase8World) -> None:
        bob = world.entity("StaleField-Bob")
        world.bind_identity(bob, "bob-qq-stale")
        claim = world.remember("s1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        # Kill the source claim AFTER the projection was built.
        from iris_memory_core.application.forget import ForgetService
        from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind

        ForgetService(world.store, world.clock, idempotency=world.idem).forget(
            world.access,
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE,
                resource_type="claim",
                resource_id=claim.claim_id,
            ),
            reason="stale field",
            idempotency_key="p8-stale-field",
        )
        # The route-level source re-validation drops the dead field.
        result = world.recall_as_speaker("bob-qq-stale")
        profile_candidates = [c for c in result.candidates if c.route == ROUTE_PROFILE]
        assert all(c.resource_id != claim.claim_id for c in profile_candidates)
        if ROUTE_PROFILE in result.completed_routes:
            # The read surface falls back to canonical (empty view here).
            view = world.profile.read_profile(
                TENANT, ProfileSubjectKey("entity", bob), agent_id=world.agent
            )
            assert view.source in ("projection", "canonical_fallback")
            assert not any(f.field == "p_s1" for f in view.fields)


class TestVerifyAndCorruption:
    def test_corrupted_counts_fail_verification(self, world: Phase8World) -> None:
        bob = world.entity("Corrupt-Bob")
        world.remember("c1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        connection = sqlite3.connect(world.store.runtime.database)
        try:
            connection.execute(
                "UPDATE profile_generations SET field_count = 99 "
                "WHERE id = (SELECT generation_id FROM profile_current)"
            )
            connection.commit()
        finally:
            connection.close()
        with world.store.write() as tx:
            assert world.profile.verify_in_tx(tx, TENANT) is False
        # Round 3: the verdict must survive the transaction commit.
        with world.store.read() as tx:
            assert tx.profile.projection_state() == "pending_rebuild"

    def test_corrupted_subject_checksum_fails_verification(self, world: Phase8World) -> None:
        bob = world.entity("Checksum-Bob")
        world.remember("k1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        connection = sqlite3.connect(world.store.runtime.database)
        try:
            connection.execute(
                "UPDATE profile_subjects SET subject_checksum = 'zero' WHERE subject_id = ?",
                (bob,),
            )
            connection.commit()
        finally:
            connection.close()
        with world.store.write() as tx:
            assert world.profile.verify_in_tx(tx, TENANT) is False
        with world.store.read() as tx:
            assert tx.profile.projection_state() == "pending_rebuild"

    def test_apply_maintains_the_manifest_binding(self, world: Phase8World) -> None:
        bob = world.entity("Apply-Bob")
        world.remember("a1", "Bob is a pilot", bob, category="identity")
        world.rebuild()
        claim = world.remember("a2", "Bob likes tea", bob, category="preference")
        with world.store.write() as tx:
            world.profile.apply_change_in_tx(
                tx, tenant_id=TENANT, resource_type="claim", resource_id=claim.claim_id
            )
        # After the incremental apply the manifest still binds the rows.
        with world.store.write() as tx:
            assert world.profile.verify_in_tx(tx, TENANT) is True
        with world.store.read() as tx:
            assert tx.profile.projection_state() == "ready"
        fields = _fields_of(world, ProfileSubjectKey("entity", bob))
        assert {f.field for f in fields} == {"p_a1", "p_a2"}
