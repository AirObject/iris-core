import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGateway, msg
from fake_openai import Clock, FakeOpenAI
from iris.api import create_app
from iris.learning import LearningEngine
from iris.memory_ops import edit_memory
from iris.model_health import ModelHealth
from iris.models import Gateway, ModelError
from iris.queue import form_batch, get_batch
from iris.scheduler import Scheduler
from test_batches import memory
from test_retrieval import put


def wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "condition did not become true"


def intake(store, clock, entry="A", count=1, pace="standard", text="普通消息"):
    for index in range(1, count + 1):
        msg(store, index, text, entry=entry)
    with store.write() as conn:
        conn.execute("UPDATE entries SET pace=? WHERE id=?", (pace, entry))
        conn.execute("UPDATE messages SET received_at=? WHERE entry_id=?", (clock().isoformat(), entry))


@pytest.mark.parametrize(("pace", "count", "seconds", "text", "manual"), [
    ("standard", 12, 0, "普通", False), ("realtime", 1, 5, "普通", False),
    ("standard", 1, 600, "尾部", False), ("economy", 1, 1800, "尾部", False),
    ("standard", 1, 60, "Iris，这件事请留意", False),
    ("standard", 1, 60, "别忘了带伞", False), ("standard", 1, 0, "普通", True),
])
def test_trigger_conditions(store, pace, count, seconds, text, manual):
    clock = Clock()
    intake(store, clock, count=count, pace=pace, text=text)
    scheduler = Scheduler(store, FakeGateway(), clock=clock)
    try:
        if manual:
            scheduler.request_learning("A")
        clock.advance(seconds)
        scheduler.tick()
        wait_for(lambda: get_batch(store, 1).state == "succeeded")
    finally:
        scheduler.stop()


def test_longest_wait_uses_reception_time_and_focus_survives_later_chatter(store):
    clock = Clock()
    intake(store, clock, text="记住带证件")
    scheduler = Scheduler(store, FakeGateway(), clock=clock)
    try:
        scheduler.tick()
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        clock.advance(3599)
        msg(store, 2, "又说了一句")
        with store.write() as conn:
            conn.execute("UPDATE messages SET received_at=? WHERE id=2", (clock().isoformat(),))
        clock.advance(1)
        scheduler.tick()
        wait_for(lambda: get_batch(store, 1).state == "succeeded")
    finally:
        scheduler.stop()


def test_B01_B02_B06_B07_B08_concurrency_serial_entries_and_frozen_batches(store, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("iris.queue.now", lambda: clock().isoformat())
    for entry in ("A", "B", "C"):
        intake(store, clock, entry, 5, "realtime")
    gate = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0
    calls = 0
    def hook(_):
        nonlocal active, maximum, calls
        with lock:
            active += 1
            calls += 1
            maximum = max(maximum, active)
        assert gate.wait(5)
        with lock:
            active -= 1
    scheduler = Scheduler(store, FakeGateway(hook=hook), clock=clock)
    try:
        scheduler.tick()
        wait_for(lambda: active == 2)
        new_id = msg(store, 6, "学习时新收到", entry="A")
        for _ in range(5):
            scheduler.tick()
        with store.read() as conn:
            rows = conn.execute("SELECT entry_id,state,target_ids FROM batches").fetchall()
        assert len(rows) == 2 and len({r["entry_id"] for r in rows}) == 2
        assert all(new_id not in json.loads(r["target_ids"]) for r in rows if r["entry_id"] == "A")
        gate.set()
        wait_for(lambda: active == 0)
        clock.advance(6)
        for _ in range(100):
            scheduler.tick()
            with store.read() as conn:
                if conn.execute("SELECT COUNT(*) FROM messages WHERE learning_state!='learned'").fetchone()[0] == 0:
                    break
            time.sleep(.01)
        assert maximum == 2
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE learning_state!='learned'").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM batches WHERE state!='succeeded'").fetchone()[0] == 0
    finally:
        gate.set()
        scheduler.stop()


def test_retry_due_and_pausing_failure_does_not_consume_attempt(store):
    clock = Clock()
    intake(store, clock, count=4, pace="realtime")
    fake = FakeGateway([ModelError("retryable", "one request failed"), {}])
    scheduler = Scheduler(store, fake, clock=clock)
    try:
        scheduler.tick()
        wait_for(lambda: get_batch(store, 1).attempt_count == 1)
        clock.advance(59)
        scheduler.tick()
        assert len(fake.requests) == 1
        clock.advance(1)
        scheduler.tick()
        wait_for(lambda: get_batch(store, 1).state == "succeeded")
    finally:
        scheduler.stop()

    intake(store, clock, "B", 4, "realtime")
    with FakeOpenAI().serve() as server:
        for _ in range(3):
            server.enqueue(503)
        configs = {"chat": server.configs["chat"]}
        health = ModelHealth(store, configs, clock=clock)
        gateway = Gateway(configs, store, health=health, clock=clock, sleeper=lambda _: None)
        scheduler = Scheduler(store, gateway, clock=clock)
        try:
            scheduler.tick()
            wait_for(lambda: health.snapshot()["chat"]["state"] == "temporarily_unavailable")
            wait_for(lambda: get_batch(store, 2).state == "waiting")
            assert get_batch(store, 2).attempt_count == 0
            for _ in range(4):
                scheduler.tick()
            assert len(server.requests) == 3 and get_batch(store, 2).attempt_count == 0
            clock.advance(60)
            for _ in range(100):
                scheduler.tick()
                if get_batch(store, 2).state == "succeeded":
                    break
                time.sleep(.01)
            assert get_batch(store, 2).state == "succeeded"
        finally:
            scheduler.stop()
            gateway.close()


