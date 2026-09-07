"""Phase 7 domain rule tests: templates, space identity, surrogate bounds,
vector validation and deterministic checksum material (ADR-0015 §2-5)."""

from __future__ import annotations

import math
from typing import Any

import pytest

from iris_memory_core.domain.vector import (
    SURROGATE_ID_MAX,
    VECTOR_INDEXABLE_RESOURCE_TYPES,
    EmbeddingProviderError,
    VectorEntryInput,
    VectorIdMapRecord,
    VectorSpaceConfig,
    builder_versions_trusted,
    embedding_input_digest,
    generation_content_hash,
    id_map_snapshot_hash,
    normalize_vector,
    render_embedding_input,
    render_query_input,
    validate_surrogate_id,
    validate_vector,
    vector_space_matches,
)


def _entry(
    resource_id: str = "r1", revision: int = 1, content_hash: str = "h1"
) -> VectorEntryInput:
    return VectorEntryInput(
        tenant_id="t1",
        resource_type="claim",
        resource_id=resource_id,
        resource_revision=revision,
        agent_id="a1",
        space_group_id=None,
        space_id=None,
        session_id=None,
        content_hash=content_hash,
        occurred_us=1,
        embedding_input="p: text",
    )


class TestTemplates:
    def test_claim_template_renders_minimal_text(self) -> None:
        assert render_embedding_input("claim", predicate="likes", canonical_text="tea") == (
            "likes: tea"
        )

    def test_episode_and_note_templates(self) -> None:
        assert render_embedding_input("episode", title="trip", summary="beach") == "trip\nbeach"
        assert render_embedding_input("note", title="todo", body="buy milk") == "todo\nbuy milk"

    def test_unknown_resource_type_rejected(self) -> None:
        from iris_memory_core.domain.errors import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            render_embedding_input("artifact", body="x")

    def test_query_input_normalizes_and_truncates(self) -> None:
        assert render_query_input("  Hello   WORLD \n") == "Hello WORLD"
        assert len(render_query_input("x" * 100, max_chars=10)) == 10

    def test_non_indexable_resource_rejected(self) -> None:
        from iris_memory_core.domain.errors import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            VectorEntryInput(
                tenant_id="t1",
                resource_type="observation",
                resource_id="o1",
                resource_revision=1,
                agent_id="a1",
                space_group_id=None,
                space_id=None,
                session_id=None,
                content_hash="h",
                occurred_us=1,
                embedding_input="text",
            )
        assert "observation" not in VECTOR_INDEXABLE_RESOURCE_TYPES


class TestVectorSpaceConfig:
    def test_metric_and_normalization_are_frozen(self) -> None:
        from iris_memory_core.domain.errors import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            VectorSpaceConfig(model="m", dimension=8, metric="l2")
        with pytest.raises(InvalidRequestError):
            VectorSpaceConfig(model="m", dimension=8, normalization="none")

    def test_dimension_bounds_enforced(self) -> None:
        from iris_memory_core.domain.errors import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            VectorSpaceConfig(model="m", dimension=0)
        with pytest.raises(InvalidRequestError):
            VectorSpaceConfig(model="m", dimension=70_000)

    def test_exact_match_rule(self) -> None:
        base = VectorSpaceConfig(model="m", dimension=8)
        assert vector_space_matches(
            base,
            model="m",
            dimension=8,
            metric="cosine",
            normalization="l2",
            template_version=1,
            builder_version=1,
        )
        assert not vector_space_matches(
            base,
            model="m2",
            dimension=8,
            metric="cosine",
            normalization="l2",
            template_version=1,
            builder_version=1,
        )
        assert not vector_space_matches(
            base,
            model="m",
            dimension=16,
            metric="cosine",
            normalization="l2",
            template_version=1,
            builder_version=1,
        )
        assert not vector_space_matches(
            base,
            model="m",
            dimension=8,
            metric="cosine",
            normalization="l2",
            template_version=2,
            builder_version=1,
        )

    def test_unknown_builder_versions_untrusted(self) -> None:
        assert builder_versions_trusted(1, 1)
        assert not builder_versions_trusted(2, 1)
        assert not builder_versions_trusted(1, 2)


