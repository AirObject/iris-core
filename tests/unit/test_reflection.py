"""Phase 10 deterministic candidate and provider-governance adversarial gates."""

from __future__ import annotations

import copy
import time

import pytest

from iris_memory_core.domain.errors import ProviderUnavailableError
from iris_memory_core.domain.reflection import (
    CandidateValidationError,
    RejectReason,
    VersionSet,
    candidate_diff,
    run_fingerprint,
    validate_candidate,
)
from iris_memory_core.providers.cognitive import CognitiveProviderLimits, ProviderGovernance

OBSERVATIONS = {"observation-1": (1, 64)}
VERSIONS = VersionSet()


def valid_candidate(seed: int = 0) -> dict[str, object]:
    return {
        "type": "claim",
        "payload": {
            "predicate": f"preference.{seed}",
            "value": {"value": seed},
            "confidence": 0.5,
            "importance": 0.5,
            "source_authority": "extracted",
        },
        "evidence": [
            {
                "observation_id": "observation-1",
                "observation_revision": 1,
                "start": 0,
                "end": 8,
            }
        ],
        "scope": {"tenant_id": "tenant-a", "agent_id": "agent-a"},
        "privacy_labels": [],
    }


@pytest.mark.parametrize("seed", range(200))
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("no_evidence", RejectReason.EVIDENCE_REQUIRED),
        ("outside_window", RejectReason.EVIDENCE_OUTSIDE_WINDOW),
        ("self_reference", RejectReason.SELF_REFERENTIAL_EVIDENCE),
        ("bad_span", RejectReason.EVIDENCE_SPAN_INVALID),
        ("stale_source", RejectReason.SOURCE_STALE),
        ("unsafe_trigger", RejectReason.UNSAFE_TRIGGER),
        ("active_task", RejectReason.ACTIVE_TASK_DENIED),
        ("persona_core", RejectReason.PERSONA_CORE_DENIED),
        ("bad_score", RejectReason.VALUE_OUT_OF_RANGE),
        ("bad_authority", RejectReason.AUTHORITY_DENIED),
    ],
)
def test_each_illegal_candidate_class_commits_zero(
    seed: int, mutation: str, reason: RejectReason
) -> None:
    raw = valid_candidate(seed)
    evidence = raw["evidence"]
    assert isinstance(evidence, list)
    span = evidence[0]
    assert isinstance(span, dict)
    payload = raw["payload"]
    assert isinstance(payload, dict)
    if mutation == "no_evidence":
        raw["evidence"] = []
    elif mutation == "outside_window":
        span["observation_id"] = f"missing-{seed}"
    elif mutation == "self_reference":
        span["observation_id"] = f"candidate:{seed}"
    elif mutation == "bad_span":
        span["end"] = 65 + seed
    elif mutation == "stale_source":
        span["observation_revision"] = 2 + seed
    elif mutation == "unsafe_trigger":
        payload["script"] = f"return {seed}"
    elif mutation == "active_task":
        raw["type"] = "task"
        payload["status"] = "active"
    elif mutation == "persona_core":
        raw["type"] = "persona_proposal"
        raw["payload"] = {"patch": {"core": {"name": str(seed)}}}
    elif mutation == "bad_score":
        payload["confidence"] = 1.01 + seed
    elif mutation == "bad_authority":
        payload["source_authority"] = "admin"
    with pytest.raises(CandidateValidationError) as raised:
        validate_candidate(raw, observations=OBSERVATIONS, versions=VERSIONS)
    assert raised.value.reason is reason


def test_same_snapshot_versions_and_parameters_replay_three_times_identically() -> None:
    source = valid_candidate(7)
    candidates = [
        validate_candidate(copy.deepcopy(source), observations=OBSERVATIONS, versions=VERSIONS)
        for _ in range(3)
    ]
    assert len({item.fingerprint for item in candidates}) == 1
    assert len({item.candidate_id for item in candidates}) == 1
    assert len({run_fingerprint("a" * 64, VERSIONS, "commit") for _ in range(3)}) == 1
    assert candidate_diff(candidates[:1], candidates[1:]) == {
        "added": (),
        "removed": (),
        "unchanged": (candidates[0].fingerprint,),
    }


@pytest.mark.parametrize("round_number", range(20))
@pytest.mark.parametrize("failure", ["timeout", "server_error", "rate_limited", "budget_exhausted"])
def test_provider_failure_injection_is_bounded(round_number: int, failure: str) -> None:
    del round_number
    limits = CognitiveProviderLimits(
        timeout_seconds=0.002,
        max_retries=0,
        max_qps=1_000_000,
        daily_budget_microunits=0 if failure == "budget_exhausted" else 100,
        concurrency=1,
        breaker_failures=1,
        breaker_cooldown_seconds=1,
    )
    governance = ProviderGovernance({"extraction": limits})

    def invoke(_timeout: float) -> object:
        if failure == "timeout":
            time.sleep(0.01)
            return {}
        error = RuntimeError("provider transport failed")
        if failure == "rate_limited":
            error.reason_code = "rate_limited"  # type: ignore[attr-defined]
        raise error

    with pytest.raises(ProviderUnavailableError) as raised:
        governance.call(
            "extraction",
            tenant_id="tenant-a",
            agent_id="agent-a",
            request_material={"safe_hash": "a" * 64},
            estimated_cost_microunits=1,
            invoke=invoke,
        )
    expected = failure
    assert raised.value.details["reason_code"] == expected


@pytest.mark.parametrize("round_number", range(20))
def test_circuit_breaker_allows_only_one_bounded_probe(round_number: int) -> None:
    del round_number
    now = [0.0]
    limits = CognitiveProviderLimits(
        timeout_seconds=0.01,
        max_retries=0,
        max_qps=1_000_000,
        daily_budget_microunits=100,
        breaker_failures=1,
        breaker_cooldown_seconds=1,
    )
    governance = ProviderGovernance({"extraction": limits}, monotonic=lambda: now[0])
    with pytest.raises(ProviderUnavailableError):
        governance.call(
            "extraction",
            tenant_id="tenant-a",
            agent_id=None,
            request_material={},
            estimated_cost_microunits=1,
            invoke=lambda _timeout: (_ for _ in ()).throw(RuntimeError("down")),
        )
    assert governance.circuit_state("tenant-a", "extraction") == "open"
    with pytest.raises(ProviderUnavailableError) as blocked:
        governance.call(
            "extraction",
            tenant_id="tenant-a",
            agent_id=None,
            request_material={},
            estimated_cost_microunits=1,
            invoke=lambda _timeout: {},
        )
    assert blocked.value.details["reason_code"] == "circuit_open"
    now[0] = 2.0
    value, outcome = governance.call(
        "extraction",
        tenant_id="tenant-a",
        agent_id=None,
        request_material={},
        estimated_cost_microunits=1,
        invoke=lambda _timeout: {"ok": True},
    )
    assert value == {"ok": True}
    assert outcome.outcome == "success"
    assert governance.circuit_state("tenant-a", "extraction") == "closed"
