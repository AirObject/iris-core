"""Recall fusion domain rules: versioned deterministic scoring, budgets (§18.6).

Pure logic only. Phase 6 upgrades the Phase 3 ranker to v2 (ADR-0014 §5):

- fixed, versioned component weights;
- MISSING score components are not zero — they are excluded from the
  weighted mean and surfaced separately (``missing ≠ 0``);
- deterministic conflict/redundancy marking with penalties;
- token budget trimming that guarantees due tasks, current focus and the
  current speaker's identity claims before spending anything else.

The stable sort key stays ``(-final_score, category_priority,
occurred_at DESC, resource_id ASC)`` (§18.6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

#: Bump when weights, missing-value semantics, conflict marking or budget
#: logic change. Ranker v1 was the Phase 3/4 skeleton (ADR-0011 §4); v2
#: added missing-score semantics, conflict marking and protected budgets
#: (ADR-0014 §5); v3 adds the ``vector`` route to the fusion set with the
#: frozen hybrid tie-breaker (ADR-0015 §6) — existing candidates rank
#: identically under v2 and v3.
RECALL_RANKER_V2 = 2
RECALL_RANKER_V3 = 3

#: Version of the char/4 token estimator used for budget math.
TOKEN_ESTIMATOR_VERSION = 1

#: Fixed component weights (sum == 1.0; penalties apply at marking time).
SCORE_WEIGHTS: Mapping[str, float] = {
    "relevance": 0.28,
    "authority": 0.12,
    "confidence": 0.12,
    "importance": 0.14,
    "accessibility": 0.08,
    "activation": 0.06,
    "recency": 0.12,
    "task_urgency": 0.08,
}

CONFLICT_PENALTY = 0.15
REDUNDANCY_PENALTY = 0.10

#: Wire route names frozen by ADR-0014 §3 (internal names win over the
#: baseline's example spellings; ``tasks`` keeps its ADR-0011/0012 identity).
#: Phase 7 adds ``vector`` (ADR-0015 §6, baseline example spelling); Phase 8
#: adds ``graph`` and ``profile`` (ADR-0016 §4-5, reserved spellings).
ROUTE_TASKS = "tasks"
ROUTE_FOCUS = "focus"
ROUTE_CLAIMS = "claims"
ROUTE_RECENT = "recent_context"
ROUTE_RELATIONS = "relations"
ROUTE_STATE = "state"
ROUTE_FTS = "fts"
ROUTE_VECTOR = "vector"
ROUTE_GRAPH = "graph"
ROUTE_PROFILE = "profile"

#: Category priority for the stable tie-breaker: prospective memory first,
#: then focus, then long-term structured memory, then hot context, then
#: relations, state, FTS matches and finally vector matches — semantic
#: recall supplements, never displaces, the deterministic routes.
CATEGORY_PRIORITY: Mapping[str, int] = {
    ROUTE_TASKS: 0,
    ROUTE_FOCUS: 1,
    ROUTE_CLAIMS: 2,
    ROUTE_RECENT: 3,
    ROUTE_RELATIONS: 4,
    ROUTE_STATE: 5,
    ROUTE_FTS: 6,
    ROUTE_VECTOR: 7,
    ROUTE_GRAPH: 8,
    ROUTE_PROFILE: 9,
}

#: Routes whose candidates are guaranteed by the budget pass (§18.6):
#: Persona metadata is top-level (never a candidate); due tasks and current
#: focus fill these protected layers.
PROTECTED_ROUTES = frozenset({ROUTE_TASKS, ROUTE_FOCUS})

#: Claim categories that constitute the speaker's necessary identity.
IDENTITY_CLAIM_CATEGORIES = frozenset({"identity"})

#: All wire route names Phase 6 may report in completed_routes.
ALL_ROUTES = frozenset(CATEGORY_PRIORITY)


class ScoreValueError(ValueError):
    """Raised when a score component is not a number in [0, 1] or None."""


def normalize_component(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoreValueError(f"score component {name!r} must be a number or None")
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise ScoreValueError(f"score component {name!r} must be within [0, 1]")
    return numeric


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """The ranker's view of one candidate (route-agnostic).

    ``scores`` values are ``float | None``: a missing component (absent key
    or explicit None) is EXCLUDED from the weighted mean and reported in
    ``missing_components``; ``0.0`` is a real score of zero. Routes that can
    name a dispute group (same subject + predicate) set ``conflict_group``.
    """

    candidate_id: str
    route: str
    resource_type: str
    resource_id: str
    resource_revision: int
    subject_entity_id: str | None
    category: str
    content_hash: str
    occurred_us: int
    token_estimate: int
    scores: Mapping[str, float | None]
    conflict_group: str | None = None
    final_score: float = 0.0
    conflict_state: str | None = None
    missing_components: tuple[str, ...] = ()


def compute_final_score(
    scores: Mapping[str, float | None],
) -> tuple[float, tuple[str, ...]]:
    """Weighted mean over PRESENT components; returns (score, missing).

    Missing components are excluded from both the numerator and the weight
    denominator — a candidate lacking a component is not dragged toward
    zero, and ``0.0`` remains a real zero (ADR-0014 §5).
    """
    missing: list[str] = []
    numerator = 0.0
    weight = 0.0
    for name, component_weight in SCORE_WEIGHTS.items():
        normalized = normalize_component(name, scores.get(name))
        if normalized is None:
            missing.append(name)
            continue
        numerator += component_weight * normalized
        weight += component_weight
    if weight <= 0.0:
        return 0.0, tuple(missing)
    return numerator / weight, tuple(missing)


def stable_sort_key(candidate: ScoredCandidate) -> tuple[float, int, int, str]:
    """§18.6 stable ordering: score desc, category priority, recency, id."""
    return (
        -candidate.final_score,
        CATEGORY_PRIORITY.get(candidate.route, 99),
        -candidate.occurred_us,
        candidate.resource_id,
    )


_FIELDS = (
    "candidate_id",
    "route",
    "resource_type",
    "resource_id",
    "resource_revision",
    "subject_entity_id",
    "category",
    "content_hash",
    "occurred_us",
    "token_estimate",
    "scores",
    "conflict_group",
    "final_score",
    "conflict_state",
    "missing_components",
)


def _replace(candidate: ScoredCandidate, **changes: object) -> ScoredCandidate:
    return ScoredCandidate(**{**{name: getattr(candidate, name) for name in _FIELDS}, **changes})


def mark_conflicts_and_redundancy(
    candidates: Sequence[ScoredCandidate],
) -> list[ScoredCandidate]:
    """Deterministic conflict/redundancy marking with penalties.

    - Candidates sharing a non-empty ``conflict_group`` (routes derive it
      from ``(subject_entity_id, predicate)``; a disputed claim joins its
      group) are all marked ``conflicts`` and lose ``CONFLICT_PENALTY``.
    - Candidates sharing a non-empty ``content_hash``: the highest-scored
      one (stable order) keeps its score, the others are marked
      ``redundant`` and lose ``REDUNDANCY_PENALTY``.
    """
    marked = list(candidates)
    order = {candidate.candidate_id: index for index, candidate in enumerate(marked)}

    seen_hashes: dict[str, str] = {}
    for candidate in sorted(marked, key=stable_sort_key):
        if not candidate.content_hash:
            continue
        # The stable sort puts the highest-scored candidate of the group
        # first; it keeps its score and every later duplicate is the
        # redundant one (ADR-0014 §5 "keep the highest-scored").
        if candidate.content_hash in seen_hashes:
            index = order[candidate.candidate_id]
            marked[index] = _replace(
                marked[index],
                conflict_state="redundant",
                final_score=round(max(0.0, marked[index].final_score - REDUNDANCY_PENALTY), 6),
            )
        else:
            seen_hashes[candidate.content_hash] = candidate.candidate_id

    groups: dict[str, list[str]] = {}
    for candidate in marked:
        if candidate.conflict_group:
            groups.setdefault(candidate.conflict_group, []).append(candidate.candidate_id)
    for members in groups.values():
        if len(members) < 2:
            continue
        for candidate_id in members:
            index = order[candidate_id]
            marked[index] = _replace(
                marked[index],
                conflict_state="conflicts",
                final_score=round(max(0.0, marked[index].final_score - CONFLICT_PENALTY), 6),
            )
    return marked


class Ranker(Protocol):
    """Versioned scorer; replaying one snapshot must be byte-identical."""

    version: int

    def score(self, candidates: Sequence[ScoredCandidate]) -> list[ScoredCandidate]: ...


class RankerV2:
    """Deterministic v2 fusion: score → mark → sort (ADR-0014 §5)."""

    version = RECALL_RANKER_V2

    def score(self, candidates: Sequence[ScoredCandidate]) -> list[ScoredCandidate]:
        scored: list[ScoredCandidate] = []
        for candidate in candidates:
            final, missing = compute_final_score(candidate.scores)
            scored.append(
                _replace(candidate, final_score=round(final, 6), missing_components=missing)
            )
        marked = mark_conflicts_and_redundancy(scored)
        return sorted(marked, key=stable_sort_key)


class RankerV3:
    """Deterministic v3 hybrid fusion (ADR-0015 §6): structured + FTS +
    vector candidates in one stable order.

    The fusion math is v2's (missing ≠ 0, deterministic conflict/redundancy
    marking, protected budgets); v3 registers the ``vector`` category
    priority and DEDUPLICATES by canonical resource: when the same
    ``(resource_type, resource_id)`` surfaces through several routes (a
    claim hit by claims, FTS and vector), exactly ONE instance — the
    best-scored in stable order — is kept and the duplicates are dropped
    BEFORE budgeting, so a multi-route resource occupies exactly one budget
    slot ("nothing is returned twice"). Marking still runs first: the
    surviving instance carries the v2 conflict/redundancy verdicts, and
    DISTINCT resources disputing a predicate (conflict groups) are never
    merged — only same-resource duplicates are. Resource types are disjoint
    across routes, so a protected candidate (due task, focus item) can
    never lose its slot to a duplicate of itself."""

    version = RECALL_RANKER_V3

    def score(self, candidates: Sequence[ScoredCandidate]) -> list[ScoredCandidate]:
        ordered = RankerV2().score(candidates)
        kept: list[ScoredCandidate] = []
        seen: set[tuple[str, str]] = set()
        for candidate in ordered:
            identity = (candidate.resource_type, candidate.resource_id)
            if identity in seen:
                continue
            seen.add(identity)
            kept.append(candidate)
        return kept


def is_speaker_identity(candidate: ScoredCandidate, speaker_entity_id: str | None) -> bool:
    """The speaker's necessary identity claims are budget-protected (§18.6)."""
    if speaker_entity_id is None or candidate.subject_entity_id != speaker_entity_id:
        return False
    return candidate.route == ROUTE_CLAIMS and candidate.category in IDENTITY_CLAIM_CATEGORIES


