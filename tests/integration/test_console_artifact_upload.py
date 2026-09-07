"""Raw upload admission, concurrent authorization, rollback and blob lifetime."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from starlette.requests import Request

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_claim_http import source, writer_token
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture
PATH = "/v1/memory/artifacts:upload"


def metadata(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": {"agent_id": world["agent"]},
        "fields": {"media_type": "application/octet-stream"},
        "reason_code": "operator_request",
    }


def upload(
    client: Any, csrf: str, value: dict[str, Any], data: bytes = b"\0raw\xfforiginal", **kwargs: Any
) -> Any:
    request_headers = kwargs.pop("request_headers", headers(csrf))
    return client.post(
        PATH,
        params={"metadata": json.dumps(value)},
        content=data,
        headers={**request_headers, "content-type": "application/octet-stream"},
        **kwargs,
    )


def files(world: dict[str, Any]) -> set[str]:
    return {str(path) for path in world["store"].artifact_root.rglob("*") if path.is_file()}


def test_raw_upload_required_mode_hash_dedup_replay_and_metadata(world: dict[str, Any]) -> None:
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.surface import SurfaceMode

    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test"
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        value = metadata(world)
        value["source_refs"] = [
            {
                "resource_type": "observation",
                "resource_id": source(client, csrf, world),
                "revision": 1,
            }
        ]
        registry = client.get("/v1/memory/resource-types")
        assert registry.status_code == 200, registry.text
        validate_response("ResourceTypePage", registry.json())
        desc = next(row for row in registry.json()["data"] if row["collection"] == "artifacts")
        Draft202012Validator(desc["upload_schema"]).validate(value)
        assert desc["upload"]["id"] == "upload"
        data = b"\0raw\xfforiginal"
        key = headers(csrf)
        response = upload(client, csrf, value, data, request_headers=key)
        assert response.status_code == 201, response.text
        validate_response("ResourceViewEnvelope", response.json())
        artifact = response.json()["data"]
        assert (
            artifact["fields"]["storage_kind"] == "local_blob"
            and artifact["fields"]["content"] is None
        )
        assert "locator" not in artifact["fields"]
        assert upload(client, csrf, value, data, request_headers=key).json()["data"] == artifact
        assert upload(client, csrf, value, data).json()["data"]["id"] == artifact["id"]
        assert upload(client, csrf, value, b"different", request_headers=key).status_code == 409
        with store.read() as tx:
            stored = tx.artifacts.get(artifact["id"])
            assert stored.refcount == 2 and stored.content_hash == hashlib.sha256(data).hexdigest()
            assert stored.size_bytes == len(data)
            assert (
                tx.artifacts.read_blob(
                    stored.locator,
                    expected_hash=stored.content_hash,
                    expected_size=stored.size_bytes,
                )
                == data
            )
        assert len(files(world)) == 1


@pytest.mark.parametrize(
    "field", ["origin", "tenant_id", "filename", "url", "locator", "size_bytes", "content_hash"]
)
def test_upload_unknown_metadata_never_opens_blob(world: dict[str, Any], field: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        value = metadata(world)
        value[field] = "forged"
        result = upload(client, csrf, value)
        assert result.status_code == 400, result.text
        assert files(world) == set()


@pytest.mark.parametrize(
    "case", ["restricted_allowed", "restricted_denied", "read_only", "outside_scope"]
)
def test_upload_current_scope_and_privacy(world: dict[str, Any], case: str) -> None:
    client, csrf = signed_in(
        world["app"],
        writer_token(world, restricted=case == "restricted_allowed", read_only=case == "read_only"),
    )
    with client:
        value = metadata(world)
        value["scope"]["space_id"] = world["spaces"][1 if case == "outside_scope" else 0]
        value["privacy_labels"] = ["restricted"] if case.startswith("restricted") else []
        result = upload(client, csrf, value)
        assert result.status_code == (201 if case == "restricted_allowed" else 403), result.text
        assert bool(files(world)) == (case == "restricted_allowed")
        if case == "restricted_allowed":
            assert upload(client, csrf, value).status_code == 201


@pytest.mark.parametrize(
    "case",
    [
        "length_limit",
        "stream_limit",
        "empty",
        "mismatch",
        "timeout",
        "compression",
        "duplicate_metadata",
        "extra_query",
        "wrong_type",
    ],
)
def test_upload_stream_admission_is_bounded(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from iris_memory_core.api.console import routes_artifacts

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        value = metadata(world)
        key = {**headers(csrf), "content-type": "application/octet-stream"}
        params: Any = {"metadata": json.dumps(value)}
        data = b"tiny"
        expected = 400
        if case == "length_limit":
            key["content-length"] = str(8 * 1024 * 1024 + 1)
            expected = 413
        if case == "mismatch":
            key["content-length"] = "7"
        if case == "empty":
            data = b""
        if case == "compression":
            key["content-encoding"] = "gzip"
            expected = 415
        if case == "wrong_type":
            key["content-type"] = "application/json"
            expected = 415
        if case == "duplicate_metadata":
            params = [("metadata", json.dumps(value)), ("metadata", json.dumps(value))]
        if case == "extra_query":
            params["path"] = "/tmp/injected"
        if case == "stream_limit":

            async def oversized(request: Request) -> Any:
                for _ in range(9):
                    yield b"x" * (1024 * 1024)

            monkeypatch.setattr(Request, "stream", oversized)
            expected = 413
        if case == "timeout":

            async def timed_out(request: Request) -> Any:
                raise TimeoutError
                yield b""

            monkeypatch.setattr(Request, "stream", timed_out)
        result = client.post(PATH, params=params, headers=key, content=data)
        assert result.status_code == expected, result.text
        assert files(world) == set()
        assert routes_artifacts.UPLOAD_CONCURRENCY.acquire(blocking=False)
        routes_artifacts.UPLOAD_CONCURRENCY.release()


def test_upload_permission_and_declared_length_fail_before_consuming_body(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, csrf = signed_in(world["app"], writer_token(world, restricted=False, read_only=True))
    with client:

        async def unread(request: Request) -> Any:
            pytest.fail("rejected upload body must not be consumed")
            yield b""

        monkeypatch.setattr(Request, "stream", unread)
        assert upload(client, csrf, metadata(world)).status_code == 403
        assert files(world) == set()


@pytest.mark.parametrize("change", ["source", "session"])
def test_upload_reauthorizes_after_stream_before_creating_blob(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        value = metadata(world)
        identifier = source(client, csrf, world)
        value["source_refs"] = [
            {"resource_type": "observation", "resource_id": identifier, "revision": 1}
        ]
        original = Request.stream

        async def changed(request: Request) -> Any:
            async for chunk in original(request):
                yield chunk
            with world["store"].write() as tx:
                if change == "source":
                    tx.record_tombstone(
                        tenant_id=world["tenant"],
                        resource_type="observation",
                        resource_id=identifier,
                        deleted_by="test",
                        reason_code="operator_request",
                    )
                else:
                    tx.console.revoke_sessions(
                        world["owner"].id, now_us=world["store"].clock.now_us()
                    )

        monkeypatch.setattr(Request, "stream", changed)
        result = upload(client, csrf, value)
        assert result.status_code == (404 if change == "source" else 401), result.text
        assert files(world) == set()
        with world["store"].read() as tx:
            assert tx.raw().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_upload_concurrency_limit_does_not_consume_or_leak(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.api.console.routes_artifacts import UPLOAD_CONCURRENCY

    client, csrf = signed_in(world["app"], world["token"])
    with client:

        async def unread(request: Request) -> Any:
            pytest.fail("busy upload must not consume bytes")
            yield b""

        monkeypatch.setattr(Request, "stream", unread)
        assert UPLOAD_CONCURRENCY.acquire(blocking=False)
        assert UPLOAD_CONCURRENCY.acquire(blocking=False)
        try:
            response = upload(client, csrf, metadata(world))
            assert response.status_code == 429, response.text
            assert response.headers["retry-after"] == "1"
        finally:
            UPLOAD_CONCURRENCY.release()
            UPLOAD_CONCURRENCY.release()
        assert files(world) == set()


def test_upload_late_failure_cleans_only_uncommitted_blob(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.artifacts import ArtifactService
    from iris_memory_core.application.console.artifacts import ConsoleArtifactCommands
    from iris_memory_core.domain.scope import Scope
    from tests.integration.test_console_commands import principal_for

    principal = principal_for(world)
    command = ConsoleArtifactCommands(world["security"])
    options: dict[str, Any] = dict(
        scope=Scope(world["tenant"], world["agent"]),
        payload=b"new bytes",
        media_type="application/octet-stream",
        privacy_labels=[],
        source_refs=[],
        reason="operator_request",
        idempotency_key="upload-rollback",
    )
    original = ArtifactService._execute_ingest

    def fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("after blob and row insertion")

    monkeypatch.setattr(ArtifactService, "_execute_ingest", fail)
    with pytest.raises(RuntimeError, match="after blob"):
        command.upload(principal, **options)
    assert files(world) == set()
    with world["store"].read() as tx:
        assert tx.raw().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    monkeypatch.setattr(ArtifactService, "_execute_ingest", original)
    first = command.upload(principal, **{**options, "idempotency_key": "upload-survivor"})
    previous = files(world)
    monkeypatch.setattr(ArtifactService, "_execute_ingest", fail)
    with pytest.raises(RuntimeError):
        command.upload(principal, **{**options, "idempotency_key": "dedup-rollback"})
    assert files(world) == previous
    with world["store"].read() as tx:
        assert tx.artifacts.get(first.id).refcount == 1


def test_upload_lost_response_after_commit_preserves_file_for_replay(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.console.artifacts import ConsoleArtifactCommands
    from iris_memory_core.domain.scope import Scope
    from tests.integration.test_console_commands import principal_for

    principal = principal_for(world)
    command = ConsoleArtifactCommands(world["security"])
    options: dict[str, Any] = dict(
        scope=Scope(world["tenant"], world["agent"]),
        payload=b"committed bytes",
        media_type="application/octet-stream",
        privacy_labels=[],
        source_refs=[],
        reason="operator_request",
        idempotency_key="upload-response-lost",
    )
    original = ConsoleArtifactCommands._result

    def lost(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("lost response after commit")

    monkeypatch.setattr(ConsoleArtifactCommands, "_result", lost)
    with pytest.raises(RuntimeError, match="lost response"):
        command.upload(principal, **options)
    assert len(files(world)) == 1
    monkeypatch.setattr(ConsoleArtifactCommands, "_result", original)
    record = command.upload(principal, **options)
    with world["store"].read() as tx:
        assert tx.artifacts.get(record.id).refcount == 1


def test_upload_corruption_and_forget_reject_cached_success(world: dict[str, Any]) -> None:
    from iris_memory_core.application.forget import ForgetService
    from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
    from iris_memory_core.storage.idempotency import IdempotencyManager

    client, csrf = signed_in(world["app"], world["token"])
    store = world["store"]
    with client:
        value = metadata(world)
        key = headers(csrf)
        result = upload(client, csrf, value, request_headers=key)
        assert result.status_code == 201, result.text
        identifier = result.json()["data"]["id"]
        with store.read() as tx:
            locator = tx.artifacts.get(identifier).locator
        file = store.artifact_root / locator
        file.write_bytes(b"corrupt")
        assert upload(client, csrf, value, request_headers=key).status_code == 409
        assert upload(client, csrf, value).status_code == 409
        ForgetService(store, store.clock, idempotency=IdempotencyManager(store)).forget(
            world["access"],
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE, resource_type="artifact", resource_id=identifier
            ),
            reason="operator_request",
            idempotency_key="forget-upload",
        )
        assert not file.exists()
        assert upload(client, csrf, value, request_headers=key).status_code == 404
        assert client.get("/v1/memory/artifacts/" + identifier).status_code == 404


def test_upload_accepts_exact_stream_bound_and_rejects_immutable_media_change(
    world: dict[str, Any],
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        value = metadata(world)
        data = b"x" * (8 * 1024 * 1024)
        result = upload(client, csrf, value, data)
        assert result.status_code == 201, result.text
        identifier = result.json()["data"]["id"]
        value["fields"]["media_type"] = "image/png"
        conflict = upload(client, csrf, value, data)
        assert conflict.status_code == 409, conflict.text
        with world["store"].read() as tx:
            assert tx.artifacts.get(identifier).refcount == 1
        assert len(files(world)) == 1


def test_upload_commit_failure_removes_file_with_canonical_rollback(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.console.artifacts import ConsoleArtifactCommands
    from iris_memory_core.domain.scope import Scope
    from iris_memory_core.storage.uow import Store
    from tests.integration.test_console_commands import principal_for

    principal = principal_for(world)
    original = Store._commit_with_retry

    def fail_commit(self: Any, connection: Any) -> None:
        if connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]:
            raise RuntimeError("upload canonical commit failed")
        original(self, connection)

    monkeypatch.setattr(Store, "_commit_with_retry", fail_commit)
    with pytest.raises(RuntimeError, match="canonical commit failed"):
        ConsoleArtifactCommands(world["security"]).upload(
            principal,
            scope=Scope(world["tenant"], world["agent"]),
            payload=b"rollback at commit",
            media_type="application/octet-stream",
            privacy_labels=[],
            source_refs=[],
            reason="operator_request",
            idempotency_key="upload-commit-failure",
        )
    assert files(world) == set()
    with world["store"].read() as tx:
        assert tx.raw().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_upload_backup_restore_keeps_bytes_and_forget_cleanup(
    world: dict[str, Any], tmp_path: Any
) -> None:
    from iris_memory_core.application.artifacts import ArtifactService
    from iris_memory_core.application.forget import ForgetService
    from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
    from iris_memory_core.storage.backup import BackupService, verify_database_invariants
    from iris_memory_core.storage.idempotency import IdempotencyManager
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from iris_memory_core.storage.uow import Store
    from tests.conftest import local_allowed_versions

    client, csrf = signed_in(world["app"], world["token"])
    data = b"original backup binary\0\xff"
    with client:
        response = upload(client, csrf, metadata(world), data)
        assert response.status_code == 201, response.text
        identifier = response.json()["data"]["id"]
    backup = BackupService(world["store"])
    archive = tmp_path / "backup"
    destination = tmp_path / "restore"
    backup.create_backup(archive)
    restored = backup.restore_backup(archive, destination)
    assert restored.check.ok, restored.check.problems
    database = destination / "canonical.sqlite3"
    recovered = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    result = ArtifactService(recovered, recovered.clock).read(world["access"], identifier)
    assert result is not None and result.content == data
    blob = recovered.artifact_root / result.record.locator
    assert blob.is_file()
    ForgetService(recovered, recovered.clock, idempotency=IdempotencyManager(recovered)).forget(
        world["access"],
        ForgetSelector(
            kind=ForgetSelectorKind.RESOURCE, resource_type="artifact", resource_id=identifier
        ),
        reason="operator_request",
        idempotency_key="forget-restored-upload",
    )
    assert not blob.exists()
    assert ArtifactService(recovered, recovered.clock).read(world["access"], identifier) is None
    assert verify_database_invariants(database) == ()


@pytest.mark.parametrize("kind", ["oversized", "fifo"])
def test_uploaded_blob_refuses_unbounded_or_nonregular_reads(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import os

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        value = metadata(world)
        key = headers(csrf)
        result = upload(client, csrf, value, request_headers=key)
        assert result.status_code == 201, result.text
        identifier = result.json()["data"]["id"]
        with world["store"].read() as tx:
            locator = tx.artifacts.get(identifier).locator
        target = world["store"].artifact_root / locator
        if kind == "fifo":
            target.unlink()
            os.mkfifo(target)
        else:
            with target.open("r+b") as handle:
                handle.truncate(100 * 1024 * 1024)

        def no_read(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("invalid blob must be rejected before any content read")

        monkeypatch.setattr(os, "fdopen", no_read)
        try:
            response = upload(client, csrf, value, request_headers=key)
            assert response.status_code == 409, response.text
        finally:
            target.unlink()


@pytest.mark.parametrize("case", ["duplicate_length", "ambiguous_framing", "declared_oversize"])
def test_upload_ambiguous_headers_fail_before_body_consumption(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:

        async def unread(request: Request) -> Any:
            pytest.fail("invalid upload framing must not consume body")
            yield b""

        monkeypatch.setattr(Request, "stream", unread)
        header_list = list(
            {
                **headers(csrf),
                "content-type": "application/octet-stream",
                "content-length": "4",
            }.items()
        )
        if case == "duplicate_length":
            header_list.append(("content-length", "5"))
        elif case == "ambiguous_framing":
            header_list.append(("transfer-encoding", "chunked"))
        else:
            header_list = [
                (key, str(8 * 1024 * 1024 + 1) if key == "content-length" else value)
                for key, value in header_list
            ]
        response = client.post(
            PATH,
            params={"metadata": json.dumps(metadata(world))},
            headers=header_list,
            content=b"data",
        )
        assert response.status_code == (413 if case == "declared_oversize" else 400), response.text
        assert files(world) == set()
