"""Management UI contracts; no real model calls or schema changes."""
from conftest import authorize_host
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from conftest import login_admin

from conftest import FakeGateway, batch, msg
from iris.api import create_app
from iris.db import now
from iris.memory_ops import setup_role
from iris.models import ModelError
from iris.retrieval import Retrieval
from test_retrieval import put
from test_batches import memory


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as client:
        authorize_host(client)
        login_admin(client)
        yield client


def trial(client, name="试用群聊 A"):
    response = client.post("/admin/api/trial/entries", json={"name": name, "kind": "group"})
    assert response.status_code == 201
    return response.json()


def send(client, entry, content="我下周三去上海出差", **extra):
    return client.post(f"/admin/api/trial/entries/{entry['id']}/messages", json={
        "speaker_id": entry["default_speaker_id"], "content": content,
        "dedupe_key": "test-1", **extra})


def test_trial_entries_speakers_receipts_and_isolation(client, store):
    a, b = trial(client), trial(client, "试用私聊 B")
    speaker = client.post("/admin/api/trial/speakers", json={"name": "小林"}).json()
    first = send(client, a).json()
    assert send(client, a).json()["message_id"] == first["message_id"]
    assert first["pending_count"] == 1 and first["learning_state"] == "pending"
    assert send(client, b, "只在 B 的原始正文", speaker_id=speaker["id"]).status_code == 201
    view = client.get(f"/admin/api/trial/entries/{a['id']}").json()
    assert len(view["messages"]) == 1 and view["messages"][0]["kind"] == "message"
    assert view["messages"][0]["sender_subject_id"] != "self"
    assert "只在 B 的原始正文" not in json.dumps(view, ensure_ascii=False)
    assert view["entry"]["pace"] == "realtime"
    assert client.post(f"/admin/api/trial/entries/{a['id']}/learn").json()["accepted"]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    prepared = client.post(f"/admin/api/trial/entries/{a['id']}/prepare").json()
    assert prepared["recent_messages"][0]["unlearned"]
    assert prepared["memories"] == []


@pytest.mark.parametrize("body", [{"name": " "}, {"name": "A", "kind": "live"}, {"name": "A", "extra": 1}])
def test_trial_entry_validation(client, body):
    assert client.post("/admin/api/trial/entries", json=body).status_code == 400


def test_message_validation_and_host_entry_cannot_be_used(client, store):
    a = trial(client)
    assert send(client, a, " ").status_code == 400
    assert send(client, a, "中" * 10923).status_code == 413
    assert send(client, a, speaker_id="self").status_code == 404
    assert send(client, a, kind="self_output").status_code == 400
    msg(store, 1, "host 原文", entry="host-entry")
    assert client.get("/admin/api/trial/entries/host-entry").status_code == 404
    assert client.post("/admin/api/trial/entries/host-entry/reply", json={"message_id": 1}).status_code == 404


