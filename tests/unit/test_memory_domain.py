"""Phase 5 domain rule tests: fixed-seed property batteries (≥200 seeds each).

Covers claim transitions, bi-temporal interval logic, dedup-key identity,
the procedure-claim guard, artifact locator defense, forget-selector
validation, legal-hold matching and retention policy matching.
"""

from __future__ import annotations

import random
import string

import pytest

from iris_memory_core.domain.memory import (
    CLAIM_TRANSITIONS,
    ArtifactInvalidError,
    ClaimStatus,
    EvidenceRequiredError,
    SourceAuthority,
    SubjectAmbiguousError,
    UnsafeProcedureClaimError,
    artifact_shard,
    authority_allows_correction,
    claim_dedup_key,
    memory_scope_key,
    normalize_external_url,
    normalize_local_locator,
    revision_current_at,
    valid_at,
    validate_claim_transition,
    validate_claim_value,
    validate_episode_transition,
    validate_evidence_spec,
    validate_procedure_value,
)
from iris_memory_core.domain.retention import (
    ForgetSelector,
    ForgetSelectorKind,
    InvalidRetentionError,
    LegalHold,
    RetentionPolicy,
    decayed_accessibility,
    forget_fingerprint,
    legal_hold_matches,
    policy_matches,
)

SEEDS = range(200)

_TRUTHY = (True, False)


@pytest.mark.parametrize("seed", SEEDS)
def test_claim_transition_machine_is_closed_under_random_statuses(seed: int) -> None:
    rng = random.Random(seed)
    statuses = list(CLAIM_TRANSITIONS)
    current = rng.choice(statuses)
    target = rng.choice(statuses)
    legal = target in CLAIM_TRANSITIONS[current]
    from iris_memory_core.domain.errors import DomainError

    if legal:
        validate_claim_transition(current, target)
    else:
        with pytest.raises(DomainError):
            validate_claim_transition(current, target)


