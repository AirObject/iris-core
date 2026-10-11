import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from conftest import FakeGateway, msg
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.maintenance import Maintenance
from iris.memory_ops import edit_memory, manage_memory
from iris.queue import form_batch
from iris.retrieval import Retrieval
from iris.scheduler import Scheduler
from test_lifecycle import Clock, configure, row, run
from test_retrieval import put


def test_D04_catchup_waits_for_all_entries_quiet_after_start_and_only_once(store):
    clock = Clock("2026-10-08T12:00:00+08:00")
    maintenance = Maintenance(store, clock=clock)
    assert maintenance.due() is None
    clock.advance(minutes=9)
    assert maintenance.due() is None
    msg(store, 1, "刚收到", entry="B")
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at=?", (clock().isoformat(),))
    clock.advance(minutes=2)
    assert maintenance.due() is None
    clock.advance(minutes=8)
    due = maintenance.due()
    assert due == ("catchup", "2026-10-08")
    rid = maintenance.request(trigger=due[0])
    maintenance.run(rid)
    clock.advance(hours=25)
    # New scheduled run may be due, but startup catchup cannot repeat.
    assert maintenance.due() != ("catchup", None)


def test_daily_schedule_respects_timezone_durable_slot_and_latest_missed_run_only(store):
    store.set_setting("timezone", "Asia/Shanghai")
    clock = Clock("2026-10-07T18:59:59+00:00")
    maintenance = Maintenance(store, clock=clock)
    assert maintenance.due() is None
    clock.advance(seconds=1)
    trigger, key = maintenance.due()
    assert trigger == "scheduled" and key == "2026-10-08"
    rid = maintenance.request(trigger=trigger, schedule_key=key)
    maintenance.run(rid)
    assert maintenance.due() is None
    clock.advance(days=30)
    trigger, key = maintenance.due()
    assert key == "2026-11-07"
    assert maintenance.request(trigger=trigger, schedule_key=key) > rid


def test_cleanup_protects_unlearned_evidence_but_not_finished_batch_references(store):
    mids = [msg(store, i, f"清理用消息 {i}") for i in range(1, 11)]
    old = (Clock()()-timedelta(days=31)).isoformat()
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='learned',received_at=?", (old,))
        conn.execute("UPDATE messages SET learning_state='pending' WHERE id=?", (mids[1],))
        conn.execute("UPDATE messages SET learning_state='refused' WHERE id=?", (mids[2],))
        conn.execute("UPDATE messages SET learning_state='abandoned' WHERE id=?", (mids[3],))
        conn.execute("INSERT INTO batches(entry_id,history_ids,target_ids,future_ids,state,prompt_version,created_at) VALUES('A',?,?,?,'succeeded','v6',?)", (json.dumps([mids[4]]),json.dumps([mids[5]]),json.dumps([mids[6]]),old))
        gid = conn.execute("INSERT INTO goals(content,kind,created_at) VALUES('目标','normal',?)", (old,)).lastrowid
        conn.execute("INSERT INTO goal_sources VALUES(?,?)", (gid,mids[7]))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias,source_message_id) VALUES('self','别名',?)", (mids[8],))
    put(store, "保留来源", evidence=[mids[9]])
    report = run(store)
    with store.read() as conn:
        present = {r[0] for r in conn.execute("SELECT id FROM messages")}
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert present == {mids[1], mids[7], mids[8], mids[9]}
    assert report["summary"]["messages_deleted"]["count"] == 6


