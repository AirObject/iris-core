"""Phase 8 domain rule unit tests (ADR-0016 §2-§6).

Pure-logic coverage: the deterministic section mapping, conflict retention,
group keys, structural claim-edge targets, deterministic digests, traversal
budget constants and the degraded-reason retryable defaults.
"""

from __future__ import annotations

import pytest

from iris_memory_core.domain.graph import (
    GRAPH_MAX_DEPTH,
    GRAPH_MAX_FANOUT,
    GRAPH_MAX_NODES,
    GRAPH_NON_RETRYABLE_REASONS,
    GRAPH_REASON_AS_OF_UNSUPPORTED,
    GRAPH_REASON_BUILDER_UNKNOWN,
    GRAPH_REASON_GENERATION_STALE,
    GRAPH_REASON_INDEX_CORRUPT,
    GRAPH_REASON_REBUILD_PENDING,
    GraphDegradedError,
    GraphEdgeDraft,
    extract_claim_edge_target,
    graph_edge_id,
    graph_generation_checksum,
    validate_edge_type,
)
from iris_memory_core.domain.profile import (
    PROFILE_NON_RETRYABLE_REASONS,
    PROFILE_REASON_AS_OF_UNSUPPORTED,
    PROFILE_REASON_BUILDER_UNKNOWN,
    PROFILE_REASON_INDEX_CORRUPT,
    ProfileDegradedError,
    ProfileFieldSource,
    compute_field_conflict_state,
    field_group_key,
    profile_section_for_claim,
    relationship_subject_id,
    render_field_value,
)


class TestSectionMapping:
    def test_entity_sections_cover_the_seven_partitions(self) -> None:
        assert (
            profile_section_for_claim(
                category="identity", predicate="x", importance=0.1, subject_kind="entity"
            )
            == "identity"
        )
        assert (
            profile_section_for_claim(
                category="preference", predicate="x", importance=0.1, subject_kind="entity"
            )
            == "preference"
        )
        assert (
            profile_section_for_claim(
                category="relationship", predicate="x", importance=0.1, subject_kind="entity"
            )
            == "relationship"
        )
        assert (
            profile_section_for_claim(
                category="procedure", predicate="x", importance=0.1, subject_kind="entity"
            )
            == "interaction"
        )

    def test_experience_requires_importance_threshold(self) -> None:
        assert (
            profile_section_for_claim(
                category="fact", predicate="visited", importance=0.5, subject_kind="entity"
            )
            == "experience"
        )
        assert (
            profile_section_for_claim(
                category="fact", predicate="visited", importance=0.49, subject_kind="entity"
            )
            is None
        )

    def test_goal_predicate_stems(self) -> None:
        for predicate in ("goal_run_marathon", "plans_to_move", "wants_a_dog", "aims_high"):
            assert (
                profile_section_for_claim(
                    category="fact", predicate=predicate, importance=0.1, subject_kind="entity"
                )
                == "goal"
            )
        assert (
            profile_section_for_claim(
                category="fact", predicate="random_fact", importance=0.9, subject_kind="entity"
            )
            == "experience"
        )

    def test_self_narrative_never_enters_profiles(self) -> None:
        assert (
            profile_section_for_claim(
                category="self_narrative", predicate="x", importance=1.0, subject_kind="entity"
            )
            is None
        )

    def test_space_group_subjects_use_community_and_fact_sections(self) -> None:
        assert (
            profile_section_for_claim(
                category="community", predicate="x", importance=0.1, subject_kind="space_group"
            )
            == "community"
        )
        assert (
            profile_section_for_claim(
                category="fact", predicate="x", importance=0.1, subject_kind="space_group"
            )
            == "fact"
        )
        assert (
            profile_section_for_claim(
                category="identity", predicate="x", importance=0.1, subject_kind="space_group"
            )
            is None
        )


class TestConflictSemantics:
    def test_conflicting_values_are_retained_not_merged(self) -> None:
        sources = (
            ProfileFieldSource(claim_id="a", revision=1, value_hash="h1"),
            ProfileFieldSource(claim_id="b", revision=1, value_hash="h2"),
        )
        assert compute_field_conflict_state(sources, any_disputed=False) == "conflict"

    def test_same_value_hashes_stay_single(self) -> None:
        sources = (
            ProfileFieldSource(claim_id="a", revision=1, value_hash="h1"),
            ProfileFieldSource(claim_id="b", revision=1, value_hash="h1"),
        )
        assert compute_field_conflict_state(sources, any_disputed=False) == "single"

    def test_any_disputed_wins(self) -> None:
        sources = (ProfileFieldSource(claim_id="a", revision=1, value_hash="h1"),)
        assert compute_field_conflict_state(sources, any_disputed=True) == "disputed"

    def test_render_lists_every_conflicting_value(self) -> None:
        value_json, summary = render_field_value(
            (("claim-b", "Tokyo", "active"), ("claim-a", "Osaka", "active"))
        )
        assert "Tokyo" in value_json and "Osaka" in value_json
        assert "Tokyo" in summary and "Osaka" in summary
        single_json, single_summary = render_field_value((("c", "Only", "active"),))
        assert "Only" in single_json
        assert single_summary == "Only"


