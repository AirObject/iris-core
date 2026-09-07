"""Real Console credentials, immutable Provider drafts and idempotent transactions."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.console.provider_configs import (
    ProviderConfigCommands,
    ProviderSecretInput,
)
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    IdempotencyKeyReusedError,
    NotFoundError,
)
from iris_memory_core.domain.provider_configs import EmbeddingDefinition
from iris_memory_core.domain.vector import VectorSpaceConfig
from iris_memory_core.providers.configured import ConfiguredEmbeddingFactory
from iris_memory_core.providers.secrets import ProviderSecrets
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import invalidate, principal_for
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


@pytest.fixture
def provider(world: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    master = tmp_path / "provider-master.key"
    master.write_bytes(os.urandom(32))
    master.chmod(0o600)
    secrets = ProviderSecrets(
        master_key_file=master,
        allowed_references={world["tenant"]: frozenset({"env:ABSENT_FIXTURE_KEY"})},
        environment={},
    )
    factory = ConfiguredEmbeddingFactory(secrets)
    return {
        **world,
        "secrets": secrets,
        "service": ProviderConfigCommands(world["security"], secrets, factory),
        "principal": principal_for(world),
        "definition": EmbeddingDefinition(
            "openai-compatible",
            "https://provider.example/v1/embeddings",
            VectorSpaceConfig(model="fixture", dimension=2),
        ),
    }


def create(provider: dict[str, Any], **changes: Any) -> dict[str, object]:
    args = {
        "definition": provider["definition"],
        "secret": ProviderSecretInput("sealed", "private-provider-credential"),
        "reason": "operator_request",
        "idempotency_key": "create-one",
        **changes,
    }
    service: ProviderConfigCommands = provider["service"]
    return service.create(provider["principal"], **args)


def test_create_replay_patch_reseals_aad_and_discard_preserves_history(
    provider: dict[str, Any],
) -> None:
    service, principal = provider["service"], provider["principal"]
    first = create(provider)
    assert first == create(provider)
    identifier = str(first["id"])
    second = service.patch(
        principal,
        identifier,
        expected_revision=1,
        definition=replace(provider["definition"], label="Updated"),
        reason="operator_request",
        idempotency_key="patch-one",
    )
    assert second["revision"] == 2 and second["content_revision"] == 2
    with provider["store"].read() as tx:
        history = tx.providers.history(provider["tenant"], identifier)
        assert len(history) == 2 and history[0].secret.ciphertext != history[1].secret.ciphertext
        for revision in history:
            assert (
                provider["secrets"].resolve(
                    revision.tenant_id,
                    revision.config_id,
                    revision.content_revision,
                    revision.secret,
                )
                == "private-provider-credential"
            )
        assert (
            len(
                [
                    event
                    for event in tx.list_audit_events(provider["tenant"])
                    if event.action.startswith("console.provider.")
                ]
            )
            == 2
        )
        receipts = (
            tx.raw()
            .execute(
                "SELECT response_body FROM idempotency_records "
                "WHERE operation LIKE 'console.provider.%'"
            )
            .fetchall()
        )
    assert "private-provider-credential" not in json.dumps(
        [first, second, [list(row) for row in receipts]]
    )
    discarded = service.discard(
        principal,
        identifier,
        expected_revision=2,
        reason="operator_request",
        idempotency_key="discard-one",
    )
    assert discarded["status"] == "discarded"
    with pytest.raises(ConflictError):
        service.patch(
            principal,
            identifier,
            expected_revision=3,
            definition=provider["definition"],
            reason="operator_request",
            idempotency_key="illegal-edit",
        )
    assert service.detail(principal, identifier)["status"] == "discarded"


def test_unresolved_allowed_reference_is_a_draft_but_unknown_reference_is_rejected(
    provider: dict[str, Any],
) -> None:
    view = create(provider, secret=ProviderSecretInput("secret_ref", "env:ABSENT_FIXTURE_KEY"))
    assert view["resolved"] is False and view["status"] == "draft"
    with pytest.raises(ConflictError):
        create(
            provider,
            secret=ProviderSecretInput("secret_ref", "env:OTHER_TENANT_KEY"),
            idempotency_key="unknown-ref",
        )
    with pytest.raises(IdempotencyKeyReusedError):
        create(provider, secret=ProviderSecretInput("sealed", "changed-private-credential"))
    assert len(provider["service"].list(provider["principal"])) == 1


@pytest.mark.parametrize(
    "kind", ["revoked", "permission", "epoch", "session-expired", "key-expired", "revision"]
)
def test_changed_authority_before_transaction_creates_nothing(
    provider: dict[str, Any], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    runner = provider["security"].idempotency
    original = runner.run

    def race(**values: Any) -> Any:
        invalidate(provider, provider["principal"], kind)
        return original(**values)

    monkeypatch.setattr(runner, "run", race)
    with pytest.raises(AccessDeniedError):
        create(provider)
    with provider["store"].read() as tx:
        assert tx.providers.list_configs(provider["tenant"]) == ()


def test_six_concurrent_patches_have_one_success_and_retained_history(
    provider: dict[str, Any],
) -> None:
    identifier = str(create(provider)["id"])

    def patch(number: int) -> bool:
        try:
            provider["service"].patch(
                provider["principal"],
                identifier,
                expected_revision=1,
                definition=replace(provider["definition"], label=f"writer-{number}"),
                reason="operator_request",
                idempotency_key=f"patch-{number}",
            )
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(patch, range(6)))
    assert results.count(True) == 1
    with provider["store"].read() as tx:
        assert len(tx.providers.history(provider["tenant"], identifier)) == 2


def test_revoked_permission_prevents_receipt_replay_and_reads(provider: dict[str, Any]) -> None:
    identifier = str(create(provider)["id"])
    invalidate(provider, provider["principal"], "permission")
    with pytest.raises(AccessDeniedError):
        create(provider)
    with pytest.raises(AccessDeniedError):
        provider["service"].detail(provider["principal"], identifier)


def test_other_tenant_and_missing_config_are_not_found(provider: dict[str, Any]) -> None:
    create(provider)
    with pytest.raises(NotFoundError):
        provider["service"].detail(provider["principal"], "absent")
    with provider["store"].read() as tx, pytest.raises(NotFoundError):
        ProviderConfigCommands.current(tx, "other-tenant", str(create(provider)["id"]))


def test_receipt_authority_is_checked_after_replay_loading(
    provider: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    create(provider)
    runner = provider["security"].idempotency
    original = runner.run

    def revoke_after_read(**values: Any) -> Any:
        result = original(**values)
        invalidate(provider, provider["principal"], "revoked")
        return result

    monkeypatch.setattr(runner, "run", revoke_after_read)
    with pytest.raises(AccessDeniedError):
        create(provider)
    with provider["store"].read() as tx:
        assert len(tx.providers.list_configs(provider["tenant"])) == 1


@pytest.mark.parametrize("permission", ["system.read", "providers.manage", "no-purpose"])
def test_system_read_and_provider_write_are_separate_permissions(
    provider: dict[str, Any], permission: str
) -> None:
    identifier = str(create(provider)["id"])
    with provider["store"].write() as tx:
        key = tx.console.key(provider["principal"].key.id)
        grant = replace(
            key.grant,
            permissions=frozenset(
                {"system.read", "providers.manage"} if permission == "no-purpose" else {permission}
            ),
            data_purposes=frozenset() if permission == "no-purpose" else key.grant.data_purposes,
        )
        tx.console.save_key(
            replace(key, revision=key.revision + 1, grant=grant), expected_revision=key.revision
        )
    if permission == "system.read":
        assert provider["service"].detail(provider["principal"], identifier)["id"] == identifier
        with pytest.raises(AccessDeniedError):
            create(provider, idempotency_key="read-cannot-write")
    elif permission == "providers.manage":
        assert create(provider, idempotency_key="write-without-read")["status"] == "draft"
        assert provider["service"].detail(provider["principal"], identifier)["id"] == identifier
    else:
        with pytest.raises(AccessDeniedError):
            provider["service"].detail(provider["principal"], identifier)
        with pytest.raises(AccessDeniedError):
            create(provider, idempotency_key="missing-purpose")


def test_actual_other_tenant_principal_cannot_read_patch_or_discard(
    provider: dict[str, Any],
) -> None:
    identifier = str(create(provider)["id"])
    with provider["store"].write() as tx:
        tx.insert_tenant("other-provider-tenant", status="active")
    _, token = provider["security"].issue_offline(
        tenant_id="other-provider-tenant",
        label="Other tenant operator",
        description="Provider isolation fixture",
        template="owner",
        grant=provider["principal"].key.grant,
        expires_us=provider["store"].clock.now_us() + 3_600_000_000,
    )
    principal, _ = provider["security"].login(token, client_digest="1" * 64)
    service = provider["service"]
    assert service.list(principal) == ()
    with pytest.raises(NotFoundError):
        service.detail(principal, identifier)
    with pytest.raises(NotFoundError):
        service.patch(
            principal,
            identifier,
            expected_revision=1,
            definition=provider["definition"],
            reason="operator_request",
            idempotency_key="wrong-tenant-patch",
        )
    with pytest.raises(NotFoundError):
        service.discard(
            principal,
            identifier,
            expected_revision=1,
            reason="operator_request",
            idempotency_key="wrong-tenant-discard",
        )
