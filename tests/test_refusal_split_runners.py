"""RS08/14/18: shared queue, bounded scheduling, and real-engine runner integration."""
import threading
from contextlib import closing


from conftest import FakeGateway
from iris import cli, evaluation
from iris.db import Store
from iris.queue import claim_batch, get_batch, split_refused_batch
from iris.scheduler import Scheduler
from test_refusal_split_queue import finish, root_batch, split
from test_scheduler import wait_for


class RunnerGateway(FakeGateway):
    def __init__(self, configs, store):
        super().__init__({})
    def close(self):
        pass


class SplittingExecutor:
    """Isolate runner ordering; real-engine integration is covered below."""
    visited = []
    def __init__(self, store, gateway):
        self.store = store
    def run_batch(self, bid, *, force=False):
        batch = claim_batch(self.store, bid, force=force)
        self.visited.append(batch.split_path)
        if len(batch.target_ids) > 1:
            with self.store.write() as conn:
                split_refused_batch(conn, bid, prompt_version="rs-test")
            return {"state": "split"}
        finish(self.store, bid)
        return {"state": "succeeded"}


def test_rs18_offline_learn_drains_entire_tree_in_shared_order(tmp_path, monkeypatch):
    path = tmp_path / "offline.db"
    store = Store(path)
    root_batch(store)
    store.close()
    SplittingExecutor.visited = []
    monkeypatch.setattr(cli, "load_test_models", lambda: {})
    monkeypatch.setattr(cli, "Gateway", RunnerGateway)
    monkeypatch.setattr(cli, "LearningEngine", SplittingExecutor)
    assert cli.main(["--db", str(path), "learn", "A", "--force"]) == 0
    assert SplittingExecutor.visited == ["", "0", "00", "01", "1", "10", "11"]
    store = Store(path)
    try:
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM memory_gaps").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE learning_state='learned'").fetchone()[0] == 4
    finally:
        store.close()


def test_rs18_learning_evaluation_drains_entire_tree(tmp_path, monkeypatch):
    # Handwritten request-shape fixture, not evaluation or acceptance corpus.
    case = {"id": "rs-runner", "entry_type": "group", "messages": [
        {"type": "message", "speaker": "小林", "content": "我喜欢猫", "at": "2026-10-12T09:00:00+08:00"},
        {"type": "message", "speaker": "小林", "content": "我也喜欢狗", "at": "2026-10-12T09:01:00+08:00"},
        {"type": "message", "speaker": "小林", "content": "我周末会去公园", "at": "2026-10-12T09:02:00+08:00"},
        {"type": "message", "speaker": "小林", "content": "公园有很多树", "at": "2026-10-12T09:03:00+08:00"}]}
    SplittingExecutor.visited = []
    monkeypatch.setattr(evaluation, "Gateway", RunnerGateway)
    monkeypatch.setattr(evaluation, "LearningEngine", SplittingExecutor)
    result = evaluation._run_case({}, case, judge_mode="external")
    assert SplittingExecutor.visited == ["", "0", "00", "01", "1", "10", "11"]
    assert {b["state"] for b in result["actual"]["batches"]} == {"split", "succeeded"}


def test_rs08_scheduler_honors_shared_depth_first_head(store, monkeypatch):
    root = root_batch(store)
    left, right = split(store, root.id)
    first, _ = split(store, left)
    scheduler = Scheduler(store, FakeGateway())
    called = []
    def execute(bid):
        called.append(bid)
    monkeypatch.setattr(scheduler, "_execute", execute)
    try:
        scheduler.tick()
        scheduler._active["A"].result(timeout=5)
        assert called == [first]  # physical right ID is lower than first's ID
    finally:
        scheduler.stop()


def test_rs08_two_slots_entry_serialization_and_round_robin(store, monkeypatch):
    root = root_batch(store)
    left, right = split(store, root.id)
    first, second = split(store, left)
    b = root_batch(store, 1, entry="B")
    c = root_batch(store, 1, entry="C")
    releases = {bid: threading.Event() for bid in (first, second, right, b.id, c.id)}
    barrier = threading.Barrier(3)
    calls, lock = [], threading.Lock()
    scheduler = Scheduler(store, FakeGateway(), max_concurrent=2)
    def execute(bid):
        claim_batch(store, bid, force=True)
        with lock:
            calls.append(bid)
            first_wave = len(calls) <= 2
        if first_wave:
            barrier.wait(timeout=5)
        assert releases[bid].wait(5)
        finish(store, bid)
    monkeypatch.setattr(scheduler, "_execute", execute)
    try:
        scheduler.tick()
        barrier.wait(timeout=5)
        assert set(calls) == {first, b.id}
        scheduler.tick()
        assert len(calls) == 2
        releases[b.id].set()
        scheduler._active["B"].result(timeout=5)
        scheduler.tick()
        wait_for(lambda: c.id in calls)
        assert second not in calls and right not in calls
        assert len(scheduler._active) == 2
        releases[first].set()
        scheduler._active["A"].result(timeout=5)
        scheduler.tick()
        wait_for(lambda: second in calls)
        assert right not in calls
    finally:
        barrier.abort()
        for release in releases.values():
            release.set()
        scheduler.stop()


