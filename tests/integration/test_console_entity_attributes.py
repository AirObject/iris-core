"""Attribute records use both entity and attribute CAS without rewriting Entity history."""

from typing import Any

import pytest

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_identity_http import entity
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def snapshot(client: Any, identifier: str) -> dict[str, Any]:
    result = client.get("/v1/memory/entities/" + identifier + "/attributes")
    assert result.status_code == 200, result.text
    validate_response("ResourceViewEnvelope", result.json())
    return dict(result.json()["data"])


def payload(view: dict[str, Any], value: str, mode: str = "confirmation") -> dict[str, Any]:
    return {
        "expected_revision": view["revision"],
        "expected_attributes_version": view["fields"]["attributes_version"],
        "fields": {"field": "display_name", "value": value, "mode": mode},
        "reason_code": "operator_request",
    }


def test_attribute_authority_outcomes_provenance_and_first_snapshot(world: dict[str, Any]) -> None:
    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        validate_response("ResourceTypePage", client.get("/v1/memory/resource-types").json())
        identifier = entity(client, csrf, "Original label")
        path = "/v1/memory/entities/" + identifier + "/attributes"
        initial = snapshot(client, identifier)
        assert initial["fields"]["identity_attributes"] == []
        body, key = payload(initial, "Confirmed name"), headers(csrf)
        first = client.post(path, headers=key, json=body)
        assert first.status_code == 200, first.text
        saved = first.json()["data"]
        assert saved["fields"]["attribute_outcome"] == "supersede"
        assert saved["revision"] == 1
        with store.read() as tx:
            current = tx.current_identity_attribute(world["tenant"], identifier, "display_name")
            assert current is not None and current.authority.value == "admin_confirmed"
            assert current.source_ref == "console:" + world["owner"].id
            assert tx.get_entity(identifier).display_name == "Original label"
        for value, mode, outcome in [
            ("Equal competing name", "confirmation", "coexist"),
            ("Explicit correction", "correction", "supersede"),
            ("Lower authority", "confirmation", "ignored"),
            ("Explicit correction", "correction", "ignored"),
            ("Equal explicit conflict", "correction", "coexist"),
        ]:
            result = client.post(
                path, headers=headers(csrf), json=payload(snapshot(client, identifier), value, mode)
            )
            assert result.status_code == 200, result.text
            assert result.json()["data"]["fields"]["attribute_outcome"] == outcome
        view = snapshot(client, identifier)
        values = view["fields"]["identity_attributes"]
        current = [row for row in values if row["status"] == "current"]
        assert len(current) == 1 and current[0]["value"] == "Explicit correction"
        assert len([row for row in values if row["status"] == "conflict"]) == 2
        assert all("source_ref" not in row for row in values)
        assert view["revision"] == 1
        assert client.post(path, headers=key, json=body).json()["data"] == saved
        history = client.get("/v1/memory/entities/" + identifier + "/history").json()["data"]
        assert len(history) == 1 and "identity_attributes" not in history[0]["fields"]


