"""Real operator deletion: fixed previews, atomic ledger, replay and restored backups."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.retention import RetentionService
from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import grant_for
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def target(client: Any, identifier: str, collection: str = "notes") -> dict[str, Any]:
    result = client.get("/v1/memory/" + collection + "/" + identifier)
    assert result.status_code == 200, result.text
    row = result.json()["data"]
    return {
        "resource_type": row["resource_type"],
        "id": row["id"],
        "expected_revision": row["revision"],
    }


def preview(
    client: Any, csrf: str, targets: list[dict[str, Any]], mode: str = "soft", **options: Any
) -> dict[str, Any]:
    result = client.post(
        "/v1/memory:forget-preview",
        headers=options.get("headers", headers(csrf)),
        json={"targets": targets, "mode": mode, "reason_code": "operator_request"},
    )
    assert result.status_code == 200, result.text
    validate_response("ConsoleForgetPreviewEnvelope", result.json())
    return dict(result.json()["data"])


def commit(client: Any, csrf: str, saved: dict[str, Any], **options: Any) -> Any:
    return client.post(
        "/v1/memory:forget",
        headers=options.get("headers", headers(csrf)),
        json={name: saved[name] for name in ["preview_id", "preview_hash"]}
        | {"reason_code": "operator_request"},
    )


@pytest.mark.parametrize("mode", ["soft", "erase"])
def test_forget_commit_receipt_replay_ledger_and_no_content(
    world: dict[str, Any], mode: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    identifier = world["ids"]["a"]
    with client:
        validate_response("ResourceTypePage", client.get("/v1/memory/resource-types").json())
        assert (
            "forget"
            in client.get("/v1/memory/notes/" + identifier).json()["data"]["available_actions"]
        )
        row = target(client, identifier)
        key = headers(csrf)
        saved = preview(client, csrf, [row], mode, headers=key)
        assert preview(client, csrf, [row], mode, headers=key) == saved
        assert saved["can_commit"]
        if mode == "erase":
            blocked = commit(client, csrf, saved)
            assert (
                blocked.status_code == 403
                and blocked.json()["error"]["details"]["kind"] == "reauth_required"
            )
            assert (
                client.post(
                    "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
                ).status_code
                == 200
            )
        key = headers(csrf)
        result = commit(client, csrf, saved, headers=key)
        assert result.status_code == 200, result.text
        validate_response("ConsoleForgetReceiptEnvelope", result.json())
        receipt = result.json()["data"]
        assert receipt["target_count"] == 1 and receipt["cleanup_status"] == "pending"
        assert identifier not in result.text and "xxx" not in result.text
        assert commit(client, csrf, saved, headers=key).json()["data"] == receipt
        assert commit(client, csrf, saved).json()["data"] == receipt
        for suffix in ["", "/history"]:
            assert client.get("/v1/memory/notes/" + identifier + suffix).status_code == 404
        with world["store"].read() as tx:
            stored = tx.console.command_preview(
                world["tenant"], world["owner"].id, saved["preview_id"]
            )
            assert stored.status == "consumed" and stored.payload_json == "{}"
            assert tx.is_tombstoned(world["tenant"], "note", identifier)
        ledger = ForgetService(world["store"], world["store"].clock).export_deletion_ledger(
            world["access"]
        )
        matching = [item for item in ledger if identifier in item.selector_json]
        assert len(matching) == 1 and matching[0].requested_by == "console:" + world["owner"].id
        if mode == "soft":
            world["store"].clock.advance(601_000_000)
            assert commit(client, csrf, saved).json()["data"] == receipt


@pytest.mark.parametrize("change", ["revision", "hold", "watermark", "ttl", "hash"])
def test_preview_invalidated_before_any_writes(world: dict[str, Any], change: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    identifier = world["ids"]["a"]
    with client:
        saved = preview(client, csrf, [target(client, identifier)])
        if change == "revision":
            world["notes"].update(
                world["access"],
                identifier,
                expected_revision=1,
                title="Changed",
                idempotency_key="change",
            )
        elif change == "hold":
            RetentionService(
                world["store"],
                world["store"].clock,
                forget=ForgetService(world["store"], world["store"].clock),
            ).create_legal_hold(world["access"], space_id=world["spaces"][0], reason="hold")
        elif change == "watermark":
            other = preview(client, csrf, [target(client, world["ids"]["b"])])
            assert commit(client, csrf, other).status_code == 200
        elif change == "ttl":
            world["store"].clock.advance(600_000_000)
        else:
            saved["preview_hash"] = "0" * 64
        result = commit(client, csrf, saved)
        assert result.status_code == 409, result.text
        assert result.json()["error"]["details"]["kind"] == "preview_stale"
        with world["store"].read() as tx:
            assert not tx.is_tombstoned(world["tenant"], "note", identifier)


def test_hold_and_protected_preview_cannot_commit(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    identifier = world["ids"]["a"]
    RetentionService(
        world["store"],
        world["store"].clock,
        forget=ForgetService(world["store"], world["store"].clock),
    ).create_legal_hold(world["access"], space_id=world["spaces"][0], reason="hold")
    with client:
        saved = preview(client, csrf, [target(client, identifier)])
        assert saved["targets"][0]["status"] == "held" and not saved["can_commit"]
        assert commit(client, csrf, saved).status_code == 409
        pinned = world["notes"].create(
            world["access"],
            agent_id=world["agent"],
            kind="idea",
            title="pinned",
            body="body",
            idempotency_key="pin",
        )
        world["notes"].transition(
            world["access"],
            pinned.note_id,
            expected_revision=1,
            target="pin",
            reason="test",
            idempotency_key="pin-status",
        )
        saved = preview(client, csrf, [target(client, pinned.note_id)])
        assert saved["targets"][0]["status"] == "protected" and not saved["can_commit"]
        assert commit(client, csrf, saved).status_code == 409


def test_forget_only_permission_hidden_targets_and_global_mutability(world: dict[str, Any]) -> None:
    grant = grant_for(world, permissions=frozenset({"memory.read", "memory.forget"}))
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="forgetter",
        description="",
        template="viewer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        registry = client.get("/v1/memory/resource-types").json()
        validate_response("ResourceTypePage", registry)
        notes = next(row for row in registry["data"] if row["collection"] == "notes")
        assert [a["id"] for a in notes["actions"]] == ["forget"]
        targets = [
            {"resource_type": "note", "id": world["ids"][key], "expected_revision": 1}
            for key in ["a", "b", "global", "private"]
        ]
        targets.append({"resource_type": "note", "id": "does-not-exist", "expected_revision": 1})
        saved = preview(client, csrf, targets)
        assert not saved["can_commit"] and saved["targets"][0]["status"] == "allowed"
        for index in range(1, 5):
            assert saved["targets"][index] == {"input_index": index, "status": "not_visible"}
        assert commit(client, csrf, saved).status_code == 409
        allowed = preview(client, csrf, targets[:1])
        assert commit(client, csrf, allowed).status_code == 200


@pytest.mark.parametrize(
    "value",
    ["duplicate", "too_many", "empty", "task_step", "actor", "selector", "missing_revision"],
)
def test_strict_explicit_preview_inputs(world: dict[str, Any], value: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    row = {"resource_type": "note", "id": world["ids"]["a"], "expected_revision": 1}
    body: dict[str, Any] = {"targets": [row], "mode": "soft", "reason_code": "operator_request"}
    if value == "duplicate":
        body["targets"] *= 2
    elif value == "too_many":
        body["targets"] = [{**row, "id": str(n)} for n in range(501)]
    elif value == "empty":
        body["targets"] = []
    elif value == "task_step":
        row["resource_type"] = "task_step"
    elif value == "missing_revision":
        row.pop("expected_revision")
    else:
        body[value] = {}
    with client:
        result = client.post("/v1/memory:forget-preview", headers=headers(csrf), json=body)
        assert result.status_code == 400, result.text


def test_preview_owner_and_current_grant_are_required_even_after_commit(
    world: dict[str, Any],
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        saved = preview(client, csrf, [target(client, world["ids"]["a"])])
        key = headers(csrf)
        assert commit(client, csrf, saved, headers=key).status_code == 200
        _, other_token = world["security"].issue_offline(
            tenant_id=world["tenant"],
            label="other",
            description="",
            template="owner",
            grant=world["owner"].grant,
            expires_us=world["owner"].expires_us,
        )
        other, other_csrf = signed_in(world["app"], other_token)
        with other:
            assert commit(other, other_csrf, saved).status_code == 404
        with world["store"].write() as tx:
            current = tx.console.key(world["owner"].id)
            tx.console.save_key(
                replace(current, revision=current.revision + 1), expected_revision=current.revision
            )
        assert commit(client, csrf, saved, headers=key).status_code == 403


@pytest.mark.parametrize("mixed_entity", [False, True])
def test_late_failure_rolls_back_whole_batch_and_same_key_retries(
    world: dict[str, Any], monkeypatch: Any, mixed_entity: bool
) -> None:
    from iris_memory_core.storage.console import ConsoleRepository

    client, csrf = signed_in(world["app"], world["token"])
    identifiers = [world["ids"]["a"], world["ids"]["b"]]
    with client:
        rows = [target(client, identifier) for identifier in identifiers]
        if mixed_entity:
            from tests.integration.test_console_identity_http import entity

            rows[0] = target(client, entity(client, csrf), "entities")
        saved = preview(client, csrf, rows)
        key = headers(csrf)
        original = ConsoleRepository.consume_command_preview

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("late injected failure")

        monkeypatch.setattr(ConsoleRepository, "consume_command_preview", fail)
        result = commit(client, csrf, saved, headers=key)
        assert result.status_code == 500, result.text
        with world["store"].read() as tx:
            assert all(
                not tx.is_tombstoned(world["tenant"], row["resource_type"], row["id"])
                for row in rows
            )
            if mixed_entity:
                assert tx.get_entity(rows[0]["id"]).revision == 1
            assert (
                tx.console.command_preview(
                    world["tenant"], world["owner"].id, saved["preview_id"]
                ).status
                == "ready"
            )
        assert not ForgetService(world["store"], world["store"].clock).export_deletion_ledger(
            world["access"]
        )
        monkeypatch.setattr(ConsoleRepository, "consume_command_preview", original)
        result = commit(client, csrf, saved, headers=key)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["target_count"] == 2


def test_restore_old_backup_replays_console_deletion_ledger(
    world: dict[str, Any], tmp_path: Any
) -> None:
    from iris_memory_core.storage.backup import BackupService, restore_backup
    from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
    from iris_memory_core.storage.uow import Store

    backup = tmp_path / "before-forget"
    BackupService(world["store"]).create_backup(backup)
    client, csrf = signed_in(world["app"], world["token"])
    identifier = world["ids"]["a"]
    with client:
        saved = preview(client, csrf, [target(client, identifier)])
        assert commit(client, csrf, saved).status_code == 200
    ledger = ForgetService(world["store"], world["store"].clock).export_deletion_ledger(
        world["access"]
    )
    destination = tmp_path / "restored"
    assert restore_backup(backup, destination).check.ok
    restored = Store(
        SQLiteRuntime(
            destination / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
        )
    )
    with restored.read() as tx:
        assert not tx.is_tombstoned(world["tenant"], "note", identifier)
    assert ForgetService(restored, restored.clock).replay_deletion_ledger(restored, ledger) == 1
    with restored.read() as tx:
        assert tx.is_tombstoned(world["tenant"], "note", identifier)


@pytest.mark.parametrize("kind", ["observation", "claim", "episode", "relation", "artifact"])
def test_each_content_type_uses_core_forget_in_required_mode(
    world: dict[str, Any], kind: str
) -> None:
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.surface import SurfaceMode
    from tests.integration.test_console_artifact_http import payload as artifact_payload
    from tests.integration.test_console_claim_http import create_payload, source
    from tests.integration.test_console_episode_http import payload as episode_payload
    from tests.integration.test_console_relation_http import payload as relation_payload

    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test"
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        observation = source(client, csrf, world)
        collection = kind + "s"
        if kind == "observation":
            identifier = observation
        else:
            payloads = {
                "claim": lambda: create_payload(world, observation),
                "episode": lambda: episode_payload(world, observation),
                "relation": lambda: relation_payload(world, observation),
                "artifact": lambda: artifact_payload(world),
            }
            body = payloads[kind]()
            result = client.post("/v1/memory/" + collection, headers=headers(csrf), json=body)
            assert result.status_code == 201, result.text
            identifier = result.json()["data"]["id"]
        saved = preview(client, csrf, [target(client, identifier, collection)])
        assert saved["can_commit"]
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        assert client.get("/v1/memory/" + collection + "/" + identifier).status_code == 404
        with store.read() as tx:
            assert tx.is_tombstoned(world["tenant"], kind, identifier)


def test_erasure_rechecks_recent_auth_on_cached_receipt(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        saved = preview(client, csrf, [target(client, world["ids"]["a"])], "erase")
        assert (
            client.post(
                "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
            ).status_code
            == 200
        )
        key = headers(csrf)
        assert commit(client, csrf, saved, headers=key).status_code == 200
        world["store"].clock.advance(300_000_000)
        result = commit(client, csrf, saved, headers=key)
        assert result.status_code == 403
        assert result.json()["error"]["details"]["kind"] == "reauth_required"


def test_hold_capacity_fails_closed(world: dict[str, Any]) -> None:
    store = world["store"]
    with store.write() as tx:
        now = store.clock.now_us()
        for index in range(501):
            tx.raw().execute(
                "INSERT INTO legal_holds (id,tenant_id,space_id,reason_code,created_by,created_us) "
                "VALUES (?,?,?,?,?,?)",
                (f"hold-{index}", world["tenant"], world["spaces"][1], "hold", "test", now),
            )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        result = client.post(
            "/v1/memory:forget-preview",
            headers=headers(csrf),
            json={
                "targets": [target(client, world["ids"]["a"])],
                "mode": "soft",
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == 503, result.text


def test_current_permission_required_before_preview(world: dict[str, Any]) -> None:
    grant = grant_for(world, permissions=frozenset({"memory.read", "memory.write"}))
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="writer",
        description="",
        template="viewer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        result = client.post(
            "/v1/memory:forget-preview",
            headers=headers(csrf),
            json={
                "targets": [target(client, world["ids"]["a"])],
                "mode": "soft",
                "reason_code": "operator_request",
            },
        )
        assert result.status_code == 403, result.text


@pytest.mark.parametrize("mode", ["soft", "erase"])
def test_blob_forget_and_restoration_preserve_erasure_semantics(
    world: dict[str, Any], tmp_path: Any, mode: str
) -> None:
    from iris_memory_core.storage.backup import BackupService
    from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
    from iris_memory_core.storage.uow import Store
    from tests.integration.test_console_artifact_upload import files, metadata, upload

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        created = upload(client, csrf, metadata(world))
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
        before = files(world)
        assert before
        backup = tmp_path / "before"
        backup_service = BackupService(store)
        backup_service.create_backup(backup)
        saved = preview(client, csrf, [target(client, identifier, "artifacts")], mode)
        if mode == "erase":
            assert (
                client.post(
                    "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
                ).status_code
                == 200
            )
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        assert files(world) == (before if mode == "soft" else set())
        forget = ForgetService(store, store.clock)
        ledger = forget.export_deletion_ledger(world["access"])
        destination = tmp_path / "restored"
        report = backup_service.restore_backup(
            backup, destination, deletion_ledger=ledger, forget_service=forget
        )
        assert report.check.ok, report.check.problems
        restored = Store(
            SQLiteRuntime(
                destination / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
            )
        )
        with restored.read() as tx:
            assert tx.is_tombstoned(world["tenant"], "artifact", identifier)
            row = tx.artifacts.get(identifier)
            assert row.agent_id == world["agent"]
            assert tx.artifacts.blob_exists(row.locator) is (mode == "soft")


def test_cleanup_failure_returns_pending_receipt_and_durable_retry(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from iris_memory_core.jobs.handlers import memory_invalidated_handler
    from iris_memory_core.storage.memory import ArtifactRepository
    from tests.integration.test_console_artifact_upload import files, metadata, upload

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        created = upload(client, csrf, metadata(world))
        assert created.status_code == 201
        identifier = created.json()["data"]["id"]
        saved = preview(client, csrf, [target(client, identifier, "artifacts")], "erase")
        assert (
            client.post(
                "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
            ).status_code
            == 200
        )
        original = ArtifactRepository.unlink_blob

        def fail(*args: Any, **kwargs: Any) -> bool:
            raise OSError("temporary filesystem failure")

        monkeypatch.setattr(ArtifactRepository, "unlink_blob", fail)
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["cleanup_status"] == "pending"
        assert files(world)
        assert client.get("/v1/memory/artifacts/" + identifier).status_code == 404
        monkeypatch.setattr(ArtifactRepository, "unlink_blob", original)
        with store.write() as tx:
            row = (
                tx.raw()
                .execute(
                    "SELECT id FROM outbox_jobs WHERE job_kind='memory.invalidated' "
                    "ORDER BY created_us DESC LIMIT 1"
                )
                .fetchone()
            )
            job = tx.outbox.get(row[0])
            memory_invalidated_handler(store.clock)(job)(tx)
        assert not files(world)


def test_fifty_targets_commit_atomically_at_advertised_limit(world: dict[str, Any]) -> None:
    identifiers = []
    for index in range(50):
        identifiers.append(
            world["notes"]
            .create(
                world["access"],
                agent_id=world["agent"],
                kind="idea",
                title=f"batch {index}",
                body="bounded batch",
                idempotency_key=f"batch-{index}",
            )
            .note_id
        )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        rows = [
            {"resource_type": "note", "id": identifier, "expected_revision": 1}
            for identifier in identifiers
        ]
        saved = preview(client, csrf, rows)
        assert len(saved["targets"]) == 50 and saved["can_commit"]
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["target_count"] == 50
        with world["store"].read() as tx:
            assert all(
                tx.is_tombstoned(world["tenant"], "note", identifier) for identifier in identifiers
            )
