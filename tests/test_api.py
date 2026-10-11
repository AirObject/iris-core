from conftest import authorize_host
import json

import pytest
from fastapi.testclient import TestClient
from conftest import login_admin

from iris.api import create_app, loopback_host
from iris.cli import main
from test_retrieval import put


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as c:
        authorize_host(c)
        login_admin(c)
        yield c


def message(**kw):
    return {"sender": "大鹏", "content": "我喜欢天文摄影", "occurred_at": "2026-09-29T10:00:00+08:00",
            "dedupe_key": "first", "platform": "chat", **kw}


def test_receive_single_batch_dedupe_and_persist_on_reopen(client, store):
    first = client.post("/api/v1/entries/A/messages", json=message()).json()
    second = client.post("/api/v1/entries/A/messages", json=[message(content="不能覆盖"), message(dedupe_key="second")]).json()
    assert first["message_ids"][0] == second["message_ids"][0]
    assert first["pending_count"] == 1 and second["pending_count"] == 2
    reply = client.post("/api/v1/entries/A/prepare", json={}).json()
    assert reply["memories"] == []
    assert reply["recent_messages"][0]["content"] == "我喜欢天文摄影"
    from iris.db import Store
    reopened = Store(store.path)
    try:
        with reopened.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    finally:
        reopened.close()


def test_400_413_batch_validation_is_atomic(client, store):
    assert client.post("/api/v1/entries/A/messages", json=[message(), message(content="中" * 10923)]).status_code == 413
    assert client.post("/api/v1/entries/A/messages", json=message(occurred_at="yesterday")).status_code == 400
    assert client.post("/api/v1/entries/A/messages", content="bad json", headers={"Content-Type": "application/json"}).status_code == 400
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert client.post("/api/v1/entries/A/messages", json=message(content="a" * 32768)).status_code == 200


def test_prepare_search_feedback_status_and_openapi(client, store):
    client.post("/api/v1/entries/A/messages", json=message())
    mid = put(store, "我喜欢天文摄影")
    reply = client.post("/api/v1/entries/A/prepare", json={"text": "天文摄影", "participants": []}).json()
    assert reply["memories"][0]["id"] == mid
    body = {"recall_id": reply["recall_id"], "memory_ids": [mid]}
    assert client.post("/api/v1/feedback", json=body).json()["strengthened"] == [mid]
    assert client.post("/api/v1/feedback", json=body).json()["strengthened"] == []
    assert client.post("/api/v1/memories/search", json={"text": "天文摄影", "include_forgotten": True}).status_code == 200
    status = client.get("/api/v1/status").json()
    assert status["service"] == "ready" and status["backlog"][0]["pending_count"] == 1
    assert client.post("/api/v1/entries/missing/prepare", json={}).status_code == 404
    assert client.post("/api/v1/feedback", json={"recall_id": "missing", "memory_ids": []}).status_code == 404
    assert client.post("/api/v1/memories/search", json={"time_from": "not-time"}).status_code == 400
    assert client.post("/api/v1/entries/A/prepare", json={"memory_limit": 9}).status_code == 400
    assert "/api/v1/feedback" in client.get("/openapi.json").json()["paths"]
    client.app.state.ready = False
    result = client.get("/api/v1/status")
    assert result.status_code == 503 and result.headers["Retry-After"] == "1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.2", "example.com"])
def test_serve_rejects_non_loopback_before_creating_database(tmp_path, host):
    db = tmp_path / "blocked.db"
    assert main(["--db", str(db), "serve", "--host", host]) == 1
    assert not db.exists()


def test_local_host_and_default_serve(monkeypatch, tmp_path):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    assert loopback_host("localhost") == "127.0.0.1"
    assert loopback_host("::1") == "::1"
    assert main(["--db", str(tmp_path / "iris.db"), "serve"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8080
    assert seen["workers"] == 1


def test_prepare_recent_window_open_goals_and_entry_gap_hints(client, store):
    from conftest import FakeGateway, batch, msg
    from iris.db import now
    from iris.models import ModelError
    for i in range(1, 26):
        msg(store, i, f"本入口消息 {i}")
    msg(store, 1, "另一个入口的消息", entry="B")
    batch(store, FakeGateway(ModelError("content_rejection", "provider refusal")), count=25)
    batch(store, FakeGateway(ModelError("content_rejection", "other entry refusal")), entry="B", count=1)
    with store.write() as conn:
        for i in range(12):
            conn.execute("INSERT INTO goals(content,kind,state,created_at,entry_id) VALUES(?,'normal','open',?,'A')",
                         (f"未结束目标 {i}", now()))
        conn.execute("INSERT INTO goals(content,kind,state,created_at,entry_id) VALUES('已完成','normal','completed',?,'A')", (now(),))
        for result in ("retryable", "success"):
            conn.execute("INSERT INTO model_calls(purpose,model,duration_ms,result_category,created_at) VALUES('learning','fake',1,?,?)", (result, now()))
    reply = client.post("/api/v1/entries/A/prepare", json={"text": "", "participants": []}).json()
    assert len(reply["recent_messages"]) == 20 and reply["recent_messages"][0]["content"] == "本入口消息 6"
    assert all(m["unlearned"] for m in reply["recent_messages"])
    assert len(reply["goals"]) == 10 and all(g["state"] == "open" for g in reply["goals"])
    gap = next(h for h in reply["hints"] if h["code"] == "memory_gaps")
    assert len(gap["gaps"]) == 1
    assert not any(h["code"] == "model_service" for h in reply["hints"])
    status = client.get("/api/v1/status").json()
    assert status["models"][0]["result_category"] == "success"
