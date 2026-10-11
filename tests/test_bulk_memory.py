"""BD: admin-only, preview-bound bulk privacy operations."""
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, msg
from iris import admin_data, memory_ops
from iris.api import create_app
from iris.db import now
from test_lifecycle import row
from test_retrieval import put


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as client:
        login_admin(client)
        yield client


def preview(store, action="forget", **scope):
    return admin_data.bulk_memory_preview(store, action=action, **(scope or {"subject_id": "self"}))


def apply(store, snap, **kwargs):
    return memory_ops.apply_bulk_memories(store, snapshot_token=snap["snapshot_token"], action=snap["action"], **kwargs)


def audit(store):
    with store.read() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT details_json FROM admin_operations WHERE action LIKE 'memory_bulk_%' ORDER BY id")]


def test_preview_counts_truncation_pin_exclusion_and_no_memory_write(store):
    active = put(store, "隐私正文" * 100)
    forgotten = put(store, "遗忘", lifecycle="forgotten")
    deleted = put(store, "已删除", lifecycle="deleted")
    pinned = put(store, "置顶")
    memory_ops.manage_memory(store, pinned, 1, pinned=True)
    before = [row(store, mid) for mid in (active, forgotten, deleted, pinned)]
    snap = preview(store)
    assert snap["counts"] == {"active": 1, "forgotten": 1, "deleted": 1}
    assert snap["total"] == 3 and snap["applicable_count"] == 2
    assert snap["pinned_count"] == 0 and snap["excluded_pinned_count"] == 1
    assert snap["examples"][0]["truncated"] and len(snap["examples"][0]["content"]) <= 161
    included = preview(store, include_pinned=True, subject_id="self")
    assert included["total"] == 4 and included["pinned_count"] == 1
    assert [row(store, mid) for mid in (active, forgotten, deleted, pinned)] == before
    assert audit(store) == []


def test_subject_scope_is_speaker_or_about_and_entry_is_direct_source_only(store):
    source = msg(store, 1, "入口 A 的来源")
    other_source = msg(store, 1, "入口 B 的来源", entry="B")
    with store.read() as conn:
        person = conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (source,)).fetchone()[0]
    spoken = put(store, "本人说", about=(), speaker=person, evidence=[source, other_source])
    about = put(store, "关于本人", about=(person,), evidence=[source])
    owner_only = put(store, "仅所属入口", entry="A")
    derived = put(store, "间接来源")
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,created_at) VALUES(?,'memory',?,?)", (derived, spoken, now()))
    assert {r["id"] for r in preview(store, subject_id=person)["examples"]} == {spoken, about}
    snap = preview(store, "delete", entry_id="A")
    assert {r["id"] for r in snap["examples"]} == {spoken, about}
    result = apply(store, snap)
    assert result["memory_ids"] == [spoken, about]
    assert row(store, owner_only)["lifecycle"] == row(store, derived)["lifecycle"] == "active"
    with store.read() as conn:
        assert conn.execute("SELECT applied_at FROM memory_dependency_losses WHERE memory_id=?", (derived,)).fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


@pytest.mark.parametrize("change", ["revision", "pin", "retention", "lifecycle", "new_match", "about", "source", "threshold"])
def test_changes_after_preview_conflict_before_any_write(store, change):
    source = msg(store, 1, "来源")
    mid = put(store, "私密", evidence=[source])
    snap = preview(store, "delete")
    if change == "revision":
        memory_ops.edit_memory(store, mid, 1, content="修订")
    elif change == "pin":
        memory_ops.manage_memory(store, mid, 1, pinned=True)
    elif change == "retention":
        memory_ops.adjust_retention(store, mid, 1)
    elif change == "lifecycle":
        memory_ops.manage_memory(store, mid, 1, action="forget")
    elif change == "new_match":
        put(store, "新符合范围")
    elif change == "threshold":
        store.set_setting("lifecycle", {"forget_threshold": 15})
    else:
        with store.write() as conn:
            conn.execute("DELETE FROM memory_subjects WHERE memory_id=?" if change == "about" else "DELETE FROM sources WHERE memory_id=?", (mid,))
    before = row(store, mid)
    with pytest.raises(memory_ops.BulkMemoryConflict) as exc:
        apply(store, snap)
    assert exc.value.result["count"] == 0
    assert row(store, mid) == before


