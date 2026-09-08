"""Actual draft command authority, immutable publication and atomic failure behavior."""

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.console.persona_drafts import ConsolePersonaDraftCommands
from iris_memory_core.domain.errors import AccessDeniedError, NotFoundError, RevisionMismatchError
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import invalidate, principal_for
from tests.integration.console.test_console_persona_commands import ready
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def command(world: Any, principal: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = dict(
        operation="persona.draft.create",
        draft_id=None,
        expected_revision=0,
        base_revision=1,
        policy_revision=1,
        fields={"core": {"name": "Iris"}, "traits": {"style": "draft content"}, "narrative": {}},
        source_refs=[],
        reason="operator_request",
        idempotency_key="create-draft",
    )
    values.update(overrides)
    return ConsolePersonaDraftCommands(world["security"]).mutate(
        principal, world["agent"], **values
    )


def test_draft_save_then_publish_and_discard_preserve_real_persona_history(world: Any) -> None:
    principal = ready(world)
    first = command(world, principal)
    second = command(world, principal, idempotency_key="second-draft")
    assert first.status == "draft" and first.revision == 1
    assert command(world, principal) == first
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1
    published = command(
        world,
        principal,
        operation="persona.draft.publish",
        draft_id=first.resource_id,
        expected_revision=1,
        fields={},
        idempotency_key="publish-draft",
    )
    assert published.status == "published" and published.revision == 2
    discarded = command(
        world,
        principal,
        operation="persona.draft.discard",
        draft_id=second.resource_id,
        expected_revision=1,
        fields={},
        base_revision=None,
        policy_revision=None,
        idempotency_key="discard-draft",
    )
    assert discarded.status == "discarded" and discarded.published_revision_id is None
    assert (
        command(
            world,
            principal,
            operation="persona.draft.discard",
            draft_id=second.resource_id,
            expected_revision=1,
            fields={},
            base_revision=None,
            policy_revision=None,
            idempotency_key="discard-draft",
        )
        == discarded
    )
    with pytest.raises(NotFoundError):
        command(world, principal, idempotency_key="second-draft")
    with world["store"].read() as tx:
        current = tx.personas.current(world["agent"])
        assert current.revision == 2 and current.id == published.published_revision_id
        assert len(tx.personas.history(world["agent"])) == 2
        assert tx.is_tombstoned(world["tenant"], "persona_draft", second.resource_id)


@pytest.mark.parametrize("operation", ["persona.draft.publish", "persona.draft.discard"])
def test_draft_publication_and_discard_require_actual_recent_reauth(
    world: Any, operation: str
) -> None:
    principal = principal_for(world)
    draft = command(world, principal)
    with pytest.raises(AccessDeniedError):
        command(
            world,
            principal,
            operation=operation,
            draft_id=draft.resource_id,
            expected_revision=1,
            fields={},
            base_revision=None if operation.endswith("discard") else 1,
            policy_revision=None if operation.endswith("discard") else 1,
            idempotency_key="sensitive-draft",
        )


def test_draft_publication_late_failure_rolls_back_real_persona_and_receipt(
    world: Any, monkeypatch: Any
) -> None:
    from iris_memory_core.storage.persona_draft import PersonaDraftRepository

    principal = ready(world)
    draft = command(world, principal)
    with monkeypatch.context() as patch:

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("draft close unavailable")

        patch.setattr(PersonaDraftRepository, "mark_published", fail)
        with pytest.raises(RuntimeError, match="draft close unavailable"):
            command(
                world,
                principal,
                operation="persona.draft.publish",
                draft_id=draft.resource_id,
                expected_revision=1,
                fields={},
                idempotency_key="publish-draft",
            )
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1
        assert len(tx.personas.history(world["agent"])) == 1
        assert (
            tx.persona_drafts.get(world["tenant"], world["agent"], draft.resource_id).status
            == "draft"
        )
    assert (
        command(
            world,
            principal,
            operation="persona.draft.publish",
            draft_id=draft.resource_id,
            expected_revision=1,
            fields={},
            idempotency_key="publish-draft",
        ).status
        == "published"
    )


@pytest.mark.parametrize(
    "change", ["revoked", "permission", "epoch", "session-expired", "key-expired"]
)
def test_cached_draft_receipt_rechecks_authority_after_lookup(
    world: Any, monkeypatch: Any, change: str
) -> None:
    principal = ready(world)
    command(world, principal)
    runner = world["security"].idempotency
    original = runner.run

    def replay_then_invalidate(**values: Any) -> Any:
        result = original(**values)
        assert result.replayed
        invalidate(world, principal, change)
        return result

    monkeypatch.setattr(runner, "run", replay_then_invalidate)
    with pytest.raises(AccessDeniedError):
        command(world, principal)


def test_policy_change_fences_stored_draft_publication(world: Any) -> None:
    from tests.integration.console.test_console_persona_policy import replace_policy

    principal = ready(world)
    draft = command(world, principal)
    replace_policy(world, principal)
    with pytest.raises(RevisionMismatchError):
        command(
            world,
            principal,
            operation="persona.draft.publish",
            draft_id=draft.resource_id,
            expected_revision=1,
            fields={},
            idempotency_key="publish-old-policy",
        )
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1


def test_draft_receipt_and_publication_reauthorize_transitive_sources(world: Any) -> None:
    from tests.integration.console.test_console_persona_commands import state_evidence

    state, task = state_evidence(world)
    principal = ready(world)
    refs = [{"resource_type": "persona_state", "resource_id": state.id}]
    draft = command(world, principal, source_refs=refs)
    with world["store"].write() as tx:
        tx.raw().execute(
            "UPDATE task_revisions SET privacy_labels='[\"restricted\"]' WHERE task_id=?", (task,)
        )
        key = tx.console.key(principal.key.id)
        tx.console.save_key(
            replace(
                key, revision=key.revision + 1, grant=replace(key.grant, allow_restricted=False)
            ),
            expected_revision=key.revision,
        )
        principal = replace(principal, key=tx.console.key(key.id))
    with pytest.raises(NotFoundError):
        command(world, principal, source_refs=refs)
    with pytest.raises(NotFoundError):
        command(
            world,
            principal,
            operation="persona.draft.publish",
            draft_id=draft.resource_id,
            expected_revision=1,
            fields={},
            idempotency_key="publish-hidden-source",
        )
    with world["store"].read() as tx:
        assert tx.personas.current(world["agent"]).revision == 1


@pytest.mark.parametrize("restricted", [False, True])
def test_draft_requires_explicit_parent_wide_persona_publish(world: Any, restricted: bool) -> None:
    from iris_memory_core.domain.console import Selector
    from tests.integration.console.test_console_reads import grant_for

    grant = grant_for(world, permissions=frozenset({"memory.read", "persona.publish"}))
    if not restricted:
        grant = replace(
            grant,
            permissions=frozenset({"memory.read", "memory.write"}),
            space_selector=Selector("all"),
        )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="draft boundary",
        description="actual parent authority",
        template="maintainer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    principal, _ = world["security"].login(token, client_digest="0" * 64)
    with pytest.raises(AccessDeniedError):
        command(world, principal)
    with world["store"].read() as tx:
        assert tx.raw().execute("SELECT COUNT(*) FROM persona_drafts").fetchone()[0] == 0


def test_real_concurrent_draft_publications_share_current_cas(world: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor

    principal = ready(world)
    drafts = [command(world, principal, idempotency_key=f"draft-{i}") for i in range(8)]

    def publish(number: int) -> str:
        try:
            command(
                world,
                principal,
                operation="persona.draft.publish",
                draft_id=drafts[number].resource_id,
                expected_revision=1,
                fields={},
                idempotency_key=f"publish-{number}",
            )
            return "published"
        except RevisionMismatchError:
            return "stale"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(publish, range(8)))
    assert outcomes.count("published") == 1 and outcomes.count("stale") == 7
    with world["store"].read() as tx:
        current = tx.personas.current(world["agent"])
        assert current.revision == 2
        assert (
            tx.raw()
            .execute("SELECT COUNT(*) FROM persona_drafts WHERE status='published'")
            .fetchone()[0]
            == 1
        )
        assert (
            tx.raw()
            .execute("SELECT published_revision_id FROM persona_drafts WHERE status='published'")
            .fetchone()[0]
            == current.id
        )


def test_cross_tenant_draft_lookup_and_command_fail_without_disclosure(world: Any) -> None:
    from iris_memory_core.application.provisioning import ProvisioningService
    from iris_memory_core.domain.console import OperatorGrant, Selector

    principal = ready(world)
    draft = command(world, principal)
    ProvisioningService(world["store"]).create_tenant("other-draft-tenant")
    _, token = world["security"].issue_offline(
        tenant_id="other-draft-tenant",
        label="another tenant",
        description="draft isolation",
        template="owner",
        grant=OperatorGrant(
            frozenset({"memory.read", "persona.publish"}),
            Selector("all"),
            Selector("all"),
            Selector("all"),
            Selector("all"),
            data_purposes=frozenset({"console.manage"}),
        ),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    alien, _ = world["security"].login(token, client_digest="1" * 64)
    alien = world["security"].reauth(alien, token)
    with pytest.raises(NotFoundError):
        ConsolePersonaDraftCommands(world["security"]).context(
            alien, world["agent"], draft.resource_id
        )
    with pytest.raises(NotFoundError):
        command(
            world,
            alien,
            operation="persona.draft.discard",
            draft_id=draft.resource_id,
            expected_revision=1,
            fields={},
            base_revision=None,
            policy_revision=None,
            idempotency_key="alien-discard",
        )
    with world["store"].read() as tx:
        assert (
            tx.persona_drafts.get(world["tenant"], world["agent"], draft.resource_id).status
            == "draft"
        )
