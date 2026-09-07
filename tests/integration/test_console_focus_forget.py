"""Managed Focus deletion preserves lifecycle and restores deletion facts."""

from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_forget_http import commit, preview, target
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def create(client: Any, csrf: str, world: dict[str, Any], *, space_id: str | None = None) -> str:
    r = client.post(
        "/v1/memory/focus-items",
        headers=headers(csrf),
        json={
            "scope": {"agent_id": world["agent"], **({"space_id": space_id} if space_id else {})},
            "fields": {
                "kind": "goal",
                "summary": "private focus body",
                "structured_payload": {"secret": "focus value"},
            },
            "reason_code": "operator_request",
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["data"]["id"])


@pytest.mark.parametrize("mode", ["soft", "erase"])
def test_focus_delete_hides_current_and_history_and_erases_all_revisions(
    world: dict[str, Any], mode: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        if mode == "erase":
            assert (
                client.post(
                    "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
                ).status_code
                == 200
            )
        identifier = create(client, csrf, world)
        r = client.patch(
            "/v1/memory/focus-items/" + identifier,
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"summary": "second private focus body"},
                "reason_code": "operator_request",
            },
        )
        assert r.status_code == 200, r.text
        row = target(client, identifier, "focus-items")
        assert row["expected_revision"] == 2
        saved = preview(client, csrf, [row], mode=mode)
        key = headers(csrf)
        r = commit(client, csrf, saved, headers=key)
        assert r.status_code == 200, r.text
        assert commit(client, csrf, saved, headers=key).json()["data"] == r.json()["data"]
        for suffix in ("", "/history"):
            assert client.get("/v1/memory/focus-items/" + identifier + suffix).status_code == 404
        with world["store"].read() as tx:
            current = tx.focus.get(identifier)
            assert current.current_revision == 2
            assert tx.focus.current_revision_row(identifier).summary == (
                "<erased>" if mode == "erase" else "second private focus body"
            )
            history = tx.focus.history(identifier)
            assert len(history) == 2
            if mode == "erase":
                assert all(
                    row.summary == "<erased>" and row.structured_payload is None for row in history
                )
            else:
                assert {row.summary for row in history} == {
                    "private focus body",
                    "second private focus body",
                }
            revision = tx.focus.current_revision_row(identifier)
            assert revision.structured_payload == (
                None if mode == "erase" else {"secret": "focus value"}
            )
        service = FocusService(world["store"], world["store"].clock)
        assert service.get(world["access"], identifier) is None
        with pytest.raises(NotFoundError):
            service.history(world["access"], identifier)
        ledger = ForgetService(world["store"], world["store"].clock).export_deletion_ledger(
            world["access"]
        )
        assert len(ledger) == 1 and ledger[0].requested_by == "console:" + world["owner"].id


@pytest.mark.parametrize("before_creation", [True, False])
@pytest.mark.parametrize("mode", ["soft", "erase"])
def test_focus_deletion_replays_into_old_backup(
    world: dict[str, Any], tmp_path: Path, before_creation: bool, mode: str
) -> None:
    store = world["store"]
    backups = BackupService(store)
    backup = tmp_path / "before"
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        if mode == "erase":
            assert (
                client.post(
                    "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
                ).status_code
                == 200
            )
        if before_creation:
            backups.create_backup(backup)
        identifier = create(client, csrf, world)
        if not before_creation:
            backups.create_backup(backup)
        saved = preview(client, csrf, [target(client, identifier, "focus-items")], mode=mode)
        r = commit(client, csrf, saved)
        assert r.status_code == 200, r.text
    forget = ForgetService(store, store.clock)
    ledger = forget.export_deletion_ledger(world["access"])
    destination = tmp_path / "restored"
    report = backups.restore_backup(
        backup, destination, deletion_ledger=ledger, forget_service=forget
    )
    assert report.check.ok, report.check.problems
    restored = Store(
        SQLiteRuntime(
            destination / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
        )
    )
    with restored.read() as tx:
        assert tx.is_tombstoned(world["tenant"], "focus_item", identifier)
        if before_creation:
            with pytest.raises(NotFoundError):
                tx.focus.get(identifier)
        else:
            assert tx.focus.current_revision_row(identifier).summary == (
                "<erased>" if mode == "erase" else "private focus body"
            )
    assert forget.replay_deletion_ledger(restored, ledger) == 0


