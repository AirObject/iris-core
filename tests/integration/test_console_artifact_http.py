"""Immutable manual text artifacts retain current authorization and verified bytes."""

import hashlib
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_claim_http import source, writer_token
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def payload(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": {"agent_id": world["agent"]},
        "fields": {
            "content": "Original 文本 <script>not executed</script>",
            "media_type": "text/plain",
        },
        "reason_code": "operator_request",
    }


def test_artifact_create_replay_dedup_and_immutable_replacement(world: dict[str, Any]) -> None:
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.surface import SurfaceMode

    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test"
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world)
        body["source_refs"] = [
            {
                "resource_type": "observation",
                "resource_id": source(client, csrf, world),
                "revision": 1,
            }
        ]
        desc = next(
            row
            for row in client.get("/v1/memory/resource-types").json()["data"]
            if row["collection"] == "artifacts"
        )
        Draft202012Validator(desc["create_schema"]).validate(body)
        assert desc["update_schema"] is None and [a["id"] for a in desc["actions"]] == ["forget"]
        key = headers(csrf)
        response = client.post("/v1/memory/artifacts", headers=key, json=body)
        assert response.status_code == 201, response.text
        validate_response("ResourceViewEnvelope", response.json())
        artifact = response.json()["data"]
        path = "/v1/memory/artifacts/" + artifact["id"]
        assert artifact["fields"]["content"] == body["fields"]["content"]
        assert artifact["fields"]["storage_kind"] == "inline"
        assert artifact["available_actions"] == []
        assert "locator" not in artifact["fields"]
        assert (
            client.post("/v1/memory/artifacts", headers=key, json=body).json()["data"] == artifact
        )
        assert (
            client.post("/v1/memory/artifacts", headers=headers(csrf), json=body).json()["data"][
                "id"
            ]
            == artifact["id"]
        )
        assert client.get(path).json()["data"]["fields"]["content"] == body["fields"]["content"]
        assert (
            client.patch(
                path,
                headers=headers(csrf),
                json={
                    "fields": {"content": "changed"},
                    "expected_revision": 1,
                    "reason_code": "operator_request",
                },
            ).status_code
            == 405
        )
        with store.read() as tx:
            record = tx.artifacts.get(artifact["id"])
            assert record.refcount == 2
            assert (
                record.content_hash
                == hashlib.sha256(body["fields"]["content"].encode()).hexdigest()
            )
            assert record.size_bytes == len(body["fields"]["content"].encode())
            assert record.source_ref == body["source_refs"][0]
            assert record.locator == "inline:" + record.id
        body["fields"]["content"] = "Replacement original text"
        replacement = client.post("/v1/memory/artifacts", headers=headers(csrf), json=body)
        assert replacement.status_code == 201, replacement.text
        assert replacement.json()["data"]["id"] != artifact["id"]
        assert client.get(path).json()["data"]["fields"]["content"] == artifact["fields"]["content"]


@pytest.mark.parametrize(
    "field",
    [
        "origin",
        "tenant_id",
        "locator",
        "storage_kind",
        "external_url",
        "source_authority",
        "content_hash",
    ],
)
def test_artifact_rejects_forged_authority_and_storage_fields(
    world: dict[str, Any], field: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world)
        (body if field in {"origin", "tenant_id"} else body["fields"])[field] = "forged"
        response = client.post("/v1/memory/artifacts", headers=headers(csrf), json=body)
        assert response.status_code == 400, response.text
        with world["store"].read() as tx:
            assert tx.raw().execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "case", ["restricted_allowed", "restricted_denied", "read_only", "outside_scope"]
)
def test_artifact_create_rechecks_current_grant(world: dict[str, Any], case: str) -> None:
    client, csrf = signed_in(
        world["app"],
        writer_token(world, restricted=case == "restricted_allowed", read_only=case == "read_only"),
    )
    with client:
        body = payload(world)
        body["scope"]["space_id"] = world["spaces"][1 if case == "outside_scope" else 0]
        body["privacy_labels"] = ["restricted"] if case.startswith("restricted") else []
        response = client.post("/v1/memory/artifacts", headers=headers(csrf), json=body)
        assert response.status_code == (201 if case == "restricted_allowed" else 403), response.text
        if case == "restricted_allowed":
            assert (
                client.post("/v1/memory/artifacts", headers=headers(csrf), json=body).status_code
                == 201
            )


@pytest.mark.parametrize("deleted", ["source", "artifact"])
def test_artifact_replay_rechecks_source_and_result(world: dict[str, Any], deleted: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world)
        source_id = source(client, csrf, world)
        body["source_refs"] = [
            {"resource_type": "observation", "resource_id": source_id, "revision": 1}
        ]
        key = headers(csrf)
        response = client.post("/v1/memory/artifacts", headers=key, json=body)
        assert response.status_code == 201, response.text
        identifier = response.json()["data"]["id"]
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type="artifact" if deleted == "artifact" else "observation",
                resource_id=identifier if deleted == "artifact" else source_id,
                deleted_by="test",
                reason_code="operator_request",
            )
        replay = client.post("/v1/memory/artifacts", headers=key, json=body)
        assert replay.status_code == 404, replay.text


