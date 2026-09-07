"""Entity soft deletion and selected dependency ordering under current grants."""

from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.domain.identity import FieldAuthority
from iris_memory_core.domain.retention import LegalHoldActiveError, ProtectedResourceError
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_forget_http import commit, preview, target
from tests.integration.console.test_console_identity_http import entity
from tests.integration.console.test_console_reads import grant_for
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def test_entity_tombstone_hides_all_history_and_records_actor(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        saved = preview(client, csrf, [target(client, identifier, "entities")])
        key = headers(csrf)
        result = commit(client, csrf, saved, headers=key)
        assert result.status_code == 200, result.text
        assert commit(client, csrf, saved, headers=key).json()["data"] == result.json()["data"]
        for path in ["", "/history", "/attributes"]:
            assert client.get("/v1/memory/entities/" + identifier + path).status_code == 404
        ledger = ForgetService(world["store"], world["store"].clock).export_deletion_ledger(
            world["access"]
        )
        assert len(ledger) == 1 and ledger[0].requested_by == "console:" + world["owner"].id


def test_entity_erase_rejected(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        descriptors = client.get("/v1/memory/resource-types").json()["data"]
        modes = {row["resource_type"]: row["supports"].get("forget_modes") for row in descriptors}
        assert modes["entity"] == ["soft"]
        assert modes["note"] == ["soft", "erase"]
        row = target(client, entity(client, csrf), "entities")
        result = client.post(
            "/v1/memory:forget-preview",
            headers=headers(csrf),
            json={"targets": [row], "mode": "erase", "reason_code": "operator_request"},
        )
        assert result.status_code == 400, result.text


def test_entity_attributes_change_invalidates_delete_preview(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        saved = preview(client, csrf, [target(client, identifier, "entities")])
        IdentityService(world["store"]).record_attribute(
            world["access"],
            identifier,
            "nickname",
            "new attribute",
            FieldAuthority.INFERRED,
            "test",
        )
        result = commit(client, csrf, saved)
        assert result.status_code == 409, result.text
        assert result.json()["error"]["details"]["kind"] == "preview_stale"


def test_entity_subject_hold_blocks_console_and_public_entry(world: dict[str, Any]) -> None:
    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        RetentionService(
            store, store.clock, forget=ForgetService(store, store.clock)
        ).create_legal_hold(world["access"], subject_entity_id=identifier, reason="hold")
        saved = preview(client, csrf, [target(client, identifier, "entities")])
        assert saved["targets"][0]["status"] == "held" and not saved["can_commit"]
        assert commit(client, csrf, saved).status_code == 409
        with pytest.raises(LegalHoldActiveError):
            IdentityService(store).tombstone_entity(
                world["access"], identifier, expected_revision=1, reason="test"
            )


def test_agent_self_identity_is_protected(world: dict[str, Any]) -> None:
    from iris_memory_core.application.memory import resolve_self_subject

    store = world["store"]
    with store.write() as tx:
        identifier = resolve_self_subject(tx, world["tenant"], world["agent"])
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        saved = preview(client, csrf, [target(client, identifier, "entities")])
        assert saved["targets"][0]["status"] == "protected" and not saved["can_commit"]
        assert commit(client, csrf, saved).status_code == 409
        with pytest.raises(ProtectedResourceError):
            IdentityService(store).tombstone_entity(
                world["access"], identifier, expected_revision=1, reason="test"
            )


def test_selected_claim_is_deleted_before_subject_entity(world: dict[str, Any]) -> None:
    from tests.integration.console.test_console_claim_http import create_payload, source

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        body = create_payload(world, source(client, csrf, world))
        body["fields"].pop("subject_is_self")
        body["fields"]["subject_entity_id"] = identifier
        claim = client.post("/v1/memory/claims", headers=headers(csrf), json=body)
        assert claim.status_code == 201, claim.text
        claim_id = claim.json()["data"]["id"]
        # Input deliberately names the prerequisite first.
        saved = preview(
            client,
            csrf,
            [target(client, identifier, "entities"), target(client, claim_id, "claims")],
        )
        assert saved["can_commit"]
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        with world["store"].read() as tx:
            assert tx.is_tombstoned(world["tenant"], "entity", identifier)
            assert tx.is_tombstoned(world["tenant"], "claim", claim_id)


def test_selected_redirect_source_deleted_before_destination(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        source = entity(client, csrf)
        destination = entity(client, csrf)
        result = client.post(
            "/v1/memory/entities/" + source + ":redirect",
            headers=headers(csrf),
            json={
                "target_id": destination,
                "expected_revision": 1,
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == 200, result.text
        saved = preview(
            client,
            csrf,
            [target(client, destination, "entities"), target(client, source, "entities")],
        )
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        with world["store"].read() as tx:
            assert all(
                tx.is_tombstoned(world["tenant"], "entity", identifier)
                for identifier in [source, destination]
            )


def test_limited_operator_cannot_delete_global_entity(world: dict[str, Any]) -> None:
    grant = grant_for(world, permissions=frozenset({"memory.read", "memory.forget"}))
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="limited",
        description="",
        template="viewer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        identifier = world["entities"][0]
        saved = preview(client, csrf, [target(client, identifier, "entities")])
        assert saved["targets"] == [{"input_index": 0, "status": "not_visible"}]
        assert commit(client, csrf, saved).status_code == 409


def test_tombstone_preserves_binding_ids_but_closes_current_and_historical_resolution(
    world: dict[str, Any],
) -> None:
    store = world["store"]
    identities = IdentityService(store)
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        external = identities.register_external_identity(
            world["access"], "test", "realm", "name", entity_id=identifier
        )
        binding = identities.propose_binding(
            world["access"], external.id, identifier, proof_digest="a" * 64, reason="test"
        )
        identities.confirm_binding(world["access"], binding.id, expected_revision=1, reason="test")
        before = store.clock.now_us()
        assert client.get("/v1/memory/bindings/" + binding.id).status_code == 200
        saved = preview(client, csrf, [target(client, identifier, "entities")])
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        for suffix in ["", "/history"]:
            assert client.get("/v1/memory/bindings/" + binding.id + suffix).status_code == 404
        assert identities.identity_view(world["access"], external.id).current_entity_id is None
        assert (
            identities.identity_view(world["access"], external.id, at_us=before).entity_id_at_ingest
            is None
        )
        with store.read() as tx:
            current = tx.get_binding(binding.id)
            assert current.entity_id == identifier and current.revision == 2
            assert tx.get_external_identity(external.id).entity_id == identifier