def test_embedding_pause_fallback_backfill_and_revision_check(store):
    clock = Clock()
    mid = put(store, "我喜欢天文摄影")
    with FakeOpenAI().serve() as server:
        health = ModelHealth(store, server.configs, clock=clock)
        gateway = Gateway(server.configs, store, health=health, sleeper=lambda _: None)
        scheduler = Scheduler(store, gateway, clock=clock)
        settings = store.setting("retrieval")
        store.set_setting("retrieval", {**settings, "embedding_model": "stub-embedding"})
        try:
            server.enqueue(401)
            with pytest.raises(ModelError):
                gateway.embedding("probe")
            with TestClient(create_app(store=store, gateway=gateway)) as client:
                result = client.post("/api/v1/memories/search", json={"text": "天文摄影"}).json()
                assert result["memories"][0]["id"] == mid
                assert any(h["code"] == "model_paused" and h["kind"] == "embedding" for h in result["hints"])
                assert len(server.requests) == 1
            from dataclasses import replace
            gateway.replace_config("embedding", replace(server.configs["embedding"], api_key="changed-fake"))
            original = gateway.embedding
            def editing(text, purpose="embedding"):
                vector = original(text, purpose)
                edit_memory(store, mid, 1, content="我喜欢月面摄影")
                return vector
            gateway.embedding = editing
            scheduler.backfill_vectors()
            with store.read() as conn:
                assert conn.execute("SELECT embedding FROM memories WHERE id=?", (mid,)).fetchone()[0] is None
            gateway.embedding = original
            scheduler.backfill_vectors()
            with store.read() as conn:
                row = conn.execute("SELECT embedding,revision FROM memories WHERE id=?", (mid,)).fetchone()
                assert row[0] is not None and row[1] == 2
        finally:
            scheduler.stop()
            gateway.close()


def test_manual_learning_accepted_while_paused_and_status_fields(store):
    with TestClient(create_app(store=store)) as client:
        payload = {"sender": "小林", "content": "喜欢猫", "occurred_at": "2026-10-04T08:00:00+08:00", "dedupe_key": "1"}
        assert client.post("/api/v1/entries/A/messages", json=payload).status_code == 200
        result = client.post("/api/v1/entries/A/learn").json()
        assert result["accepted"] and result["paused"] and result["reason"]
        assert client.post("/api/v1/entries/missing/learn").status_code == 404
        status = client.get("/api/v1/status").json()
        assert status["scheduler"]["running"]
        assert status["model_health"]["chat"]["state"] == "configuration_error"
        assert status["entries"][0]["pending_count"] == 1
        assert status["memory_gap_count"] == 0
        assert status["usage"]["today"]["calls"] == 0
        assert status["learning_latency_24h"] == {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None, "timeouts": 0}
        assert status["timeouts_seconds"]["learning"] == 180
    with store.read() as conn:
        assert conn.execute("SELECT learn_requested_through FROM entries WHERE id='A'").fetchone()[0] == 1


def test_custom_entry_pace_uses_same_trigger_and_batch_size(store):
    from iris.queue import add_message
    clock = Clock()
    for index in range(3):
        add_message(store, entry_id="custom", entry_name="custom", entry_kind="private", platform="test",
                    kind="message", sender="小林", content="自定义节奏消息", occurred_at=clock().isoformat(),
                    dedupe_key=str(index), pace={"count": 3, "idle_seconds": 15, "max_wait_seconds": 80})
    scheduler = Scheduler(store, FakeGateway(), clock=clock)
    try:
        scheduler.tick()
        wait_for(lambda: get_batch(store, 1).state == "succeeded")
        assert get_batch(store, 1).target_ids == [1, 2, 3]
    finally:
        scheduler.stop()


def test_focus_in_earlier_pending_message_shortens_idle_after_last_chatter(store):
    clock = Clock()
    intake(store, clock, text="请记住这件事情")
    clock.advance(20)
    msg(store, 2, "闲聊一句")
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at=? WHERE id=2", (clock().isoformat(),))
    scheduler = Scheduler(store, FakeGateway(), clock=clock)
    try:
        clock.advance(59)
        scheduler.tick()
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        clock.advance(1)
        scheduler.tick()
        wait_for(lambda: get_batch(store, 1).state == "succeeded")
    finally:
        scheduler.stop()