@pytest.mark.parametrize("changed", ["media_type", "source"])
def test_artifact_dedup_cannot_replace_immutable_metadata(
    world: dict[str, Any], changed: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world)
        created = client.post("/v1/memory/artifacts", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        if changed == "media_type":
            body["fields"]["media_type"] = "text/markdown"
        else:
            body["source_refs"] = [
                {
                    "resource_type": "observation",
                    "resource_id": source(client, csrf, world),
                    "revision": 1,
                }
            ]
        response = client.post("/v1/memory/artifacts", headers=headers(csrf), json=body)
        assert response.status_code == 409, response.text
        with world["store"].read() as tx:
            record = tx.artifacts.get(created.json()["data"]["id"])
            assert (
                record.refcount == 1
                and record.media_type == "text/plain"
                and record.source_ref is None
            )


def test_artifact_inline_limit_is_bytes_and_does_not_accept_binary(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world)
        body["fields"]["content"] = "x" * 262144
        accepted = client.post("/v1/memory/artifacts", headers=headers(csrf), json=body)
        assert accepted.status_code == 201, accepted.text
        for content, media in [
            ("é" * 131073, "text/plain"),
            ("x", "image/png"),
            ("", "text/plain"),
        ]:
            body["fields"] = {"content": content, "media_type": media}
            response = client.post("/v1/memory/artifacts", headers=headers(csrf), json=body)
            assert response.status_code == 400, response.text


@pytest.mark.parametrize("tamper", ["content", "size"])
def test_artifact_corruption_never_discloses_bytes_or_increments_refcount(
    world: dict[str, Any], tamper: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world)
        key = headers(csrf)
        response = client.post("/v1/memory/artifacts", headers=key, json=body)
        assert response.status_code == 201, response.text
        identifier = response.json()["data"]["id"]
        path = "/v1/memory/artifacts/" + identifier
        with world["store"].write() as tx:
            if tamper == "content":
                tx.raw().execute(
                    "UPDATE artifacts SET content=? WHERE id=?", (b"CORRUPTED SECRET", identifier)
                )
            else:
                tx.raw().execute(
                    "UPDATE artifacts SET size_bytes=size_bytes+1 WHERE id=?", (identifier,)
                )
        for result in [
            client.get(path),
            client.get("/v1/memory/artifacts"),
            client.post("/v1/memory/artifacts", headers=key, json=body),
            client.post("/v1/memory/artifacts", headers=headers(csrf), json=body),
        ]:
            assert result.status_code == 409, result.text
            assert (
                "CORRUPTED SECRET" not in result.text
                and body["fields"]["content"] not in result.text
            )
        with world["store"].read() as tx:
            assert tx.artifacts.get(identifier).refcount == 1


def test_artifact_failure_after_canonical_write_rolls_back(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.artifacts import ArtifactService
    from iris_memory_core.application.console.artifacts import ConsoleArtifactCommands
    from iris_memory_core.domain.scope import Scope
    from tests.integration.test_console_commands import principal_for

    principal = principal_for(world)
    tables = ("artifacts", "audit_events", "agent_watermark_entries")
    with world["store"].read() as tx:
        before = [list(tx.raw().execute(f"SELECT * FROM {t}")) for t in tables]
    original = ArtifactService._execute_ingest

    def fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("injected after artifact creation")

    monkeypatch.setattr(ArtifactService, "_execute_ingest", fail)
    with pytest.raises(RuntimeError, match="injected after artifact"):
        ConsoleArtifactCommands(world["security"]).create(
            principal,
            scope=Scope(world["tenant"], world["agent"]),
            fields=payload(world)["fields"],
            privacy_labels=[],
            source_refs=[],
            reason="operator_request",
            idempotency_key="rollback-artifact",
        )
    with world["store"].read() as tx:
        assert [list(tx.raw().execute(f"SELECT * FROM {t}")) for t in tables] == before


def test_online_artifact_still_requires_surface_lease(world: dict[str, Any]) -> None:
    from iris_memory_core.application.artifacts import ArtifactService
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.errors import LeaseExpiredError
    from iris_memory_core.domain.surface import SurfaceMode
    from iris_memory_core.storage.idempotency import IdempotencyManager

    store = world["store"]
    surface = SurfaceCoordinatorService(store, store.clock)
    surface.set_mode(world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test")
    service = ArtifactService(
        store, store.clock, surface=surface, idempotency=IdempotencyManager(store)
    )
    with pytest.raises(LeaseExpiredError):
        service.ingest_inline(
            world["access"],
            agent_id=world["agent"],
            content=b"online",
            media_type="text/plain",
            idempotency_key="online-artifact",
        )


def test_artifact_forget_erases_bytes_and_survives_backup_restore(
    world: dict[str, Any], tmp_path: Any
) -> None:
    from iris_memory_core.application.forget import ForgetService
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
    from iris_memory_core.storage.backup import BackupService, verify_database_invariants
    from iris_memory_core.storage.idempotency import IdempotencyManager
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from iris_memory_core.storage.uow import Store
    from tests.conftest import local_allowed_versions

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world)
        key = headers(csrf)
        created = client.post("/v1/memory/artifacts", headers=key, json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
        service = ForgetService(
            store,
            store.clock,
            idempotency=IdempotencyManager(store),
            surface=SurfaceCoordinatorService(store, store.clock),
        )
        service.forget(
            world["access"],
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE, resource_type="artifact", resource_id=identifier
            ),
            reason="operator_request",
            idempotency_key="forget-manual-artifact",
        )
        assert client.get("/v1/memory/artifacts/" + identifier).status_code == 404
        assert client.post("/v1/memory/artifacts", headers=key, json=body).status_code == 404
    backup = BackupService(store)
    archive = tmp_path / "backup"
    destination = tmp_path / "restored"
    backup.create_backup(archive)
    restored = backup.restore_backup(archive, destination)
    assert restored.check.ok, restored.check.problems
    database = destination / "canonical.sqlite3"
    recovered = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    with recovered.read() as tx:
        assert tx.is_tombstoned(world["tenant"], "artifact", identifier)
        row = (
            tx.raw()
            .execute("SELECT content,status FROM artifacts WHERE id=?", (identifier,))
            .fetchone()
        )
        assert row is None or (row[0] is None and row[1] == "tombstoned")
    assert verify_database_invariants(database) == ()