class TestGroupKeys:
    def test_group_key_isolates_scope_and_privacy(self) -> None:
        base = field_group_key("scope|a", ("tenant",))
        assert base != field_group_key("scope|b", ("tenant",))
        assert base != field_group_key("scope|a", ("tenant", "restricted"))

    def test_relationship_subject_id_is_order_independent(self) -> None:
        assert relationship_subject_id("b", "a") == relationship_subject_id("a", "b")
        assert relationship_subject_id("a", "b") == "a|b"


class TestClaimEdgeTarget:
    def test_structural_target_extraction(self) -> None:
        assert extract_claim_edge_target('{"target_entity_id": "e-1"}') == "e-1"
        assert extract_claim_edge_target('{"target_entity_id":"e-2"}') == "e-2"

    def test_non_structural_values_never_produce_targets(self) -> None:
        assert extract_claim_edge_target("Bob likes Alice") is None
        assert extract_claim_edge_target('{"note": "Alice is nice"}') is None
        assert extract_claim_edge_target('{"target_entity_id": ""}') is None
        assert extract_claim_edge_target("not json") is None
        assert extract_claim_edge_target('["target_entity_id", "x"]') is None

    def test_edge_type_bounds(self) -> None:
        from iris_memory_core.domain.errors import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            validate_edge_type("")
        with pytest.raises(InvalidRequestError):
            validate_edge_type("x" * 201)


def _draft(**overrides: object) -> GraphEdgeDraft:
    payload: dict[str, object] = {
        "edge_kind": "claim",
        "edge_type": "p_friend",
        "source_node_id": "a",
        "source_node_kind": "entity",
        "target_node_id": "b",
        "target_node_kind": "entity",
        "resource_type": "claim",
        "resource_id": "r1",
        "resource_revision": 1,
        "agent_id": "ag",
        "space_group_id": None,
        "space_id": "sp",
        "session_id": None,
        "privacy_labels": ("tenant",),
        "status": "active",
        "confidence": 0.5,
        "importance": 0.5,
        "valid_from_us": None,
        "valid_until_us": None,
        "content_hash": "h",
    }
    payload.update(overrides)
    return GraphEdgeDraft(**payload)  # type: ignore[arg-type]


class TestEdgeIdentity:
    def test_same_content_same_id(self) -> None:
        assert graph_edge_id(_draft()) == graph_edge_id(_draft())

    def test_revision_change_changes_id(self) -> None:
        assert graph_edge_id(_draft()) != graph_edge_id(_draft(resource_revision=2))

    def test_checksum_is_deterministic_and_content_bound(self) -> None:
        edges = (_draft(), _draft(edge_kind="relation", resource_type="relation"))
        count_a, nodes_a, checksum_a = graph_generation_checksum(edges)
        count_b, nodes_b, checksum_b = graph_generation_checksum(
            (_draft(), _draft(edge_kind="relation", resource_type="relation"))
        )
        assert (count_a, nodes_a, checksum_a) == (count_b, nodes_b, checksum_b)
        _count_c, _nodes_c, checksum_c = graph_generation_checksum((_draft(resource_revision=3),))
        assert checksum_a != checksum_c

    def test_binding_edge_may_be_agentless(self) -> None:
        draft = _draft(
            edge_kind="binding",
            edge_type="verified_binding",
            source_node_kind="external_identity",
            resource_type="binding",
            agent_id=None,
        )
        assert draft.edge_kind == "binding"


class TestDegradedReasons:
    @pytest.mark.parametrize(
        ("reason", "retryable"),
        [
            (GRAPH_REASON_REBUILD_PENDING, True),
            (GRAPH_REASON_GENERATION_STALE, True),
            (GRAPH_REASON_INDEX_CORRUPT, True),
            (GRAPH_REASON_BUILDER_UNKNOWN, False),
            (GRAPH_REASON_AS_OF_UNSUPPORTED, False),
        ],
    )
    def test_graph_reason_retryable_defaults(self, reason: str, retryable: bool) -> None:
        error = GraphDegradedError(reason)
        assert error.reason_code == reason
        assert error.retryable is retryable
        assert (reason not in GRAPH_NON_RETRYABLE_REASONS) is retryable

    @pytest.mark.parametrize(
        ("reason", "retryable"),
        [
            (PROFILE_REASON_INDEX_CORRUPT, True),
            (PROFILE_REASON_BUILDER_UNKNOWN, False),
            (PROFILE_REASON_AS_OF_UNSUPPORTED, False),
        ],
    )
    def test_profile_reason_retryable_defaults(self, reason: str, retryable: bool) -> None:
        assert ProfileDegradedError(reason).retryable is retryable
        assert (reason not in PROFILE_NON_RETRYABLE_REASONS) is retryable


class TestBudgets:
    def test_budget_constants_are_bounded_and_sane(self) -> None:
        assert 1 <= GRAPH_MAX_DEPTH <= 4
        assert 1 <= GRAPH_MAX_FANOUT <= 64
        assert GRAPH_MAX_NODES >= GRAPH_MAX_FANOUT
