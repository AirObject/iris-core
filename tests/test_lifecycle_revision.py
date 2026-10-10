"""Planner's second review: cleanup, compact progress, and pinned isolation."""
import json
from datetime import timedelta

import pytest

from conftest import FakeGateway, msg
from iris.learning import LearningEngine
from iris.maintenance import Maintenance
from iris.memory_ops import adjust_retention, manage_memory, purge_memory
from iris.queue import form_batch, get_batch
from test_admin_batches import client, gap, terminal_batch
from test_lifecycle import Clock, configure, row, run
from test_retrieval import put


@pytest.mark.parametrize("state", ["waiting", "running", "succeeded", "abandoned", "refused"])
def test_only_unfinished_batch_segments_protect_old_messages(store, state):
    ids = [msg(store, i, "过期三段消息") for i in range(1, 4)]
    clock = Clock()
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='refused',received_at=?", ((clock()-timedelta(days=31)).isoformat(),))
        conn.execute("""INSERT INTO batches(entry_id,history_ids,target_ids,future_ids,state,prompt_version,created_at)
            VALUES('A',?,?,?,?, 'v6',?)""", (*(json.dumps([i]) for i in ids), state, clock().isoformat()))
    report = run(store, clock)
    with store.read() as conn:
        remaining = [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY id")]
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert remaining == (ids if state in ("waiting", "running") else [])
    assert report["summary"]["checked"]["by_phase"]["messages"] == 3


def test_trial_reply_dedupe_key_does_not_protect_original_or_reply(store):
    original = msg(store, 1, "原消息")
    reply = msg(store, 2, "试用回复", kind="self_output")
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='learned',received_at=?", ((Clock()()-timedelta(days=31)).isoformat(),))
        conn.execute("UPDATE messages SET dedupe_key=? WHERE id=?", (f"trial-reply:{original}", reply))
    report = run(store)
    assert report["summary"]["messages_deleted"]["object_ids"] == [original, reply]
    new = msg(store, 3, "新消息不能复用已清理的编号")
    assert new > reply


@pytest.mark.parametrize("state", ["abandoned", "refused"])
def test_cleared_target_has_placeholder_and_relearn_409_keeps_gap(client, store, state):
    batch = terminal_batch(store, state)
    attempt_count = get_batch(store, batch.id).attempt_count
    before = gap(store)
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at=?", ((Clock()()-timedelta(days=31)).isoformat(),))
    run(store)
    later = msg(store, 2, "后来的消息")
    assert later > max(batch.target_ids)
    response = client.get(f"/admin/api/batches/{batch.id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["segments"]["target"] == [{"id": batch.target_ids[0], "missing": True, "content": "已清理"}]
    assert not detail["can_relearn"] and "已清理" in detail["relearn_blocked_reason"]
    response = client.post(f"/admin/api/batches/{batch.id}/relearn", json={})
    assert response.status_code == 409 and "已清理" in response.json()["error"]["message"]
    assert response.json()["error"]["code"] == "batch_targets_cleared"
    assert get_batch(store, batch.id).attempt_count == attempt_count
    assert gap(store) == before
    with store.read() as conn:
        assert conn.execute("SELECT state FROM batches WHERE id=?", (batch.id,)).fetchone()[0] == state
        assert not conn.execute("SELECT 1 FROM admin_operations WHERE action='batch_relearn'").fetchone()


def test_automatic_retry_skips_batch_whose_targets_were_cleared(store):
    batch = terminal_batch(store, "abandoned")
    clock = Clock()
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at=?", ((clock()-timedelta(days=31)).isoformat(),))
        conn.execute("UPDATE batches SET finished_at=?", ((clock()-timedelta(days=1)).isoformat(),))
        # Already cleared before this run (e.g. while automatic retry was disabled).
        conn.execute("DELETE FROM messages")
    report = run(store, clock)
    with store.read() as conn:
        assert conn.execute("SELECT state FROM batches WHERE id=?", (batch.id,)).fetchone()[0] == "abandoned"
        assert not conn.execute("SELECT 1 FROM maintenance_batch_retries").fetchone()
    assert report["summary"]["skipped"]["reasons"]["target_messages_cleared"] == 1
    assert gap(store)


def test_purge_uses_finished_batch_rule_and_preserves_attempt_and_call_records(store):
    source = msg(store, 1, "原始来源")
    batch = form_batch(store, "A", "v6")
    memory = put(store, "待清除", evidence=[source])
    clock = Clock()
    with store.write() as conn:
        conn.execute("UPDATE batches SET state='succeeded' WHERE id=?", (batch.id,))
        conn.execute("""INSERT INTO batch_attempts(batch_id,number,started_at,finished_at,raw_output,parse_status,duration_ms)
            VALUES(?,1,?,?,'模型原始正文','direct',1)""", (batch.id,clock().isoformat(),clock().isoformat()))
        conn.execute("""INSERT INTO model_calls(purpose,model,duration_ms,result_category,error_summary,created_at)
            VALUES('learning','fake',1,'success','已有调用内容',?)""", (clock().isoformat(),))
    result = purge_memory(store, memory, 1, confirm=True)
    assert result["deleted_message_ids"] == [source]
    assert result["retained_messages"] == []
    with store.read() as conn:
        assert conn.execute("SELECT raw_output FROM batch_attempts").fetchone()[0] == "模型原始正文"
        assert conn.execute("SELECT error_summary FROM model_calls").fetchone()[0] == "已有调用内容"
    assert msg(store, 2, "新来源") > source


def test_noop_runs_store_counts_but_no_per_object_journal(store):
    for i in range(1, 41):
        evidence = msg(store, i, "受记忆来源保护的过期消息")
        put(store, "高重要度不衰减", importance=70, evidence=[evidence])
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='learned',received_at=?", ((Clock()()-timedelta(days=31)).isoformat(),))
    for _ in range(2):
        report = run(store)
        assert report["items"] == []
        assert report["summary"]["checked"]["count"] == 80
        assert report["summary"]["checked"]["by_phase"] == {"decay": 40, "expiry": 0, "dependency": 0, "messages": 40, "retry": 0, "goals": 0, "media": 0}
        assert report["summary"]["skipped"]["reasons"]["referenced"] == 40
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM maintenance_items").fetchone()[0] == 0


def test_cursor_resumes_unlogged_counter_advance_and_rollback(store, monkeypatch):
    ids = [put(store, "中等重要度", importance=50) for _ in range(2)]
    worker = Maintenance(store, clock=Clock())
    rid = worker.request()
    original = worker._process_item
    def interrupt(*args):
        original(*args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(worker, "_process_item", interrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.run(rid)
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM maintenance_items").fetchone()[0] == 0
    resumed = Maintenance(store, clock=Clock())
    resumed.run(rid)
    assert [row(store, mid)["decay_visits"] for mid in ids] == [1, 1]
    assert resumed.report(rid)["summary"]["checked"]["count"] == 2
    run(store)
    run(store)
    assert [row(store, mid)["retention"] for mid in ids] == [49, 49]


def test_forgotten_strength_and_counter_do_not_decay_during_retention_period(store):
    mid = put(store, "遗忘后两次使用可恢复", importance=0)
    adjust_retention(store, mid, value=20, current=Clock()())
    run(store)
    forgotten = row(store, mid)
    assert forgotten["retention"] == 19 and forgotten["lifecycle"] == "forgotten"
    for _ in range(10):
        run(store)
    assert row(store, mid) == forgotten


def test_pinned_ignores_threshold_decay_expiry_and_dependency_until_unpinned(store):
    parent = put(store, "依据", importance=70)
    child = put(store, "置顶的派生", importance=0)
    pinned_forgotten = put(store, "已遗忘后置顶", importance=0)
    clock = Clock()
    adjust_retention(store, pinned_forgotten, value=19, current=clock()-timedelta(days=200))
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,created_at) VALUES(?,'memory',?,?)", (child,parent,clock().isoformat()))
    for mid in (child,pinned_forgotten):
        manage_memory(store, mid, 1, pinned=True)
    adjust_retention(store, parent, value=19, current=clock())
    before = [row(store, mid) for mid in (child,pinned_forgotten)]
    for _ in range(2):
        run(store,clock)
    assert [row(store, mid) for mid in (child,pinned_forgotten)] == before
    manage_memory(store, child, 1, pinned=False)
    run(store, clock)
    assert row(store, child)["retention"] == 39  # one decay and one delayed loss
    assert row(store, pinned_forgotten) == before[1]


def test_manual_forgetting_unpins_and_restoring_does_not_repin(store):
    mid = put(store, "手动遗忘置顶内容", importance=0)
    manage_memory(store, mid, 1, pinned=True)
    manage_memory(store, mid, 1, action="forget")
    assert not row(store,mid)["pinned"]
    manage_memory(store, mid, 1, action="restore")
    assert not row(store,mid)["pinned"] and row(store,mid)["retention"] == 35


def test_upcoming_deletion_omits_pinned_forgotten(client, store):
    from datetime import datetime, timezone
    mid = put(store, "置顶遗忘不将删除")
    adjust_retention(store, mid, value=19, current=datetime.now(timezone.utc)-timedelta(days=200))
    manage_memory(store, mid, 1, pinned=True)
    assert client.get('/admin/api/memories/upcoming-deletion').json()['items'] == []


def test_new_learning_after_history_cleanup_has_no_missing_context_failure(store):
    source = msg(store, 1, "历史消息")
    first = form_batch(store, "A", "v6")
    LearningEngine(store, FakeGateway()).run_batch(first.id)
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at=? WHERE id=?", ((Clock()()-timedelta(days=31)).isoformat(),source))
    run(store)
    msg(store, 2, "新一批消息")
    second = form_batch(store, "A", "v6")
    LearningEngine(store, FakeGateway()).run_batch(second.id)
    assert get_batch(store, second.id).state == "succeeded"


def test_learning_with_cleared_context_and_intact_target(store):
    history = msg(store, 1, "将清理的旧历史")
    first = form_batch(store, "A", "v6")
    LearningEngine(store, FakeGateway()).run_batch(first.id)
    msg(store, 2, "完好的目标消息")
    second = form_batch(store, "A", "v6")
    # Reproduces a relearn with an already-cleared context (target is intact).
    with store.write() as conn:
        conn.execute("DELETE FROM messages WHERE id=?", (history,))
    LearningEngine(store, FakeGateway()).run_batch(second.id)
    assert get_batch(store, second.id).state == "succeeded"



@pytest.mark.parametrize("segment,label,cleared_index", [
    ("history", "历史段（仅供理解）", 1),
    ("future", "后续段（仅供理解）", 3),
])
def test_material_shortens_only_cleared_context(store, segment, label, cleared_index):
    contents = ["保留的历史", "将被清理的历史", "完整的目标", "将被清理的后续", "保留的后续"]
    ids = [msg(store, i+1, text, sender=f"参与人{i+1}") for i, text in enumerate(contents[:2])]
    first = form_batch(store, "A", "v6")
    engine = LearningEngine(store, FakeGateway())
    engine.run_batch(first.id)
    ids += [msg(store, i+1, contents[i], sender=f"参与人{i+1}") for i in range(2, 5)]
    batch = form_batch(store, "A", "v6", target_count=1, future_count=2, history_count=2)
    before, _, _ = engine._material(batch, engine._snapshot(batch), [])
    with store.write() as conn:
        conn.execute("DELETE FROM messages WHERE id=?", (ids[cleared_index],))
    snapshot = engine._snapshot(batch)
    material, numbers, _ = engine._material(batch, snapshot, [])
    def section(text):
        return text.split(f"—— {label} ——\n", 1)[1].split("\n—— ", 1)[0]
    assert len(section(before).splitlines()) == 2
    assert len(section(material).splitlines()) == 1
    assert contents[cleared_index] not in material and contents[2] in material
    assert f"参与人{cleared_index+1}" not in material
    assert list(numbers) == [1, 2, 3, 4]
    assert list(numbers.values()) == ids[:cleared_index] + ids[cleared_index+1:]
    assert get_batch(store, batch.id) == batch  # Frozen ranges are not rewritten.
    assert len(getattr(batch, f"{segment}_ids")) == 2


def test_material_does_not_silently_skip_a_missing_target(store):
    missing = msg(store, 1, "缺失的目标")
    msg(store, 2, "仍在的目标")
    batch = form_batch(store, "A", "v6")
    with store.write() as conn:
        conn.execute("DELETE FROM messages WHERE id=?", (missing,))
    engine = LearningEngine(store, FakeGateway())
    with pytest.raises(KeyError) as exc:
        engine._material(batch, engine._snapshot(batch), [])
    assert exc.value.args == (missing,)


def test_cursor_and_unlogged_visit_roll_back_together(store, monkeypatch):
    mid = put(store, "计数也必须回滚", importance=50)
    worker = Maintenance(store, clock=Clock())
    rid = worker.request()
    original = worker._record
    def interrupted_inside_transaction(*args):
        original(*args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(worker, '_record', interrupted_inside_transaction)
    with pytest.raises(KeyboardInterrupt):
        worker.run(rid)
    assert row(store, mid)['decay_visits'] == 0
    with store.read() as conn:
        assert conn.execute('SELECT cursor_id FROM maintenance_runs WHERE id=?', (rid,)).fetchone()[0] == 0
    restarted = Maintenance(store, clock=Clock())
    restarted.run(rid)
    assert row(store, mid)['decay_visits'] == 1
    assert restarted.report(rid)['items'] == []
    assert restarted.report(rid)['summary']['checked']['count'] == 1


def test_relearn_winning_cleanup_race_protects_target(store, monkeypatch):
    from iris.queue import reset_batch
    batch = terminal_batch(store, 'abandoned')
    with store.write() as conn:
        conn.execute('UPDATE messages SET received_at=?', ((Clock()()-timedelta(days=31)).isoformat(),))
    worker = Maintenance(store, clock=Clock())
    rid = worker.request()
    original = worker._process_item
    def relearn_before_cleanup(run_row, phase, candidate):
        if phase == 'messages':
            reset_batch(store, batch.id)
        return original(run_row, phase, candidate)
    monkeypatch.setattr(worker, '_process_item', relearn_before_cleanup)
    worker.run(rid)
    with store.read() as conn:
        assert conn.execute('SELECT id FROM messages WHERE id=?', (batch.target_ids[0],)).fetchone()
        assert conn.execute('SELECT state FROM batches WHERE id=?', (batch.id,)).fetchone()[0] == 'waiting'
    assert worker.report(rid)['summary']['skipped']['reasons']['no_longer_eligible'] == 1


def test_pinning_between_selection_and_write_prevents_expiry(store, monkeypatch):
    mid = put(store, '到期候选被人工置顶', importance=70)
    adjust_retention(store, mid, value=19, current=Clock()()-timedelta(days=181))
    worker = Maintenance(store, clock=Clock())
    original = worker._process_item
    def pin_before_expiry(run_row, phase, candidate):
        if phase == 'expiry':
            manage_memory(store, mid, 1, pinned=True)
        return original(run_row, phase, candidate)
    monkeypatch.setattr(worker, '_process_item', pin_before_expiry)
    rid = worker.request()
    worker.run(rid)
    assert row(store, mid)['lifecycle'] == 'forgotten' and row(store, mid)['pinned']
    assert worker.report(rid)['summary']['deleted']['count'] == 0


@pytest.mark.parametrize('pinned', [False, True])
def test_new_restore_threshold_rechecks_forgotten_without_decay_or_visits(store, pinned):
    mid = put(store, '调低恢复阈值', importance=50)
    adjust_retention(store, mid, value=19, current=Clock()())
    adjust_retention(store, mid, 8, current=Clock()())
    manage_memory(store, mid, 1, pinned=pinned)
    configure(store, restore_threshold=25)
    run(store)
    result = row(store, mid)
    assert result['retention'] == 27 and result['decay_visits'] == 0
    assert result['lifecycle'] == ('forgotten' if pinned else 'active')