class TestSurrogateRules:
    def test_bounds(self) -> None:
        assert validate_surrogate_id(1) == 1
        assert validate_surrogate_id(SURROGATE_ID_MAX) == SURROGATE_ID_MAX
        from iris_memory_core.domain.errors import ConflictError

        for bad in (0, -1, SURROGATE_ID_MAX + 1, 2**63 - 1, True, 1.5):
            with pytest.raises(ConflictError):
                validate_surrogate_id(bad)  # type: ignore[arg-type]

    def test_id_map_record_requires_invalidation_stamp(self) -> None:
        from iris_memory_core.domain.errors import ConflictError

        with pytest.raises(ConflictError):
            VectorIdMapRecord(
                tenant_id="t1",
                resource_type="claim",
                resource_id="r1",
                resource_revision=1,
                surrogate_id=1,
                agent_id="a1",
                model="m",
                dimension=8,
                content_hash="h",
                status="invalid",
                created_us=1,
                invalidated_us=None,
            )


class TestVectorValidation:
    def test_valid_normalized_vector_passes(self) -> None:
        vector = normalize_vector([1.0, 2.0, 2.0])
        validate_vector(vector, dimension=3)
        assert abs(math.sqrt(sum(v * v for v in vector)) - 1.0) < 1e-12

    def test_rejects_wrong_dimension(self) -> None:
        with pytest.raises(EmbeddingProviderError) as error:
            validate_vector([1.0, 0.0], dimension=3)
        assert error.value.reason_code == "embedding_dimension_mismatch"

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite(self, bad: float) -> None:
        with pytest.raises(EmbeddingProviderError) as error:
            validate_vector([bad, 1.0], dimension=2)
        assert error.value.reason_code == "embedding_not_finite"

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(EmbeddingProviderError) as error:
            validate_vector(["x", 1.0], dimension=2)  # type: ignore[list-item]
        assert error.value.reason_code == "embedding_not_numeric"

    def test_rejects_unnormalized(self) -> None:
        with pytest.raises(EmbeddingProviderError) as error:
            validate_vector([0.5, 0.5], dimension=2)
        assert error.value.reason_code == "embedding_not_normalized"

    def test_rejects_zero_vector(self) -> None:
        with pytest.raises(EmbeddingProviderError) as error:
            validate_vector([0.0, 0.0], dimension=2)
        assert error.value.reason_code == "embedding_empty"


class TestDeterministicMaterial:
    def test_snapshot_and_content_hashes_order_independent(self) -> None:
        a = [(_entry("r1", 1, "h1"), 10), (_entry("r2", 2, "h2"), 20)]
        b = list(reversed(a))
        assert id_map_snapshot_hash(a) == id_map_snapshot_hash(b)
        assert generation_content_hash(a) == generation_content_hash(b)

    def test_content_hash_binds_revision(self) -> None:
        base = [(_entry("r1", 1, "h1"), 10)]
        changed = [(_entry("r1", 2, "h1"), 10)]
        assert generation_content_hash(base) != generation_content_hash(changed)

    def test_content_hash_binds_canonical_content_hash(self) -> None:
        base = [(_entry("r1", 1, "h1"), 10)]
        changed = [(_entry("r1", 1, "h9"), 10)]
        assert generation_content_hash(base) != generation_content_hash(changed)

    def test_digest_is_low_sensitivity(self) -> None:
        digest = embedding_input_digest("secret user content")
        assert len(digest) == 16
        assert int(digest, 16) >= 0
        assert "secret" not in digest