def test_abandoned_yesterday_retries_once_preserves_attempts_gap_and_refused_is_excluded(store):
    clock = Clock()
    ids = []
    for i, state in enumerate(("abandoned", "refused", "abandoned")):
        mid = msg(store, 1, "消息", entry=str(i))
        batch = form_batch(store, str(i), PROMPT_VERSION)
        ids.append(batch.id)
        finish = (clock()-timedelta(days=1 if i < 2 else 2)).isoformat()
        with store.write() as conn:
            conn.execute("UPDATE batches SET state=?,attempt_count=4,finished_at=? WHERE id=?", (state,finish,batch.id))
            conn.execute("INSERT INTO memory_gaps(batch_id,entry_id,started_at,ended_at,reason,created_at) VALUES(?,?,?,?,?,?)", (batch.id,str(i),finish,finish,'attempts_exhausted',finish))
            conn.execute("INSERT INTO batch_attempts(batch_id,number,started_at,finished_at,parse_status,duration_ms) VALUES(?,4,?,?,'failed',0)", (batch.id,finish,finish))
    report = run(store, clock)
    with store.read() as conn:
        assert [tuple(r) for r in conn.execute("SELECT state,attempt_count FROM batches ORDER BY id")] == [("waiting",0),("refused",4),("abandoned",4)]
        assert conn.execute("SELECT COUNT(*) FROM memory_gaps").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM batch_attempts").fetchone()[0] == 3
    assert report["summary"]["batches_retried"]["object_ids"] == [ids[0]]
    with store.write() as conn:
        conn.execute("UPDATE batches SET state='abandoned',attempt_count=4 WHERE id=?", (ids[0],))
    run(store, clock)
    with store.read() as conn:
        assert conn.execute("SELECT state FROM batches WHERE id=?", (ids[0],)).fetchone()[0] == "abandoned"


def test_item_failure_is_safe_in_report_and_other_items_complete(store, monkeypatch):
    import iris.maintenance as module
    ids = [put(store, "记忆甲", importance=0),put(store, "记忆乙", importance=0)]
    original = module.adjust_retention
    def fail_one(store, mid, *args, **kwargs):
        if mid == ids[0]:
            raise ValueError("sensitive private content must never be recorded")
        return original(store, mid, *args, **kwargs)
    monkeypatch.setattr(module, "adjust_retention", fail_one)
    report = run(store)
    assert [row(store, i)["retention"] for i in ids] == [50,49]
    assert "sensitive private" not in json.dumps(report)
    assert any(i["outcome"] == "failed" and i["reason"] == "ValueError" for i in report["items"])


def test_D01_B15_learning_edit_feedback_and_maintenance_share_writer_without_lock_errors(store, monkeypatch):
    target = put(store, "我喜欢天文摄影", importance=0)
    msg(store, 1, "我喜欢天文摄影", entry="A")
    msg(store, 1, "我喜欢天文摄影", entry="B")
    batches = [form_batch(store, e, PROMPT_VERSION) for e in ("A", "B")]
    retrieval = Retrieval(store)
    recall = retrieval.search(text="天文摄影")
    gate, reached = threading.Event(), threading.Event()
    maintenance = Maintenance(store, clock=Clock())
    original = maintenance._process_item
    def pause_before_transaction(*args):
        reached.set()
        assert gate.wait(5)
        return original(*args)
    monkeypatch.setattr(maintenance, "_process_item", pause_before_transaction)
    rid = maintenance.request()
    with ThreadPoolExecutor(max_workers=6) as pool:
        job = pool.submit(maintenance.run,rid)
        assert reached.wait(5)
        futures = [pool.submit(LearningEngine(store,FakeGateway()).run_batch,b.id) for b in batches]
        futures.extend([pool.submit(edit_memory,store,target,1,content="我喜欢天文摄影器材"),
                        pool.submit(retrieval.feedback,recall["recall_id"],[target]),
                        pool.submit(retrieval.prepare,"A",text="天文摄影",participants=[])])
        # All other operations finish while maintenance is paused, outside its transactions.
        for future in futures:
            future.result(timeout=5)
        msg(store,2,"仍能接收",entry="A")
        gate.set()
        job.result(timeout=5)
    assert row(store,target)["revision"] == 2
    assert row(store,target)["retention"] == 58  # revision conflict skipped decay
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM batches WHERE state='succeeded'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='batch_result'").fetchone()[0] == 2


def test_scheduler_runs_due_maintenance_even_when_chat_paused_and_resumes_interrupted(store):
    clock=Clock("2026-10-08T02:59:59+08:00")
    mid=put(store,"可衰减",importance=0)
    from iris.model_health import ModelHealth
    from test_scheduler import wait_for
    gateway=FakeGateway()
    gateway.health=ModelHealth(store,{},clock=clock)
    scheduler=Scheduler(store,gateway,clock=clock)
    try:
        clock.advance(seconds=1)
        scheduler.tick()
        wait_for(lambda: row(store,mid)["retention"] == 49)
        wait_for(lambda: scheduler.lifecycle.report(1)["state"] == "completed")
        scheduler.tick()
        assert row(store,mid)["retention"] == 49
    finally:
        scheduler.stop()
    # A durable accepted manual request is resumed without waiting ten minutes.
    pending=Maintenance(store,clock=clock).request()
    restarted=Scheduler(store,gateway,clock=clock)
    try:
        restarted.tick()
        wait_for(lambda: restarted.lifecycle.report(pending)["state"] == "completed")
        assert row(store,mid)["retention"] == 48
    finally:
        restarted.stop()


