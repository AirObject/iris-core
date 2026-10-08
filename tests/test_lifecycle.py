"""Deterministic M01–M12, D01–D04, and R08/R09 lifecycle contracts."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import msg
from iris.memory_ops import (
    adjust_retention, confirm_retention, delete_memory, edit_memory,
    manage_memory, purge_memory, recreate_memory,
)
from iris.maintenance import Maintenance
from iris.retrieval import Retrieval
from test_retrieval import put


class Clock:
    def __init__(self, value="2026-10-08T04:00:00+08:00"):
        self.value = datetime.fromisoformat(value)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


def row(store, mid):
    with store.read() as conn:
        return dict(conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())


def configure(store, **values):
    store.set_setting("lifecycle", {**store.setting("lifecycle", {}), **values})


def run(store, clock=None):
    maintenance = Maintenance(store, clock=clock or Clock())
    rid = maintenance.request()
    maintenance.run(rid)
    return maintenance.report(rid)


def test_M01_M02_M03_M04_M05_R08_R09_feedback_and_hysteresis(store):
    mid = put(store, "天文摄影需要防潮")
    adjust_retention(store, mid, value=19)
    forgotten = row(store, mid)
    assert forgotten["lifecycle"] == "forgotten" and forgotten["forgotten_at"]
    retrieval = Retrieval(store)
    assert retrieval.search(text="天文摄影")["memories"] == []
    deep = retrieval.search(text="天文摄影", include_forgotten=True)
    assert deep["memories"][0]["lifecycle"] == "forgotten"
    assert row(store, mid) == forgotten
    retrieval.feedback(deep["recall_id"], [mid, mid])
    assert (row(store, mid)["retention"], row(store, mid)["lifecycle"]) == (27, "forgotten")
    assert retrieval.feedback(deep["recall_id"], [mid])["strengthened"] == []
    next_recall = retrieval.search(text="天文摄影", include_forgotten=True)
    assert edit_memory(store, mid, 1, content="天文摄影器材需要防潮")
    retrieval.feedback(next_recall["recall_id"], [mid])
    active = row(store, mid)
    assert (active["retention"], active["lifecycle"], active["forgotten_at"], active["revision"], active["belief"]) == (35, "active", None, 2, 70)
    adjust_retention(store, mid, -1)
    assert row(store, mid)["lifecycle"] == "active"
    adjust_retention(store, mid, value=20)
    assert row(store, mid)["lifecycle"] == "active"
    adjust_retention(store, mid, -1)
    assert row(store, mid)["lifecycle"] == "forgotten"


def test_shared_interface_confirmation_atomic_rollback_clamping_and_deleted_guard(store):
    mid = put(store, "我喜欢素描")
    adjust_retention(store, mid, value=19)
    configure(store, confirmation_increment=16)
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            confirm_retention(store, mid, _conn=conn)
            assert conn.execute("SELECT lifecycle FROM memories WHERE id=?", (mid,)).fetchone()[0] == "active"
            raise RuntimeError("rollback")
    assert row(store, mid)["retention"] == 19
    confirm_retention(store, mid)
    assert row(store, mid)["retention"] == 35
    assert row(store, mid)["revision"] == 1
    adjust_retention(store, mid, 1000)
    assert row(store, mid)["retention"] == 100
    adjust_retention(store, mid, -1000)
    assert row(store, mid)["retention"] == 0
    assert delete_memory(store, mid, 1)
    assert adjust_retention(store, mid, 100) is None
    assert row(store, mid)["lifecycle"] == "deleted"


def test_M07_deleted_feedback_is_all_or_nothing_and_expiry_stays_24_hours(store):
    ids = [put(store, "摄影需要光线"), put(store, "摄影需要耐心")]
    retrieval = Retrieval(store)
    recall = retrieval.search(text="摄影")
    assert delete_memory(store, ids[0], 1)
    with pytest.raises(KeyError):
        retrieval.feedback(recall["recall_id"], ids)
    assert row(store, ids[1])["retention"] == 50
    assert ids[0] not in [m["id"] for m in retrieval.search(text="摄影", include_forgotten=True)["memories"]]
    with store.write() as conn:
        conn.execute("UPDATE recalls SET created_at=? WHERE id=?", ((datetime.now(timezone.utc)-timedelta(hours=25)).isoformat(), recall["recall_id"]))
    with pytest.raises(ValueError, match="expired"):
        retrieval.feedback(recall["recall_id"], [ids[1]])


def test_manual_pin_forget_restore_scores_and_revision_contract(store):
    mid = put(store, "我喜欢素描", importance=30)
    assert not manage_memory(store, mid, 9, pinned=True)
    assert manage_memory(store, mid, 1, pinned=True)
    assert row(store, mid)["revision"] == 1
    assert manage_memory(store, mid, 1, action="forget")
    assert row(store, mid)["retention"] == 19
    run(store)
    assert row(store, mid)["retention"] == 19
    assert row(store, mid)["lifecycle"] == "forgotten"
    assert manage_memory(store, mid, 1, action="restore")
    assert row(store, mid)["retention"] == 35
    assert manage_memory(store, mid, 1, importance=70, retention=10)
    assert row(store, mid)["revision"] == 2
    assert row(store, mid)["lifecycle"] == "forgotten"
    assert manage_memory(store, mid, 2, pinned=False)
    assert row(store, mid)["revision"] == 2


def test_M12_decay_counts_only_eligible_actual_visits_and_pinned_never_decays(store):
    low = put(store, "低重要度", importance=39)
    medium = put(store, "中重要度", importance=40)
    high = put(store, "高重要度", importance=70)
    pinned = put(store, "置顶", importance=0)
    manage_memory(store, pinned, 1, pinned=True)
    clock = Clock()
    run(store, clock)
    clock.advance(days=40)
    run(store, clock)
    assert [row(store, i)["retention"] for i in (low, medium, high, pinned)] == [48, 50, 50, 50]
    run(store, clock)
    assert row(store, medium)["retention"] == 49
    manage_memory(store, medium, 1, pinned=True)
    for _ in range(3):
        run(store, clock)
    manage_memory(store, medium, 1, pinned=False)
    run(store, clock)
    assert row(store, medium)["retention"] == 49
    assert row(store, low)["revision"] == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_M06_expiry_waits_until_unpinned_and_respects_toggle(store, enabled):
    mid = put(store, "遗忘信息", importance=70)
    clock = Clock()
    adjust_retention(store, mid, value=19, current=clock())
    manage_memory(store, mid, 1, pinned=True)
    configure(store, auto_delete_enabled=enabled)
    clock.advance(days=180)
    report = run(store, clock)
    assert row(store, mid)["lifecycle"] == "forgotten"
    assert not report["summary"]["deleted"]["memory_ids"]
    manage_memory(store, mid, 1, pinned=False)
    report = run(store, clock)
    assert row(store, mid)["lifecycle"] == ("deleted" if enabled else "forgotten")
    assert bool(report["summary"].get("deleted", {}).get("memory_ids")) == enabled


def test_M09_recreate_revision_uses_new_id_and_copies_evidence_without_reviving_old(store):
    evidence = msg(store, 1, "原始证据")
    mid = put(store, "旧正文", evidence=[evidence])
    assert edit_memory(store, mid, 1, content="新正文")
    assert delete_memory(store, mid, 2)
    created = recreate_memory(store, mid, 3, source_revision=1)
    assert created > mid
    assert row(store, created)["content"] == "旧正文"
    assert row(store, created)["retention"] == 54
    assert row(store, created)["lifecycle"] == "active"
    assert row(store, mid)["lifecycle"] == "deleted"
    with store.read() as conn:
        assert conn.execute("SELECT message_id FROM sources WHERE memory_id=?", (created,)).fetchone()[0] == evidence


def test_M10_purge_removes_history_exclusive_sources_and_reserves_largest_id(store):
    exclusive = msg(store, 1, "仅此记忆的正文")
    shared = msg(store, 2, "共享来源")
    other = put(store, "其他记忆", evidence=[shared])
    mid = put(store, "私密记忆", evidence=[exclusive, shared])
    assert edit_memory(store, mid, 1, content="私密修订")
    with pytest.raises(ValueError, match="confirmation"):
        purge_memory(store, mid, 2, confirm=False)
    result = purge_memory(store, mid, 2, confirm=True)
    assert result["deleted_message_ids"] == [exclusive]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?", (mid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE memory_id=?", (mid,)).fetchone()[0] == 0
        assert conn.execute("SELECT 1 FROM messages WHERE id=?", (exclusive,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM messages WHERE id=?", (shared,)).fetchone()
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert row(store, other)["lifecycle"] == "active"
    new = put(store, "以后重新学到的信息")
    assert new > mid
    assert row(store, mid)["content"] == ""
    assert row(store, mid)["purged_at"]


def test_M11_loss_event_survives_restore_and_each_pair_charged_once_with_cycles(store):
    a = put(store, "依据", importance=70)
    b = put(store, "推断", importance=70)
    with store.write() as conn:
        for child, parent in ((b, a), (a, b), (b, a)):
            conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)", (child, parent, Clock()().isoformat()))
    adjust_retention(store, a, value=19)
    manage_memory(store, a, 1, action="restore")
    run(store)
    assert row(store, b)["retention"] == 40
    assert row(store, b)["belief"] == 70 and row(store, b)["revision"] == 1
    delete_memory(store, a, 1)
    run(store)
    assert row(store, b)["retention"] == 40
    adjust_retention(store, b, value=19)
    run(store)
    assert row(store, a)["lifecycle"] == "deleted"


def test_D03_restart_after_committed_item_cannot_decay_twice(store, monkeypatch):
    ids = [put(store, "记忆甲", importance=0), put(store, "记忆乙", importance=0)]
    maintenance = Maintenance(store, clock=Clock())
    rid = maintenance.request()
    original = maintenance._process_item
    count = 0
    def interrupted(*args, **kwargs):
        nonlocal count
        result = original(*args, **kwargs)
        count += 1
        if count == 1:
            raise KeyboardInterrupt()
        return result
    monkeypatch.setattr(maintenance, "_process_item", interrupted)
    with pytest.raises(KeyboardInterrupt):
        maintenance.run(rid)
    restarted = Maintenance(store, clock=Clock())
    assert restarted.request() == rid
    restarted.run(rid)
    assert [row(store, i)["retention"] for i in ids] == [49, 49]
    assert restarted.report(rid)["state"] == "completed"
    restarted.run(rid)
    assert [row(store, i)["retention"] for i in ids] == [49, 49]


def test_D02_snapshot_revision_conflict_is_reported_and_retried_next_run(store, monkeypatch):
    mid = put(store, "原正文", importance=0)
    maintenance = Maintenance(store, clock=Clock())
    original = maintenance._process_item
    def edit_before_write(run_row, phase, candidate):
        if phase == "decay":
            edit_memory(store, mid, 1, content="人工新正文")
        return original(run_row, phase, candidate)
    monkeypatch.setattr(maintenance, "_process_item", edit_before_write)
    rid = maintenance.request()
    maintenance.run(rid)
    assert row(store, mid)["retention"] == 50
    assert maintenance.report(rid)["summary"]["skipped"]["reasons"]["revision_conflict"] == 1
    run(store)
    assert row(store, mid)["retention"] == 49


def test_M08_learning_same_information_after_delete_creates_different_id(store):
    from conftest import FakeGateway, batch
    from test_batches import memory
    msg(store, 1, "我喜欢猫")
    _, learned = batch(store, FakeGateway({"memories": [memory()]}), count=1)
    old = learned["created"][0]
    assert delete_memory(store, old, 1)
    msg(store, 2, "我喜欢猫")
    _, learned = batch(store, FakeGateway({"memories": [memory(evidence=[2])]}), count=1)
    assert learned["created"] and learned["created"][0] > old
    assert row(store, old)["lifecycle"] == "deleted"


def test_purge_preserves_batch_goal_subject_evidence_and_incoming_dependency(store):
    from iris.queue import form_batch
    source = msg(store, 1, "冻结的来源")
    formed = form_batch(store, "A", "v6")
    parent = put(store, "依据正文", evidence=[source])
    child = put(store, "派生正文", importance=70)
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)", (child,parent,Clock()().isoformat()))
    purged = purge_memory(store, parent, 1, confirm=True)
    assert purged["deleted_message_ids"] == []
    assert purged["retained_messages"] == [{"message_id":source,"reasons":["batch_segment"]}]
    run(store)
    assert row(store, child)["retention"] == 40
    with store.read() as conn:
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert conn.execute("SELECT content FROM messages WHERE id=?",(source,)).fetchone()[0] == "冻结的来源"


def test_dependency_cycle_cascades_once_per_pair_and_does_not_charge_unrelated_memory(store):
    a = put(store,"相互支持甲",importance=70)
    b = put(store,"相互支持乙",importance=70)
    unrelated = put(store,"相似但无依赖",importance=70)
    with store.write() as conn:
        for child,parent in ((a,b),(b,a)):
            conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,created_at) VALUES(?,'memory',?,?)",(child,parent,Clock()().isoformat()))
    adjust_retention(store,a,value=19)
    adjust_retention(store,b,value=25)
    report=run(store)
    assert [row(store,i)["retention"] for i in (a,b,unrelated)] == [9,15,50]
    assert report["summary"]["dependencies_weakened"]["count"] == 2
    run(store)
    assert [row(store,i)["retention"] for i in (a,b)] == [9,15]


def test_ordinary_prepare_excludes_forgotten_highlights(store):
    mid = msg(store,1,"晚上好")
    with store.read() as conn:
        person = conn.execute("SELECT sender_subject_id FROM messages WHERE id=?",(mid,)).fetchone()[0]
    memory_id = put(store,"职业是摄影师",about=[person],speaker=person,importance=80)
    adjust_retention(store,memory_id,value=19)
    result=Retrieval(store).prepare("A",text="",participants=[person])
    assert result["memories"] == []
