"""High-traffic admission is deterministic and never removes recent conversation."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGateway, login_admin, msg
from iris import queue
from iris.api import create_app
from iris.db import Store
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.maintenance import Maintenance
from iris.retrieval import Retrieval, backlog
from iris.scheduler import Scheduler
from test_lifecycle import Clock
from test_retrieval import put
from test_scheduler import wait_for


@pytest.fixture
def clock(monkeypatch):
    value = Clock("2026-10-09T12:00:00+00:00")
    monkeypatch.setattr(queue, "now", lambda: value().isoformat())
    return value


def configure(store, *, entry="A", pace="standard", **filters):
    with store.write() as conn:
        conn.execute("INSERT OR IGNORE INTO entries(id,name,platform,kind) VALUES(?,?, 'test','live')", (entry, entry))
    return queue.update_entry_settings(store, entry, pace=pace, filters={
        "min_chars": 0, "mention_only": False, "context_messages": 0,
        "max_batches_per_hour": 0, **filters})


def states(store):
    with store.read() as conn:
        return {r["id"]: r["learning_state"] for r in conn.execute("SELECT id,learning_state FROM messages")}


def form(store, **kwargs):
    return queue.form_batch(store, "A", PROMPT_VERSION, **kwargs)


def finish(store, batch):
    LearningEngine(store, FakeGateway()).run_batch(batch.id)


def test_short_filter_character_boundary_recent_messages_and_no_gap(store, clock):
    configure(store, min_chars=3)
    short = msg(store, 1, " 嗯😀 ")
    kept = msg(store, 2, "猫😀a")
    assert states(store) == {short: "filtered", kept: "pending"}
    prepared = Retrieval(store).prepare("A", text="")
    assert [r["id"] for r in prepared["recent_messages"]] == [short, kept]
    assert not any(h["code"] == "memory_gaps" for h in prepared["hints"])
    with store.read() as conn:
        assert backlog(conn)[0]["pending_count"] == 1
    first = form(store)
    assert first.target_ids == [kept] and not first.future_ids
    finish(store, first)
    msg(store, 3, "新的消息")
    second = form(store)
    assert second.history_ids == [kept]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM batch_message_refs WHERE message_id=?", (short,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_gaps").fetchone()[0] == 0


@pytest.mark.parametrize("content,accepted", [
    ("Iris 今天读什么", True), ("@Iris", True), ("小鸢请看这里", True),
    ("@小鸢", True), ("@别人请记住", False), ("记住带伞", False),
    ("别忘了", False), ("未确认昵称", False),
])
def test_role_name_and_confirmed_self_alias_only(store, clock, content, accepted):
    configure(store, mention_only=True)
    with store.write() as conn:
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('self','小鸢')")
    mid = msg(store, 1, content)
    assert states(store)[mid] == ("pending" if accepted else "filtered")
    batch = form(store)
    assert (batch.target_ids if batch else []) == ([mid] if accepted else [])


# Handwritten livestream fixture; it is not a quality-evaluation corpus.
LIVE_CHAT = ["来了", "音量刚好", "我换了新耳机", "Iris 我周六去观星", "祝你天气晴朗", "带好外套", "哈哈", "播放到下一首了", "这首我听过"]


def test_handwritten_live_window_and_combined_filters(store, clock):
    configure(store, min_chars=3, mention_only=True, context_messages=1)
    ids = [msg(store, i, text) for i, text in enumerate(LIVE_CHAT, 1)]
    # The last unmatched row needs one more arrival or idle; it is not yet filtered.
    assert states(store)[ids[-1]] == "pending"
    clock.advance(minutes=1)  # Role-name focus closes the tail at the existing short idle.
    first = form(store, target_count=2)
    assert first.target_ids == ids[2:4]
    assert first.future_ids == ids[4:5]
    finish(store, first)
    second = form(store, target_count=2)
    assert second.target_ids == ids[4:5]
    assert second.history_ids == ids[2:4]
    finish(store, second)
    assert form(store) is None
    assert {mid for mid, state in states(store).items() if state == "filtered"} == set(ids[:2] + ids[5:])


def test_window_waits_for_successors_or_idle_and_is_entry_local(store, clock):
    configure(store, mention_only=True, context_messages=2)
    before = msg(store, 1, "前面的问题")
    hit = msg(store, 2, "Iris 我喜欢天文摄影")
    msg(store, 1, "另一个入口", entry="B")
    assert form(store) is None
    after = msg(store, 3, "第一次后续")
    first = form(store, target_count=1)
    assert first.target_ids == [before]
    assert not first.future_ids  # Undecided messages never become frozen context.
    finish(store, first)
    assert form(store) is None
    clock.advance(seconds=59)
    assert form(store) is None
    clock.advance(seconds=1)
    second = form(store)
    assert second.target_ids == [hit, after]
    assert second.history_ids == [before]
    assert states(store)[before] == "learned"


def test_combined_short_anchor_admits_neighbors_but_is_itself_filtered(store, clock):
    store.set_setting("role_name", "鸢")
    configure(store, min_chars=3, mention_only=True, context_messages=1)
    a = msg(store, 1, "前面一句话")
    hit = msg(store, 2, "@鸢")
    b = msg(store, 3, "后面一句话")
    msg(store, 4, "无关的尾部")
    batch = form(store)
    assert batch.target_ids == [a, b]
    assert hit not in batch.history_ids + batch.future_ids
    assert states(store)[hit] == "filtered"


@pytest.mark.parametrize("signal", ["Iris", "@Iris", "记住", "别忘了"])
def test_focus_still_uses_one_minute_and_never_overrides_short_filter(store, clock, signal):
    configure(store, min_chars=3)
    short = msg(store, 1, "嗯")
    kept = msg(store, 2, signal + "下周去观星")
    scheduler = Scheduler(store, FakeGateway(), clock=clock)
    try:
        clock.advance(seconds=59)
        scheduler.tick()
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        clock.advance(seconds=1)
        scheduler.tick()
        wait_for(lambda: states(store)[kept] == "learned")
        assert queue.get_batch(store, 1).target_ids == [kept]
        assert states(store)[short] == "filtered"
    finally:
        scheduler.stop()


def test_hourly_limit_rolling_boundary_manual_and_other_entry(store, clock):
    configure(store, max_batches_per_hour=1)
    first_id = msg(store, 1, "第一条")
    first = form(store)
    finish(store, first)
    waiting = msg(store, 2, "第二条")
    scheduler = Scheduler(store, FakeGateway(), clock=clock)
    try:
        scheduler.request_learning("A")
        clock.advance(minutes=59, seconds=59)
        scheduler.tick()
        assert states(store)[waiting] == "pending"
        assert form(store) is None
        msg(store, 1, "独立入口", entry="B")
        assert queue.form_batch(store, "B", PROMPT_VERSION) is not None
        clock.advance(seconds=1)
        scheduler.tick()
        wait_for(lambda: states(store)[waiting] == "learned")
        assert states(store)[first_id] == "learned"
    finally:
        scheduler.stop()


def test_concurrent_formation_cannot_overspend_and_retry_does_not_count(store, clock):
    configure(store, max_batches_per_hour=1)
    for i in range(1, 4):
        msg(store, i, "直播消息")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: form(store, target_count=1), range(2)))
    batches = [b for b in results if b]
    assert len(batches) == 1
    first = batches[0]
    with store.write() as conn:
        conn.execute("UPDATE batches SET state='abandoned' WHERE id=?", (first.id,))
        conn.execute("UPDATE messages SET learning_state='abandoned' WHERE batch_id=?", (first.id,))
    queue.reset_batch(store, first.id)
    finish(store, first)
    assert form(store) is None
    clock.advance(hours=1)
    assert form(store).target_ids == [2, 3]


def test_settings_apply_to_unfrozen_work_and_preserve_frozen_segments(store, clock):
    configure(store)
    ids = [msg(store, i, text) for i, text in enumerate(["旧目标", "旧后续", "短", "这是足够长的新消息"], 1)]
    first = form(store, target_count=1, future_count=1)
    original = queue.get_batch(store, first.id)
    configure(store, pace="realtime", min_chars=8)
    assert queue.get_batch(store, first.id) == original
    assert states(store)[ids[2]] == "filtered"
    finish(store, first)
    second = form(store)
    # Admission frozen in a prior future segment remains valid when it becomes a target.
    assert second.target_ids == [ids[1], ids[3]]
    assert second.history_ids == [ids[0]]
    configure(store)
    assert states(store)[ids[2]] == "filtered"  # Final exclusions are never revived.


def test_filtered_cleanup_uses_existing_retention_and_reference_guards(store, clock):
    configure(store, min_chars=10)
    ids = [msg(store, i, "短") for i in range(1, 5)]
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at='2026-08-01T00:00:00+00:00' WHERE id!=?", (ids[-1],))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias,source_message_id) VALUES('self','鸢',?)", (ids[1],))
    put(store, "引用来源", evidence=[ids[2]])
    maintenance = Maintenance(store, clock=clock)
    run_id = maintenance.request()
    maintenance.run(run_id)
    assert set(states(store)) == set(ids[1:])
    assert maintenance.report(run_id)["summary"]["messages_deleted"]["count"] == 1
    with store.read() as conn:
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as value:
        login_admin(value)
        value.app.state.scheduler.stop()
        yield value


def test_settings_api_projection_audit_and_limit_wait_status(client, store, clock, monkeypatch):
    configure(store)
    url = "/admin/api/entries/A/settings"
    before = client.get(url).json()
    assert before["pace"] == "standard" and not before["filters"]["mention_only"]
    payload = {"pace": {"count": 1, "idle_seconds": 2, "max_wait_seconds": 3},
               "filters": {"min_chars": 2, "mention_only": False, "context_messages": 1, "max_batches_per_hour": 1}}
    assert client.patch(url, json=payload).json() == payload
    msg(store, 1, "第一条")
    finish(store, form(store))
    msg(store, 2, "第二条")
    # Read projections use the same clock, but must not write or form batches.
    from iris import admin_data
    monkeypatch.setattr(admin_data, "utc_now", clock)
    with store.read() as conn:
        count_before = conn.execute("SELECT COUNT(*) FROM admin_operations").fetchone()[0]
    entries = client.get("/admin/api/entries").json()["items"]
    status = client.get("/admin/api/status").json()
    batches = client.get("/admin/api/batches").json()
    for row in (entries[0], status["entries"][0], batches["entry_waits"][0]):
        assert row["queue_wait"]["reason"] == "hourly_batch_limit"
        assert row["queue_wait"]["retry_at"] == "2026-10-09T13:00:00+00:00"
    assert entries[0]["filters"] == payload["filters"]
    assert entries[0]["pending_count"] == 1
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations").fetchone()[0] == count_before
        rows = conn.execute("SELECT * FROM admin_operations WHERE action='entry_settings_updated' ORDER BY id").fetchall()
        audit = rows[-1]
        assert audit["actor"] == "admin" and audit["object_id"] == "A" and audit["object_type"] == "entry"
        assert json.loads(audit["details_json"]) == {"before": before, "after": payload}


@pytest.mark.parametrize("payload", [
    {}, {"pace": "fast"}, {"pace": None}, {"filters": None},
    {"pace": {"count": True, "idle_seconds": 1, "max_wait_seconds": 1}},
    {"pace": {"count": 0, "idle_seconds": 1, "max_wait_seconds": 1}},
    {"pace": {"count": 1001, "idle_seconds": 1, "max_wait_seconds": 1}},
    {"pace": {"count": 1, "idle_seconds": 86401, "max_wait_seconds": 1}},
    {"pace": {"count": 1, "idle_seconds": 1, "max_wait_seconds": 604801}},
    {"filters": {"min_chars": -1}}, {"filters": {"min_chars": True}},
    {"filters": {"min_chars": 32769}}, {"filters": {"min_chars": "3"}},
    {"filters": {"mention_only": 1}}, {"filters": {"context_messages": 101}},
    {"filters": {"max_batches_per_hour": 1001}}, {"filters": {"max_batches_per_hour": -1}},
    {"filters": {"typo": 1}}, {"typo": 1},
])
def test_settings_validation_is_atomic(client, store, payload):
    configure(store)
    url = "/admin/api/entries/A/settings"
    before = client.get(url).json()
    with store.read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM admin_operations").fetchone()[0]
    assert client.patch(url, json=payload).status_code == 400
    assert client.get(url).json() == before
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations").fetchone()[0] == count


def test_settings_auth_csrf_host_and_unknown_entry(client, store):
    configure(store)
    url = "/admin/api/entries/A/settings"
    assert client.patch(url, json={"pace": "economy"}, headers={"X-Iris-CSRF": "wrong"}).status_code == 403
    assert client.patch(url, json={"pace": "economy"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.patch(url, json={"pace": "economy"}, headers={"Host": "evil.example"}).status_code == 400
    assert client.patch(url, content='{}', headers={"Content-Type": "text/plain"}).status_code == 403
    assert client.get("/admin/api/entries/missing/settings").status_code == 404
    assert client.patch("/admin/api/entries/missing/settings", json={"pace": "economy"}).status_code == 404
    client.cookies.clear()
    assert client.get(url).status_code == 401
    assert client.patch(url, json={"pace": "economy"}).status_code == 401


def test_cleanup_does_not_make_old_mention_adjacent_to_new_messages(store, clock):
    configure(store, mention_only=True, context_messages=1)
    hit = msg(store, 1, "Iris 留下的旧记录")
    old_neighbor = msg(store, 2, "当时的后文")
    msg(store, 3, "无关聊天")
    old_far = msg(store, 4, "很久以前的其他聊天")
    clock.advance(minutes=1)
    batch = form(store)
    finish(store, batch)
    put(store, "只保护旧提及", evidence=[hit])
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at='2026-08-01T00:00:00+00:00'")
    maintenance = Maintenance(store, clock=clock)
    maintenance.run(maintenance.request())
    assert states(store) == {hit: "learned"}
    new = msg(store, 5, "完全不相关的新聊天")
    msg(store, 6, "新聊天的后文")
    assert states(store)[new] == "filtered"
    assert old_neighbor not in states(store) and old_far not in states(store)


def test_filter_decision_and_rate_limit_survive_restart(tmp_path, clock):
    path = tmp_path / "restart.db"
    store = Store(path)
    try:
        configure(store, mention_only=True, context_messages=1, max_batches_per_hour=1)
        a = msg(store, 1, "Iris 第一个窗口")
        b = msg(store, 2, "第一个后文")
        clock.advance(minutes=1)
        finish(store, form(store))
        c = msg(store, 3, "Iris 新的窗口")
    finally:
        store.close()
    reopened = Store(path)
    try:
        reopened.recover_inflight()
        clock.advance(minutes=1)
        assert form(reopened) is None
        assert states(reopened) == {a: "learned", b: "learned", c: "pending"}
        clock.advance(hours=1)
        assert form(reopened).target_ids == [c]
    finally:
        reopened.close()


def test_manual_all_filtered_tail_is_not_retained_by_watermark(store, clock):
    configure(store, mention_only=True, context_messages=2)
    mid = msg(store, 1, "没有提到角色")
    scheduler = Scheduler(store, FakeGateway(), clock=clock)
    try:
        scheduler.request_learning("A")
        scheduler.tick()
        assert states(store)[mid] == "pending"
        clock.advance(minutes=10)
        scheduler.tick()
        assert states(store)[mid] == "filtered"
        with store.read() as conn:
            assert conn.execute("SELECT learn_requested_through FROM entries WHERE id='A'").fetchone()[0] is None
            assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    finally:
        scheduler.stop()


def test_all_filters_apply_before_hourly_limit_and_wait_releases_in_order(store, clock):
    configure(store, min_chars=3, mention_only=True, context_messages=1, max_batches_per_hour=1)
    ids = [msg(store, i, text) for i, text in enumerate(["噪音噪音", "Iris 第一个", "嗯", "Iris 第二个", "第二个后文", "窗口外的后文"], 1)]
    first = form(store, target_count=1, future_count=0)
    assert first.target_ids == ids[:1]
    finish(store, first)
    clock.advance(minutes=1)
    assert form(store) is None
    assert states(store)[ids[2]] == "filtered" and states(store)[ids[5]] == "filtered"
    clock.advance(minutes=59)
    second = form(store)
    assert second.target_ids == [ids[1], ids[3], ids[4]]


def test_lowering_hourly_limit_waits_for_mth_newest_expiry(store, clock):
    configure(store)
    for i in range(1, 4):
        msg(store, i, "每次一条")
        finish(store, form(store))
        clock.advance(minutes=10)
    configure(store, max_batches_per_hour=1)
    msg(store, 4, "等待最后一批满一小时")
    from iris.admin_data import entry_queue_wait
    with store.read() as conn:
        entry = conn.execute("SELECT * FROM entries WHERE id='A'").fetchone()
        state = entry_queue_wait(conn, entry, clock())
    assert state["retry_at"] == "2026-10-09T13:20:00+00:00"
    clock.advance(minutes=30)
    assert form(store) is None
    clock.advance(minutes=20)
    assert form(store) is not None


def test_only_body_mentions_count_and_other_subject_aliases_do_not(store, clock):
    configure(store, mention_only=True)
    mid = queue.add_message(store, entry_id="A", entry_name="A", platform="test", entry_kind="live",
        kind="message", sender="Iris", content="没有正文提及", occurred_at=clock().isoformat(), dedupe_key="quote",
        quote_author="Iris", quote_content="Iris 引用里的名字")
    assert states(store)[mid] == "filtered"
    with store.write() as conn:
        sender_id = conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (mid,)).fetchone()[0]
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES(?,'小竹')", (sender_id,))
    other = msg(store, 2, "小竹 你好")
    assert states(store)[other] == "filtered"


def test_settings_patch_preserves_omitted_values_and_batch_snapshot(client, store, clock):
    configure(store, mention_only=True, context_messages=2)
    url = "/admin/api/entries/A/settings"
    response = client.patch(url, json={"filters": {"min_chars": 3}})
    assert response.status_code == 200
    assert response.json()["filters"] == {"min_chars": 3, "mention_only": True, "context_messages": 2, "max_batches_per_hour": 0}
    msg(store, 1, "Iris 快要组批")
    clock.advance(minutes=1)
    batch = form(store)
    assert client.patch(url, json={"pace": "economy"}).status_code == 200
    detail = client.get(f"/admin/api/batches/{batch.id}").json()
    assert detail["entry_settings"] == response.json()
    assert detail["entry"]["pace"] == "economy"


def test_migration_009_defaults_and_existing_frozen_admission(tmp_path, clock):
    import sqlite3
    from pathlib import Path
    from iris.search_text import segmented
    path = tmp_path / "v8.db"
    migrations = Path(__file__).resolve().parents[1] / "src/iris/migrations"
    with sqlite3.connect(path) as conn:
        conn.create_function("iris_terms", 1, segmented, deterministic=True)
        conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)")
        for script in sorted(migrations.glob('00[1-8]_*.sql')):
            conn.executescript(script.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (script.name, clock().isoformat()))
            conn.commit()
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('self','self','我',?)", (clock().isoformat(),))
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','test','live')")
        for i in range(1, 4):
            conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key)
                VALUES('A','message','self','原消息',?,?,?)""", (clock().isoformat(), clock().isoformat(), str(i)))
        conn.execute("""INSERT INTO batches(entry_id,history_ids,target_ids,future_ids,state,prompt_version,created_at)
            VALUES('A','[]','[1]','[2]','succeeded','v6',?)""", (clock().isoformat(),))
        conn.execute("UPDATE messages SET learning_state='learned',batch_id=1 WHERE id=1")
    upgraded = Store(path)
    try:
        from iris.admin_data import entry_learning_settings, batch_detail
        assert entry_learning_settings(upgraded, 'A') == {"pace": "standard", "filters": queue.FILTER_DEFAULTS}
        assert batch_detail(upgraded, 1)["entry_settings"] is None
        configure(upgraded, min_chars=10)
        assert states(upgraded) == {1: "learned", 2: "pending", 3: "filtered"}
        assert form(upgraded).target_ids == [2]
        with upgraded.read() as conn:
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert len(list(tmp_path.glob('v8.db.*.bak'))) == 1
    finally:
        upgraded.close()


def test_default_recent_message_fields_remain_compatible(store, clock):
    mid = msg(store, 1, "近期对话")
    recent = Retrieval(store).prepare("A", text="")["recent_messages"]
    assert len(recent) == 1 and recent[0]["id"] == mid
    assert set(recent[0]) == {"id", "entry_id", "kind", "sender_subject_id", "scene_identity", "content",
        "quote_author_subject_id", "quote_content", "occurred_at", "received_at", "dedupe_key",
        "learning_state", "batch_id", "sender_name", "quote_author_name", "unlearned"}
