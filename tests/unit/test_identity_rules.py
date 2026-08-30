"""Generated property tests for binding, redirect and field authority rules (§6)."""

from __future__ import annotations

import random

import pytest

from iris_memory_core.domain.errors import ConflictError, InvalidTransitionError, RedirectCycleError
from iris_memory_core.domain.identity import (
    AUTHORITY_RANK,
    BINDING_TRANSITIONS,
    REDIRECT_MAX_DEPTH,
    BindingState,
    EntityKind,
    ExternalIdentityKey,
    FieldAuthority,
    MergeOutcome,
    binding_verified_at,
    decide_attribute_merge,
    resolve_redirect,
    transition_binding,
    would_cycle,
)

CASES = 250
SEED = 424242


def test_binding_transition_table_is_enforced() -> None:
    rng = random.Random(SEED)
    states = list(BindingState)
    for _ in range(CASES):
        current = rng.choice(states)
        target = rng.choice(states)
        if (current, target) in BINDING_TRANSITIONS:
            assert transition_binding(current, target) is target
        else:
            with pytest.raises(InvalidTransitionError):
                transition_binding(current, target)
    # Terminal state: revoked accepts nothing.
    for target in states:
        if target is not BindingState.REVOKED:
            with pytest.raises(InvalidTransitionError):
                transition_binding(BindingState.REVOKED, target)


def test_redirect_resolution_matches_transitive_closure() -> None:
    rng = random.Random(SEED + 1)
    for _ in range(CASES):
        size = rng.randrange(1, 8)
        nodes = [f"e{i}" for i in range(size)]
        edges: dict[str, str] = {}
        for node in nodes:
            if rng.random() < 0.5:
                edges[node] = rng.choice(nodes)
        # Skip graphs that contain cycles (tested separately).
        for node in nodes:
            try:
                resolve_redirect(node, edges)
            except RedirectCycleError:
                break
        else:
            for node in nodes:
                current = node
                while current in edges:
                    current = edges[current]
                assert resolve_redirect(node, edges) == current
            return
    pytest.fail("no acyclic graph generated")


def test_redirect_cycles_are_detected() -> None:
    random.Random(SEED + 2)  # deterministic seed kept for reproducibility
    a, b, c = "a", "b", "c"
    chain = {"a": b, "b": c}
    cyclic = dict(chain)
    cyclic[c] = a
    with pytest.raises(RedirectCycleError):
        resolve_redirect(a, cyclic)
    assert resolve_redirect(a, chain) == c
    assert would_cycle(chain, c, a) is True  # adding c -> a closes the loop
    assert would_cycle(chain, a, c) is False  # a -> c terminates, still acyclic
    assert would_cycle(cyclic, c, c) is True  # self loop


def test_redirect_depth_bound_is_enforced() -> None:
    rng = random.Random(SEED + 3)
    for _ in range(CASES):
        length = rng.choice([REDIRECT_MAX_DEPTH - 1, REDIRECT_MAX_DEPTH, REDIRECT_MAX_DEPTH + 5])
        edges = {f"n{i}": f"n{i + 1}" for i in range(length)}
        start, end = "n0", f"n{length}"
        if length <= REDIRECT_MAX_DEPTH:
            assert resolve_redirect(start, edges) == end
        else:
            from iris_memory_core.domain.errors import RedirectDepthExceededError

            with pytest.raises(RedirectDepthExceededError):
                resolve_redirect(start, edges)


def test_field_authority_never_lets_low_overwrite_high() -> None:
    rng = random.Random(SEED + 4)
    ranks = list(FieldAuthority)
    for _ in range(CASES):
        current_auth = rng.choice(ranks)
        candidate_auth = rng.choice(ranks)
        current_value = f"v{rng.randrange(3)}"
        candidate_value = f"v{rng.randrange(3)}"
        decision = decide_attribute_merge(
            current_auth, current_value, candidate_auth, candidate_value
        )
        if candidate_value == current_value:
            # Identical values promote authority when strictly higher; they
            # never demote and never conflict.
            if AUTHORITY_RANK[candidate_auth] > AUTHORITY_RANK[current_auth]:
                assert decision.outcome is MergeOutcome.SUPERSEDE
            else:
                assert decision.outcome is MergeOutcome.IGNORED
        elif AUTHORITY_RANK[candidate_auth] > AUTHORITY_RANK[current_auth]:
            assert decision.outcome is MergeOutcome.SUPERSEDE
        elif AUTHORITY_RANK[candidate_auth] < AUTHORITY_RANK[current_auth]:
            assert decision.outcome is MergeOutcome.IGNORED
            # Explicit corrections are never lost to inferred values.
            if current_auth is FieldAuthority.EXPLICIT_CORRECTION:
                assert decision.outcome is MergeOutcome.IGNORED
        else:
            assert decision.outcome is MergeOutcome.COEXIST


def test_binding_history_reconstruction() -> None:
    rng = random.Random(SEED + 5)
    for _ in range(CASES):
        events: list[tuple[int, BindingState]] = []
        moment = 0
        for _ in range(rng.randrange(1, 6)):
            moment += rng.randrange(1, 100)
            events.append((moment, rng.choice(list(BindingState))))
        at = rng.randrange(0, moment + 1)
        expected: BindingState | None = None
        for recorded, state in events:
            if recorded <= at:
                expected = state
        assert binding_verified_at(tuple(events), at) is (expected is BindingState.VERIFIED)


def test_external_identity_key_requires_all_parts() -> None:
    rng = random.Random(SEED + 6)
    for _ in range(CASES):
        parts = ["t", "p", "r", "x"]
        drop = rng.randrange(4)
        values = list(parts)
        values[drop] = ""
        with pytest.raises(ConflictError):
            ExternalIdentityKey(*values)
    assert ExternalIdentityKey("t", "p", "r", "x").external_id == "x"


def test_entity_kinds_are_the_canonical_set() -> None:
    assert {kind.value for kind in EntityKind} == {
        "person",
        "agent",
        "organization",
        "community",
        "place",
        "topic",
        "object",
        "system",
    }