def test_rs14_refusal_usage_counts_once_tree_pauses_at_daily_cap_and_resumes_next_day(store, monkeypatch):
    from iris.model_health import ModelHealth
    from iris.models import ModelConfig
    from iris.service_status import service_status
    from test_lifecycle import Clock
    clock = Clock("2026-10-13T23:59:50+08:00")
    root = root_batch(store, 2)
    left, _ = split(store, root.id)
    gateway = FakeGateway()
    gateway.configs = {"chat": ModelConfig("http://fake", "", "fake")}
    health = ModelHealth(store, gateway.configs, clock=clock)
    gateway.health = health
    health.set_daily_token_limit(30)
    with store.write() as conn:
        conn.execute("""INSERT INTO model_calls(purpose,model,duration_ms,created_at,batch_id,
            prompt_tokens,completion_tokens,result_category) VALUES('learning','fake',1,?,?,20,10,'content_rejection')""",
            (clock().isoformat(), root.id))
        conn.execute("""INSERT INTO model_calls(purpose,model,duration_ms,created_at,batch_id,
            result_category) VALUES('learning','fake',1,?,?,'content_rejection')""", (clock().isoformat(), left))
    scheduler = Scheduler(store, gateway, clock=clock)
    called = []
    monkeypatch.setattr(scheduler, "_execute", lambda bid: called.append(bid))
    monkeypatch.setattr(scheduler, "_schedule_lifecycle", lambda: None)
    try:
        scheduler.tick()
        assert called == [] and health.budget()["used"] == 30
        usage = service_status(store, scheduler, health, clock=clock)["usage"]["today"]
        assert usage["calls"] == 2 and usage["calls_without_usage"] == 1 and usage["tokens"] == 30
        clock.advance(seconds=20)
        scheduler.tick()
        scheduler._active["A"].result(timeout=5)
        assert called == [left] and health.budget()["used"] == 0
        assert get_batch(store, left).attempt_count == 0
    finally:
        scheduler.stop()


def test_rs18_real_engine_offline_continues_immediate_context_retry(tmp_path, monkeypatch):
    from conftest import msg
    from iris.queue import form_batch
    from iris.learning import PROMPT_VERSION
    from test_refusal_split_learning import MarkerGateway
    path = tmp_path / 'actual-offline.db'
    with closing(Store(path)) as store:
        msg(store, 1, '安全目标')
        msg(store, 2, '触发拒绝')
        root = form_batch(store, 'A', PROMPT_VERSION, target_count=1)
    class Model(MarkerGateway):
        def __init__(self, configs, store):
            super().__init__(store)
        def close(self):
            pass
    monkeypatch.setattr(cli, 'load_test_models', lambda: {})
    monkeypatch.setattr(cli, 'Gateway', Model)
    assert cli.main(['--db', str(path), 'learn', 'A', '--force']) == 0
    with closing(Store(path)) as store:
        assert get_batch(store, root.id).state == 'succeeded'
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE learning_state IN ('pending','batched')").fetchone()[0] == 0


def test_rs18_real_engine_evaluation_drains_split_and_context_retries(monkeypatch):
    from test_refusal_split_learning import MarkerGateway
    class Model(MarkerGateway):
        def __init__(self, configs, store):
            super().__init__(store)
        def close(self):
            pass
    case = {'id':'rs-real-runner', 'entry_type':'group', 'messages':[
        {'type':'message','speaker':'小林','content':'安全甲','at':'2026-10-12T09:00:00+08:00'},
        {'type':'message','speaker':'小林','content':'触发拒绝','at':'2026-10-12T09:01:00+08:00'},
        {'type':'message','speaker':'小林','content':'安全乙','at':'2026-10-12T09:02:00+08:00'},
        {'type':'message','speaker':'小林','content':'安全丙','at':'2026-10-12T09:03:00+08:00'}]}
    monkeypatch.setattr(evaluation, 'Gateway', Model)
    result = evaluation._run_case({}, case, judge_mode='external')
    states = [b['state'] for b in result['actual']['batches']]
    assert 'split' in states and 'succeeded' in states and states.count('refused') == 1
    assert not {'waiting','running'} & set(states)
