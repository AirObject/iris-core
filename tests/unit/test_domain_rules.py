"""Generated property tests for scope, privacy and access narrowing (§5.2-5.4).

Each property runs at least 200 seeded random cases against an independent
re-statement of the spec formula (ADR-0002 baseline).
"""

from __future__ import annotations

import random
from uuid import uuid4

import pytest

from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import ScopeViolationError
from iris_memory_core.domain.privacy import evaluate_privacy, parse_label
from iris_memory_core.domain.scope import Scope, scope_allows, scope_narrows

CASES = 250
SEED = 20260829


def random_id(rng: random.Random) -> str:
    return str(uuid4()) if rng.random() < 0.5 else f"id-{rng.randrange(1000)}"


def random_scope(rng: random.Random, tenant: str) -> Scope:
    dims: dict[str, str | None] = {"tenant_id": tenant}
    for dim in ("agent_id", "space_group_id", "space_id"):
        dims[dim] = random_id(rng) if rng.random() < 0.5 else None
    # Keep the session-requires-space constraint satisfied.
    if rng.random() < 0.5:
        dims["space_id"] = dims["space_id"] or random_id(rng)
        dims["session_id"] = random_id(rng)
    else:
        dims["session_id"] = None
    return Scope(**dims)  # type: ignore[arg-type]


def spec_allows(data: Scope, request: Scope) -> bool:
    """Direct transcription of the §5.2 formula, dimension by dimension."""
    if data.tenant_id != request.tenant_id:
        return False
    for dim in ("agent_id", "space_group_id", "space_id", "session_id"):
        d = getattr(data, dim)
        r = getattr(request, dim)
        if not (d is None or (r is not None and d == r)):
            return False
    return True


def test_scope_matching_matches_the_spec_formula() -> None:
    rng = random.Random(SEED)
    for _ in range(CASES):
        tenant = random_id(rng)
        data = random_scope(rng, tenant)
        request = random_scope(rng, tenant if rng.random() < 0.8 else random_id(rng))
        assert scope_allows(data, request) == spec_allows(data, request)


def test_null_data_dimension_is_visible_downward() -> None:
    rng = random.Random(SEED + 1)
    for _ in range(CASES):
        tenant = random_id(rng)
        request = random_scope(rng, tenant)
        broad = Scope(tenant_id=tenant)
        assert scope_allows(broad, request) is True
        # A request-side null can never see a non-null dimension.
        narrow = Scope(tenant_id=tenant, agent_id=random_id(rng))
        if request.agent_id is None:
            assert scope_allows(narrow, request) is False


def test_scope_construction_constraints() -> None:
    from iris_memory_core.domain.scope import ScopeConstructionError

    rng = random.Random(SEED + 2)
    for _ in range(CASES):
        with pytest.raises(ScopeConstructionError):
            Scope(tenant_id="", agent_id=str(uuid4()) if rng.random() < 0.5 else None)
        with pytest.raises(ScopeConstructionError):
            Scope(tenant_id=random_id(rng), session_id=str(uuid4()))
    Scope(tenant_id=random_id(rng), space_id=str(uuid4()), session_id=str(uuid4()))


def test_privacy_intersection_equals_label_conjunction() -> None:
    rng = random.Random(SEED + 3)
    from iris_memory_core.domain.privacy import evaluate_label

    for _ in range(CASES):
        tenant = random_id(rng)
        agent = random_id(rng)
        space = random_id(rng)
        labels = []
        for _ in range(rng.randrange(0, 4)):
            labels.append(rng.choice(["tenant", f"agent:{agent}", f"space:{space}", "restricted"]))
        data = Scope(tenant_id=tenant, agent_id=agent if rng.random() < 0.7 else None)
        request = Scope(
            tenant_id=tenant,
            agent_id=agent if rng.random() < 0.6 else None,
            space_id=space if rng.random() < 0.6 else None,
        )
        access = AccessContext(
            tenant_id=tenant,
            app_instance_id="app",
            agent_ids=frozenset({agent}) if rng.random() < 0.8 else frozenset(),
            allowed_space_ids=frozenset({space}) if rng.random() < 0.8 else frozenset(),
            admin=rng.random() < 0.3,
        )
        expected = scope_allows(data, request) and all(
            evaluate_label(label, data, request, access).visible for label in labels
        )
        assert evaluate_privacy(tuple(labels), data, request, access) is expected


