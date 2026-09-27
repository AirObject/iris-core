import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from iris.db import Store
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.memory_ops import adjust_retention, edit_memory
from iris.models import ModelError
from iris.queue import form_batch, get_batch, should_learn, truncate_material

from conftest import FakeGateway, batch, msg


def memory(content="小林喜欢猫", evidence=None, speaker="小林", about=None, stance="亲历"):
    return {"content": content, "type": "偏好", "about": about or ["小林"], "speaker": speaker,
            "stance": stance, "evidence": evidence or [1], "importance": 60}


def count(store, table):
    with store.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_B01_B02_independent_entries_and_trigger(store):
    msg(store, 1, "我喜欢猫", entry="A")
    msg(store, 1, "我喜欢狗", entry="B")
    msg(store, 2, "今天有空", entry="A")
    a = form_batch(store, "A", PROMPT_VERSION, target_count=2)
    assert a.target_ids and all(i != msg(store, 1, "我喜欢狗", entry="B") for i in a.target_ids)
    assert form_batch(store, "B", PROMPT_VERSION, target_count=2).target_ids
    current = datetime.now(timezone.utc)
    assert should_learn(12, current, current, current)
    assert not should_learn(1, current, current, current)


def test_B03_B04_B05_only_target_evidence_and_no_history_repeat(store):
    for i, text in enumerate(("我喜欢猫", "嗯", "我准备开店", "好的"), 1):
        msg(store, i, text)
    fake = FakeGateway({"memories": [memory("小林准备开店", [3])]})
    first, result = batch(store, fake)
    assert result["created"] == []
    assert result["dropped"][0]["reason"] == "no target-segment evidence"
    fake.response = {"memories": [memory("小林喜欢猫", [1])]}
    second, result = batch(store, fake)
    assert second.history_ids == first.target_ids
    assert result["created"] == []


def test_B06_B07_B08_frozen_batch_rolls_and_zero_memory_succeeds(store):
    for i in range(1, 4):
        msg(store, i, f"第{i}条")
    first = form_batch(store, "A", PROMPT_VERSION, target_count=2)
    msg(store, 4, "运行中到达")
    assert first.future_ids == [3]
    assert LearningEngine(store, FakeGateway({})).run_batch(first.id)["created"] == []
    assert get_batch(store, first.id).state == "succeeded"
    second = form_batch(store, "A", PROMPT_VERSION, target_count=2)
    assert second.history_ids == first.target_ids
    assert second.target_ids == [3, 4]


def test_B09_parse_fail_retries_gap_and_manual_relearn(store):
    msg(store, 1, "我喜欢猫")
    formed = form_batch(store, "A", PROMPT_VERSION)
    engine = LearningEngine(store, FakeGateway(ModelError("retryable", "JSON parse failed after repair")))
    for _ in range(3):
        assert engine.run_batch(formed.id, force=True)["state"] == "waiting"
    assert engine.run_batch(formed.id, force=True)["state"] == "abandoned"
    assert count(store, "memory_gaps") == 1
    from iris.queue import reset_batch
    reset_batch(store, formed.id)
    assert get_batch(store, formed.id).state == "waiting"
    assert LearningEngine(store, FakeGateway({})).run_batch(formed.id, force=True)["created"] == []
    assert count(store, "memory_gaps") == 0


def test_B10_content_refusal_clears_next_history(store):
    for i in range(1, 4):
        msg(store, i, f"消息{i}")
    first = form_batch(store, "A", PROMPT_VERSION, target_count=2)
    assert LearningEngine(store, FakeGateway(ModelError("content_rejection", "provider refusal"))).run_batch(first.id)["state"] == "refused"
    assert count(store, "memory_gaps") == 1
    second = form_batch(store, "A", PROMPT_VERSION, target_count=2)
    assert second.history_ids == [] and second.target_ids == [3]


def test_B11_standard_idle_tail_and_focus_function():
    current = datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert should_learn(1, current - timedelta(minutes=11), current - timedelta(minutes=10), current)
    assert should_learn(1, current - timedelta(minutes=2), current - timedelta(minutes=1), current, latest_content="别忘了")
    assert should_learn(1, current, current, current, manual=True)


def test_B12_long_message_is_truncated_only_in_material(store):
    content = "猫" * 5000
    message_id = msg(store, 1, content)
    fake = FakeGateway({})
    batch(store, fake)
    assert "已截断" in fake.materials[0]
    with store.read() as conn:
        assert conn.execute("SELECT content FROM messages WHERE id=?", (message_id,)).fetchone()[0] == content


