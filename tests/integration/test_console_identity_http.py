"""Tenant registry management preserves explicit verification and historical identity."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_claim_http import writer_token
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def create(client: Any, csrf: str, collection: str, fields: dict[str, Any], **extra: Any) -> Any:
    return client.post(
        "/v1/memory/" + collection,
        headers=headers(csrf),
        json={"fields": fields, "reason_code": "operator_request", **extra},
    )


def entity(client: Any, csrf: str, name: str = "Reviewed entity", **extra: Any) -> str:
    response = create(client, csrf, "entities", {"kind": "person", "display_name": name}, **extra)
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


def identity(client: Any, csrf: str, entity_id: str | None = None) -> str:
    response = create(
        client,
        csrf,
        "identities",
        {
            "provider": "manual",
            "realm": "console-review",
            "external_id": "account-123",
            **({"entity_id": entity_id} if entity_id else {}),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


def propose(client: Any, csrf: str, external_id: str, entity_id: str) -> str:
    response = create(
        client, csrf, "bindings", {"external_identity_id": external_id, "entity_id": entity_id}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


def action(
    client: Any, csrf: str, identifier: str, name: str, revision: int, *, key: Any = None
) -> Any:
    return client.post(
        "/v1/memory/bindings/" + identifier + ":" + name,
        headers=key or headers(csrf),
        json={"expected_revision": revision, "reason_code": "operator_request"},
    )


def test_registry_creation_confirmation_revocation_history_and_replay(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.application.identity import IdentityService

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        registry = client.get("/v1/memory/resource-types")
        assert registry.status_code == 200, registry.text
        validate_response("ResourceTypePage", registry.json())
        entity_id = entity(client, csrf)
        second = entity(client, csrf)
        assert second != entity_id  # same display name is never an identity match
        external_id = identity(client, csrf, entity_id)
        view = IdentityService(store).identity_view(world["access"], external_id)
        assert view.current_entity_id is None
        lookup = client.get("/v1/lookups/identities").json()["data"]
        assert external_id in {item["id"] for item in lookup}
        key = headers(csrf)
        body = {
            "fields": {"external_identity_id": external_id, "entity_id": entity_id},
            "reason_code": "operator_request",
        }
        desc = next(row for row in registry.json()["data"] if row["collection"] == "bindings")
        Draft202012Validator(desc["create_schema"]).validate(body)
        created = client.post("/v1/memory/bindings", headers=key, json=body)
        assert created.status_code == 201, created.text
        validate_response("ResourceViewEnvelope", created.json())
        initial = created.json()["data"]
        identifier = initial["id"]
        path = "/v1/memory/bindings/" + identifier
        assert initial["status"] == "proposed"
        assert client.get(path).json()["data"]["available_actions"] == ["confirm", "revoke"]
        before = store.clock.now_us()
        store.clock.advance(1000)
        confirm_key = headers(csrf)
        confirmed = action(client, csrf, identifier, "confirm", 1, key=confirm_key)
        assert confirmed.status_code == 200, confirmed.text
        verified = confirmed.json()["data"]
        assert verified["revision"] == 2 and verified["status"] == "verified"
        assert (
            action(client, csrf, identifier, "confirm", 1, key=confirm_key).json()["data"]
            == verified
        )
        verified_at = store.clock.now_us()
        with store.read() as tx:
            assert tx.get_entity(entity_id).state.value == "canonical"
            assert tx.get_binding(identifier).confirmed_by == "console:" + world["owner"].id
            assert tx.get_binding(identifier).proof_digest.startswith("sha256:")
        assert (
            IdentityService(store)
            .identity_view(world["access"], external_id, at_us=before)
            .entity_id_at_ingest
            is None
        )
        assert (
            IdentityService(store).identity_view(world["access"], external_id).current_entity_id
            == entity_id
        )
        store.clock.advance(1000)
        assert action(client, csrf, identifier, "revoke", 1).status_code == 409
        revoked = action(client, csrf, identifier, "revoke", 2)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["data"]["status"] == "revoked"
        assert (
            IdentityService(store).identity_view(world["access"], external_id).current_entity_id
            is None
        )
        assert (
            IdentityService(store)
            .identity_view(world["access"], external_id, at_us=verified_at)
            .entity_id_at_ingest
            == entity_id
        )
        replay = client.post("/v1/memory/bindings", headers=key, json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json()["data"] == initial
        assert (
            action(client, csrf, identifier, "confirm", 1, key=confirm_key).json()["data"]
            == verified
        )
        history = client.get(path + "/history").json()["data"]
        assert [item["status"] for item in history] == ["revoked", "verified", "proposed"]
        assert client.get(path).json()["data"]["available_actions"] == []
        assert action(client, csrf, identifier, "confirm", 3).status_code == 409


@pytest.mark.parametrize(
    "resource,field",
    [
        ("entities", "state"),
        ("entities", "tenant_id"),
        ("entities", "origin"),
        ("identities", "verified"),
        ("identities", "scope"),
        ("bindings", "proof_digest"),
        ("bindings", "method"),
        ("bindings", "confidence"),
        ("bindings", "confirmed_by"),
    ],
)
def test_registry_rejects_forged_authority_and_scope(
    world: dict[str, Any], resource: str, field: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        base = {
            "entities": {"kind": "person", "display_name": "Name"},
            "identities": {"provider": "manual", "realm": "r", "external_id": "e"},
            "bindings": {"entity_id": world["entities"][0], "external_identity_id": "absent"},
        }[resource]
        body = {"fields": base, "reason_code": "operator_request"}
        (body if field in {"tenant_id", "origin", "scope"} else base)[field] = "forged"
        response = client.post("/v1/memory/" + resource, headers=headers(csrf), json=body)
        assert response.status_code == 400, response.text


@pytest.mark.parametrize("resource", ["entities", "identities", "bindings"])
def test_global_registry_is_not_writable_through_downward_read_grant(
    world: dict[str, Any], resource: str
) -> None:
    owner, csrf = signed_in(world["app"], world["token"])
    with owner:
        entity_id = entity(owner, csrf)
        external_id = identity(owner, csrf, entity_id)
    client, csrf = signed_in(world["app"], writer_token(world, restricted=False))
    with client:
        assert client.get("/v1/memory/entities/" + entity_id).status_code == 200
        fields = {
            "entities": {"kind": "person", "display_name": "global"},
            "identities": {"provider": "manual", "realm": "r", "external_id": "other"},
            "bindings": {"entity_id": entity_id, "external_identity_id": external_id},
        }[resource]
        result = create(client, csrf, resource, fields)
        assert result.status_code == 403, result.text


def test_identity_natural_key_reuse_never_rewrites_association(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        first, second = entity(client, csrf, "First"), entity(client, csrf, "Second")
        external_id = identity(client, csrf, first)
        assert identity(client, csrf) == external_id
        result = create(
            client,
            csrf,
            "identities",
            {
                "provider": "manual",
                "realm": "console-review",
                "external_id": "account-123",
                "entity_id": second,
            },
        )
        assert result.status_code == 409, result.text
        with world["store"].read() as tx:
            assert tx.get_external_identity(external_id).entity_id == first


def test_rival_binding_conflict_is_committed_once_and_can_be_adjudicated(
    world: dict[str, Any],
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        first, second = entity(client, csrf, "A"), entity(client, csrf, "B")
        external_id = identity(client, csrf)
        winner, loser = (
            propose(client, csrf, external_id, first),
            propose(client, csrf, external_id, second),
        )
        assert action(client, csrf, winner, "confirm", 1).status_code == 200
        key = headers(csrf)
        for _ in range(2):
            conflict = action(client, csrf, loser, "confirm", 1, key=key)
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()["error"]["code"] == "binding_conflict"
            assert winner not in conflict.text
        with world["store"].read() as tx:
            assert tx.get_binding(loser).state.value == "conflicted"
            assert tx.get_binding(loser).revision == 2
            assert tx.get_entity(second).state.value == "provisional"
        assert action(client, csrf, winner, "revoke", 2).status_code == 200
        assert action(client, csrf, loser, "confirm", 2).status_code == 200
        with world["store"].read() as tx:
            verified = tx.verified_binding_for(external_id)
            assert verified is not None and verified.id == loser


def test_hidden_identity_dedup_and_rival_do_not_mutate_or_disclose(world: dict[str, Any]) -> None:
    grant = replace(world["owner"].grant, allow_restricted=True)
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="private registry",
        description="test",
        template="owner",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    owner, csrf = signed_in(world["app"], token)
    with owner:
        hidden = entity(owner, csrf, "Hidden", privacy_labels=["restricted"])
        hidden_identity = identity(owner, csrf, hidden)
        shared = create(
            owner,
            csrf,
            "identities",
            {"provider": "manual", "realm": "shared", "external_id": "rival"},
        ).json()["data"]["id"]
        winner = propose(owner, csrf, shared, hidden)
        assert action(owner, csrf, winner, "confirm", 1).status_code == 200
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        assert client.get("/v1/memory/identities/" + hidden_identity).status_code == 404
        result = create(
            client,
            csrf,
            "identities",
            {"provider": "manual", "realm": "console-review", "external_id": "account-123"},
        )
        assert result.status_code == 404, result.text
        visible = entity(client, csrf, "Visible")
        loser = propose(client, csrf, shared, visible)
        conflict = action(client, csrf, loser, "confirm", 1)
        assert conflict.status_code == 404, conflict.text
        with world["store"].read() as tx:
            assert tx.get_binding(loser).revision == 1


@pytest.mark.parametrize("operation", ["create", "confirm", "revoke"])
@pytest.mark.parametrize("deleted", ["entity", "external_identity", "binding"])
def test_binding_replay_rechecks_endpoints_and_result(
    world: dict[str, Any], operation: str, deleted: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        entity_id = entity(client, csrf)
        external_id = identity(client, csrf)
        path = "/v1/memory/bindings"
        key = headers(csrf)
        body: dict[str, Any] = {
            "fields": {"external_identity_id": external_id, "entity_id": entity_id},
            "reason_code": "operator_request",
        }
        response = client.post(path, headers=key, json=body)
        assert response.status_code == 201, response.text
        identifier = response.json()["data"]["id"]
        if operation != "create":
            path += "/" + identifier + ":" + operation
            key = headers(csrf)
            body = {"expected_revision": 1, "reason_code": "operator_request"}
            assert client.post(path, headers=key, json=body).status_code == 200
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type=deleted,
                resource_id={
                    "entity": entity_id,
                    "external_identity": external_id,
                    "binding": identifier,
                }[deleted],
                deleted_by="test",
                reason_code="operator_request",
            )
        assert client.post(path, headers=key, json=body).status_code == 404


def test_confirmation_failure_rolls_back_entity_state_binding_and_graph_job(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.console.identity import ConsoleIdentityCommands
    from iris_memory_core.application.identity import IdentityService
    from tests.integration.test_console_commands import principal_for

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        entity_id = entity(client, csrf)
        external_id = identity(client, csrf)
        identifier = propose(client, csrf, external_id, entity_id)
    principal = principal_for(world)
    tables = (
        "entities",
        "entity_revisions",
        "bindings",
        "binding_revisions",
        "audit_events",
        "outbox_jobs",
    )
    with world["store"].read() as tx:
        before = [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables]
    original = IdentityService._confirm_binding_in_tx

    def fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("after confirmation and entity promotion")

    monkeypatch.setattr(IdentityService, "_confirm_binding_in_tx", fail)
    with pytest.raises(RuntimeError, match="after confirmation"):
        ConsoleIdentityCommands(world["security"]).mutate_binding(
            principal,
            identifier,
            operation="binding.confirm",
            expected_revision=1,
            reason="operator_request",
            idempotency_key="rollback-confirm",
        )
    with world["store"].read() as tx:
        assert [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables] == before


@pytest.mark.parametrize("resource_type", ["entity", "external_identity", "binding"])
def test_registry_creation_failure_rolls_back_canonical_audit_and_receipt(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, resource_type: str
) -> None:
    from iris_memory_core.application.console.identity import ConsoleIdentityCommands
    from iris_memory_core.application.identity import IdentityService
    from tests.integration.test_console_commands import principal_for

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        entity_id = entity(client, csrf)
        external_id = identity(client, csrf)
    fields = {
        "entity": {"kind": "person", "display_name": "Must roll back"},
        "external_identity": {"provider": "manual", "realm": "new", "external_id": "new"},
        "binding": {"external_identity_id": external_id, "entity_id": entity_id},
    }[resource_type]
    principal = principal_for(world)
    tables = (
        "entities",
        "entity_revisions",
        "external_identities",
        "bindings",
        "binding_revisions",
        "audit_events",
        "outbox_jobs",
        "idempotency_outcomes",
    )
    with world["store"].read() as tx:
        before = [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables]
    original = IdentityService.create_for_command

    def fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("after registry creation")

    monkeypatch.setattr(IdentityService, "create_for_command", fail)
    with pytest.raises(RuntimeError, match="after registry creation"):
        ConsoleIdentityCommands(world["security"]).create(
            principal,
            resource_type=resource_type,
            fields=fields,
            privacy_labels=[],
            reason="operator_request",
            idempotency_key="rollback-registry-create",
        )
    with world["store"].read() as tx:
        assert [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables] == before

        lease = (
            tx.raw()
            .execute(
                "SELECT status, response_body, resource_refs FROM idempotency_records "
                "WHERE idempotency_key = ?",
                ("rollback-registry-create",),
            )
            .fetchone()
        )
        assert tuple(lease) == ("failed_replayable", None, "[]")
    monkeypatch.setattr(IdentityService, "create_for_command", original)
    commands = ConsoleIdentityCommands(world["security"])
    retry = commands.create(
        principal,
        resource_type=resource_type,
        fields=fields,
        privacy_labels=[],
        reason="operator_request",
        idempotency_key="rollback-registry-create",
    )
    replay = commands.create(
        principal,
        resource_type=resource_type,
        fields=fields,
        privacy_labels=[],
        reason="operator_request",
        idempotency_key="rollback-registry-create",
    )
    assert retry.id == replay.id and retry.revision == replay.revision == 1


def test_revoked_binding_and_historical_identity_survive_isolated_restore(
    world: dict[str, Any], tmp_path: Any
) -> None:
    from iris_memory_core.application.identity import IdentityService
    from iris_memory_core.storage.backup import BackupService, verify_database_invariants
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from iris_memory_core.storage.uow import Store
    from tests.conftest import local_allowed_versions

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        entity_id = entity(client, csrf)
        external_id = identity(client, csrf)
        identifier = propose(client, csrf, external_id, entity_id)
        store.clock.advance(1000)
        assert action(client, csrf, identifier, "confirm", 1).status_code == 200
        at_us = store.clock.now_us()
        store.clock.advance(1000)
        assert action(client, csrf, identifier, "revoke", 2).status_code == 200
    backup, destination = tmp_path / "backup", tmp_path / "restored"
    service = BackupService(store)
    service.create_backup(backup)
    restored = service.restore_backup(backup, destination)
    assert restored.check.ok, restored.check.problems
    database = destination / "canonical.sqlite3"
    restored_store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    view = IdentityService(restored_store).identity_view(world["access"], external_id, at_us=at_us)
    assert view.entity_id_at_ingest == entity_id and view.current_entity_id is None
    with restored_store.read() as tx:
        binding = tx.get_binding(identifier)
        assert binding.state.value == "revoked" and binding.revision == 3
        assert tx.get_entity(entity_id).state.value == "canonical"
        assert tx.verified_binding_for(external_id) is None
    assert verify_database_invariants(database) == ()
