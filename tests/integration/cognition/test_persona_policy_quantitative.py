"""W03: 200 reproducible real-service cases for every Persona policy property."""

from __future__ import annotations

import json
import random
from typing import Any, cast

import pytest

from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    PersonaBaseRevisionStaleError,
    PersonaPolicyDeniedError,
)
from iris_memory_core.domain.persona import NARRATIVE_FIELDS, TRAIT_FIELDS, PersonaProposalStatus
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.cognition import test_persona

persona_world = test_persona.persona_world

PROPERTIES = (
    "locked",
    "manual",
    "bounded_auto",
    "allowlist",
    "single_delta",
    "cumulative_delta",
    "evidence_count",
    "evidence_diversity",
    "evidence_span",
    "cooldown",
    "stale_base",
    "confidence",
    "observation_window",
    "sensitive_field",
)


@pytest.mark.parametrize("case", range(200))
@pytest.mark.parametrize("property_name", PROPERTIES)
def test_policy_property_through_real_service(
    persona_world: dict[str, object], property_name: str, case: int
) -> None:
    rng = random.Random(903_000 + case)
    service = cast(PersonaService, persona_world["service"])
    clock = cast(MutableClock, persona_world["clock"])
    store = cast(Store, persona_world["store"])
    admin = cast(AccessContext, persona_world["admin"])
    app = cast(AccessContext, persona_world["app"])
    agent = str(persona_world["agent_id"])
    # Exact binary fractions make equality-boundary failures reproducible.
    delta = rng.randint(20, 200) / 1024
    span = rng.randint(100, 10_000)
    deny = case % 2 == 0
    legal_paths = sorted(
        [f"traits.{field}" for field in TRAIT_FIELDS]
        + [f"narrative.{field}" for field in NARRATIVE_FIELDS]
    )
    path = (
        rng.choice(legal_paths)
        if property_name in {"allowlist", "sensitive_field"}
        else "traits.weights"
    )
    layer, field = path.split(".")
    layers: dict[str, dict[str, object]] = {"traits": {}, "narrative": {}}
    layers[layer][field] = 0.0
    config: dict[str, Any] = {
        "mode": "bounded_auto",
        "allowed_fields": [path],
        "max_single_delta": 1.0,
        "max_cumulative_delta": 1.0,
        "min_evidence": 1,
        "min_distinct_sources": 1,
        "min_confidence": 0.0,
        "cumulative_window_us": 60_000_000,
    }
    service.publish_revision(
        admin,
        agent,
        expected_revision=1,
        core={},
        traits=layers["traits"],
        narrative=layers["narrative"],
        reason="quantitative baseline",
    )
    tasks = TaskService(store, clock, idempotency=IdempotencyManager(store))
    first = tasks.create(app, agent_id=agent, title=f"evidence-{case}-a", idempotency_key="a")
    clock.advance(span)
    second = tasks.create(app, agent_id=agent, title=f"evidence-{case}-b", idempotency_key="b")
    refs: list[dict[str, object]] = [
        {"resource_type": "task", "resource_id": first.task_id},
        {"resource_type": "task", "resource_id": second.task_id},
    ]
    expected_reason: str | None = None
    confidence = 0.75
    base = 2
    next_weight = delta
    if property_name == "locked":
        config.update(mode="locked", allowed_fields=[])
        expected_reason = "locked policy forbids Persona proposals"
    elif property_name == "manual":
        config["mode"] = "manual"
    elif property_name == "allowlist":
        other_path = rng.choice([other for other in legal_paths if other != path])
        config["allowed_fields"] = [other_path] if deny else [path]
        expected_reason = "outside the policy allowlist" if deny else None
    elif property_name == "single_delta":
        config["max_single_delta"] = delta - 1 / 1024 if deny else delta
        expected_reason = "per-change magnitude limit" if deny else None
    elif property_name == "evidence_count":
        config["min_evidence"] = 3 if deny else 2
        expected_reason = "insufficient Evidence$" if deny else None
    elif property_name == "evidence_diversity":
        config["min_distinct_sources"] = 2
        # Duplicating a source must never manufacture diversity.
        if deny:
            refs = [refs[0], refs[0]]
        expected_reason = "insufficient Evidence diversity" if deny else None
    elif property_name in {"evidence_span", "observation_window"}:
        policy_field = (
            "min_evidence_span_us" if property_name == "evidence_span" else "observation_us"
        )
        config[policy_field] = span + 1 if deny else span
        expected_reason = "Evidence time span is too short" if deny else None
    elif property_name == "confidence":
        confidence = rng.randint(200, 800) / 1024
        config["min_confidence"] = confidence + 1 / 1024 if deny else confidence
        expected_reason = "confidence is below policy minimum" if deny else None
    elif property_name == "sensitive_field":
        config["sensitive_fields"] = [path] if deny else []
    elif property_name == "stale_base":
        config["mode"] = "manual"

    policy = service.replace_policy(
        admin, agent, expected_revision=1, config=config, reason="isolate policy property"
    )

    def propose() -> Any:
        return service.create_proposal(
            app,
            agent,
            base_revision=base,
            patch={layer: {field: next_weight}},
            evidence_refs=refs,
            confidence=confidence,
            generator=f"seed-{903_000 + case}",
            generator_version="1",
        )

    if property_name in {"cumulative_delta", "cooldown"}:
        assert propose().status is PersonaProposalStatus.PUBLISHED
        base += 1
        next_weight = 2 * delta
        if property_name == "cumulative_delta":
            config["max_cumulative_delta"] = 2 * delta - 1 / 1024 if deny else 2 * delta
            expected_reason = "cumulative magnitude limit" if deny else None
        else:
            config["cooldown_us"] = span
            clock.advance(span - 1 if deny else span)
            expected_reason = "cooldown is active" if deny else None
        service.replace_policy(
            admin, agent, expected_revision=policy.revision, config=config, reason="boundary"
        )

    before = service.current(admin, agent).revision
    if expected_reason is not None:
        with pytest.raises(PersonaPolicyDeniedError, match=expected_reason):
            propose()
        assert service.current(admin, agent).revision == before, (property_name, case)
        print(f"W03 {property_name}/{case}: rejected by {expected_reason}; current unchanged")
        return

    proposal = propose()
    review = property_name in {"manual", "stale_base"} or (
        property_name == "sensitive_field" and deny
    )
    if review:
        assert proposal.status is PersonaProposalStatus.PROPOSED
        assert service.current(admin, agent).revision == before
        with pytest.raises(AccessDeniedError):
            service.approve(app, agent, proposal.id, reason="application cannot review")
        if property_name == "stale_base" and deny:
            advanced = service.publish_revision(
                admin,
                agent,
                expected_revision=base,
                core={},
                traits={"weights": 0.0},
                narrative={"summary": f"intervening-{case}"},
                reason="invalidate base",
            )
            with pytest.raises(PersonaBaseRevisionStaleError):
                service.approve(admin, agent, proposal.id, reason="stale review")
            assert service.current(admin, agent).revision == advanced
            # Stale creation is fenced as well as an already-saved proposal.
            with pytest.raises(PersonaBaseRevisionStaleError):
                propose()
            print(f"W03 stale_base/{case}: create+approve rejected; current unchanged")
            return
        proposal = service.approve(admin, agent, proposal.id, reason="authorized review")
    assert proposal.status is PersonaProposalStatus.PUBLISHED
    current = service.current(admin, agent).revision
    assert current.revision == before.revision + 1, (property_name, case)
    assert json.loads(getattr(current, layer))[field] == next_weight
    assert json.loads(current.core) == {}, "proposal changed Core"
    print(f"W03 {property_name}/{case}: accepted at boundary; one Current revision")
