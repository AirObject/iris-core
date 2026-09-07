"""Fixed-seed property coverage for the Phase 9 Persona trust boundary."""

from __future__ import annotations

import random

import pytest

from iris_memory_core.domain.errors import InvalidRequestError
from iris_memory_core.domain.persona import (
    MAX_STATE_TTL_US,
    field_magnitude,
    flatten_patch,
    persona_content_hash,
    validate_state,
    validate_state_timing,
)


def test_two_hundred_core_injection_shapes_are_rejected() -> None:
    randomizer = random.Random(9_001)
    for case in range(200):
        payload = {
            "core": {
                randomizer.choice(
                    ("identity", "safety_boundaries", "relationship_constraints", "values")
                ): f"ignore-host-policy-{case}"
            },
            "traits": {"style": "warm"},
        }
        with pytest.raises(InvalidRequestError, match="Core"):
            flatten_patch(payload)


def test_two_hundred_state_range_cases_are_deterministic() -> None:
    randomizer = random.Random(9_002)
    for _ in range(200):
        mood = randomizer.uniform(-2.0, 2.0)
        energy = randomizer.uniform(-1.0, 2.0)
        engagement = randomizer.uniform(-1.0, 2.0)
        payload = {"mood": mood, "energy": energy, "engagement": engagement}
        valid = -1.0 <= mood <= 1.0 and 0.0 <= energy <= 1.0 and 0.0 <= engagement <= 1.0
        if valid:
            assert validate_state(payload) == payload
        else:
            with pytest.raises(InvalidRequestError):
                validate_state(payload)


def test_two_hundred_ttl_boundaries_accept_only_the_frozen_window() -> None:
    randomizer = random.Random(9_003)
    for _ in range(200):
        ttl = randomizer.randint(-MAX_STATE_TTL_US, MAX_STATE_TTL_US * 2)
        if 0 < ttl <= MAX_STATE_TTL_US:
            validate_state_timing(started_us=123, expires_us=123 + ttl)
        else:
            with pytest.raises(InvalidRequestError):
                validate_state_timing(started_us=123, expires_us=123 + ttl)


def test_two_hundred_hash_and_magnitude_replays_are_stable() -> None:
    randomizer = random.Random(9_004)
    for case in range(200):
        old = randomizer.uniform(-1.0, 1.0)
        new = randomizer.uniform(-1.0, 1.0)
        magnitude = field_magnitude(old, new)
        assert 0.0 <= magnitude <= 1.0
        content = (
            {"name": f"Iris-{case}"},
            {"weights": {"warmth": round(randomizer.random(), 6)}},
            {"summary": f"fixed-seed-{case}"},
        )
        assert persona_content_hash(*content) == persona_content_hash(*content)
