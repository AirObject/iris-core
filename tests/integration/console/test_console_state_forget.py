"""State deletion preserves old IDs and requires explicit natural-key recreation."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.scope import Scope
from iris_memory_core.domain.state import DEFAULT_STATE_POLICY, state_scope_key
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_forget_http import commit, preview, target
from tests.integration.console.test_console_reads import world as world_fixture
from tests.integration.console.test_console_state_http import create_body

auth = auth_fixture
world = world_fixture


def create(client: Any, csrf: str, world: dict[str, Any], key: str = "current") -> str:
    body = create_body(world, "topic")
    body["fields"]["key"] = key
    result = client.post("/v1/memory/states", headers=headers(csrf), json=body)
    assert result.status_code == 201, result.text
    return str(result.json()["data"]["resource_id"])


def reauth(client: Any, csrf: str, world: dict[str, Any]) -> None:
    assert (
        client.post(
            "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
        ).status_code
        == 200
    )


@pytest.mark.parametrize("mode", ["soft", "erase"])
@pytest.mark.parametrize("global_scope", [False, True])
def test_state_forget_and_explicit_recreation(
    world: dict[str, Any], mode: str, global_scope: bool
) -> None:
    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    namespace = "custom" if global_scope else "topic"
    if global_scope:
        StateService(store, store.clock).set_namespace_policy(
            world["access"],
            replace(
                DEFAULT_STATE_POLICY,
                namespace=namespace,
                retain_history=True,
                max_history_revisions=5,
            ),
            reason="test",
        )
    body = create_body(world, namespace)
    with client:
        original_key = headers(csrf)
        created = client.post("/v1/memory/states", headers=original_key, json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["resource_id"]
        update_key = headers(csrf)
        update_body = {
            "expected_revision": 1,
            "fields": {"value": {"text": "updated private state"}},
            "reason_code": "operator_request",
        }
        updated = client.patch(
            "/v1/memory/states/" + identifier, headers=update_key, json=update_body
        )
        assert updated.status_code == 200, updated.text
        if global_scope:
            # HTTP creation requires an Agent. Seed the nullable storage scope
            # to cover historical/imported global rows without extending that API.
            with store.write() as tx:
                tx.raw().execute(
                    "UPDATE state_records SET agent_id=NULL,space_id=NULL,scope_key=? WHERE id=?",
                    (state_scope_key(Scope(world["tenant"])), identifier),
                )
        with store.read() as tx:
            before = tx.watermark(world["tenant"], "" if global_scope else world["agent"])
        if mode == "erase":
            reauth(client, csrf, world)
        saved = preview(client, csrf, [target(client, identifier, "states")], mode=mode)
        commit_key = headers(csrf)
        result = commit(client, csrf, saved, headers=commit_key)
        assert result.status_code == 200, result.text
        assert (
            commit(client, csrf, saved, headers=commit_key).json()["data"] == result.json()["data"]
        )
        for suffix in ("", "/history"):
            assert client.get("/v1/memory/states/" + identifier + suffix).status_code == 404
        assert (
            client.patch(
                "/v1/memory/states/" + identifier, headers=update_key, json=update_body
            ).status_code
            == 404
        )
        with store.read() as tx:
            row = tx.states.get(identifier)
            assert row.current_revision == 2 and row.namespace == namespace and row.key == "current"
            revisions = tx.states.history(identifier)
            assert len(revisions) == 2
            if mode == "erase":
                assert all(
                    json.loads(rev.value_json) == {}
                    and rev.source_ref is None
                    and rev.coalesce_key is None
                    for rev in revisions
                )
            else:
                assert {json.loads(rev.value_json)["text"] for rev in revisions} == {
                    "private state value",
                    "updated private state",
                }
            assert (
                tx.raw()
                .execute("SELECT deleted_us FROM state_records WHERE id=?", (identifier,))
                .fetchone()[0]
                is not None
            )
            after = tx.watermark(world["tenant"], "" if global_scope else world["agent"])
            assert after is not None and (before is None or after.current_seq > before.current_seq)
        if global_scope:
            return
        service = StateService(store, store.clock, idempotency=IdempotencyManager(store))
        with pytest.raises(NotFoundError):
            service.put(
                world["access"],
                namespace,
                "current",
                agent_id=world["agent"],
                space_id=world["spaces"][0],
                value={"text": "implicit recreation"},
                source_authority="user",
                idempotency_key="implicit-recreate",
            )
        recreated = client.post("/v1/memory/states", headers=headers(csrf), json=body)
        assert recreated.status_code == 201, recreated.text
        assert recreated.json()["data"]["resource_id"] != identifier
        assert recreated.json()["data"]["revision"] == 1
        assert client.post("/v1/memory/states", headers=original_key, json=body).status_code == 404
        assert client.post("/v1/memory/states", headers=headers(csrf), json=body).status_code == 409
        with store.read() as tx:
            rows = tx.states.list_scope(
                tenant_id=world["tenant"],
                agent_id=world["agent"],
                namespace=namespace,
                space_id=world["spaces"][0],
                session_id=None,
                prefix="current",
                limit=1,
            )
            assert [r.record.id for r in rows] == [recreated.json()["data"]["resource_id"]]


@pytest.mark.parametrize("before_creation", [True, False])
@pytest.mark.parametrize("mode", ["soft", "erase"])
def test_state_deletion_replays_backup(
    world: dict[str, Any], tmp_path: Path, before_creation: bool, mode: str
) -> None:
    store = world["store"]
    backups = BackupService(store)
    backup = tmp_path / "before"
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        if before_creation:
            backups.create_backup(backup)
        identifier = create(client, csrf, world)
        if not before_creation:
            backups.create_backup(backup)
        if mode == "erase":
            reauth(client, csrf, world)
        saved = preview(client, csrf, [target(client, identifier, "states")], mode=mode)
        assert commit(client, csrf, saved).status_code == 200
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
        assert tx.is_tombstoned(world["tenant"], "state_record", identifier)
        if before_creation:
            with pytest.raises(NotFoundError):
                tx.states.get(identifier)
        else:
            record = tx.states.get(identifier)
            assert json.loads(
                tx.states.current_revision(record.current_revision_id).value_json
            ) == ({} if mode == "erase" else {"text": "private state value"})
            assert (
                tx.raw()
                .execute("SELECT deleted_us FROM state_records WHERE id=?", (identifier,))
                .fetchone()[0]
                is not None
            )
    assert forget.replay_deletion_ledger(restored, ledger) == 0
    from iris_memory_core.domain.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError):
        forget.replay_deletion_ledger(restored, (replace(ledger[0], selector_key="wrong"),))


def test_namespace_withdrawal_and_hold_invalidate_preview(world: dict[str, Any]) -> None:
    from iris_memory_core.application.retention import RetentionService

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = create(client, csrf, world)
        row = target(client, identifier, "states")
        saved = preview(client, csrf, [row])
        with store.write() as tx:
            tx.states.upsert_policy(
                replace(
                    DEFAULT_STATE_POLICY,
                    namespace="topic",
                    allowed_source_authorities=frozenset({"host"}),
                ),
                tenant_id=world["tenant"],
            )
        assert commit(client, csrf, saved).status_code == 409
        protected = preview(client, csrf, [row])
        assert protected["targets"][0]["status"] == "protected" and not protected["can_commit"]
        with store.write() as tx:
            tx.states.upsert_policy(
                replace(DEFAULT_STATE_POLICY, namespace="topic"), tenant_id=world["tenant"]
            )
        saved = preview(client, csrf, [row])
        RetentionService(
            store, store.clock, forget=ForgetService(store, store.clock)
        ).create_legal_hold(world["access"], space_id=world["spaces"][0], reason="hold")
        assert commit(client, csrf, saved).status_code == 409
        held = preview(client, csrf, [row])
        assert held["targets"][0]["status"] == "held" and not held["can_commit"]


def test_late_state_failure_rolls_back_flag_and_content(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from iris_memory_core.storage.console import ConsoleRepository

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = create(client, csrf, world)
        reauth(client, csrf, world)
        saved = preview(
            client,
            csrf,
            [target(client, world["ids"]["a"]), target(client, identifier, "states")],
            mode="erase",
        )
        key = headers(csrf)

        def fail(*a: Any, **kw: Any) -> None:
            raise RuntimeError("late injected failure")

        with monkeypatch.context() as patch:
            patch.setattr(ConsoleRepository, "consume_command_preview", fail)
            assert commit(client, csrf, saved, headers=key).status_code == 500
        with store.read() as tx:
            assert not tx.is_tombstoned(world["tenant"], "state_record", identifier)
            assert not tx.is_tombstoned(world["tenant"], "note", world["ids"]["a"])
            assert (
                tx.raw()
                .execute("SELECT deleted_us FROM state_records WHERE id=?", (identifier,))
                .fetchone()[0]
                is None
            )
            assert json.loads(tx.states.history(identifier)[0].value_json) == {
                "text": "private state value"
            }
        assert commit(client, csrf, saved, headers=key).status_code == 200


def test_fifty_state_targets_commit_atomically(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        ids = [create(client, csrf, world, key=f"key-{n}") for n in range(50)]
        saved = preview(client, csrf, [target(client, identifier, "states") for identifier in ids])
        result = commit(client, csrf, saved)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["target_count"] == 50
        with world["store"].read() as tx:
            assert all(
                tx.is_tombstoned(world["tenant"], "state_record", identifier) for identifier in ids
            )


def test_state_erasure_deadline_rolls_back_batch_and_allows_retry(
    world: dict[str, Any], monkeypatch: Any
) -> None:
    from types import SimpleNamespace

    from iris_memory_core.storage import cognitive

    store = world["store"]
    with store.write() as tx:
        tx.states.upsert_policy(
            replace(
                DEFAULT_STATE_POLICY,
                namespace="topic",
                retain_history=True,
                max_history_revisions=200,
            ),
            tenant_id=world["tenant"],
        )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        identifier = create(client, csrf, world)
        for revision in range(1, 101):
            result = client.patch(
                "/v1/memory/states/" + identifier,
                headers=headers(csrf),
                json={
                    "expected_revision": revision,
                    "fields": {"value": {"private": revision}},
                    "reason_code": "operator_request",
                },
            )
            assert result.status_code == 200, result.text
        reauth(client, csrf, world)
        saved = preview(
            client,
            csrf,
            [target(client, world["ids"]["a"]), target(client, identifier, "states")],
            mode="erase",
        )
        calls = 0

        def deadline() -> float:
            nonlocal calls
            calls += 1
            return 0.0 if calls == 1 else 1.0

        key = headers(csrf)
        with monkeypatch.context() as patch:
            patch.setattr(cognitive, "time", SimpleNamespace(monotonic=deadline))
            result = commit(client, csrf, saved, headers=key)
        assert calls > 1 and result.status_code == 503, result.text
        with store.read() as tx:
            assert not tx.is_tombstoned(world["tenant"], "note", world["ids"]["a"])
            assert not tx.is_tombstoned(world["tenant"], "state_record", identifier)
            assert len(tx.states.history(identifier, limit=200)) == 101
            assert all(
                json.loads(row.value_json) != {} for row in tx.states.history(identifier, limit=200)
            )
            assert (
                tx.raw()
                .execute("SELECT deleted_us FROM state_records WHERE id=?", (identifier,))
                .fetchone()[0]
                is None
            )
        assert commit(client, csrf, saved, headers=key).status_code == 200


def test_state_forget_only_grant_and_cached_receipt_recheck(world: dict[str, Any]) -> None:
    from tests.integration.console.test_console_reads import grant_for

    owner, owner_csrf = signed_in(world["app"], world["token"])
    ids = []
    with owner:
        for space in (world["spaces"][0], world["spaces"][1], None):
            body = create_body(world)
            body["scope"] = {"agent_id": world["agent"]}
            if space:
                body["scope"]["space_id"] = space
            result = owner.post("/v1/memory/states", headers=headers(owner_csrf), json=body)
            assert result.status_code == 201, result.text
            ids.append(result.json()["data"]["resource_id"])
    issued, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="State forgetter",
        description="",
        template="viewer",
        grant=grant_for(world, permissions=frozenset({"memory.read", "memory.forget"})),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        rows = [
            {"resource_type": "state_record", "id": identifier, "expected_revision": 1}
            for identifier in ids
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