@pytest.mark.parametrize("seed", SEEDS)
def test_bi_temporal_revision_visibility_interval(seed: int) -> None:
    rng = random.Random(seed)
    recorded = rng.randint(1, 10_000)
    superseded = recorded + rng.randint(1, 5_000) if rng.random() < 0.5 else None
    at = rng.randint(0, 20_000)
    expected = recorded <= at and (superseded is None or superseded > at)
    assert (
        revision_current_at(recorded_at_us=recorded, superseded_at_us=superseded, as_of_us=at)
        == expected
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_valid_time_interval_with_unbounded_ends(seed: int) -> None:
    rng = random.Random(seed)
    valid_from = rng.randint(-100, 100) if rng.random() < 0.7 else None
    valid_until = (
        valid_from + rng.randint(1, 200) if valid_from is not None and rng.random() < 0.7 else None
    )
    if valid_until is None and rng.random() < 0.3:
        valid_until = rng.randint(-100, 300)
    at = rng.randint(-300, 500)
    after_start = valid_from is not None and valid_from > at
    before_end = valid_until is not None and valid_until <= at
    expected = not (after_start or before_end)
    assert valid_at(valid_from_us=valid_from, valid_until_us=valid_until, at_us=at) == expected


@pytest.mark.parametrize("seed", SEEDS)
def test_dedup_key_distinguishes_every_component(seed: int) -> None:
    rng = random.Random(seed)
    letters = string.ascii_lowercase
    base = {
        "tenant_id": "t1",
        "agent_id": "a1",
        "subject_entity_id": "e1",
        "predicate": "likes",
        "value_hash": "h1",
        "scope_key": memory_scope_key("t1", "a1", None, None, None),
    }
    key = claim_dedup_key(**base)
    component = rng.choice(list(base))
    mutated = dict(base)
    if component in ("tenant_id", "agent_id", "subject_entity_id", "predicate", "value_hash"):
        mutated[component] = "".join(rng.choices(letters, k=8))
    else:
        mutated["scope_key"] = memory_scope_key("t1", "a1", None, "other-space", None)
    assert claim_dedup_key(**mutated) != key
    assert claim_dedup_key(**base) == key


@pytest.mark.parametrize("seed", SEEDS)
def test_procedure_guard_rejects_executable_payloads(seed: int) -> None:
    rng = random.Random(seed)
    forbidden_keys = (
        "exec",
        "code",
        "shell",
        "sql",
        "command",
        "script",
        "eval",
        "python",
        "bash",
    )
    key = rng.choice(forbidden_keys)
    if rng.random() < 0.5:
        bad = {"name": "x", "steps": [{"action": "run", "params": {key: "rm -rf /"}}]}
    else:
        bad = {"name": "x", "steps": [{"action": "run", key: "SELECT 1"}]}
    with pytest.raises((UnsafeProcedureClaimError, InvalidRetentionError)):
        validate_procedure_value(bad)
    # The declarative twin stays admissible.
    validate_procedure_value({"name": "x", "steps": [{"action": "say", "description": "d"}]})


@pytest.mark.parametrize("seed", SEEDS)
def test_local_locator_defense_rejects_traversal_shapes(seed: int) -> None:
    rng = random.Random(seed)
    shapes = [
        "../" + str(rng.randint(0, 99)) + "/x",
        "/etc/passwd",
        "~/.ssh/id_rsa",
        "..\\windows\\evil",
        "aa/../../bb/cc",
        "aa/",
        "/".join(["".join(rng.choices("ab", k=2)) for _ in range(rng.randint(1, 4))]) + "/" + "zz",
        "C:/evil",
        "aa//bb",
        "aa/./bb",
    ]
    locator = rng.choice(shapes)
    with pytest.raises(ArtifactInvalidError):
        normalize_local_locator(locator)


def test_local_locator_accepts_canonical_shape() -> None:
    assert normalize_local_locator("ab/01928374-afac-7d1e-8a2e-3f4b5c6d7e8f") == (
        "ab/01928374-afac-7d1e-8a2e-3f4b5c6d7e8f"
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_external_url_normalization(seed: int) -> None:
    rng = random.Random(seed)
    bad_urls = [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pass@example.com/x",
        "/relative/path",
        "not a url",
        "http://exa mple.com",
    ]
    if rng.random() < 0.5:
        url = rng.choice(bad_urls)
        with pytest.raises(ArtifactInvalidError):
            normalize_external_url(url)
    else:
        host = "".join(rng.choices(string.ascii_lowercase, k=8))
        path = "/" + "".join(rng.choices(string.ascii_lowercase + "/-", k=6))
        url = f"https://{host}.example.com{path}"
        assert normalize_external_url(url) == url


@pytest.mark.parametrize("seed", SEEDS)
def test_evidence_spec_validation(seed: int) -> None:
    rng = random.Random(seed)
    source_types = ("observation", "artifact", "episode", "claim", "note", "vibes")
    relations = ("supports", "contradicts", "corrects", "overrides")
    authorities = (*[item.value for item in SourceAuthority], "omniscient")
    source_type = rng.choice(source_types)
    relation = rng.choice(relations)
    authority = rng.choice(authorities)
    valid = source_type != "vibes" and relation != "overrides" and authority != "omniscient"
    try:
        validate_evidence_spec(
            source_type=source_type,
            source_id="id-1",
            relation=relation,
            source_authority=authority,
            evidence_span=None,
        )
        assert valid
    except Exception:
        assert not valid


@pytest.mark.parametrize("seed", SEEDS)
def test_authority_ladder_gates_corrections(seed: int) -> None:
    rng = random.Random(seed)
    authorities = [item.value for item in SourceAuthority]
    current = rng.choice(authorities)
    proposed = rng.choice(authorities)
    from iris_memory_core.domain.memory import SOURCE_AUTHORITY_RANK
    from iris_memory_core.domain.memory import SourceAuthority as SA

    expected = SOURCE_AUTHORITY_RANK[SA(proposed)] >= SOURCE_AUTHORITY_RANK[SA(current)]
    assert authority_allows_correction(current, proposed) == expected
    # Model inference can never correct an explicit correction.
    assert not authority_allows_correction("explicit_correction", "agent_inference")


@pytest.mark.parametrize("seed", SEEDS)
def test_selector_shape_validation(seed: int) -> None:
    rng = random.Random(seed)
    kind = rng.choice(
        [
            "resource",
            "subject_predicate",
            "session",
            "space",
            "data_request",
            "everything",
        ]
    )
    kwargs: dict[str, object] = {}
    if kind in ("resource", "everything") and rng.random() < 0.7:
        kwargs["resource_type"] = "claim"
        kwargs["resource_id"] = "c1"
    if kind in ("subject_predicate", "everything") and rng.random() < 0.7:
        kwargs["agent_id"] = "a1"
        kwargs["subject_entity_id"] = "e1"
    if kind in ("session", "everything") and rng.random() < 0.7:
        kwargs["space_id"] = "s1"
        kwargs["session_id"] = "ss1"
    if kind in ("space", "everything") and rng.random() < 0.7:
        kwargs["space_id"] = "s1"
    if kind == "data_request" and rng.random() < 0.7:
        kwargs["subject_entity_id"] = "e1"
    if kind == "resource":
        valid = "resource_type" in kwargs and "resource_id" in kwargs
    elif kind == "subject_predicate":
        valid = "agent_id" in kwargs and "subject_entity_id" in kwargs
    elif kind == "session":
        valid = "space_id" in kwargs and "session_id" in kwargs
    elif kind == "space":
        valid = "space_id" in kwargs
    elif kind == "data_request":
        valid = "subject_entity_id" in kwargs
    else:
        valid = False
    try:
        ForgetSelector(kind=kind, **kwargs)  # type: ignore[arg-type]
        assert valid, (kind, kwargs)
    except InvalidRetentionError:
        assert not valid, (kind, kwargs)


@pytest.mark.parametrize("seed", SEEDS)
def test_legal_hold_matching_wildcards(seed: int) -> None:
    rng = random.Random(seed)
    dims = {
        "space_id": rng.choice([None, "sp1", "sp2"]),
        "session_id": rng.choice([None, "se1"]),
        "subject_entity_id": rng.choice([None, "en1"]),
    }
    if all(value is None for value in dims.values()) or (
        dims["session_id"] is not None and dims["space_id"] is None
    ):
        dims["space_id"] = "sp1"

    hold = LegalHold(
        id="h1",
        tenant_id="t1",
        agent_id=None,
        reason_code="litigation",
        created_by="admin",
        created_us=1,
        released_us=None,
        **dims,
    )
    resource = {
        "space_id": rng.choice([None, "sp1", "sp2"]),
        "session_id": rng.choice([None, "se1", "se2"]),
        "subject_entity_id": rng.choice([None, "en1", "en2"]),
    }
    expected = all(
        hold_dim is None or hold_dim == resource_dim
        for hold_dim, resource_dim in (
            (hold.space_id, resource["space_id"]),
            (hold.session_id, resource["session_id"]),
            (hold.subject_entity_id, resource["subject_entity_id"]),
        )
    )
    assert (
        legal_hold_matches(
            hold,
            space_id=resource["space_id"],
            session_id=resource["session_id"],
            subject_entity_id=resource["subject_entity_id"],
        )
        == expected
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_policy_matching_thresholds_and_privacy(seed: int) -> None:
    rng = random.Random(seed)
    policy = RetentionPolicy(
        id="p1",
        tenant_id="t1",
        resource_type=rng.choice(["claim", "note"]),
        action=rng.choice(["decay", "archive", "delete"]),
        privacy_label=rng.choice([None, "space:sp1"]),
        threshold_days=rng.randint(1, 30),
        policy_version=1,
        enabled=rng.random() < 0.8,
        created_us=0,
        updated_us=0,
        created_by="admin",
    )
    labels: tuple[str, ...] = ("space:sp1",) if rng.random() < 0.6 else ("agent:a1",)
    now_us = 100 * 86_400_000_000
    updated_us = now_us - rng.randint(0, 60) * 86_400_000_000
    expected = policy.enabled and updated_us <= now_us - policy.threshold_days * 86_400_000_000
    if policy.privacy_label is not None and policy.privacy_label not in labels:
        expected = False
    assert (
        policy_matches(
            policy,
            resource_type=policy.resource_type,
            privacy_labels=labels,
            updated_us=updated_us,
            now_us=now_us,
        )
        == expected
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_decayed_accessibility_stays_at_or_above_the_floor(seed: int) -> None:
    rng = random.Random(seed)
    current = rng.random()
    decayed = decayed_accessibility(current)
    assert decayed >= 0.1 - 1e-12
    if current > 0.1 + 1e-12:
        assert decayed < current
    # Repeated decay converges to the floor monotonically from above it.
    again = decayed_accessibility(decayed)
    assert again <= decayed + 1e-12
    assert again >= 0.1 - 1e-12


@pytest.mark.parametrize("seed", SEEDS)
def test_claim_value_validation_bounds(seed: int) -> None:
    rng = random.Random(seed)
    from iris_memory_core.domain.memory import ALL_CLAIM_CATEGORIES

    category = rng.choice([*sorted(ALL_CLAIM_CATEGORIES), "vibes"])
    confidence = rng.choice([0.0, 0.5, 1.0, -0.1, 1.1])
    valid_from = rng.choice([None, 100])
    valid_until = rng.choice([None, 50, 100, 200])
    should_fail = (
        category == "vibes"
        or confidence in (-0.1, 1.1)
        or (valid_from is not None and valid_until is not None and valid_until <= valid_from)
    )
    try:
        validate_claim_value(
            predicate="likes",
            value_json='{"x":1}',
            canonical_text="text",
            category=category,
            confidence=confidence,
            importance=0.5,
            accessibility=1.0,
            valid_from_us=valid_from,
            valid_until_us=valid_until,
        )
        assert not should_fail
    except Exception:
        assert should_fail


@pytest.mark.parametrize("seed", SEEDS)
def test_forget_fingerprint_stability_and_distinctness(seed: int) -> None:
    selector = ForgetSelector(
        kind=ForgetSelectorKind.RESOURCE,
        resource_type="claim",
        resource_id=f"claim-{seed}",
    )
    assert forget_fingerprint(selector=selector, reason="r", erase_content=True) == (
        forget_fingerprint(selector=selector, reason="r", erase_content=True)
    )
    assert forget_fingerprint(selector=selector, reason="r", erase_content=True) != (
        forget_fingerprint(selector=selector, reason="other", erase_content=True)
    )


def test_subject_ambiguity_is_a_first_class_error() -> None:
    assert SubjectAmbiguousError.code == "subject_ambiguous"
    assert EvidenceRequiredError.code == "evidence_required"
    assert UnsafeProcedureClaimError.code == "unsafe_procedure_claim"


def test_artifact_shard_is_stable_prefix() -> None:
    artifact_id = "01928374-afac-7d1e-8a2e-3f4b5c6d7e8f"
    assert artifact_shard(artifact_id) == "01"
    assert artifact_shard(artifact_id) == artifact_id[:2]


def test_episode_transition_machine() -> None:
    from iris_memory_core.domain.errors import DomainError

    validate_episode_transition("open", "sealed")
    validate_episode_transition("sealed", "superseded")
    validate_episode_transition("open", "archived")
    with pytest.raises(DomainError):
        validate_episode_transition("tombstoned", "open")


def test_claim_status_enum_is_the_contracted_set() -> None:
    assert {item.value for item in ClaimStatus} == {
        "active",
        "disputed",
        "superseded",
        "retracted",
        "expired",
        "archived",
        "tombstoned",
    }


def test_locator_defense_rejects_symlink_style_names() -> None:
    # Path-shaped strings are never accepted even if they point inside root.
    for bad in ("aa/bb/cc", "a/b", "aa/" + "x" * 40):
        with pytest.raises(ArtifactInvalidError):
            normalize_local_locator(bad)
