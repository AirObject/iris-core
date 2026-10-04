import json
import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from conftest import FakeGateway, msg
from fake_openai import Clock
from iris.db import Store
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.memory_ops import edit_memory
from iris.model_health import ModelHealth
from iris.models import ModelConfig
from iris.queue import form_batch, get_batch
from iris.retrieval import Retrieval
from iris.runtime_logging import configure_logging
from iris.scheduler import Scheduler
from iris.service_status import service_status
from test_batches import memory
from test_retrieval import put
from test_scheduler import intake, wait_for


def test_B13_real_process_exit_mid_learning_transaction_rolls_back(tmp_path):
    path = tmp_path / "crash.db"
    child = """
import os,sys
from iris.db import Store
from iris.learning import LearningEngine
from iris.models import ModelConfig
from iris.queue import add_message,form_batch
class Fake:
    configs={"chat":ModelConfig("fake","","fake")}
    def json_chat(self,*args,**kw):
        return {"memories":[{"content":"小林喜欢猫","type":"偏好","about":["P1"],"speaker":"P1","stance":"亲历","evidence":[1]}]},"{}",None,"direct"
class Crash(LearningEngine):
    def _source(self,conn,memory_id,message_id):
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        os._exit(73)
store=Store(sys.argv[1])
add_message(store,entry_id="A",entry_name="A",platform="test",entry_kind="private",kind="message",sender="小林",account_id="lin",content="我喜欢猫",occurred_at="2026-10-04T08:00:00+08:00",dedupe_key="1")
batch=form_batch(store,"A","test")
Crash(store,Fake()).run_batch(batch.id)
"""
    result = subprocess.run([sys.executable, "-c", child, str(path)], timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 73
    store = Store(path)
    try:
        assert get_batch(store, 1).state == "running"
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        scheduler = Scheduler(store, FakeGateway({"memories": [memory()]}))
        scheduler.start()
        try:
            wait_for(lambda: get_batch(store, 1).state == "succeeded")
        finally:
            scheduler.stop()
        assert get_batch(store, 1).attempt_count == 2
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    finally:
        store.close()


def test_B15_two_batches_edit_and_real_usage_feedback_share_one_writer(store):
    mid = put(store, "我喜欢天文摄影")
    retrieval = Retrieval(store)
    recall = retrieval.search(text="天文摄影")
    for entry in ("A", "B"):
        msg(store, 1, "我喜欢猫", entry=entry)
    batches = [form_batch(store, e, PROMPT_VERSION) for e in ("A", "B")]
    barrier = threading.Barrier(4)
    def learn(batch):
        def hook(_):
            assert not store._lock._is_owned()
            barrier.wait(5)
        return LearningEngine(store, FakeGateway({"memories": [memory()]}, hook=hook)).run_batch(batch.id)
    def edit():
        barrier.wait(5)
        return edit_memory(store, mid, 1, content="我喜欢用望远镜拍月亮")
    def feedback():
        barrier.wait(5)
        return retrieval.feedback(recall["recall_id"], [mid])
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(learn, b) for b in batches] + [pool.submit(edit), pool.submit(feedback)]
        results = [f.result(timeout=10) for f in futures]
    assert all(get_batch(store, b.id).state == "succeeded" for b in batches)
    assert results[2] and results[3]["strengthened"] == [mid]
    with store.read() as conn:
        assert tuple(conn.execute("SELECT revision,retention FROM memories WHERE id=?", (mid,)).fetchone()) == (2, 58)


def test_status_usage_latency_and_timezone_boundaries(store):
    clock = Clock()
    configs = {"chat": ModelConfig("http://fake", "", "chat")}
    health = ModelHealth(store, configs, clock=clock)
    scheduler = Scheduler(store, FakeGateway(), clock=clock)
    try:
        with store.write() as conn:
            for duration in (100, 200, 1000):
                conn.execute("""INSERT INTO model_calls(purpose,model,duration_ms,prompt_tokens,completion_tokens,
                    result_category,created_at,timed_out) VALUES('learning','fake',?,10,20,?,?,?)""",
                    (duration, "retryable" if duration == 1000 else "success", clock().isoformat(), duration == 1000))
        result = service_status(store, scheduler, health, clock=clock)
        assert result["usage"]["today"]["tokens"] == 90
        assert result["usage"]["today"]["failure_rate"] == 1 / 3
        assert result["learning_latency_24h"] == {"count": 3, "p50_ms": 200, "p95_ms": 920, "max_ms": 1000, "timeouts": 1}
        clock.advance(60)
        assert service_status(store, scheduler, health, clock=clock)["usage"]["today"]["calls"] == 0
    finally:
        scheduler.stop()


def test_logs_are_utf8_rolled_and_redacted(tmp_path):
    configs = {"chat": ModelConfig("http://fake", "test-only-credential", "fake")}
    logger = configure_logging(tmp_path, configs, max_bytes=200, backups=2)
    try:
        for _ in range(12):
            logger.info("用途=chat state=normal credential=%s", configs["chat"].api_key)
        files = list((tmp_path / "logs").iterdir())
        assert len(files) == 3
        text = "".join(p.read_text(encoding="utf-8") for p in files)
        assert "test-only-credential" not in text and "用途" in text
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


def test_request_learning_watermark_survives_scheduler_restart(store):
    clock = Clock()
    intake(store, clock, count=13)
    first = Scheduler(store, FakeGateway(), clock=clock)
    first.request_learning("A")
    first.stop()
    second = Scheduler(store, FakeGateway(), clock=clock)
    try:
        second.tick()
        wait_for(lambda: get_batch(store, 1).state == "succeeded")
        second.tick()
        wait_for(lambda: get_batch(store, 2).state == "succeeded")
        assert get_batch(store, 2).target_ids == [13]
    finally:
        second.stop()


def test_offline_learning_is_rejected_while_service_owns_database(tmp_path):
    from iris.process_lock import StoreLease
    from iris.cli import main
    path = tmp_path / "owned.db"
    with StoreLease(path):
        assert main(["--db", str(path), "learn", "A"]) == 1
        assert not path.exists()
    with StoreLease(path):
        pass