def test_action_binding_expiry_unknown_and_required_confirmation(store, monkeypatch):
    mid = put(store, "隐私")
    snap = preview(store, "purge")
    with pytest.raises(ValueError):
        memory_ops.apply_bulk_memories(store, snapshot_token=snap["snapshot_token"], action="delete")
    with pytest.raises(ValueError):
        apply(store, snap)
    monkeypatch.setattr(memory_ops, "BULK_PREVIEW_SECONDS", -1)
    expired = preview(store)
    with pytest.raises(memory_ops.BulkMemoryConflict):
        apply(store, expired)
    with pytest.raises(memory_ops.BulkMemoryConflict):
        memory_ops.apply_bulk_memories(store, snapshot_token="invalid", action="delete")
    assert row(store, mid)["lifecycle"] == "active"


@pytest.mark.parametrize("action", ["forget", "delete", "purge"])
def test_chunks_single_semantics_one_audit_and_safe_retry(store, monkeypatch, action):
    monkeypatch.setattr(memory_ops, "BULK_BATCH_SIZE", 1)
    ids = [put(store, "不得进入操作记录的隐私"), put(store, "另一个隐私")]
    memory_ops.manage_memory(store, ids[0], 1, pinned=True)
    snap = preview(store, action, subject_id="self", include_pinned=True)
    result = apply(store, snap, confirm=True)
    assert result["status"] == "completed" and result["count"] == 2
    assert result["memory_ids"] == ids
    assert all(row(store, mid)["lifecycle"] == ("forgotten" if action == "forget" else "deleted") for mid in ids)
    assert row(store, ids[0])["pinned"] == (1 if action == "delete" else 0)
    assert apply(store, snap, confirm=True) == result
    records = audit(store)
    assert len(records) == 1 and records[0]["memory_ids"] == ids
    assert records[0]["scope"] == {"subject_id": "self"}
    assert "隐私" not in json.dumps(records, ensure_ascii=False)
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='memory_forget'").fetchone()[0] == 0


def test_conflict_between_batches_reports_committed_ids_and_rolls_back_chunk(store, monkeypatch):
    monkeypatch.setattr(memory_ops, "BULK_BATCH_SIZE", 1)
    ids = [put(store, "第一条"), put(store, "第二条")]
    snap = preview(store, "delete")
    original = store.write
    fired = False

    @contextmanager
    def interleave():
        nonlocal fired
        with original() as conn:
            yield conn
        if not fired and row(store, ids[0])["lifecycle"] == "deleted":
            fired = True
            with original() as conn:
                conn.execute("UPDATE memories SET revision=revision+1 WHERE id=?", (ids[1],))
    monkeypatch.setattr(store, "write", interleave)
    with pytest.raises(memory_ops.BulkMemoryConflict) as exc:
        apply(store, snap)
    assert exc.value.result["memory_ids"] == [ids[0]]
    assert row(store, ids[1])["lifecycle"] == "active"
    assert audit(store)[0]["status"] == "conflict"
    with pytest.raises(memory_ops.BulkMemoryConflict):
        apply(store, snap)
    assert len(audit(store)) == 1


def test_purge_shared_sources_history_protected_refs_and_derived_review(store, monkeypatch):
    monkeypatch.setattr(memory_ops, "BULK_BATCH_SIZE", 1)
    shared = msg(store, 1, "仅这两条记忆共用的来源")
    protected = msg(store, 2, "其他记忆也在引用")
    one = put(store, "待清除一", evidence=[shared, protected])
    two = put(store, "待清除二", evidence=[shared])
    other = put(store, "保留", about=(), speaker="scene", evidence=[protected])
    child = put(store, "派生", about=(), speaker="scene")
    memory_ops.edit_memory(store, one, 1, content="待清除一修订")
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,created_at) VALUES(?,'memory',?,?)", (child, one, now()))
    result = apply(store, preview(store, "purge"), confirm=True)
    assert result["memory_ids"] == [one, two]
    assert result["deleted_message_ids"] == [shared]
    assert result["retained_messages"] == [{"message_id": protected, "reasons": ["memory_source"]}]
    with store.read() as conn:
        assert not conn.execute("SELECT 1 FROM memory_revisions WHERE memory_id=?", (one,)).fetchone()
        assert not conn.execute("SELECT 1 FROM messages WHERE id=?", (shared,)).fetchone()
        assert conn.execute("SELECT 1 FROM memory_dependency_losses WHERE memory_id=? AND applied_at IS NULL", (child,)).fetchone()
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert row(store, other)["lifecycle"] == row(store, child)["lifecycle"] == "active"
    assert put(store, "后来新建") > child