def test_mixed_focus_erase_rolls_back_and_scrubs_cache_only_on_commit(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from iris_memory_core.jobs.handlers import memory_invalidated_handler
    from iris_memory_core.storage.console import ConsoleRepository

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        assert (
            client.post(
                "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
            ).status_code
            == 200
        )
        identifier = create(client, csrf, world)
        with store.write() as tx:
            tx.usage.insert_request(
                request_id="focus-recall",
                tenant_id=world["tenant"],
                agent_id=world["agent"],
                persona_revision=1,
                source_watermark=0,
                tombstone_watermark=0,
                schema_version=1,
                ranker_version=1,
                token_estimator_version=1,
                retrieved_count=1,
                returned_candidate_ids=["candidate"],
                request_fingerprint="test",
                resource_ids=[identifier],
                response_json='{"body":"private focus body"}',
            )
        saved = preview(
            client,
            csrf,
            [target(client, identifier, "focus-items"), target(client, world["ids"]["a"])],
            mode="erase",
        )
        original = ConsoleRepository.consume_command_preview

        def fail(*a: Any, **kw: Any) -> None:
            raise RuntimeError("late injected failure")

        monkeypatch.setattr(ConsoleRepository, "consume_command_preview", fail)
        key = headers(csrf)
        r = commit(client, csrf, saved, headers=key)
        assert r.status_code == 500, r.text
        with store.read() as tx:
            assert not tx.is_tombstoned(world["tenant"], "focus_item", identifier)
            assert not tx.is_tombstoned(world["tenant"], "note", world["ids"]["a"])
            assert tx.focus.current_revision_row(identifier).summary == "private focus body"
            assert (
                tx.usage.get_request(world["tenant"], "focus-recall")["response_json"] is not None
            )
        monkeypatch.setattr(ConsoleRepository, "consume_command_preview", original)
        r = commit(client, csrf, saved, headers=key)
        assert r.status_code == 200, r.text
        with store.write() as tx:
            assert tx.usage.get_request(world["tenant"], "focus-recall")["response_json"] is None
            jobs = (
                tx.raw()
                .execute("SELECT id FROM outbox_jobs WHERE job_kind='memory.invalidated'")
                .fetchall()
            )
            assert len(jobs) == 2
            for row in jobs:
                memory_invalidated_handler(store.clock)(tx.outbox.get(row[0]))(tx)


def test_focus_hold_blocks_and_new_hold_invalidates_preview(world: dict[str, Any]) -> None:
    from iris_memory_core.application.retention import RetentionService

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = create(client, csrf, world, space_id=world["spaces"][0])
        row = target(client, identifier, "focus-items")
        saved = preview(client, csrf, [row])
        RetentionService(
            store, store.clock, forget=ForgetService(store, store.clock)
        ).create_legal_hold(world["access"], space_id=world["spaces"][0], reason="hold")
        r = commit(client, csrf, saved)
        assert r.status_code == 409, r.text
        saved = preview(client, csrf, [row])
        assert saved["targets"][0]["status"] == "held" and not saved["can_commit"]
        assert commit(client, csrf, saved).status_code == 409
        with store.read() as tx:
            assert not tx.is_tombstoned(world["tenant"], "focus_item", identifier)


def test_fifty_focus_items_commit_atomically(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifiers = [create(client, csrf, world) for _ in range(50)]
        rows = [target(client, identifier, "focus-items") for identifier in identifiers]
        saved = preview(client, csrf, rows)
        assert saved["can_commit"] and len(saved["targets"]) == 50
        r = commit(client, csrf, saved)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["target_count"] == 50
        with world["store"].read() as tx:
            assert all(
                tx.is_tombstoned(world["tenant"], "focus_item", identifier)
                for identifier in identifiers
            )


def test_deleted_focus_does_not_consume_capacity_or_maintenance_limit(
    world: dict[str, Any],
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifiers = [create(client, csrf, world) for _ in range(10)]
        rows = [target(client, identifier, "focus-items") for identifier in identifiers]
        saved = preview(client, csrf, rows)
        assert commit(client, csrf, saved).status_code == 200
        newest = create(client, csrf, world)
        with world["store"].read() as tx:
            assert [item.id for item in tx.focus.active_items(world["tenant"], world["agent"])] == [
                newest
            ]
            assert [
                item.id
                for item in tx.focus.items_for_agent(world["tenant"], world["agent"], limit=1)
            ] == [newest]
            assert [
                item.id
                for item in tx.focus.maintenance_items(
                    world["tenant"], world["agent"], now_us=world["store"].clock.now_us(), limit=1
                )
            ] == [newest]


def test_focus_erasure_deadline_rolls_back_all_targets_and_allows_retry(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from types import SimpleNamespace

    from iris_memory_core.storage import cognitive

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        assert (
            client.post(
                "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
            ).status_code
            == 200
        )
        identifier = create(client, csrf, world)
        for revision in range(1, 31):
            result = client.patch(
                "/v1/memory/focus-items/" + identifier,
                headers=headers(csrf),
                json={
                    "expected_revision": revision,
                    "fields": {"summary": f"private history {revision}"},
                    "reason_code": "operator_request",
                },
            )
            assert result.status_code == 200, result.text
        saved = preview(
            client,
            csrf,
            [target(client, world["ids"]["a"]), target(client, identifier, "focus-items")],
            mode="erase",
        )
        calls = 0

        def deadline() -> float:
            nonlocal calls
            calls += 1
            return 0.0 if calls == 1 else 1.0

        key = headers(csrf)
        with monkeypatch.context() as patch:
            # Replace only this module's clock; authentication/reader clocks stay real.
            patch.setattr(cognitive, "time", SimpleNamespace(monotonic=deadline))
            result = commit(client, csrf, saved, headers=key)
        assert calls > 1
        assert result.status_code == 503, result.text
        with store.read() as tx:
            assert not tx.is_tombstoned(world["tenant"], "note", world["ids"]["a"])
            assert not tx.is_tombstoned(world["tenant"], "focus_item", identifier)
            assert len(tx.focus.history(identifier)) == 31
            assert all(row.summary != "<erased>" for row in tx.focus.history(identifier))
        result = commit(client, csrf, saved, headers=key)
        assert result.status_code == 200, result.text
        with store.read() as tx:
            assert all(row.summary == "<erased>" for row in tx.focus.history(identifier))


def test_focus_replay_rejects_inconsistent_selector_identity(world: dict[str, Any]) -> None:
    from dataclasses import replace

    from iris_memory_core.domain.errors import InvalidRequestError

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = create(client, csrf, world)
        saved = preview(client, csrf, [target(client, identifier, "focus-items")])
        assert commit(client, csrf, saved).status_code == 200
    forget = ForgetService(store, store.clock)
    ledger = forget.export_deletion_ledger(world["access"])
    with pytest.raises(InvalidRequestError):
        forget.replay_deletion_ledger(store, (replace(ledger[0], selector_key="wrong-identity"),))
    assert forget.export_deletion_ledger(world["access"]) == ledger


def test_focus_forget_only_grant_is_scoped_and_cached_receipt_rechecks_key(
    world: dict[str, Any],
) -> None:
    from dataclasses import replace

    from tests.integration.test_console_reads import grant_for

    owner, owner_csrf = signed_in(world["app"], world["token"])
    with owner:
        allowed = create(owner, owner_csrf, world, space_id=world["spaces"][0])
        hidden = create(owner, owner_csrf, world, space_id=world["spaces"][1])
        global_item = create(owner, owner_csrf, world)
    issued, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="Focus forgetter",
        description="",
        template="viewer",
        grant=grant_for(world, permissions=frozenset({"memory.read", "memory.forget"})),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        rows = [
            {"resource_type": "focus_item", "id": identifier, "expected_revision": 1}
            for identifier in (allowed, hidden, global_item)
        ]
        saved = preview(client, csrf, rows)
        assert saved["targets"][0]["status"] == "allowed"
        assert saved["targets"][1:] == [
            {"input_index": index, "status": "not_visible"} for index in (1, 2)
        ]
        assert commit(client, csrf, saved).status_code == 409
        saved = preview(client, csrf, rows[:1])
        key = headers(csrf)
        result = commit(client, csrf, saved, headers=key)
        assert result.status_code == 200, result.text
        with world["store"].write() as tx:
            current = tx.console.key(issued.id)
            tx.console.save_key(
                replace(current, revision=current.revision + 1), expected_revision=current.revision
            )
        assert commit(client, csrf, saved, headers=key).status_code == 403