@dataclass(frozen=True, slots=True)
class BudgetOutcome:
    kept: tuple[ScoredCandidate, ...]
    token_total: int
    protected_kept: int


def apply_token_budgets(
    ordered: Sequence[ScoredCandidate],
    *,
    token_budget: int,
    layer_budgets: Mapping[str, int],
    speaker_entity_id: str | None = None,
) -> BudgetOutcome:
    """Budget pass over STABLE-ORDERED candidates.

    Protected candidates (due tasks, focus, speaker identity claims) are
    admitted first regardless of the global token budget — the guarantee in
    §18.6 — while still respecting their own layer budgets (candidate caps
    are applied upstream by each route). Everything else fills the REMAINING
    budget in stable order. Deterministic for one ordered input.
    """
    layer_used: dict[str, int] = {}
    total_used = 0
    kept: list[ScoredCandidate] = []
    admitted: set[str] = set()
    protected_kept = 0

    def layer_allows(candidate: ScoredCandidate) -> bool:
        limit = layer_budgets.get(candidate.route)
        if limit is None:
            return True
        return layer_used.get(candidate.route, 0) + candidate.token_estimate <= limit

    for candidate in ordered:
        protected = candidate.route in PROTECTED_ROUTES or is_speaker_identity(
            candidate, speaker_entity_id
        )
        if protected and layer_allows(candidate):
            kept.append(candidate)
            admitted.add(candidate.candidate_id)
            layer_used[candidate.route] = (
                layer_used.get(candidate.route, 0) + candidate.token_estimate
            )
            total_used += candidate.token_estimate
            protected_kept += 1

    remaining = token_budget - total_used
    for candidate in ordered:
        if candidate.candidate_id in admitted:
            continue
        if candidate.token_estimate > remaining:
            continue
        if not layer_allows(candidate):
            continue
        kept.append(candidate)
        admitted.add(candidate.candidate_id)
        layer_used[candidate.route] = layer_used.get(candidate.route, 0) + candidate.token_estimate
        remaining -= candidate.token_estimate
        total_used += candidate.token_estimate
    return BudgetOutcome(tuple(kept), total_used, protected_kept)


__all__ = [
    "ALL_ROUTES",
    "CATEGORY_PRIORITY",
    "CONFLICT_PENALTY",
    "IDENTITY_CLAIM_CATEGORIES",
    "PROTECTED_ROUTES",
    "RECALL_RANKER_V2",
    "RECALL_RANKER_V3",
    "SCORE_WEIGHTS",
    "TOKEN_ESTIMATOR_VERSION",
    "RankerV2",
    "RankerV3",
    "apply_token_budgets",
    "compute_final_score",
    "is_speaker_identity",
    "mark_conflicts_and_redundancy",
    "stable_sort_key",
]