def test_trial_reply_uses_prepare_and_persona_publishes_once_and_learns_self_output(store):
    setup_role(store, "Iris", "表达温和")
    gateway = FakeGateway({"reply": "好，祝你上海之行顺利。"}, hook=lambda _: assert_outside_transaction(store))
    with TestClient(create_app(store=store, gateway=gateway), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as c:
        authorize_host(c)
        login_admin(c)
        c.app.state.scheduler.stop()  # Explicit deterministic learning below.
        a = trial(c)
        trigger = send(c, a).json()["message_id"]
        first = c.post(f"/admin/api/trial/entries/{a['id']}/reply", json={"message_id": trigger})
        assert first.status_code == 200
        payload = first.json()
        assert payload["message"]["kind"] == "self_output"
        assert payload["message"]["sender_subject_id"] == "self"
        assert payload["message"]["content"] == "好，祝你上海之行顺利。"
        material = json.loads(gateway.materials[0])
        assert material["prepared"] == payload["prepared"]
        assert material["prepared"]["persona"]["content"].startswith("我是Iris")
        assert gateway.requests[0]["purpose"] == "trial_reply"
        assert c.post(f"/admin/api/trial/entries/{a['id']}/reply", json={"message_id": trigger}).json()["message"]["id"] == payload["message"]["id"]
        assert len(gateway.requests) == 1
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM recall_items WHERE used_at IS NOT NULL").fetchone()[0] == 0
        learner = FakeGateway({"memories": [memory("我祝愿用户上海之行顺利", [payload["message"]["id"]], "我", ["我"], "观点")]})
        formed, result = batch(store, learner, entry=a["id"], count=2)
        assert "[我实际发言]" in learner.materials[0]
        assert "好，祝你上海之行顺利。" in learner.materials[0]
        assert len(result["created"]) == 1
        with store.read() as conn:
            learned = conn.execute("SELECT speaker_subject_id FROM memories WHERE id=?", (result["created"][0],)).fetchone()
            assert learned[0] == "self"
            assert conn.execute("SELECT message_id FROM sources WHERE memory_id=?", (result["created"][0],)).fetchone()[0] == payload["message"]["id"]
        view = c.get(f"/admin/api/trial/entries/{a['id']}").json()
        assert all(m["learning_state"] == "learned" for m in view["messages"])


def assert_outside_transaction(store):
    assert not store._writer.in_transaction


@pytest.mark.parametrize("reply", [{}, {"reply": " "}, {"reply": 5}, {"reply": "中" * 10923}, ModelError("retryable", "do not expose this provider response")])
def test_failed_reply_never_publishes_model_material_or_loses_input(store, reply):
    gateway = FakeGateway(reply)
    with TestClient(create_app(store=store, gateway=gateway), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as c:
        authorize_host(c)
        login_admin(c)
        c.app.state.scheduler.stop()
        a = trial(c)
        trigger = send(c, a).json()["message_id"]
        response = c.post(f"/admin/api/trial/entries/{a['id']}/reply", json={"message_id": trigger})
        assert response.status_code == 502
        assert "provider response" not in response.text
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_parallel_reply_is_rejected_and_retry_is_idempotent(store):
    entered, release = Event(), Event()
    def block(_):
        entered.set()
        assert release.wait(5)
    gateway = FakeGateway({"reply": "好的"}, hook=block)
    with TestClient(create_app(store=store, gateway=gateway), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as c, ThreadPoolExecutor() as pool:
        authorize_host(c)
        login_admin(c)
        c.app.state.scheduler.stop()
        a = trial(c)
        trigger = send(c, a).json()["message_id"]
        url = f"/admin/api/trial/entries/{a['id']}/reply"
        first = pool.submit(c.post, url, json={"message_id": trigger})
        try:
            assert entered.wait(5)
            assert c.post(url, json={"message_id": trigger}).status_code == 409
        finally:
            release.set()
        assert first.result().status_code == 200


def test_memory_list_filters_pagination_and_read_does_not_record_recall(client, store):
    source = msg(store, 1, "我下周去上海", entry="A")
    a = put(store, "上海出差计划", kind="计划", entry="A", evidence=[source], event_time="2026-10-14")
    b = put(store, "北京旅行计划", kind="计划", event_time="2026-11-14")
    put(store, "喜欢热茶", kind="偏好")
    listed = client.get("/admin/api/memories", params={"text": "上海", "kind": "计划", "entry_id": "A", "person_id": "self", "time_from": "2026-10-01", "time_to": "2026-10-31", "sort": "relevance"}).json()
    assert [m["id"] for m in listed["items"]] == [a]
    assert listed["total"] == 1
    assert client.get("/admin/api/memories", params={"limit": 1, "offset": 1}).json()["total"] == 3
    with store.write() as conn:
        conn.execute("UPDATE memories SET retention=99 WHERE id=?", (b,))
    assert client.get("/admin/api/memories?sort=retention").json()["items"][0]["id"] == b
    assert client.get("/admin/api/memories?sort=garbage").status_code == 400
    assert client.get("/admin/api/memories?time_from=2026-11-01&time_to=2026-10-01").status_code == 400
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] == 0


def test_memory_detail_context_derivation_revision_operations_and_usage(client, store):
    before = msg(store, 1, "前文")
    source = msg(store, 2, "上海出差")
    after = msg(store, 3, "后文")
    other = msg(store, 1, "别的入口", entry="B")
    parent = put(store, "上海出差", evidence=[source])
    child = put(store, "需要准备差旅行李")
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)", (child, parent, now()))
    recall = Retrieval(store).search(text="上海")
    Retrieval(store).feedback(recall["recall_id"], [parent])
    detail = client.get(f"/admin/api/memories/{parent}").json()
    assert [m["id"] for m in detail["sources"][0]["context"]] == [before, source, after]
    assert detail["derived_memories"][0]["id"] == child
    assert detail["recall_count"] == detail["used_count"] == 1
    assert detail["about"][0]["id"] == "self"
    assert "embedding" not in detail
    assert client.patch(f"/admin/api/memories/{parent}", json={"expected_revision": 1, "content": "上海出差改期"}).status_code == 200
    stale = client.patch(f"/admin/api/memories/{parent}", json={"expected_revision": 1, "content": "覆盖新内容"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "revision_conflict"
    detail = client.get(f"/admin/api/memories/{parent}").json()
    assert detail["revision"] == 2 and detail["revisions"][0]["before"]["content"] == "上海出差"
    assert detail["operations"][0]["action"] == "edit" and detail["operations"][0]["actor"] == "admin"
    assert client.get(f"/admin/api/memories/{child}").json()["sources"][0]["needs_review"]


def test_delete_tombstone_shared_sources_survive_and_id_never_revives(client, store):
    source = msg(store, 1, "上海与广州出差")
    a = put(store, "上海出差", evidence=[source], vector=[1., 0.])
    b = put(store, "广州出差", evidence=[source])
    index = store.vector_index("fake-vector")
    assert client.request("DELETE", f"/admin/api/memories/{a}", json={"expected_revision": 2}).status_code == 409
    response = client.request("DELETE", f"/admin/api/memories/{a}", json={"expected_revision": 1})
    assert response.status_code == 200
    assert Retrieval(store).search(text="上海", include_forgotten=True)["memories"] == []
    assert client.patch(f"/admin/api/memories/{a}", json={"expected_revision": 2, "content": "恢复"}).status_code == 404
    old = client.get(f"/admin/api/memories/{a}").json()
    assert old["lifecycle"] == "deleted" and old["operations"][0]["action"] == "delete"
    assert old["sources"][0]["message"]["id"] == source
    assert client.get(f"/admin/api/memories/{b}").json()["lifecycle"] == "active"
    assert put(store, "上海出差") != a
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_fts_jieba WHERE rowid=?", (a,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE message_id=?", (source,)).fetchone()[0] == 2


def test_admin_status_reuses_service_status_without_credentials(client, store):
    with store.write() as conn:
        conn.execute("INSERT INTO model_calls(purpose,model,duration_ms,result_category,error_summary,timed_out,created_at) VALUES('learning','fake',180000,'retryable','timeout',1,?)", (now(),))
    status = client.get("/admin/api/status").json()
    host = client.get("/api/v1/status").json()
    for key in ("model_health", "entries", "usage", "learning_latency_24h", "timeouts_seconds", "models"):
        assert status[key] == host[key]
    assert status["learning_latency_24h"]["timeouts"] == 1
    assert "api_key" not in json.dumps(status)


def test_web_bundle_is_served_and_cannot_shadow_apis(client):
    page = client.get("/")
    assert page.status_code == 200 and 'lang="zh-CN"' in page.text
    assert "<script" in page.text
    assert client.get("/admin/api/unknown").status_code == 404
    assert client.get("/api/v1/unknown").status_code == 404


def test_trial_snapshot_includes_memories_updated_from_another_entry(client, store):
    a, b = trial(client), trial(client, "试用私聊 B")
    source = send(client, a).json()["message_id"]
    mid = put(store, "跨入口更新的出差计划", entry=b["id"], evidence=[source])
    view = client.get(f"/admin/api/trial/entries/{a['id']}").json()
    assert [m["id"] for m in view["recent_memories"]] == [mid]


def test_reply_to_message_outside_prepared_window_is_rejected_without_generation(store):
    gateway = FakeGateway({"reply": "不该生成"})
    with TestClient(create_app(store=store, gateway=gateway), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as c:
        authorize_host(c)
        login_admin(c)
        c.app.state.scheduler.stop()
        a = trial(c)
        first = send(c, a).json()["message_id"]
        for i in range(21):
            send(c, a, f"之后的消息 {i}", dedupe_key=f"later-{i}")
        response = c.post(f"/admin/api/trial/entries/{a['id']}/reply", json={"message_id": first})
        assert response.status_code == 400
        assert gateway.requests == []


def test_admin_strict_revision_fields_utf8_and_reserved_reply_key(client, store):
    mid = put(store, "中文记忆")
    for revision in [True, "1", 0]:
        assert client.patch(f"/admin/api/memories/{mid}", json={"expected_revision": revision, "content": "新的内容"}).status_code == 400
    assert client.patch(f"/admin/api/memories/{mid}", json={"expected_revision": 1, "content": " ", "lifecycle": "deleted"}).status_code == 400
    assert "中文记忆" in client.get(f"/admin/api/memories/{mid}").text
    a = trial(client)
    assert send(client, a, dedupe_key="trial-reply:1").status_code == 400


def test_reply_dedupe_survives_service_restart(store):
    first_gateway = FakeGateway({"reply": "已经发出的话"})
    with TestClient(create_app(store=store, gateway=first_gateway), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as c:
        authorize_host(c)
        login_admin(c)
        c.app.state.scheduler.stop()
        a = trial(c)
        mid = send(c, a).json()["message_id"]
        first = c.post(f"/admin/api/trial/entries/{a['id']}/reply", json={"message_id": mid}).json()
    next_gateway = FakeGateway(RuntimeError("must not call model again"))
    with TestClient(create_app(store=store, gateway=next_gateway), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as c:
        authorize_host(c)
        login_admin(c)
        c.app.state.scheduler.stop()
        second = c.post(f"/admin/api/trial/entries/{a['id']}/reply", json={"message_id": mid}).json()
        assert second["reused"] and second["message"]["id"] == first["message"]["id"]
        assert next_gateway.requests == []
