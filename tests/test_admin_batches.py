"""Entry/batch browsing and design 7.6/17.7 relearning; no real models."""
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeGateway, login_admin, msg
from iris.api import create_app
from iris.db import dumps
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.models import Gateway, ModelConfig, ModelError
from iris.queue import form_batch, get_batch, reset_batch
from iris.retrieval import Retrieval
from test_batches import memory


def terminal_batch(store, state, *, entry="A"):
    msg(store, 1, "我喜欢猫", entry=entry)
    formed = form_batch(store, entry, PROMPT_VERSION)
    category = "content_rejection" if state == "refused" else "retryable"
    engine = LearningEngine(store, FakeGateway(ModelError(category, "deterministic failure")))
    for _ in range(1 if state == "refused" else 4):
        engine.run_batch(formed.id, force=True)
    assert get_batch(store, formed.id).state == state
    return formed


def gap(store):
    with store.read() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM memory_gaps")]
    assert len(rows) == 1
    return rows[0]


def hints(store):
    return [h for h in Retrieval(store).prepare("A")["hints"] if h["code"] == "memory_gaps"]


@pytest.mark.parametrize("state", ["abandoned", "refused"])
def test_relearning_keeps_gap_and_hints_until_success(store, state):
    formed = terminal_batch(store, state)
    with store.read() as conn:
        previous = [tuple(r) for r in conn.execute("SELECT * FROM batch_attempts ORDER BY number")]
    original_gap = gap(store)
    reset_batch(store, formed.id)
    assert gap(store) == original_gap  # Regression: reset used to delete this immediately.
    assert len(hints(store)[0]["gaps"]) == 1
    reset = get_batch(store, formed.id)
    assert reset.state == "waiting" and reset.attempt_count == 0
    assert (reset.history_ids, reset.target_ids, reset.future_ids) == (formed.history_ids, formed.target_ids, formed.future_ids)
    with pytest.raises(ValueError):
        reset_batch(store, formed.id)
    with store.read() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM batch_attempts ORDER BY number")] == previous
    # A different terminal result replaces the existing unique batch gap.
    with store.write() as conn:
        conn.execute("UPDATE memory_gaps SET reason='old',created_at='2000-01-01T00:00:00Z'")
    failing = LearningEngine(store, FakeGateway(ModelError("retryable", "new failure")))
    for i in range(4):
        result = failing.run_batch(formed.id, force=True)
        assert result["state"] == ("abandoned" if i == 3 else "waiting")
        assert len(hints(store)[0]["gaps"]) == 1
    refreshed = gap(store)
    assert refreshed["reason"] == "attempts_exhausted"
    assert refreshed["created_at"] != "2000-01-01T00:00:00Z"
    assert (refreshed["started_at"], refreshed["ended_at"]) == (original_gap["started_at"], original_gap["ended_at"])
    reset_batch(store, formed.id)
    assert gap(store)
    LearningEngine(store, FakeGateway({})).run_batch(formed.id, force=True)
    assert not hints(store)
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_gaps").fetchone()[0] == 0
        assert [r[0] for r in conn.execute("SELECT number FROM batch_attempts ORDER BY number")] == list(range(1, len(previous) + 6))
        assert conn.execute("SELECT learning_state FROM messages").fetchone()[0] == "learned"


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as value:
        login_admin(value)
        value.app.state.scheduler.stop()
        yield value


def test_entry_list_includes_trial_host_pace_counts_and_batches(client, store):
    trial = client.post("/admin/api/trial/entries", json={"name": "试用群聊", "kind": "group"}).json()
    msg(store, 1, "host message")
    formed = form_batch(store, "A", PROMPT_VERSION)
    msg(store, 2, "still pending")
    result = client.get("/admin/api/entries").json()
    items = {row["id"]: row for row in result["items"]}
    assert items[trial["id"]]["platform"] == "iris-trial"
    assert items[trial["id"]]["pace"] == "realtime"
    host = items["A"]
    assert host["name"] == "A" and host["kind"] == "group" and host["platform"] == "test"
    assert host["pending_count"] == 2
    assert host["current_batch"]["id"] == host["latest_batch"]["id"] == formed.id
    assert host["latest_batch"]["state"] == "waiting"
    assert client.get("/admin/api/batches?entry_id=A&limit=1").json()["items"][0]["id"] == formed.id