def test_abandoned_retry_disable_and_cleanup_reference_types(store):
    from iris.queue import form_batch
    clock=Clock()
    source=msg(store,1,"主体证据")
    batch=form_batch(store,"A","v6")
    configure(store,abandoned_retry_enabled=False)
    with store.write() as conn:
        conn.execute("UPDATE batches SET state='abandoned',finished_at=? WHERE id=?",((clock()-timedelta(days=1)).isoformat(),batch.id))
    run(store,clock)
    with store.read() as conn:
        assert conn.execute("SELECT state FROM batches").fetchone()[0] == "abandoned"
        assert conn.execute("SELECT COUNT(*) FROM maintenance_batch_retries").fetchone()[0] == 0


def test_configuration_freezes_per_run_and_new_run_reads_latest_settings(store):
    mid=put(store,"计数",importance=0)
    maintenance=Maintenance(store,clock=Clock())
    rid=maintenance.request()
    configure(store,decay_amount=3)
    maintenance.run(rid)
    assert row(store,mid)["retention"] == 49
    run(store)
    assert row(store,mid)["retention"] == 46


def test_revision_conflict_on_expiry_preserves_latest_admin_edit(store,monkeypatch):
    from iris.memory_ops import adjust_retention
    clock=Clock()
    mid=put(store,"原文",importance=70)
    adjust_retention(store,mid,value=19,current=clock()-timedelta(days=180))
    maintenance=Maintenance(store,clock=clock)
    original=maintenance._process_item
    def edit_on_expiry(run,phase,candidate):
        if phase == "expiry":
            assert edit_memory(store,mid,1,content="修订后")
        return original(run,phase,candidate)
    monkeypatch.setattr(maintenance,"_process_item",edit_on_expiry)
    rid=maintenance.request()
    maintenance.run(rid)
    assert row(store,mid)["lifecycle"] == "forgotten"
    assert maintenance.report(rid)["summary"]["skipped"]["reasons"]["revision_conflict"] == 1


def test_defaults_match_migration_and_validated_api_model(store):
    from iris.memory_ops import LIFECYCLE_DEFAULTS
    from iris.settings_api import Lifecycle
    assert store.setting("lifecycle") == LIFECYCLE_DEFAULTS == Lifecycle().model_dump()


def test_cleanup_protects_subject_link_learning_request_but_not_trial_reply_key(store):
    ids=[msg(store,i,"旧证据") for i in range(1,5)]
    clock=Clock()
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='learned',received_at=?",((clock()-timedelta(days=31)).isoformat(),))
        person=conn.execute("SELECT sender_subject_id FROM messages WHERE id=?",(ids[0],)).fetchone()[0]
        conn.execute("INSERT INTO subject_links(subject_a,subject_b,kind,belief,source_message_id,created_at) VALUES('self',?,'same_as',50,?,?)",(person,ids[0],clock().isoformat()))
        conn.execute("UPDATE entries SET learn_requested_through=? WHERE id='A'",(ids[1],))
        conn.execute("UPDATE messages SET dedupe_key=? WHERE id=?",(f"trial-reply:{ids[2]}",ids[3]))
    report=run(store,clock)
    with store.read() as conn:
        assert {r[0] for r in conn.execute("SELECT id FROM messages")} == set(ids[:2])
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert report["summary"]["skipped"]["reasons"]["referenced"] == 2
    assert report["summary"]["messages_deleted"]["object_ids"] == ids[2:]