def test_B13_crash_recovery_has_no_partial_result(tmp_path):
    path = tmp_path / "crash.db"
    store = Store(path)
    msg(store, 1, "我喜欢猫")
    formed = form_batch(store, "A", PROMPT_VERSION)
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            stamp = "2026-09-28T00:00:00+00:00"
            conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
                created_at,updated_at,first_confirmed_at,last_confirmed_at)
                VALUES('半写入','事实','self','观点',60,50,50,?,?,?,?)""", (stamp, stamp, stamp, stamp))
            raise RuntimeError("simulated crash before commit")
    assert count(store, "memories") == 0
    with store.write() as conn:
        conn.execute("UPDATE batches SET state='running' WHERE id=?", (formed.id,))
    store.close()
    reopened = Store(path)
    try:
        assert get_batch(reopened, formed.id).state == "waiting"
        assert get_batch(reopened, formed.id).attempt_count == 1
        assert count(reopened, "memories") == 0
        assert count(reopened, "batch_attempts") == 1
    finally:
        reopened.close()


def test_B13_crash_on_fourth_attempt_abandons_with_gap(tmp_path):
    path = tmp_path / "crash-fourth.db"
    store = Store(path)
    msg(store, 1, "我喜欢猫")
    formed = form_batch(store, "A", PROMPT_VERSION)
    with store.write() as conn:
        conn.execute("UPDATE batches SET state='running',attempt_count=3 WHERE id=?", (formed.id,))
    store.close()
    reopened = Store(path)
    try:
        assert get_batch(reopened, formed.id).state == "abandoned"
        assert count(reopened, "memory_gaps") == 1
    finally:
        reopened.close()


def test_B14_failed_then_success_writes_memory_once(store):
    msg(store, 1, "我喜欢猫")
    formed = form_batch(store, "A", PROMPT_VERSION)
    fake = FakeGateway([ModelError("retryable", "network"), {"memories": [memory()]}])
    engine = LearningEngine(store, fake)
    assert engine.run_batch(formed.id)["state"] == "waiting"
    assert len(engine.run_batch(formed.id, force=True)["created"]) == 1
    assert count(store, "memories") == 1


def test_B15_single_writer_handles_concurrent_changes(store):
    msg(store, 1, "我喜欢猫")
    _, result = batch(store, FakeGateway({"memories": [memory()]}))
    memory_id = result["created"][0]
    msg(store, 2, "A 的新消息")
    msg(store, 1, "B 的新消息", entry="B")
    a = form_batch(store, "A", PROMPT_VERSION)
    b = form_batch(store, "B", PROMPT_VERSION)
    def write(index):
        if index == 0:
            LearningEngine(store, FakeGateway({})).run_batch(a.id)
        elif index == 1:
            LearningEngine(store, FakeGateway({})).run_batch(b.id)
        elif index % 2:
            adjust_retention(store, memory_id, 1)
        else:
            with store.read() as conn:
                revision = conn.execute("SELECT revision FROM memories WHERE id=?", (memory_id,)).fetchone()[0]
            edit_memory(store, memory_id, revision, content=f"小林喜欢猫 {index}", actor="dream" if index == 2 else "admin")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(20)))
    assert count(store, "memories") == 1
    assert get_batch(store, a.id).state == "succeeded" and get_batch(store, b.id).state == "succeeded"


def test_B16_revision_conflict_skips_only_changed_update(store):
    msg(store, 1, "我喜欢猫")
    batch(store, FakeGateway({"memories": [memory()]}), count=1)
    msg(store, 2, "我现在喜欢狗")
    with store.read() as conn:
        memory_id = conn.execute("SELECT id FROM memories").fetchone()[0]
    def edit(_):
        edit_memory(store, memory_id, 1, content="管理员修正内容")
    output = {"updates": [{"ref": "M1", "action": "修正", "content": "小林喜欢狗", "evidence": [2]}],
              "memories": [memory("小林现在喜欢狗", [2])]}
    _, result = batch(store, FakeGateway(output, hook=edit), count=1)
    assert any(item["reason"] == "memory revision changed" for item in result["dropped"])
    assert result["created"]


def test_B17_retention_increment_does_not_block_update(store):
    msg(store, 1, "我喜欢猫")
    batch(store, FakeGateway({"memories": [memory()]}), count=1)
    msg(store, 2, "我现在喜欢狗")
    with store.read() as conn:
        memory_id = conn.execute("SELECT id FROM memories").fetchone()[0]
    def feedback(_):
        adjust_retention(store, memory_id, 8)
    output = {"updates": [{"ref": "M1", "action": "修正", "content": "小林现在喜欢狗", "evidence": [2]}]}
    _, result = batch(store, FakeGateway(output, hook=feedback), count=1)
    assert result["updated"] == [memory_id]
    with store.read() as conn:
        row = conn.execute("SELECT revision,retention FROM memories WHERE id=?", (memory_id,)).fetchone()
    assert row["revision"] == 2 and row["retention"] >= 62