def test_http_validation_conflict_results_and_admin_security(client, store):
    mid = put(store, "私密")
    base = "/admin/api/memories/bulk/"
    for payload in ({"action": "delete"}, {"subject_id": "self", "entry_id": "A", "action": "delete"},
                    {"subject_id": "self", "action": "invalid"}, {"subject_id": "self", "action": "delete", "include_pinned": "false"}):
        assert client.post(base + "preview", json=payload).status_code == 400
    assert client.post(base + "preview", json={"subject_id": "missing", "action": "forget"}).status_code == 404
    snap_response = client.post(base + "preview", json={"subject_id": "self", "action": "forget"})
    assert snap_response.status_code == 200
    assert snap_response.headers["cache-control"] == "no-store"
    snap = snap_response.json()
    memory_ops.manage_memory(store, mid, 1, pinned=True)
    response = client.post(base + "apply", json={"snapshot_token": snap["snapshot_token"], "action": "forget"})
    assert response.status_code == 409 and response.json()["result"]["count"] == 0
    assert response.json()["error"]["code"] == "bulk_preview_stale"
    client.headers.pop("X-Iris-CSRF")
    assert client.post(base + "preview", json={"subject_id": "self", "action": "delete"}).status_code == 403
    client.cookies.clear()
    assert client.post(base + "apply", json={"snapshot_token": snap["snapshot_token"], "action": "forget"}).status_code == 401
    assert row(store, mid)["lifecycle"] == "active"


def test_failing_item_rolls_back_entire_chunk(store, monkeypatch):
    ids = [put(store, "同一批一"), put(store, "同一批二")]
    snap = preview(store, "delete")
    original = memory_ops.delete_memory
    def fail_second(store, mid, revision, **kwargs):
        return False if mid == ids[1] else original(store, mid, revision, **kwargs)
    monkeypatch.setattr(memory_ops, "delete_memory", fail_second)
    with pytest.raises(memory_ops.BulkMemoryConflict) as exc:
        apply(store, snap)
    assert exc.value.result["count"] == 0
    assert all(row(store, mid)["lifecycle"] == "active" for mid in ids)
    assert audit(store) == []


def test_new_match_during_execution_and_concurrent_apply_are_detected(store, monkeypatch):
    monkeypatch.setattr(memory_ops, "BULK_BATCH_SIZE", 1)
    ids = [put(store, "第一批"), put(store, "最后一批")]
    snap = preview(store, "forget")
    original = memory_ops.manage_memory
    def interleave(store, mid, revision, **kwargs):
        with pytest.raises(memory_ops.BulkMemoryConflict):
            apply(store, snap)
        ok = original(store, mid, revision, **kwargs)
        if mid == ids[1]:
            # Represents a match arriving between chunks; insert via this
            # transaction so the final membership check sees it deterministically.
            conn = kwargs["_conn"]
            conn.execute("INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,created_at,updated_at,first_confirmed_at,last_confirmed_at) VALUES('新进入范围','事实','self','亲历',70,50,50,?,?,?,?)", (now(), now(), now(), now()))
        return ok
    monkeypatch.setattr(memory_ops, "manage_memory", interleave)
    with pytest.raises(memory_ops.BulkMemoryConflict) as exc:
        apply(store, snap)
    assert exc.value.result["memory_ids"] == [ids[0]]
    assert row(store, ids[1])["lifecycle"] == "active"


def test_http_success_retry_purge_confirmation_and_json_origin_guards(client, store):
    mid = put(store, "经 HTTP 清除")
    base = "/admin/api/memories/bulk/"
    request = {"subject_id": "self", "action": "purge"}
    assert client.post(base + "preview", content=json.dumps(request), headers={"Content-Type": "text/plain"}).status_code == 403
    assert client.post(base + "preview", json=request, headers={"Origin": "https://other.example"}).status_code == 403
    snap = client.post(base + "preview", json=request).json()
    payload = {"snapshot_token": snap["snapshot_token"], "action": "purge"}
    assert client.post(base + "apply", json=payload).status_code == 400
    payload["confirm"] = True
    done = client.post(base + "apply", json=payload)
    assert done.status_code == 200 and done.json()["memory_ids"] == [mid]
    assert client.post(base + "apply", json=payload).json() == done.json()
    assert len(audit(store)) == 1


def test_skipped_deleted_and_empty_scope_are_explicit(store):
    deleted = put(store, "已经撤销", lifecycle="deleted")
    result = apply(store, preview(store, "delete"))
    assert result["status"] == "completed" and result["count"] == 0 and result["skipped_count"] == 1
    assert row(store, deleted)["revision"] == 1
    result = apply(store, preview(store, "purge"), confirm=True)
    assert result["count"] == 1
    assert preview(store)["total"] == 0