def test_D03_store_reopen_resumes_exact_journal_without_repeat(tmp_path,monkeypatch):
    from iris.db import Store
    path=tmp_path/"restart.db"
    with_store=Store(path)
    mid=put(with_store,"持久化进度",importance=0)
    worker=Maintenance(with_store,clock=Clock())
    rid=worker.request()
    original=worker._process_item
    def interrupt(*args):
        original(*args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(worker,"_process_item",interrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.run(rid)
    with_store.close()
    reopened=Store(path)
    try:
        next_worker=Maintenance(reopened,clock=Clock())
        assert next_worker.pending() == rid
        next_worker.run(rid)
        assert row(reopened,mid)["retention"] == 49
        assert next_worker.report(rid)["summary"]["decayed"]["count"] == 1
    finally:
        reopened.close()


def test_recent_completion_prevents_startup_catchup_and_setting_time_is_immediate(store):
    clock=Clock("2026-10-08T12:00:00+08:00")
    run(store,clock)
    clock.advance(hours=23)
    maintenance=Maintenance(store,clock=clock)
    clock.advance(minutes=10)
    assert maintenance.due() is None
    configure(store,maintenance_time="11:11")
    clock.advance(minutes=1)
    assert maintenance.due() == ("scheduled","2026-10-09")


def test_migration_008_preserves_existing_memory_refs_and_backfills_admin_history(tmp_path):
    import sqlite3
    from pathlib import Path
    from iris.db import Store
    from iris.search_text import segmented
    path=tmp_path/"v7.db"
    scripts=Path(__file__).resolve().parents[1]/"src/iris/migrations"
    with sqlite3.connect(path) as conn:
        conn.create_function("iris_terms",1,segmented,deterministic=True)
        conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)")
        for script in sorted(scripts.glob("*.sql")):
            if script.name >= "008":
                break
            conn.executescript(script.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations VALUES(?,'2026-10-08')",(script.name,))
            conn.commit()
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('self','self','我','2026-10-08')")
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','test','group')")
        conn.execute("INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key) VALUES('A','message','self','旧消息','2026-10-08','2026-10-08','one')")
        conn.execute("INSERT INTO batches(entry_id,history_ids,target_ids,future_ids,state,prompt_version,created_at) VALUES('A','[]','[1]','[]','succeeded','v6','2026-10-08')")
        conn.execute("INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,created_at,updated_at,first_confirmed_at,last_confirmed_at) VALUES('已有正文','事实','self','亲历',70,50,50,'2026-10-08','2026-10-08','2026-10-08','2026-10-08')")
        conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(1,'message',1,'2026-10-08')")
        conn.execute("INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,reason,actor,created_at) VALUES(1,1,2,'{}','{}','manual edit','admin','2026-10-08')")
    upgraded=Store(path)
    try:
        with upgraded.read() as conn:
            assert conn.execute("SELECT content,retention,decay_visits,purged_at FROM memories").fetchone()[:] == ("已有正文",50,0,None)
            assert conn.execute("SELECT message_id,segment FROM batch_message_refs").fetchone()[:] == (1,"target")
            assert conn.execute("SELECT action,object_type,object_id FROM admin_operations").fetchone()[:] == ("memory_revision","memory","1")
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert upgraded.setting("lifecycle")["forget_threshold"] == 20
        assert len(list(tmp_path.glob("v7.db.*.bak"))) == 1
    finally:
        upgraded.close()


def test_D03_delayed_resume_starts_forgetting_period_when_item_actually_executes(store):
    from iris.memory_ops import adjust_retention
    clock = Clock()
    mid = put(store, "长期停机后才处理的项目", importance=0)
    adjust_retention(store, mid, value=20, current=clock())
    maintenance = Maintenance(store, clock=clock)
    rid = maintenance.request()
    clock.advance(days=200)
    maintenance.run(rid)
    assert row(store, mid)["forgotten_at"] == clock().isoformat()
    clock.advance(days=1)
    run(store, clock)
    assert row(store, mid)["lifecycle"] == "forgotten"


def test_D04_non_overdue_start_does_not_create_a_catchup_the_following_day(store):
    clock = Clock("2026-10-08T01:00:00+08:00")
    run(store, clock)
    clock.advance(hours=3)
    maintenance = Maintenance(store, clock=clock)
    clock.advance(hours=22)
    assert maintenance.due() is None
    clock.advance(hours=1)
    assert maintenance.due() == ("scheduled", "2026-10-09")
