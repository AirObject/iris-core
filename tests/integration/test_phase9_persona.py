"""Phase 9 Persona vertical-slice integration tests."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.persona import (
    PERSONA_MANAGE_CAPABILITY,
    PERSONA_READ_CAPABILITY,
    PERSONA_REVIEW_CAPABILITY,
    PERSONA_STATE_CAPABILITY,
    PersonaService,
)
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    PersonaBaseRevisionStaleError,
    PersonaPolicyDeniedError,
    RevisionMismatchError,
)
from iris_memory_core.domain.model import BOOTSTRAP_PERSONA_CORE
from iris_memory_core.domain.persona import PersonaProposalStatus
from iris_memory_core.observability.metrics import Metrics
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock

TENANT = "phase9-tenant"


@pytest.fixture
def persona_world(clocked_store: Store, mutable_clock: MutableClock) -> dict[str, object]:
    provisioning = ProvisioningService(clocked_store)
    provisioning.create_tenant(TENANT)
    bootstrap_admin = AccessContext(
        tenant_id=TENANT,
        app_instance_id="provisioner",
        admin=True,
    )
    agent = provisioning.create_agent(bootstrap_admin, "Iris")
    capabilities = frozenset(
        {
            PERSONA_READ_CAPABILITY,
            PERSONA_STATE_CAPABILITY,
            PERSONA_REVIEW_CAPABILITY,
            PERSONA_MANAGE_CAPABILITY,
        }
    )
    admin = AccessContext(
        tenant_id=TENANT,
        app_instance_id="persona-admin",
        agent_ids=frozenset({agent.id}),
        capabilities=capabilities,
        admin=True,
    )
    app = AccessContext(
        tenant_id=TENANT,
        app_instance_id="persona-app",
        agent_ids=frozenset({agent.id}),
        capabilities=capabilities - {PERSONA_MANAGE_CAPABILITY},
    )
    return {
        "store": clocked_store,
        "clock": mutable_clock,
        "service": PersonaService(clocked_store, mutable_clock, IdempotencyManager(clocked_store)),
        "agent_id": agent.id,
        "admin": admin,
        "app": app,
    }


def test_bootstrap_expands_without_changing_content_or_hash(
    persona_world: dict[str, object],
) -> None:
    service = persona_world["service"]
    assert isinstance(service, PersonaService)
    view = service.current(persona_world["admin"], str(persona_world["agent_id"]))  # type: ignore[arg-type]
    assert view.revision.revision == 1
    assert view.revision.core == BOOTSTRAP_PERSONA_CORE
    assert len(view.revision.content_hash) == 64
    assert view.revision.change_reason == "bootstrap"
    assert view.policy.mode.value == "locked"
    assert view.state is None


def test_direct_admin_publish_and_rollback_create_new_revisions(
    persona_world: dict[str, object],
) -> None:
    service = persona_world["service"]
    admin = persona_world["admin"]
    agent_id = str(persona_world["agent_id"])
    assert isinstance(service, PersonaService)
    second = service.publish_revision(
        admin,  # type: ignore[arg-type]
        agent_id,
        expected_revision=1,
        core={"name": "Iris"},
        traits={"style": "warm"},
        narrative={"summary": "phase nine"},
        reason="initial_admin_publication",
    )
    assert second.revision == 2
    rolled = service.rollback(
        admin,  # type: ignore[arg-type]
        agent_id,
        target_revision=1,
        expected_revision=2,
        reason="operator_rollback",
    )
    assert rolled.revision == 3
    assert rolled.id != service.history(admin, agent_id)[-1].id  # type: ignore[arg-type]
    history = service.history(admin, agent_id)  # type: ignore[arg-type]
    assert [item.revision for item in history] == [3, 2, 1]
    bootstrap = history[-1]
    assert rolled.core == bootstrap.core == BOOTSTRAP_PERSONA_CORE
    assert rolled.traits == bootstrap.traits
    assert rolled.narrative == bootstrap.narrative
    assert rolled.content_hash == bootstrap.content_hash


def test_application_credential_cannot_publish(persona_world: dict[str, object]) -> None:
    service = persona_world["service"]
    assert isinstance(service, PersonaService)
    with pytest.raises(AccessDeniedError):
        service.publish_revision(
            persona_world["app"],  # type: ignore[arg-type]
            str(persona_world["agent_id"]),
            expected_revision=1,
            core={},
            traits={},
            narrative={},
            reason="forged",
        )


def test_state_ttl_returns_deterministically_to_baseline(persona_world: dict[str, object]) -> None:
    service = persona_world["service"]
    clock = cast(MutableClock, persona_world["clock"])
    agent_id = str(persona_world["agent_id"])
    assert isinstance(service, PersonaService)
    state = service.update_state(
        persona_world["app"],  # type: ignore[arg-type]
        agent_id,
        expected_revision=0,
        state={"mood": 0.7, "energy": 0.8, "focus": ["release"]},
        baseline={"mood": 0.0, "energy": 0.5},
        ttl_us=1_000,
    )
    clock.advance(1_000)
    assert service.expire_due_states() == 1
    view = service.current(persona_world["admin"], agent_id)  # type: ignore[arg-type]
    assert view.state is not None
    assert view.state.revision == state.revision + 1
    assert json.loads(view.state.state_json) == {"energy": 0.5, "mood": 0.0}
    assert service.expire_due_states() == 0


def test_locked_policy_rejects_proposal(persona_world: dict[str, object]) -> None:
    service = persona_world["service"]
    assert isinstance(service, PersonaService)
    with pytest.raises(PersonaPolicyDeniedError):
        service.create_proposal(
            persona_world["app"],  # type: ignore[arg-type]
            str(persona_world["agent_id"]),
            base_revision=1,
            patch={"traits": {"style": "warm"}},
            evidence_refs=[{"resource_type": "persona_state", "resource_id": "unknown"}],
            confidence=1.0,
            generator="test",
            generator_version="1",
        )


def test_manual_proposal_approval_publishes_after_policy_and_evidence(
    persona_world: dict[str, object],
) -> None:
    service = persona_world["service"]
    agent_id = str(persona_world["agent_id"])
    admin = persona_world["admin"]
    app = persona_world["app"]
    assert isinstance(service, PersonaService)
    service.replace_policy(
        admin,  # type: ignore[arg-type]
        agent_id,
        expected_revision=1,
        config={
            "mode": "manual",
            "allowed_fields": ["traits.style"],
            "max_single_delta": 1.0,
            "max_cumulative_delta": 1.0,
            "min_evidence": 1,
            "min_distinct_sources": 1,
            "min_confidence": 0.8,
        },
        reason="enable_manual",
    )
    state = service.update_state(
        app,  # type: ignore[arg-type]
        agent_id,
        expected_revision=0,
        state={"engagement": 0.8},
        baseline={"engagement": 0.5},
        ttl_us=10_000,
    )
    proposal = service.create_proposal(
        app,  # type: ignore[arg-type]
        agent_id,
        base_revision=1,
        patch={"traits": {"style": "warm"}},
        evidence_refs=[{"resource_type": "persona_state", "resource_id": state.id}],
        confidence=0.9,
        generator="deterministic-test",
        generator_version="1",
    )
    assert proposal.status is PersonaProposalStatus.PROPOSED
    published = service.approve(admin, agent_id, proposal.id, reason="reviewed")  # type: ignore[arg-type]
    assert published.status is PersonaProposalStatus.PUBLISHED
    assert published.published_revision_id
    assert service.current(admin, agent_id).revision.revision == 2  # type: ignore[arg-type]


def test_same_expected_revision_concurrency_has_exactly_one_winner(
    persona_world: dict[str, object],
) -> None:
    service = persona_world["service"]
    admin = persona_world["admin"]
    agent_id = str(persona_world["agent_id"])
    assert isinstance(service, PersonaService)

    def publish(index: int) -> str:
        try:
            service.publish_revision(
                admin,  # type: ignore[arg-type]
                agent_id,
                expected_revision=1,
                core={"name": f"Iris-{index}"},
                traits={},
                narrative={},
                reason="race",
            )
            return "won"
        except RevisionMismatchError:
            return "stale"

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(publish, range(50)))
    assert outcomes.count("won") == 1
    assert outcomes.count("stale") == 49
    assert service.current(admin, agent_id).revision.revision == 2  # type: ignore[arg-type]


def test_bounded_auto_publishes_only_non_sensitive_policy_valid_delta(
    persona_world: dict[str, object],
) -> None:
    service = cast(PersonaService, persona_world["service"])
    admin = cast(AccessContext, persona_world["admin"])
    app = cast(AccessContext, persona_world["app"])
    agent_id = str(persona_world["agent_id"])
    service.replace_policy(
        admin,
        agent_id,
        expected_revision=1,
        config={
            "mode": "bounded_auto",
            "allowed_fields": ["traits.style"],
            "max_single_delta": 1.0,
            "max_cumulative_delta": 1.0,
            "min_evidence": 1,
            "min_distinct_sources": 1,
            "min_confidence": 0.8,
        },
        reason="enable_bounded_auto",
    )
    state = service.update_state(
        app,
        agent_id,
        expected_revision=0,
        state={"engagement": 0.8},
        baseline={"engagement": 0.5},
        ttl_us=10_000,
    )
    proposal = service.create_proposal(
        app,
        agent_id,
        base_revision=1,
        patch={"traits": {"style": "warm"}},
        evidence_refs=[{"resource_type": "persona_state", "resource_id": state.id}],
        confidence=0.9,
        generator="fixed-test",
        generator_version="1",
    )
    assert proposal.status is PersonaProposalStatus.PUBLISHED
    assert service.current(admin, agent_id).revision.revision == 2


def test_proposal_is_rechecked_against_current_policy_before_approval(
    persona_world: dict[str, object],
) -> None:
    service = cast(PersonaService, persona_world["service"])
    admin = cast(AccessContext, persona_world["admin"])
    app = cast(AccessContext, persona_world["app"])
    agent_id = str(persona_world["agent_id"])
    service.replace_policy(
        admin,
        agent_id,
        expected_revision=1,
        config={
            "mode": "manual",
            "allowed_fields": ["traits.style"],
            "max_single_delta": 1.0,
            "max_cumulative_delta": 1.0,
            "min_evidence": 1,
            "min_distinct_sources": 1,
            "min_confidence": 0.8,
        },
        reason="enable_manual",
    )
    state = service.update_state(
        app,
        agent_id,
        expected_revision=0,
        state={"engagement": 0.8},
        baseline={"engagement": 0.5},
        ttl_us=10_000,
    )
    proposal = service.create_proposal(
        app,
        agent_id,
        base_revision=1,
        patch={"traits": {"style": "warm"}},
        evidence_refs=[{"resource_type": "persona_state", "resource_id": state.id}],
        confidence=0.9,
        generator="fixed-test",
        generator_version="1",
    )
    service.replace_policy(
        admin,
        agent_id,
        expected_revision=2,
        config={"mode": "locked"},
        reason="freeze_before_review",
    )
    with pytest.raises(PersonaPolicyDeniedError):
        service.approve(admin, agent_id, proposal.id, reason="must_recheck")
    assert service.current(admin, agent_id).revision.revision == 1


def test_stale_base_proposal_cannot_publish(persona_world: dict[str, object]) -> None:
    service = cast(PersonaService, persona_world["service"])
    admin = cast(AccessContext, persona_world["admin"])
    app = cast(AccessContext, persona_world["app"])
    agent_id = str(persona_world["agent_id"])
    service.replace_policy(
        admin,
        agent_id,
        expected_revision=1,
        config={
            "mode": "manual",
            "allowed_fields": ["traits.style"],
            "max_single_delta": 1.0,
            "max_cumulative_delta": 1.0,
            "min_evidence": 1,
            "min_distinct_sources": 1,
            "min_confidence": 0.8,
        },
        reason="enable_manual",
    )
    state = service.update_state(
        app,
        agent_id,
        expected_revision=0,
        state={"engagement": 0.8},
        baseline={"engagement": 0.5},
        ttl_us=10_000,
    )
    proposal = service.create_proposal(
        app,
        agent_id,
        base_revision=1,
        patch={"traits": {"style": "warm"}},
        evidence_refs=[{"resource_type": "persona_state", "resource_id": state.id}],
        confidence=0.9,
        generator="fixed-test",
        generator_version="1",
    )
    service.publish_revision(
        admin,
        agent_id,
        expected_revision=1,
        core={"name": "Iris"},
        traits={},
        narrative={},
        reason="intervening_admin_change",
    )
    with pytest.raises(PersonaBaseRevisionStaleError):
        service.approve(admin, agent_id, proposal.id, reason="stale")
    assert service.current(admin, agent_id).revision.revision == 2


def test_persona_outbox_contains_refs_only(persona_world: dict[str, object]) -> None:
    service = persona_world["service"]
    store = persona_world["store"]
    admin = persona_world["admin"]
    agent_id = str(persona_world["agent_id"])
    assert isinstance(service, PersonaService)
    assert isinstance(store, Store)
    service.publish_revision(
        admin,  # type: ignore[arg-type]
        agent_id,
        expected_revision=1,
        core={"name": "secret-persona-text"},
        traits={},
        narrative={},
        reason="publish",
    )
    with store.read() as tx:
        jobs = tx.outbox.list_jobs(tenant_id=TENANT, limit=100)
    revised = [item for item in jobs if item.job_kind == "persona.revised"]
    assert len(revised) == 1
    assert revised[0].payload["event_schema"] == "persona.revised.v1"
    assert "secret-persona-text" not in json.dumps(revised[0].payload)


def test_publish_idempotency_replays_first_revision(persona_world: dict[str, object]) -> None:
    service = persona_world["service"]
    admin = persona_world["admin"]
    agent_id = str(persona_world["agent_id"])
    assert isinstance(service, PersonaService)
    kwargs = {
        "expected_revision": 1,
        "core": {"name": "Iris"},
        "traits": {},
        "narrative": {},
        "reason": "idempotent_publish",
        "idempotency_key": "persona-publish-1",
    }
    first = service.publish_revision(admin, agent_id, **kwargs)  # type: ignore[arg-type]
    replay = service.publish_revision(admin, agent_id, **kwargs)  # type: ignore[arg-type]
    assert replay == first
    assert service.current(admin, agent_id).revision.revision == 2  # type: ignore[arg-type]


def test_state_expiry_worker_fences_stale_job_and_expires_current(
    persona_world: dict[str, object],
) -> None:
    from iris_memory_core.jobs.worker import OutboxWorker, phase9_handlers

    service = cast(PersonaService, persona_world["service"])
    store = cast(Store, persona_world["store"])
    clock = cast(MutableClock, persona_world["clock"])
    app = cast(AccessContext, persona_world["app"])
    admin = cast(AccessContext, persona_world["admin"])
    agent_id = str(persona_world["agent_id"])
    first = service.update_state(
        app,
        agent_id,
        expected_revision=0,
        state={"mood": 0.8},
        baseline={"mood": 0.0},
        ttl_us=1_000,
    )
    second = service.update_state(
        app,
        agent_id,
        expected_revision=first.revision,
        state={"mood": 0.4},
        baseline={"mood": -0.1},
        ttl_us=2_000,
    )
    worker = OutboxWorker(
        OutboxService(store, clock),
        phase9_handlers(clock),
        owner="persona-expiry-test",
    )

    clock.advance(1_000)
    assert worker.run_once()["completed"] == 1
    assert service.current(admin, agent_id).state == second

    clock.advance(1_000)
    assert worker.run_once()["completed"] == 1
    expired = service.current(admin, agent_id).state
    assert expired is not None
    assert expired.revision == second.revision + 1
    assert json.loads(expired.state_json) == {"mood": -0.1}


def _bounded_auto_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "mode": "bounded_auto",
        "allowed_fields": ["traits.style"],
        "max_single_delta": 1.0,
        "max_cumulative_delta": 1.0,
        "min_evidence": 1,
        "min_distinct_sources": 1,
        "min_confidence": 0.8,
    }
    config.update(overrides)
    return config


def test_evidence_diversity_span_cumulative_and_cooldown_each_deny_publication(
    persona_world: dict[str, object],
) -> None:
    """Every remaining ``bounded_auto`` policy limit denies on its own.

    The other policy tests all pin the permissive configuration, so the
    diversity, Evidence-span, cumulative-window and cooldown branches of
    ADR-0018 §3 were reachable only through the happy path.  Each limit is
    driven here in isolation with the others left wide open, so a regression
    in one cannot be masked by another.
    """
    service = cast(PersonaService, persona_world["service"])
    admin = cast(AccessContext, persona_world["admin"])
    app = cast(AccessContext, persona_world["app"])
    clock = cast(MutableClock, persona_world["clock"])
    agent_id = str(persona_world["agent_id"])
    policy_revision = 1

    def set_policy(**overrides: object) -> None:
        nonlocal policy_revision
        service.replace_policy(
            admin,
            agent_id,
            expected_revision=policy_revision,
            config=_bounded_auto_config(**overrides),
            reason="policy_limit_probe",
        )
        policy_revision += 1

    # Only the CURRENT Persona State is admissible Evidence, so one long-lived
    # state backs every probe below; two refs to it are still one source.
    current_state = service.update_state(
        app,
        agent_id,
        expected_revision=0,
        state={"mood": 0.1},
        baseline={"mood": 0.0},
        ttl_us=600_000_000,
    )
    evidence = {"resource_type": "persona_state", "resource_id": current_state.id}

    def propose(refs: list[dict[str, str]], *, style: str) -> object:
        return service.create_proposal(
            app,
            agent_id,
            base_revision=service.current(admin, agent_id).revision.revision,
            patch={"traits": {"style": style}},
            evidence_refs=refs,
            confidence=0.9,
            generator="fixed-test",
            generator_version="1",
        )

    # Evidence diversity: two refs to the SAME state are one source.
    set_policy(min_distinct_sources=2)
    with pytest.raises(PersonaPolicyDeniedError, match="diversity"):
        propose([evidence, evidence], style="warm")

    # Evidence span: a single-source proposal spans zero microseconds.
    set_policy(min_evidence_span_us=5_000_000)
    with pytest.raises(PersonaPolicyDeniedError, match="time span"):
        propose([evidence], style="warm")

    # Per-change magnitude: a fresh trait value is a full-magnitude change.
    set_policy(max_single_delta=0.1)
    with pytest.raises(PersonaPolicyDeniedError, match="per-change magnitude"):
        propose([evidence], style="warm")

    # Cumulative window: one publication already spends the whole budget.
    set_policy(cumulative_window_us=60_000_000, max_cumulative_delta=1.0)
    published = propose([evidence], style="warm")
    assert cast(Any, published).status is PersonaProposalStatus.PUBLISHED
    with pytest.raises(PersonaPolicyDeniedError, match="cumulative magnitude"):
        propose([evidence], style="formal")

    # Cooldown: the publication above is recent, so the next one is refused
    # even with the cumulative budget reopened.
    clock.advance(1_000_000)
    set_policy(cumulative_window_us=0, max_cumulative_delta=1.0, cooldown_us=60_000_000)
    with pytest.raises(PersonaPolicyDeniedError, match="cooldown"):
        propose([evidence], style="formal")

    # Past the cooldown the same proposal is admissible again — the limits
    # deny, they do not permanently freeze the agent.
    clock.advance(60_000_000)
    assert cast(Any, propose([evidence], style="formal")).status is (
        PersonaProposalStatus.PUBLISHED
    )


def test_restart_catch_up_expires_due_states_deterministically(
    persona_world: dict[str, object],
) -> None:
    """Exit gate 5: a process that was down over the TTL still converges.

    ``expire_due_states`` is the startup scan ADR-0018 §5 requires for the
    case where no expiry job ran at all.  Three runs over the same clock
    trajectory must reach the same baseline revision and value.
    """
    store = cast(Store, persona_world["store"])
    clock = cast(MutableClock, persona_world["clock"])
    admin = cast(AccessContext, persona_world["admin"])
    app = cast(AccessContext, persona_world["app"])
    agent_id = str(persona_world["agent_id"])

    outcomes = []
    for _attempt in range(3):
        service = PersonaService(store, clock, IdempotencyManager(store))
        current = service.current(admin, agent_id).state
        record = service.update_state(
            app,
            agent_id,
            expected_revision=current.revision if current else 0,
            state={"mood": 0.9},
            baseline={"mood": -0.2},
            ttl_us=1_000,
        )
        # The process is down across the whole TTL: no expiry job is claimed.
        clock.advance(10_000_000)
        assert service.expire_due_states() == 1
        # A second scan has nothing left to do — the catch-up is idempotent.
        assert service.expire_due_states() == 0
        expired = service.current(admin, agent_id).state
        assert expired is not None
        outcomes.append((expired.revision - record.revision, json.loads(expired.state_json)))

    assert outcomes == [(1, {"mood": -0.2})] * 3, outcomes


def test_recall_top_level_persona_tracks_publish_and_rollback(
    persona_world: dict[str, object],
) -> None:
    """Phase 9 exit gate 1 + the first quantified baseline.

    ``RecallResponse`` reports the Persona revision/hash at the top level and
    never as a candidate (ADR-0014 §3).  Phase 9 moved publication off the
    Phase 1 bootstrap seam, so the pointer that recall reads and the pointer
    ``PersonaService.current`` reads have to advance together — including
    across a rollback, which is a NEW revision carrying the OLD content hash.
    """
    from iris_memory_core.application.focus import FocusService
    from iris_memory_core.application.recall import (
        RecallService,
        StructuredRecallOrchestrator,
    )
    from iris_memory_core.application.recent import RecentContextService
    from iris_memory_core.application.state import StateService

    service = cast(PersonaService, persona_world["service"])
    store = cast(Store, persona_world["store"])
    clock = cast(MutableClock, persona_world["clock"])
    admin = cast(AccessContext, persona_world["admin"])
    agent_id = str(persona_world["agent_id"])

    with store.write() as tx:
        space_id = tx.insert_space(TENANT, "direct").id
        tx.advance_watermark(TENANT, agent_id, [("agent", agent_id, 1)])
    idempotency = IdempotencyManager(store)
    orchestrator = StructuredRecallOrchestrator(
        store,
        RecentContextService(store, clock),
        StateService(store, clock, idempotency=idempotency),
        FocusService(store, clock, idempotency=idempotency),
        clock=clock,
    )
    recall = RecallService(orchestrator, store, clock)
    recall_access = AccessContext(
        tenant_id=TENANT,
        app_instance_id="persona-reader",
        agent_ids=frozenset({agent_id}),
        allowed_space_ids=frozenset({space_id}),
    )

    probe = iter(range(1, 100))

    def recalled() -> tuple[int, str]:
        request = recall.build_request(
            request_id=f"persona-consistency-{next(probe)}",
            agent_id=agent_id,
            space_id=space_id,
            deadline_at_us=clock.now_us() + 5_000_000,
            topic="persona consistency",
        )
        result = recall.recall(recall_access, request)
        assert all(
            candidate.resource_type != "persona_revision" for candidate in result.candidates
        ), "persona must never be served as a candidate"
        return result.persona_revision, result.persona_content_hash

    def published() -> tuple[int, str]:
        view = service.current(admin, agent_id)
        return view.revision.revision, view.revision.content_hash

    assert recalled() == published()

    bootstrap_revision, bootstrap_hash = published()
    service.publish_revision(
        admin,
        agent_id,
        expected_revision=bootstrap_revision,
        core={"name": "Iris-consistency"},
        traits={"style": "warm"},
        narrative={},
        reason="consistency_publish",
    )
    after_publish = published()
    assert after_publish[0] == bootstrap_revision + 1
    assert after_publish[1] != bootstrap_hash
    assert recalled() == after_publish

    service.rollback(
        admin,
        agent_id,
        target_revision=bootstrap_revision,
        expected_revision=after_publish[0],
        reason="consistency_rollback",
    )
    after_rollback = published()
    # A rollback is a new revision that copies the target's content bytes:
    # the revision advances while the hash returns to the target's.
    assert after_rollback == (after_publish[0] + 1, bootstrap_hash)
    assert recalled() == after_rollback


def test_proposal_production_path_emits_low_cardinality_metric(
    persona_world: dict[str, object],
) -> None:
    store = cast(Store, persona_world["store"])
    clock = cast(MutableClock, persona_world["clock"])
    admin = cast(AccessContext, persona_world["admin"])
    app = cast(AccessContext, persona_world["app"])
    agent_id = str(persona_world["agent_id"])
    metrics = Metrics()
    service = PersonaService(
        store,
        clock,
        IdempotencyManager(store),
        metrics=metrics,
    )
    service.replace_policy(
        admin,
        agent_id,
        expected_revision=1,
        config={
            "mode": "manual",
            "allowed_fields": ["traits.style"],
            "max_single_delta": 1.0,
            "max_cumulative_delta": 1.0,
            "min_evidence": 1,
            "min_distinct_sources": 1,
            "min_confidence": 0.8,
        },
        reason="enable_manual",
    )
    state = service.update_state(
        app,
        agent_id,
        expected_revision=0,
        state={"engagement": 0.8},
        baseline={"engagement": 0.5},
        ttl_us=10_000,
    )
    service.create_proposal(
        app,
        agent_id,
        base_revision=1,
        patch={"traits": {"style": "warm"}},
        evidence_refs=[{"resource_type": "persona_state", "resource_id": state.id}],
        confidence=0.9,
        generator="fixed-test",
        generator_version="1",
    )
    counters = metrics.snapshot()["counters"]
    assert counters == [
        {
            "name": "iris_persona_proposals_total",
            "labels": {"outcome": "proposed"},
            "value": 1,
        }
    ]
