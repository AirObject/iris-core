"""Phase 3 domain property tests (fixed seeds, 200 cases per property).

Covers the pure domain cores: the deterministic recent-context builder, state
namespace-policy validation, and the focus state machine / decay / capacity
rules (9.1-9.3). Everything here is clock-free or explicitly injectable.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import replace

import pytest

from iris_memory_core.domain.focus import (
    ALL_FOCUS_KINDS,
    FOCUS_TRANSITIONS,
    FocusCapacityPolicy,
    InvalidFocusItemError,
    clamp01,
    decayed_activation,
    validate_scores,
    validate_summary,
    validate_transition,
)
from iris_memory_core.domain.observation import ObservationRole, StoredObservation
from iris_memory_core.domain.recent import (
    DefaultTokenEstimator,
    RecentWindowPolicy,
    build_projection,
    projection_invariants,
)
from iris_memory_core.domain.scope import Scope
from iris_memory_core.domain.state import (
    BUILTIN_STATE_POLICIES,
    InvalidStateWriteError,
    ScopeRequirement,
    StateNamespacePolicy,
    state_scope_key,
    validate_state_write,
)

CASES = 200  # per property, fixed seeds (Phase 3 quantified baseline)

_ESTIMATOR = DefaultTokenEstimator()


def _observation(seed: random.Random, index: int, space_id: str = "sp1") -> StoredObservation:
    identifier = uuid.UUID(int=seed.getrandbits(128), version=4)
    occurred = seed.randrange(1_000_000_000_000_000, 1_000_000_080_000_000)
    length = seed.randrange(0, 400)
    return StoredObservation(
        id=str(identifier),
        tenant_id="t1",
        agent_id="a1",
        app_instance_id="app",
        role=ObservationRole.USER,
        kind="message.text",
        idempotency_key=f"k{index}",
        effect_state="committed",  # type: ignore[arg-type]
        occurred_us=occurred,
        committed_us=occurred + 1,
        created_us=occurred + 2,
        revision=1,
        space_id=space_id,
        content="x" * length,
    )


class TestRecentBuilderProperties:
    def test_rebuild_three_times_is_byte_identical(self) -> None:
        for case in range(CASES):
            seed = random.Random(10_000 + case)
            observations = [_observation(seed, i) for i in range(seed.randrange(0, 60))]
            policy = RecentWindowPolicy(
                token_budget=seed.choice([50, 200, 2_000]),
                max_observations=seed.choice([5, 20, 200]),
                segment_sources=seed.choice([3, 10]),
            )
            watermark = seed.randrange(0, 999)
            builds = [
                build_projection(
                    observations,
                    watermark=watermark,
                    policy=policy,
                    estimator=_ESTIMATOR,
                )
                for _ in range(3)
            ]
            assert builds[0] == builds[1] == builds[2], case
            assert builds[0].result_hash == builds[1].result_hash, case

    def test_ordering_independent_of_input_order(self) -> None:
        for case in range(CASES):
            seed = random.Random(20_000 + case)
            observations = [_observation(seed, i) for i in range(seed.randrange(1, 40))]
            shuffled = list(observations)
            seed.shuffle(shuffled)
            policy = RecentWindowPolicy(token_budget=10_000, max_observations=50)
            first = build_projection(observations, watermark=7, policy=policy, estimator=_ESTIMATOR)
            second = build_projection(shuffled, watermark=7, policy=policy, estimator=_ESTIMATOR)
            assert first == second, case

    def test_invariants_hold_and_tail_is_newest(self) -> None:
        for case in range(CASES):
            seed = random.Random(30_000 + case)
            observations = [_observation(seed, i) for i in range(seed.randrange(0, 80))]
            policy = RecentWindowPolicy(
                token_budget=seed.choice([20, 500, 5_000]),
                max_observations=seed.choice([3, 30, 300]),
            )
            projection = build_projection(
                observations, watermark=case, policy=policy, estimator=_ESTIMATOR
            )
            assert projection_invariants(projection) == (), case
            if projection.hot_observation_refs:
                newest = max(item.occurred_us for item in projection.hot_observation_refs)
                tail = projection.hot_observation_refs[-1]
                assert tail.occurred_us == newest, case

    def test_token_estimate_is_sum_of_parts(self) -> None:
        for case in range(CASES):
            seed = random.Random(40_000 + case)
            observations = [_observation(seed, i) for i in range(seed.randrange(0, 50))]
            projection = build_projection(
                observations,
                watermark=1,
                policy=RecentWindowPolicy(token_budget=99_999, max_observations=500),
                estimator=_ESTIMATOR,
            )
            hot = sum(item.token_estimate for item in projection.hot_observation_refs)
            segments = sum(seg.token_estimate for seg in projection.summary_segments)
            assert projection.token_estimate == hot + segments, case

    def test_result_hash_is_an_integrity_invariant(self) -> None:
        projection = build_projection(
            [_observation(random.Random(50_000), 0)],
            watermark=1,
            policy=RecentWindowPolicy(),
            estimator=_ESTIMATOR,
        )
        tampered = replace(projection, result_hash="0" * 64)
        assert "result_hash does not match projection contents" in projection_invariants(tampered)


class TestStatePolicyProperties:
    def _scope(self, seed: random.Random) -> Scope:
        return Scope(
            tenant_id="t1",
            agent_id=seed.choice([None, "a1"]),
            space_id=seed.choice([None, "s1"]),
            session_id=None,
        )

    def test_ttl_never_exceeds_policy_cap(self) -> None:
        for case in range(CASES):
            seed = random.Random(50_000 + case)
            policy = BUILTIN_STATE_POLICIES["environment"]
            scope = self._scope(seed)
            ttl = seed.choice([None, 0, 1, 86_400_000_000, 10**12])
            draft = validate_state_write(
                policy,
                scope,
                "environment",
                "k",
                {"v": seed.randrange(0, 100)},
                source_authority=seed.choice(sorted(policy.allowed_source_authorities)),
                observed_us=1_000,
                ttl_us=ttl,
                expires_us=None,
            )
            if draft.expires_us is not None and policy.max_ttl_us:
                assert draft.expires_us <= 1_000 + policy.max_ttl_us, case

    def test_default_applies_when_ttl_missing(self) -> None:
        policy = BUILTIN_STATE_POLICIES["runtime"]
        for case in range(CASES):
            draft = validate_state_write(
                policy,
                Scope(tenant_id="t1"),
                "runtime",
                "k",
                {"n": case},
                source_authority="host",
                observed_us=5_000,
                ttl_us=None,
                expires_us=None,
            )
            assert draft.expires_us == 5_000 + policy.default_ttl_us

    def test_authority_whitelist_enforced(self) -> None:
        policy = BUILTIN_STATE_POLICIES["runtime"]  # host/adapter/system only
        for case in range(CASES):
            with pytest.raises(InvalidStateWriteError):
                validate_state_write(
                    policy,
                    Scope(tenant_id="t1"),
                    "runtime",
                    "k",
                    {"n": case},
                    source_authority="model",
                    observed_us=1,
                    ttl_us=None,
                    expires_us=None,
                )

    def test_value_ceiling_enforced(self) -> None:
        policy = StateNamespacePolicy(
            namespace="tiny",
            default_ttl_us=0,
            max_ttl_us=0,
            retain_history=False,
            max_history_revisions=0,
            max_value_bytes=16,
            allowed_source_authorities=frozenset({"host"}),
            required_scope=ScopeRequirement.ANY,
        )
        for _case in range(CASES):
            with pytest.raises(InvalidStateWriteError):
                validate_state_write(
                    policy,
                    Scope(tenant_id="t1"),
                    "tiny",
                    "k",
                    {"pad": "y" * 64},
                    source_authority="host",
                    observed_us=1,
                    ttl_us=None,
                    expires_us=None,
                )

    def test_scope_requirement_enforced(self) -> None:
        policy = BUILTIN_STATE_POLICIES["topic"]  # requires agent scope
        for case in range(CASES):
            with pytest.raises(InvalidStateWriteError):
                validate_state_write(
                    policy,
                    Scope(tenant_id="t1"),
                    "topic",
                    "k",
                    {"n": case},
                    source_authority="host",
                    observed_us=1,
                    ttl_us=None,
                    expires_us=None,
                )

    def test_scope_key_is_canonical(self) -> None:
        a = Scope(tenant_id="t1", agent_id="a1")
        b = Scope(tenant_id="t1", agent_id="a1", space_id=None, session_id=None)
        assert state_scope_key(a) == state_scope_key(b)
        assert state_scope_key(a) != state_scope_key(Scope(tenant_id="t1", agent_id="a2"))


class TestFocusStateMachineProperties:
    def test_terminal_statuses_have_no_exits(self) -> None:
        for status in ("promoted", "dismissed", "expired"):
            assert FOCUS_TRANSITIONS[status] == frozenset()
        for _case in range(CASES):
            for terminal in ("promoted", "dismissed", "expired"):
                for target in ALL_FOCUS_KINDS | {
                    "active",
                    "dormant",
                    "promoted",
                    "dismissed",
                    "expired",
                }:
                    if target == terminal:
                        continue
                    with pytest.raises(InvalidFocusItemError):
                        validate_transition(terminal, target)

    def test_legal_transitions_accepted(self) -> None:
        for _case in range(CASES):
            validate_transition("active", "dormant")
            validate_transition("active", "promoted")
            validate_transition("active", "dismissed")
            validate_transition("active", "expired")
            validate_transition("dormant", "active")

    def test_decay_is_pure_and_monotone(self) -> None:
        for case in range(CASES):
            seed = random.Random(60_000 + case)
            base = seed.random()
            now = seed.randrange(1_000, 10**12)
            half_life = seed.randrange(1, 10**9)
            first = decayed_activation(base, now, now + 1, half_life_us=half_life)
            second = decayed_activation(base, now, now + 1, half_life_us=half_life)
            assert first == second, case
            later = decayed_activation(base, now, now + half_life * 4, half_life_us=half_life)
            assert later <= first, case
            assert 0.0 <= later <= 1.0

    def test_decay_never_double_compounds(self) -> None:
        """Decay derives from (base, last_activation) — never from the stored
        decayed value — so re-running maintenance is idempotent."""
        for case in range(CASES):
            seed = random.Random(70_000 + case)
            base = seed.random()
            last = seed.randrange(0, 10**9)
            half = 1_000_000
            t1 = last + 5 * half
            direct = decayed_activation(base, last, t1, half_life_us=half)
            # simulate a maintenance run storing decayed-at-t0, then re-decay
            t0 = last + half
            stored = decayed_activation(base, last, t0, half_life_us=half)
            assert stored == base * 0.5
            recomputed = decayed_activation(base, last, t1, half_life_us=half)
            assert recomputed == direct != stored * 0.5 or base == 0.0, case

    def test_scores_must_be_unit_interval(self) -> None:
        for _case in range(CASES):
            with pytest.raises(InvalidFocusItemError):
                validate_scores(salience=-0.1, activation=0.5, importance=0.5)
            with pytest.raises(InvalidFocusItemError):
                validate_scores(salience=0.5, activation=1.4, importance=0.5)
            validate_scores(salience=0.0, activation=1.0, importance=0.5)

    def test_summary_bounds(self) -> None:
        with pytest.raises(InvalidFocusItemError):
            validate_summary("")
        with pytest.raises(InvalidFocusItemError):
            validate_summary("y" * 2001)
        for case in range(CASES):
            validate_summary("ok" + str(case))

    def test_capacity_policy_validation(self) -> None:
        with pytest.raises(InvalidFocusItemError):
            FocusCapacityPolicy(max_items=0)
        with pytest.raises(InvalidFocusItemError):
            FocusCapacityPolicy(token_budget=0)
        with pytest.raises(InvalidFocusItemError):
            FocusCapacityPolicy(activation_boost=0.0)
        with pytest.raises(InvalidFocusItemError):
            FocusCapacityPolicy(kind_quotas={"goal": 0})
        with pytest.raises(InvalidFocusItemError):
            FocusCapacityPolicy(kind_quotas={"vibe": 3})

    def test_clamp01(self) -> None:
        for case in range(CASES):
            value = (case - 100) / 100
            assert 0.0 <= clamp01(value) <= 1.0