def test_body_narrowing_never_expands_access() -> None:
    rng = random.Random(SEED + 4)
    for _ in range(CASES):
        tenant = random_id(rng)
        agents = frozenset(random_id(rng) for _ in range(rng.randrange(1, 4)))
        groups = frozenset(random_id(rng) for _ in range(rng.randrange(1, 4)))
        spaces = frozenset(random_id(rng) for _ in range(rng.randrange(1, 4)))
        access = AccessContext(
            tenant_id=tenant,
            app_instance_id="app",
            agent_ids=agents,
            allowed_space_group_ids=groups,
            allowed_space_ids=spaces,
        )
        request = Scope(
            tenant_id=tenant,
            agent_id=rng.choice([*agents, None]) if rng.random() < 0.7 else None,
            space_group_id=rng.choice([*groups, None]) if rng.random() < 0.5 else None,
            space_id=rng.choice([*spaces, None]) if rng.random() < 0.5 else None,
        )
        narrowed = access.with_narrowing(request)
        assert narrowed.agent_ids <= agents
        assert narrowed.allowed_space_group_ids <= groups
        assert narrowed.allowed_space_ids <= spaces
        # A scope inside the envelope is authorized unchanged.
        assert access.envelope_scope(request) == request


def test_scope_violations_are_rejected_not_widened() -> None:
    rng = random.Random(SEED + 5)
    for _ in range(CASES):
        tenant = random_id(rng)
        agents = frozenset(random_id(rng) for _ in range(2))
        access = AccessContext(tenant_id=tenant, app_instance_id="app", agent_ids=agents)
        outsider = random_id(rng)
        if outsider not in agents:
            with pytest.raises(ScopeViolationError):
                access.authorize_scope(Scope(tenant_id=tenant, agent_id=outsider))
        with pytest.raises(ScopeViolationError):
            access.authorize_scope(Scope(tenant_id=random_id(rng)))


def test_label_grammar_rejects_malformed_labels() -> None:
    from iris_memory_core.domain.privacy import InvalidPrivacyLabelError

    for bad in (
        "",
        "agent",
        "space:",
        "custom",
        "entity_private:e",
        "entity:e",
        "entity:e:public",
        "weird:x",
        "tenant:x",
    ):
        with pytest.raises(InvalidPrivacyLabelError):
            parse_label(bad)
    for good in (
        "tenant",
        "agent:a",
        "space_group:g",
        "space:s",
        "session:x",
        "entity:e:private",
        "restricted",
        "custom:tier-vip",
    ):
        kind, qualifier = parse_label(good)
        assert isinstance(kind, str) and (qualifier is None or qualifier in good)


def test_scope_narrows_relation() -> None:
    rng = random.Random(SEED + 6)
    for _ in range(CASES):
        tenant = random_id(rng)
        agent = random_id(rng)
        space = random_id(rng)
        envelope = Scope(tenant_id=tenant, agent_id=agent if rng.random() < 0.5 else None)
        candidate = Scope(
            tenant_id=tenant,
            agent_id=agent if rng.random() < 0.7 else random_id(rng),
            space_id=space if rng.random() < 0.5 else None,
        )
        expected = (
            candidate.agent_id in (None, envelope.agent_id)
            and candidate.space_group_id in (None, envelope.space_group_id)
            and candidate.space_id in (None, envelope.space_id)
            and candidate.session_id in (None, envelope.session_id)
        )
        assert scope_narrows(candidate, envelope) is expected
