"""Explicit redirect preserves ingest history and authorizes the current chain."""

from itertools import pairwise
from typing import Any

import pytest

from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_identity_http import action, entity, identity, propose
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def redirect(
    client: Any, csrf: str, source: str, target: str, revision: int, key: Any = None
) -> Any:
    return client.post(
        "/v1/memory/entities/" + source + ":redirect",
        headers=key or headers(csrf),
        json={
            "expected_revision": revision,
            "target_id": target,
            "reason_code": "operator_request",
        },
    )


def test_redirect_preserves_at_ingest_binding_history_and_first_outcome(
    world: dict[str, Any],
    tmp_path: Any,
) -> None:
    from iris_memory_core.application.identity import IdentityService
    from iris_memory_core.indexing.graph import GraphProjectionService

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        validate_response("ResourceTypePage", client.get("/v1/memory/resource-types").json())
        source, target = entity(client, csrf, "Original"), entity(client, csrf, "Destination")
        external_id = identity(client, csrf)
        binding = propose(client, csrf, external_id, source)
        assert action(client, csrf, binding, "confirm", 1).status_code == 200
        graph = GraphProjectionService(store, store.clock)
        generation = graph.rebuild(world["tenant"])
        with store.read() as tx:
            assert any(
                edge.resource_id == binding
                for edge in tx.graph.all_edges(world["tenant"], generation.generation_id)
            )
        at_us = store.clock.now_us()
        store.clock.advance(1000)
        key = headers(csrf)
        result = redirect(client, csrf, source, target, 2, key)
        assert result.status_code == 200, result.text
        validate_response("ResourceViewEnvelope", result.json())
        original = result.json()["data"]
        assert original["revision"] == 3 and original["status"] == "redirected"
        assert original["fields"]["redirect_entity_id"] == target
        with store.write() as tx:
            assert graph.apply_change_in_tx(
                tx, tenant_id=world["tenant"], resource_type="entity", resource_id=source
            )
        with store.read() as tx:
            assert all(
                edge.resource_id != binding
                for edge in tx.graph.all_edges(world["tenant"], generation.generation_id)
            )

        assert redirect(client, csrf, source, target, 2, key).json()["data"] == original
        view = IdentityService(store).identity_view(world["access"], external_id, at_us=at_us)
        assert view.entity_id_at_ingest == source
        current = IdentityService(store).identity_view(world["access"], external_id)
        assert current.entity_id_at_ingest == source and current.current_entity_id == target
        path = "/v1/memory/entities/" + source
        assert client.get(path).json()["data"]["available_actions"] == ["forget"]
        history = client.get(path + "/history").json()["data"]
        assert [row["status"] for row in history] == ["redirected", "canonical", "provisional"]
        assert all(row["fields"]["redirect_entity_id"] is None for row in history[1:])
        with store.read() as tx:
            assert tx.get_binding(binding).entity_id == source
            assert tx.get_binding(binding).revision == 2
            record = tx.get_entity_redirect(source)
            assert record is not None and record.created_by == "console:" + world["owner"].id
        next_target = entity(client, csrf, "Later destination")
        assert redirect(client, csrf, target, next_target, 1).status_code == 200
        assert redirect(client, csrf, source, target, 2, key).json()["data"] == original
        assert (
            IdentityService(store).identity_view(world["access"], external_id).current_entity_id
            == next_target
        )
        assert redirect(client, csrf, next_target, source, 1).status_code == 409
        assert redirect(client, csrf, source, next_target, 3).status_code == 409
    from iris_memory_core.storage.backup import BackupService, verify_database_invariants
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from iris_memory_core.storage.uow import Store
    from tests.conftest import local_allowed_versions

    backup, destination = tmp_path / "backup", tmp_path / "restored"
    backup_service = BackupService(store)
    backup_service.create_backup(backup)
    restored = backup_service.restore_backup(backup, destination)
    assert restored.check.ok, restored.check.problems
    database = destination / "canonical.sqlite3"
    restored_store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    service = IdentityService(restored_store)
    assert (
        service.identity_view(world["access"], external_id, at_us=at_us).entity_id_at_ingest
        == source
    )
    assert service.identity_view(world["access"], external_id).current_entity_id == next_target
    with restored_store.read() as tx:
        assert tx.get_binding(binding).entity_id == source
    assert verify_database_invariants(database) == ()


@pytest.mark.parametrize("field", ["scope", "tenant_id", "fields", "created_by", "source_id"])
def test_redirect_rejects_client_authority_and_topology(world: dict[str, Any], field: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        source, target = world["entities"][:2]
        result = client.post(
            "/v1/memory/entities/" + source + ":redirect",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "target_id": target,
                "reason_code": "operator_request",
                field: "forged",
            },
        )
        assert result.status_code == 400, result.text