class TestRankerV3Dedupe:
    """[P1 hybrid dedupe review finding] v3 keeps exactly ONE instance per
    canonical resource (best stable-order score) BEFORE budgeting, so a
    multi-route resource occupies one budget slot; distinct disputing
    resources (conflict groups) are never merged."""

    @staticmethod
    def _candidate(
        candidate_id: str,
        route: str,
        resource_id: str,
        *,
        resource_type: str = "claim",
        relevance: float | None = 0.5,
        importance: float | None = 0.5,
        conflict_group: str | None = None,
        content_hash: str = "",
        token_estimate: int = 10,
    ) -> Any:
        from iris_memory_core.domain.recall import ScoredCandidate

        return ScoredCandidate(
            candidate_id=candidate_id,
            route=route,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_revision=1,
            subject_entity_id=None,
            category=route,
            content_hash=content_hash or f"h-{resource_id}",
            occurred_us=1,
            token_estimate=token_estimate,
            scores={"relevance": relevance, "importance": importance},
            conflict_group=conflict_group,
        )

    def test_same_resource_across_routes_collapses_to_one_best_instance(self) -> None:
        from iris_memory_core.domain.recall import (
            RECALL_RANKER_V3,
            ROUTE_CLAIMS,
            ROUTE_FTS,
            ROUTE_VECTOR,
            RankerV3,
        )

        candidates = [
            self._candidate("c-claims", ROUTE_CLAIMS, "r1", relevance=0.9),
            self._candidate("c-fts", ROUTE_FTS, "r1", relevance=0.4),
            self._candidate("c-vector", ROUTE_VECTOR, "r1", relevance=0.99),
            self._candidate("c-other", ROUTE_CLAIMS, "r2", relevance=0.1),
        ]
        ranked = RankerV3().score(candidates)
        assert RankerV3.version == RECALL_RANKER_V3
        assert [c.candidate_id for c in ranked if c.resource_id == "r1"] == ["c-vector"]
        assert [c.candidate_id for c in ranked if c.resource_id == "r2"] == ["c-other"]

    def test_duplicates_consume_one_budget_slot(self) -> None:
        from iris_memory_core.domain.recall import (
            ROUTE_CLAIMS,
            ROUTE_FTS,
            ROUTE_VECTOR,
            RankerV3,
            apply_token_budgets,
        )

        candidates = [
            self._candidate("c-claims", ROUTE_CLAIMS, "r1", relevance=0.9, token_estimate=6),
            self._candidate("c-fts", ROUTE_FTS, "r1", relevance=0.8, token_estimate=6),
            self._candidate("c-vector", ROUTE_VECTOR, "r1", relevance=0.7, token_estimate=6),
        ]
        ranked = RankerV3().score(candidates)
        # A 7-token budget admits the single deduped instance (6 tokens);
        # under v2 semantics the three duplicates would each consume 6.
        outcome = apply_token_budgets(ranked, token_budget=7, layer_budgets={})
        assert [c.candidate_id for c in outcome.kept] == ["c-claims"]
        assert outcome.token_total == 6

    def test_distinct_disputing_resources_are_never_merged(self) -> None:
        from iris_memory_core.domain.recall import ROUTE_CLAIMS, RankerV3

        candidates = [
            self._candidate("c-a", ROUTE_CLAIMS, "r1", conflict_group="ent:predicate"),
            self._candidate("c-b", ROUTE_CLAIMS, "r2", conflict_group="ent:predicate"),
        ]
        ranked = RankerV3().score(candidates)
        assert {c.candidate_id for c in ranked} == {"c-a", "c-b"}
        # The conflict marking from v2 survives on both distinct resources.
        assert all(c.conflict_state == "conflicts" for c in ranked)

    def test_dedupe_is_deterministic_across_permutations(self) -> None:
        from iris_memory_core.domain.recall import RankerV3

        base = [
            self._candidate("c-claims", "claims", "r1", relevance=0.9),
            self._candidate("c-fts", "fts", "r1", relevance=0.9),
            self._candidate("c-other", "claims", "r2", relevance=0.2),
        ]
        direct = RankerV3().score(base)
        shuffled = RankerV3().score(list(reversed(base)))
        assert [c.candidate_id for c in direct] == [c.candidate_id for c in shuffled]
        # Equal scores: the stable order (category priority) breaks the tie,
        # never input order.
        assert [c.candidate_id for c in direct if c.resource_id == "r1"] == ["c-claims"]

    def test_v2_semantics_unchanged_for_duplicates(self) -> None:
        """V2 (Phase 6, ADR-0014 §5) still marks-and-penalizes duplicates —
        the dedupe is a v3 hybrid rule only."""
        from iris_memory_core.domain.recall import RankerV2

        candidates = [
            self._candidate("c-claims", "claims", "r1", relevance=0.9),
            self._candidate("c-fts", "fts", "r1", relevance=0.9),
        ]
        ranked = RankerV2().score(candidates)
        assert [c.candidate_id for c in ranked] == ["c-claims", "c-fts"]
