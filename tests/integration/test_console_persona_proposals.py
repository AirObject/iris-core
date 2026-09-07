"""Console proposal creation cannot publish; review retains Policy and status fencing."""

import json
from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.console.persona_proposals import ConsolePersonaProposalCommands
from iris_memory_core.application.persona import PERSONA_MANAGE_CAPABILITY, PersonaService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.console import Selector
from iris_memory_core.domain.errors import AccessDeniedError, ConflictError, RevisionMismatchError
from iris_memory_core.domain.surface import SurfaceMode
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_persona_commands import publish, ready, state_evidence
from tests.integration.test_console_reads import grant_for
from tests.integration.test_console_reads import world as world_fixture
from tests.integration.test_phase9_persona import _bounded_auto_config

auth = auth_fixture
world = world_fixture


def prepare(world: Any) -> tuple[Any, list[dict[str, object]]]:
    store = world["store"]
    service = PersonaService(store, store.clock)
    service.replace_policy(
        replace(world["access"], capabilities=frozenset({PERSONA_MANAGE_CAPABILITY})),
        world["agent"],
        expected_revision=1,
        config=_bounded_auto_config(),
        reason="proposal_test",
    )
    state, _ = state_evidence(world)
    grant = replace(
        grant_for(world, permissions=frozenset({"memory.read", "memory.write"})),
        space_selector=Selector("all"),
    )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="proposer",
        description="cannot publish",
        template="maintainer",
        grant=grant,
        expires_us=store.clock.now_us() + 3_600_000_000,
    )
    principal, _ = world["security"].login(token, client_digest="0" * 64)
    world["proposal_token"] = token
    return principal, [{"resource_type": "persona_state", "resource_id": state.id}]


def command(world: Any, principal: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = dict(
        operation="persona.proposal.create",
        base_revision=1,
        expected_policy_revision=2,
        fields={"patch": {"traits": {"style": "warm"}}, "confidence": 0.9, "ttl_us": 600_000_000},
        evidence_refs=[],
        proposal_id=None,
        reason="operator_request",
        idempotency_key="managed-proposal",
    )
    values.update(overrides)
    return ConsolePersonaProposalCommands(world["security"]).mutate(
        principal, world["agent"], **values
    )


def test_required_proposal_create_never_auto_publishes_and_approval_rechecks_authority(
    world: Any,
) -> None:
    writer, refs = prepare(world)
    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="proposal management"
    )
    first = command(world, writer, evidence_refs=refs)
    assert first.status == "proposed" and first.published_revision_id is None
    assert command(world, writer, evidence_refs=refs) == first
    with store.read() as tx:
        proposal = tx.personas.proposal(first.resource_id)
        assert proposal.generator == "console:" + writer.key.id
        assert proposal.generator_version == "console-v1"
        assert (
            tx.raw()
            .execute(
                "SELECT reason_code FROM audit_events "
                "WHERE action='persona.proposal_created' AND resource_id=?",
                (proposal.id,),
            )
            .fetchone()[0]
            == "operator_request"
        )
        assert json.loads(proposal.policy_evaluation_json)["requires_review"] is True
        assert tx.personas.current(world["agent"]).revision == 1
    review = dict(
        operation="persona.proposal.approve",
        proposal_id=first.resource_id,
        fields={},
        idempotency_key="approve-managed-proposal",
    )
    with pytest.raises(AccessDeniedError):
        command(world, writer, **review)
    reviewer = ready(world)
    approved = command(world, reviewer, **review)
    assert approved.status == "published" and approved.published_revision_id
    assert command(world, reviewer, **review) == approved
    with store.read() as tx:
        current = tx.personas.current(world["agent"])
        assert current.id == approved.published_revision_id and current.revision == 2
        assert json.loads(current.traits) == {"style": "warm"}
        assert tx.personas.proposal(first.resource_id).reviewed_by == "console:" + reviewer.key.id


def test_reject_can_close_stale_and_expired_proposal_without_changing_persona(world: Any) -> None:
    writer, refs = prepare(world)
    proposal = command(
        world,
        writer,
        evidence_refs=refs,
        fields={"patch": {"traits": {"style": "warm"}}, "confidence": 0.9, "ttl_us": 1000},
    )
    reviewer = ready(world)
    current = publish(world, reviewer, expected_policy_revision=2)
    world["store"].clock.advance(1001)
    rejected = command(
        world,
        reviewer,
        operation="persona.proposal.reject",
        proposal_id=proposal.resource_id,
        expected_policy_revision=None,
        fields={},
        idempotency_key="reject-old-proposal",
    )
    assert rejected.status == "rejected" and rejected.published_revision_id is None
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).id == current.resource_id
        assert tx.personas.proposal(proposal.resource_id).patch_json


def test_policy_race_and_late_publication_failure_leave_proposal_proposed(
    world: Any, monkeypatch: Any
) -> None:
    writer, refs = prepare(world)
    proposal = command(world, writer, evidence_refs=refs)
    reviewer = ready(world)
    review = dict(
        operation="persona.proposal.approve",
        proposal_id=proposal.resource_id,
        fields={},
        idempotency_key="atomic-approval",
    )
    with pytest.raises(RevisionMismatchError):
        command(
            world,
            reviewer,
            **{**review, "expected_policy_revision": 1, "idempotency_key": "stale-policy-approval"},
        )
    with monkeypatch.context() as patch:

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("notification failed")

        patch.setattr(PersonaService, "_notification", fail)
        with pytest.raises(RuntimeError, match="notification failed"):
            command(world, reviewer, **review)
    with world["store"].read() as tx:
        assert tx.personas.proposal(proposal.resource_id).status.value == "proposed"
        assert tx.personas.current(world["agent"]).revision == 1
    assert command(world, reviewer, **review).status == "published"