def test_redirect_extending_tail_cannot_overflow_existing_ancestor_chain(
    world: dict[str, Any],
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        nodes = [entity(client, csrf, "Depth " + str(i)) for i in range(18)]
        for source, target in pairwise(nodes[:17]):
            result = redirect(client, csrf, source, target, 1)
            assert result.status_code == 200, result.text
        assert client.get("/v1/memory/entities/" + nodes[0]).status_code == 200
        result = redirect(client, csrf, nodes[16], nodes[17], 1)
        assert result.status_code == 409, result.text
        assert result.json()["error"]["code"] == "redirect_depth_exceeded"
        with world["store"].read() as tx:
            assert tx.get_entity_redirect(nodes[16]) is None
            assert tx.get_entity(nodes[16]).revision == 1


@pytest.mark.parametrize("deleted", ["source", "middle", "target"])
def test_current_redirect_chain_deletion_hides_cached_result_and_history(
    world: dict[str, Any], deleted: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        source, middle, target = [
            entity(client, csrf, label) for label in ["Source", "Middle", "Target"]
        ]
        key = headers(csrf)
        assert redirect(client, csrf, source, middle, 1, key).status_code == 200
        assert redirect(client, csrf, middle, target, 1).status_code == 200
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type="entity",
                resource_id={"source": source, "middle": middle, "target": target}[deleted],
                deleted_by="test",
                reason_code="operator_request",
            )
        assert client.get("/v1/memory/entities/" + source).status_code == 404
        assert client.get("/v1/memory/entities/" + source + "/history").status_code == 404
        assert redirect(client, csrf, source, middle, 1, key).status_code == 404


def test_redirect_stale_revision_and_late_failure_roll_back_edge_revision_audit_and_graph(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.console.identity import ConsoleIdentityCommands
    from iris_memory_core.application.identity import IdentityService
    from tests.integration.console.test_console_commands import principal_for

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        source, target = entity(client, csrf), entity(client, csrf)
        assert redirect(client, csrf, source, target, 2).status_code == 409
    principal = principal_for(world)
    tables = (
        "entities",
        "entity_revisions",
        "entity_redirects",
        "audit_events",
        "outbox_jobs",
        "idempotency_outcomes",
    )
    with world["store"].read() as tx:
        before = [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables]
    original = IdentityService._redirect_entity_in_tx

    def fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("after redirect graph enqueue")

    monkeypatch.setattr(IdentityService, "_redirect_entity_in_tx", staticmethod(fail))
    with pytest.raises(RuntimeError, match="after redirect graph enqueue"):
        ConsoleIdentityCommands(world["security"]).redirect_entity(
            principal,
            source,
            target_id=target,
            expected_revision=1,
            reason="operator_request",
            idempotency_key="failed-redirect",
        )
    with world["store"].read() as tx:
        assert [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables] == before


@pytest.mark.parametrize(
    "case", ["hidden_source", "hidden_target", "read_only", "limited_global_write"]
)
def test_redirect_authorizes_both_endpoints_and_current_grant(
    world: dict[str, Any], case: str
) -> None:
    from dataclasses import replace

    from tests.integration.console.test_console_claim_http import writer_token

    _, private_token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="redirect privacy",
        description="test",
        template="owner",
        grant=replace(world["owner"].grant, allow_restricted=True),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    owner, csrf = signed_in(world["app"], private_token)
    with owner:
        source = entity(
            owner, csrf, "Source", privacy_labels=["restricted"] if case == "hidden_source" else []
        )
        target = entity(
            owner, csrf, "Target", privacy_labels=["restricted"] if case == "hidden_target" else []
        )
    token = (
        writer_token(world, restricted=False, read_only=case == "read_only")
        if case in {"read_only", "limited_global_write"}
        else world["token"]
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        result = redirect(client, csrf, source, target, 1)
        assert result.status_code == (404 if case.startswith("hidden") else 403), result.text
    with world["store"].read() as tx:
        assert tx.get_entity_redirect(source) is None
        assert tx.get_entity(source).revision == 1


def test_redirect_fan_in_is_bounded_without_committing_a_tail_edge(world: dict[str, Any]) -> None:
    from iris_memory_core.domain.identity import EntityKind

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        source, target = entity(client, csrf, "Fan in"), entity(client, csrf, "Tail")
        with store.write() as tx:
            for i in range(5000):
                ancestor = tx.insert_entity(world["tenant"], EntityKind.PERSON, display_name=str(i))
                tx.insert_entity_redirect(
                    world["tenant"], ancestor.id, source, actor="fixture", reason_code="test"
                )
        result = redirect(client, csrf, source, target, 1)
        assert result.status_code == 503, result.text
        with store.read() as tx:
            assert tx.get_entity_redirect(source) is None
            assert tx.get_entity(source).revision == 1