def test_batch_detail_three_segments_attempts_body_only_results_and_readonly(client, store):
    msg(store, 1, "历史消息")
    history = form_batch(store, "A", PROMPT_VERSION)
    LearningEngine(store, FakeGateway({})).run_batch(history.id)
    target = msg(store, 2, "我下周去上海")
    future = msg(store, 3, "祝你顺利", kind="self_output")
    formed = form_batch(store, "A", PROMPT_VERSION, target_count=1)
    output = {"memories": [{**memory("小林计划去上海", [2]), "stance": "计划"}, memory("只在后续段的信息", [3])]}
    body = dumps(output)
    responses = iter([("{broken", "length"), (body, "stop")])
    def handler(request):
        content, finish = next(responses)
        return httpx.Response(200, json={"choices": [{"message": {"content": content,
            "reasoning_content": "NEVER_SHOW_REASONING", "reasoning": "SECRET_THOUGHT"}, "finish_reason": finish}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        gateway = Gateway({"chat": ModelConfig("http://fake/v1", "", "fake-chat", reasoning_effort="low")}, store, client=transport)
        try:
            result = LearningEngine(store, gateway).run_batch(formed.id)
        finally:
            gateway.close()
    assert result["created"] and result["dropped"] and result["normalizations"]
    with store.read() as conn:
        before = list(conn.iterdump())
    response = client.get(f"/admin/api/batches/{formed.id}")
    assert response.status_code == 200
    data = response.json()
    assert [m["id"] for m in data["segments"]["history"]] == history.target_ids
    assert [m["id"] for m in data["segments"]["target"]] == [target]
    assert [m["id"] for m in data["segments"]["future"]] == [future]
    assert data["segments"]["future"][0]["kind"] == "self_output"
    assert data["segments"]["target"][0]["sender_name"] == "小林"
    attempt = data["attempts"][0]
    assert attempt["raw_output"] == "{broken" and attempt["repair_output"] == body
    assert attempt["parse_status"] == "repaired" and attempt["duration_ms"] >= 0
    assert [call["finish_reason"] for call in attempt["calls"]] == ["length", "stop"]
    assert {call["reasoning_effort"] for call in attempt["calls"]} == {"low"}
    for forbidden in ("NEVER_SHOW_REASONING", "SECRET_THOUGHT", "reasoning_content", "reasoning_chars", "reasoning_present"):
        assert forbidden not in response.text
    assert data["result"] == result
    assert data["memories"][0]["id"] == result["created"][0]
    assert data["memories"][0]["change"] == "created"
    assert data["can_relearn"] is False
    client.get("/admin/api/entries")
    client.get("/admin/api/batches")
    client.get("/admin/api/memory-gaps")
    with store.read() as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("state", ["abandoned", "refused"])
def test_relearn_api_conflict_audit_and_gap_lifecycle(client, store, state):
    formed = terminal_batch(store, state)
    url = f"/admin/api/batches/{formed.id}/relearn"
    original = client.get("/admin/api/memory-gaps?entry_id=A").json()
    assert original["total"] == 1 and not original["items"][0]["relearning"]
    response = client.post(url, json={})
    assert response.status_code == 200 and response.json()["state"] == "waiting"
    assert client.post(url, json={}).status_code == 409
    detail = client.get(f"/admin/api/batches/{formed.id}").json()
    assert detail["can_relearn"] is False and detail["relearning"] is True
    with store.read() as conn:
        operations = [dict(r) for r in conn.execute("SELECT * FROM admin_operations WHERE action='batch_relearn'")]
    assert len(operations) == 1 and operations[0]["actor"] == "admin"
    assert json.loads(operations[0]["details_json"]) == {"batch_id": formed.id, "entry_id": "A", "previous_state": state}
    for batch_state in ("waiting", "running"):
        with store.write() as conn:
            conn.execute("UPDATE batches SET state=? WHERE id=?", (batch_state, formed.id))
        item = client.get("/admin/api/memory-gaps").json()["items"][0]
        assert item["relearning"] is True and item["batch_state"] == batch_state
    with store.write() as conn:
        conn.execute("UPDATE batches SET state='waiting' WHERE id=?", (formed.id,))
    LearningEngine(store, FakeGateway({})).run_batch(formed.id)
    assert client.get("/admin/api/memory-gaps").json()["total"] == 0
    assert client.post(url, json={}).status_code == 409


def test_relearn_concurrent_clicks_queue_once(client, store):
    formed = terminal_batch(store, "abandoned")
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: client.post(f"/admin/api/batches/{formed.id}/relearn", json={}).status_code, range(2)))
    assert sorted(statuses) == [200, 409]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='batch_relearn'").fetchone()[0] == 1


def test_gap_filters_pagination_and_validation(client, store):
    a = terminal_batch(store, "abandoned")
    b = terminal_batch(store, "refused", entry="B")
    with store.write() as conn:
        conn.execute("UPDATE memory_gaps SET started_at='2026-10-01T23:00:00+08:00',ended_at='2026-10-02T01:00:00+08:00' WHERE batch_id=?", (a.id,))
        conn.execute("UPDATE memory_gaps SET started_at='2026-10-03T00:00:00+08:00',ended_at='2026-10-03T01:00:00+08:00' WHERE batch_id=?", (b.id,))
    result = client.get("/admin/api/memory-gaps?limit=1").json()
    assert result["total"] == 2 and len(result["items"]) == 1
    assert result["items"][0]["batch_id"] == b.id
    assert client.get("/admin/api/memory-gaps?entry_id=A").json()["items"][0]["batch_id"] == a.id
    assert client.get("/admin/api/memory-gaps?time_from=2026-10-02&time_to=2026-10-02").json()["total"] == 1
    for query in ("limit=101", "offset=-1", "time_from=bad", "time_from=2026-10-03&time_to=2026-10-01", "unknown=x"):
        assert client.get("/admin/api/memory-gaps?" + query).status_code == 400
    assert client.get("/admin/api/batches/99999").status_code == 404
    assert client.post("/admin/api/batches/99999/relearn", json={}).status_code == 404
    assert client.post(f"/admin/api/batches/{a.id}/relearn", json={"extra": True}).status_code == 400


def test_batch_routes_require_login_and_csrf(client, store):
    formed = terminal_batch(store, "refused")
    url = f"/admin/api/batches/{formed.id}/relearn"
    token = client.headers.pop("X-Iris-CSRF")
    assert client.post(url, json={}).status_code == 403
    client.headers["X-Iris-CSRF"] = token
    client.post("/admin/api/logout", json={})
    for path in ("/entries", "/batches", f"/batches/{formed.id}", "/memory-gaps"):
        assert client.get("/admin/api" + path).status_code == 401
    assert client.post(url, json={}).status_code == 401
    assert get_batch(store, formed.id).state == "refused"


@pytest.mark.parametrize("state", ["abandoned", "refused"])
def test_relearning_crash_on_fourth_attempt_keeps_one_gap(store, state):
    formed = terminal_batch(store, state)
    reset_batch(store, formed.id)
    engine = LearningEngine(store, FakeGateway(ModelError("retryable", "format failed")))
    for _ in range(3):
        engine.run_batch(formed.id, force=True)
    with store.write() as conn:
        conn.execute("UPDATE batches SET state='running' WHERE id=?", (formed.id,))
    store.recover_inflight()
    assert get_batch(store, formed.id).state == "abandoned"
    assert gap(store)["reason"] == "attempts_exhausted"


def test_reset_keeps_all_frozen_segments_and_later_pending_messages(client, store):
    msg(store, 1, "历史")
    previous = form_batch(store, "A", PROMPT_VERSION)
    LearningEngine(store, FakeGateway({})).run_batch(previous.id)
    msg(store, 2, "目标")
    msg(store, 3, "后续")
    formed = form_batch(store, "A", PROMPT_VERSION, target_count=1)
    LearningEngine(store, FakeGateway(ModelError("content_rejection", "refused"))).run_batch(formed.id)
    msg(store, 4, "冻结后新消息")
    response = client.post(f"/admin/api/batches/{formed.id}/relearn", json={})
    assert response.status_code == 200
    reset = get_batch(store, formed.id)
    assert reset.history_ids == [1] and reset.target_ids == [2] and reset.future_ids == [3]
    with store.read() as conn:
        assert [r[0] for r in conn.execute("SELECT learning_state FROM messages ORDER BY id")] == ["learned", "batched", "pending", "pending"]


def test_relearn_and_audit_are_atomic(client, store, monkeypatch):
    formed = terminal_batch(store, "refused")
    def fail(*args, **kwargs):
        raise RuntimeError("audit storage failure")
    monkeypatch.setattr("iris.admin.audit", fail)
    with pytest.raises(RuntimeError, match="audit storage failure"):
        client.post(f"/admin/api/batches/{formed.id}/relearn", json={})
    assert get_batch(store, formed.id).state == "refused"
    assert gap(store)


def test_detail_updated_and_confirmed_memories_zero_success_and_unassigned_calls(client, store):
    msg(store, 1, "我喜欢猫")
    first = form_batch(store, "A", PROMPT_VERSION)
    created = LearningEngine(store, FakeGateway({"memories": [memory()]})).run_batch(first.id)["created"][0]
    msg(store, 2, "我现在喜欢狗，不喜欢猫了")
    second = form_batch(store, "A", PROMPT_VERSION)
    output = {"updates": [{"ref": "M1", "action": "修正", "content": "小林现在喜欢狗", "evidence": [2]}]}
    result = LearningEngine(store, FakeGateway(output)).run_batch(second.id)
    assert result["updated"] == [created]
    detail = client.get(f"/admin/api/batches/{second.id}").json()
    assert detail["memories"][0]["change"] == "updated"
    assert detail["memories"][0]["content"] == "小林现在喜欢狗"
    assert detail["attempts"][0]["calls"] == []  # Unknown, never inferred from current config.
    msg(store, 3, "我喜欢狗")
    third = form_batch(store, "A", PROMPT_VERSION)
    LearningEngine(store, FakeGateway({"updates": [{"ref": "M1", "action": "确认", "evidence": [2]}]})).run_batch(third.id)
    assert client.get(f"/admin/api/batches/{third.id}").json()["memories"][0]["change"] == "confirmed"
    msg(store, 4, "嗯")
    fourth = form_batch(store, "A", PROMPT_VERSION)
    LearningEngine(store, FakeGateway({})).run_batch(fourth.id)
    with store.write() as conn:
        conn.execute("""INSERT INTO model_calls(purpose,model,duration_ms,result_category,created_at,batch_id)
            VALUES('learning','old-model',5,'success','2000-01-01T00:00:00Z',?)""", (fourth.id,))
    detail = client.get(f"/admin/api/batches/{fourth.id}").json()
    assert detail["state"] == "succeeded" and detail["memories"] == []
    assert detail["attempts"][0]["calls"] == []
    assert detail["unassigned_calls"][0]["model"] == "old-model"


def test_scheduler_relearn_waits_for_later_inflight_batch(store):
    from threading import Event
    from iris.scheduler import Scheduler
    from test_scheduler import wait_for
    old = terminal_batch(store, "refused")
    msg(store, 2, "后续批次")
    later = form_batch(store, "A", PROMPT_VERSION)
    entered, release = Event(), Event()
    def hook(_):
        entered.set()
        assert release.wait(5)
    fake = FakeGateway(hook=hook)
    scheduler = Scheduler(store, fake)
    try:
        scheduler.tick()
        assert entered.wait(5)
        reset_batch(store, old.id)
        scheduler.tick()
        assert len(fake.requests) == 1 and get_batch(store, later.id).state == "running"
        assert get_batch(store, old.id).state == "waiting"
        release.set()
        wait_for(lambda: get_batch(store, later.id).state == "succeeded")
        # Reap the completed future before the next dispatch, as the service loop does.
        wait_for(lambda: scheduler._active["A"].done())
        scheduler.tick()
        wait_for(lambda: get_batch(store, old.id).state == "succeeded")
        assert len(fake.requests) == 2
    finally:
        release.set()
        scheduler.stop()


def test_learning_date_range_uses_role_timezone_before_order_validation(client, store):
    formed = terminal_batch(store, "refused")
    with store.write() as conn:
        conn.execute("UPDATE batches SET created_at='2026-10-02T00:10:00+08:00' WHERE id=?", (formed.id,))
        conn.execute("UPDATE memory_gaps SET started_at='2026-10-02T00:10:00+08:00',ended_at='2026-10-02T00:20:00+08:00'")
    for route in ("batches", "memory-gaps"):
        response = client.get(f"/admin/api/{route}", params={"time_from": "2026-10-02", "time_to": "2026-10-02T01:00:00+08:00"})
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert client.get(f"/admin/api/{route}", params={"time_from": "2026-10-03", "time_to": "2026-10-02"}).status_code == 400


def test_status_learning_calls_are_newest_first_and_keep_all_latency_samples(client, store):
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(minutes=30)
    # Insert out of chronological order, mix learning/repair, and tie timestamps.
    minutes = [7, 1, 12, 4, 9, 0, 11, 3, 10, 2, 8, 5, 6, 12, 12]
    with store.write() as conn:
        for index, minute in enumerate(minutes):
            conn.execute("""INSERT INTO model_calls(purpose,model,duration_ms,result_category,timed_out,created_at)
                VALUES(?,?,?,'success',?,?)""", ("learning_repair" if index % 2 else "learning", "fake",
                    (index + 1) * 100, int(index == 0), (stamp + timedelta(minutes=minute)).isoformat()))
    expected = [(i + 1) * 100 for i in sorted(range(15), key=lambda i: (minutes[i], i), reverse=True)]
    for route in ("/admin/api/status", "/api/v1/status"):
        response = client.get(route)
        assert response.status_code == 200
        data = response.json()
        durations = [call["duration_ms"] for call in data["learning_calls_24h"]]
        assert durations[:12] == expected[:12]
        assert durations == expected  # The API still supplies all 24-hour samples.
        assert data["learning_latency_24h"] == {
            "count": 15, "p50_ms": 800.0, "p95_ms": 1430.0, "max_ms": 1500, "timeouts": 1}


def test_gap_management_identity_is_batch_id_even_when_gap_row_is_replaced(client, store):
    formed = terminal_batch(store, "refused")
    original_id = gap(store)["id"]
    reset_batch(store, formed.id)
    failing = LearningEngine(store, FakeGateway(ModelError("retryable", "failed again")))
    for _ in range(4):
        failing.run_batch(formed.id, force=True)
    assert gap(store)["id"] != original_id
    listing = client.get("/admin/api/memory-gaps").json()
    detail = client.get(f"/admin/api/batches/{formed.id}").json()
    for item in (listing["items"][0], detail["gap"]):
        assert item["batch_id"] == formed.id
        assert "id" not in item