def test_only_one_concurrent_reviewer_can_transition_proposal(world: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor

    writer, refs = prepare(world)
    proposal = command(world, writer, evidence_refs=refs)
    reviewer = ready(world)

    def attempt(number: int) -> str:
        try:
            command(
                world,
                reviewer,
                operation="persona.proposal.reject",
                proposal_id=proposal.resource_id,
                expected_policy_revision=None,
                fields={},
                idempotency_key=f"review-{number}",
            )
            return "rejected"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(attempt, range(12)))
    assert outcomes.count("rejected") == 1 and outcomes.count("conflict") == 11


@pytest.mark.parametrize(
    "change",
    ["locked-policy", "expired-proposal", "expired-evidence", "stale-evidence", "hidden-evidence"],
)
def test_approval_rechecks_current_policy_expiry_and_evidence(world: Any, change: str) -> None:
    from iris_memory_core.domain.errors import NotFoundError, PersonaPolicyDeniedError

    writer, refs = prepare(world)
    proposal = command(world, writer, evidence_refs=refs)
    reviewer = ready(world)
    store = world["store"]
    policy_revision = 2
    if change == "locked-policy":
        PersonaService(store, store.clock).replace_policy(
            replace(world["access"], capabilities=frozenset({PERSONA_MANAGE_CAPABILITY})),
            world["agent"],
            expected_revision=2,
            config={"mode": "locked"},
            reason="freeze_policy",
        )
        policy_revision = 3
    else:
        with store.write() as tx:
            state = tx.personas.current_state(world["agent"])
            assert state is not None
            if change == "expired-proposal":
                tx.raw().execute(
                    "UPDATE persona_proposals SET expires_us=created_us+1 WHERE id=?",
                    (proposal.resource_id,),
                )
            elif change == "expired-evidence":
                tx.raw().execute(
                    "UPDATE persona_states SET expires_us=started_us+1 WHERE id=?", (state.id,)
                )
            elif change == "stale-evidence":
                tx.personas.put_state(
                    tenant_id=world["tenant"],
                    agent_id=world["agent"],
                    expected_revision=state.revision,
                    state_json="{}",
                    baseline_json="{}",
                    source_refs_json="[]",
                    started_us=store.clock.now_us(),
                    expires_us=state.expires_us,
                    created_by="new-state",
                )
            else:
                task = json.loads(state.source_refs)[0]["resource_id"]
                tx.raw().execute(
                    "UPDATE task_revisions SET privacy_labels='[\"restricted\"]' WHERE task_id=?",
                    (task,),
                )
                key = tx.console.key(reviewer.key.id)
                tx.console.save_key(
                    replace(
                        key,
                        revision=key.revision + 1,
                        grant=replace(key.grant, allow_restricted=False),
                    ),
                    expected_revision=key.revision,
                )
                reviewer = replace(reviewer, key=tx.console.key(key.id))
        store.clock.advance(2)
    with pytest.raises((ConflictError, NotFoundError, PersonaPolicyDeniedError)):
        command(
            world,
            reviewer,
            operation="persona.proposal.approve",
            proposal_id=proposal.resource_id,
            expected_policy_revision=policy_revision,
            fields={},
            idempotency_key="changed-evidence-approval",
        )
    with store.read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1
        assert tx.personas.proposal(proposal.resource_id).status.value == "proposed"


@pytest.mark.parametrize("operation", ["create", "approve"])
def test_proposal_receipt_rechecks_authority_after_cache_lookup(
    world: Any, monkeypatch: Any, operation: str
) -> None:
    from tests.integration.test_console_commands import invalidate

    writer, refs = prepare(world)
    proposal = command(world, writer, evidence_refs=refs)
    principal = writer
    arguments: dict[str, Any] = {"evidence_refs": refs}
    if operation == "approve":
        principal = ready(world)
        arguments = dict(
            operation="persona.proposal.approve",
            proposal_id=proposal.resource_id,
            fields={},
            idempotency_key="replayed-approval",
        )
        command(world, principal, **arguments)
    runner = world["security"].idempotency
    original = runner.run

    def replay_then_revoke(**values: Any) -> Any:
        result = original(**values)
        assert result.replayed
        invalidate(world, principal, "revoked")
        return result

    monkeypatch.setattr(runner, "run", replay_then_revoke)
    with pytest.raises(AccessDeniedError):
        command(world, principal, **arguments)


def test_proposal_creation_requires_parent_wide_agent_grant(world: Any) -> None:
    from tests.integration.test_console_commands import principal_for

    _, refs = prepare(world)
    with pytest.raises(AccessDeniedError):
        command(world, principal_for(world, limited=True), evidence_refs=refs)
    with world["store"].read() as tx:
        assert not tx.personas.proposals(world["agent"])