def test_intervening_public_attribute_write_invalidates_snapshot_even_when_entity_revision_matches(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.application.identity import IdentityService
    from iris_memory_core.domain.identity import FieldAuthority

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        old = snapshot(client, identifier)
        IdentityService(world["store"]).record_attribute(
            world["access"],
            identifier,
            "display_name",
            "Intervening inferred name",
            FieldAuthority.INFERRED,
            "app:source",
        )
        current = snapshot(client, identifier)
        assert current["revision"] == old["revision"] == 1
        assert current["fields"]["attributes_version"] != old["fields"]["attributes_version"]
        result = client.post(
            "/v1/memory/entities/" + identifier + "/attributes",
            headers=headers(csrf),
            json=payload(old, "Stale correction", "correction"),
        )
        assert result.status_code == 409, result.text
        assert result.json()["error"]["code"] == "revision_mismatch"
        assert snapshot(client, identifier)["fields"] == current["fields"]


@pytest.mark.parametrize(
    "field", ["authority", "source_ref", "effective_us", "status", "tenant_id", "scope"]
)
def test_attribute_authority_provenance_and_clock_are_server_owned(
    world: dict[str, Any], field: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        body = payload(snapshot(client, identifier), "Value")
        body["fields"][field] = "forged"
        result = client.post(
            "/v1/memory/entities/" + identifier + "/attributes", headers=headers(csrf), json=body
        )
        assert result.status_code == 400, result.text
        assert snapshot(client, identifier)["fields"]["identity_attributes"] == []


@pytest.mark.parametrize(
    "case", ["restricted_allowed", "restricted_hidden", "read_only", "limited_global_write"]
)
def test_attribute_current_grants_apply_to_reads_and_writes(
    world: dict[str, Any], case: str
) -> None:
    from dataclasses import replace

    from tests.integration.test_console_claim_http import writer_token

    _, private_token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="attribute privacy",
        description="test",
        template="owner",
        grant=replace(world["owner"].grant, allow_restricted=True),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    owner, csrf = signed_in(world["app"], private_token)
    with owner:
        identifier = entity(
            owner, csrf, privacy_labels=["restricted"] if case.startswith("restricted") else []
        )
        body = payload(snapshot(owner, identifier), "private value")
    token = (
        private_token
        if case == "restricted_allowed"
        else (
            writer_token(world, restricted=False, read_only=case == "read_only")
            if case in {"read_only", "limited_global_write"}
            else world["token"]
        )
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        path = "/v1/memory/entities/" + identifier + "/attributes"
        read = client.get(path)
        assert read.status_code == (404 if case == "restricted_hidden" else 200), read.text
        result = client.post(path, headers=headers(csrf), json=body)
        assert (
            result.status_code
            == {
                "restricted_allowed": 200,
                "restricted_hidden": 404,
                "read_only": 403,
                "limited_global_write": 403,
            }[case]
        ), result.text


@pytest.mark.parametrize("deleted", ["entity", "redirect_target"])
def test_attribute_replay_rechecks_current_entity_and_redirect_chain(
    world: dict[str, Any], deleted: str
) -> None:
    from tests.integration.test_console_entity_redirect import redirect

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        path = "/v1/memory/entities/" + identifier + "/attributes"
        body, key = payload(snapshot(client, identifier), "Saved value"), headers(csrf)
        result = client.post(path, headers=key, json=body)
        assert result.status_code == 200, result.text
        target = identifier
        if deleted == "redirect_target":
            target = entity(client, csrf, "Destination")
            assert redirect(client, csrf, identifier, target, 1).status_code == 200
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type="entity",
                resource_id=target,
                deleted_by="test",
                reason_code="operator_request",
            )
        assert client.get(path).status_code == 404
        assert client.post(path, headers=key, json=body).status_code == 404


def test_oversized_legacy_attribute_is_rejected_before_value_materialization(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.identity import IdentityService
    from iris_memory_core.domain.identity import FieldAuthority
    from iris_memory_core.storage.repositories import IdentityRepository

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        IdentityService(world["store"]).record_attribute(
            world["access"],
            identifier,
            "large",
            "x" * (256 * 1024 + 1),
            FieldAuthority.INFERRED,
            "app:source",
        )

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("oversized value must not reach Python conversion")

        monkeypatch.setattr(IdentityRepository, "_attribute_row", fail)
        result = client.get("/v1/memory/entities/" + identifier + "/attributes")
        assert result.status_code == 503, result.text


def test_attribute_snapshot_capacity_rejects_new_row_atomically(world: dict[str, Any]) -> None:
    from iris_memory_core.application.identity import IdentityService
    from iris_memory_core.domain.identity import FieldAuthority

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        service = IdentityService(store)
        for i in range(250):
            service.record_attribute(
                world["access"],
                identifier,
                "field_" + str(i),
                "value",
                FieldAuthority.INFERRED,
                "app:source",
            )
        before = snapshot(client, identifier)
        result = client.post(
            "/v1/memory/entities/" + identifier + "/attributes",
            headers=headers(csrf),
            json=payload(before, "One too many"),
        )
        assert result.status_code == 503, result.text
        assert snapshot(client, identifier)["fields"] == before["fields"]
        with store.read() as tx:
            assert (
                tx.current_identity_attribute(world["tenant"], identifier, "display_name") is None
            )


def test_attribute_late_failure_rolls_back_and_same_key_retry_succeeds(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.console.identity import ConsoleIdentityCommands
    from iris_memory_core.application.identity import IdentityService
    from tests.integration.test_console_commands import principal_for

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        body = payload(snapshot(client, identifier), "Retry value")
    principal = principal_for(world)
    tables = (
        "entities",
        "entity_revisions",
        "identity_attributes",
        "audit_events",
        "idempotency_outcomes",
    )
    with store.read() as tx:
        before = [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables]
    original = IdentityService._record_attribute_in_tx

    def fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("after attribute and audit")

    monkeypatch.setattr(IdentityService, "_record_attribute_in_tx", staticmethod(fail))
    commands = ConsoleIdentityCommands(world["security"])

    def execute() -> Any:
        return commands.record_attribute(
            principal,
            identifier,
            expected_revision=1,
            expected_attributes_version=body["expected_attributes_version"],
            fields=body["fields"],
            reason="operator_request",
            idempotency_key="retry-attribute",
        )

    with pytest.raises(RuntimeError, match="after attribute and audit"):
        execute()
    with store.read() as tx:
        assert [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables] == before
    monkeypatch.setattr(IdentityService, "_record_attribute_in_tx", staticmethod(original))
    first, replay = execute(), execute()
    assert first.fields == replay.fields
    assert len(first.fields["identity_attributes"]) == 1


def test_attribute_current_conflicts_and_superseded_history_survive_restore(
    world: dict[str, Any], tmp_path: Any
) -> None:
    from iris_memory_core.storage.backup import BackupService, verify_database_invariants
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from iris_memory_core.storage.uow import Store
    from tests.conftest import local_allowed_versions

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        path = "/v1/memory/entities/" + identifier + "/attributes"
        for value, mode in [
            ("Confirmed", "confirmation"),
            ("Corrected", "correction"),
            ("Conflicted", "correction"),
        ]:
            result = client.post(
                path, headers=headers(csrf), json=payload(snapshot(client, identifier), value, mode)
            )
            assert result.status_code == 200, result.text
    service = BackupService(store)
    backup, destination = tmp_path / "backup", tmp_path / "restored"
    service.create_backup(backup)
    restored = service.restore_backup(backup, destination)
    assert restored.check.ok, restored.check.problems
    database = destination / "canonical.sqlite3"
    restored_store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    with restored_store.read() as tx:
        current = tx.current_identity_attribute(world["tenant"], identifier, "display_name")
        assert current is not None and current.value == "Corrected"
        assert [row.value for row in tx.conflicted_attributes(identifier)] == ["Conflicted"]
        assert (
            tx.raw()
            .execute(
                "SELECT value FROM identity_attributes WHERE entity_id=? AND status='superseded'",
                (identifier,),
            )
            .fetchone()[0]
            == "Confirmed"
        )
        assert tx.get_entity(identifier).revision == 1
    assert verify_database_invariants(database) == ()


def test_legacy_out_of_range_attribute_time_returns_stable_integrity_error(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.application.identity import IdentityService
    from iris_memory_core.domain.identity import FieldAuthority

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = entity(client, csrf)
        IdentityService(world["store"]).record_attribute(
            world["access"],
            identifier,
            "old",
            "Value",
            FieldAuthority.INFERRED,
            "app:legacy",
            effective_us=2**63 - 1,
        )
        result = client.get("/v1/memory/entities/" + identifier + "/attributes")
        assert result.status_code == 409, result.text
        assert result.json()["error"]["code"] == "conflict"
