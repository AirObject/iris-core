import json

from conftest import FakeGateway, batch, learning_output, msg
from iris.learning import PROMPT, PROMPT_VERSION


def memory(content, about, *, stance="亲历", speaker="P1"):
    return dict(content=content, about=about, speaker=speaker, stance=stance,
                type="事实", evidence=[1], tags=["P1 大鹏", "P1的约定"])


def test_temporary_participant_numbers_removed_after_validation_and_raw_preserved(store):
    msg(store, 1, "我带望远镜，和大家周六见", sender="大鹏")
    output = {"memories": [memory("P1 大鹏会带望远镜，P1的设备编号为XP10", ["P1"])]}
    _, result = batch(store, FakeGateway(output), count=1)
    with store.read() as conn:
        row = conn.execute("SELECT content,speaker_subject_id FROM memories").fetchone()
        tags = {r[0] for r in conn.execute("SELECT tag FROM memory_tags")}
        raw = json.loads(conn.execute("SELECT raw_output FROM batch_attempts").fetchone()[0])
        speaker = conn.execute("SELECT sender_subject_id FROM messages").fetchone()[0]
    assert row[0] == "大鹏会带望远镜，大鹏的设备编号为XP10"
    assert tags == {"大鹏", "大鹏的约定"}
    assert row[1] == speaker
    assert raw == learning_output(output)
    assert any(n["field"] == "content" for n in result["normalizations"])


def test_other_person_inference_does_not_become_self_knowledge(store):
    msg(store, 1, "这几天接了三份工作", sender="大鹏")
    item = memory("我根据工作数量推断大鹏最近很忙", ["P1"], speaker="我", stance="推断")
    batch(store, FakeGateway({"memories": [item]}), count=1)
    with store.read() as conn:
        assert "self" not in {r[0] for r in conn.execute("SELECT subject_id FROM memory_subjects")}


def test_feedback_inference_about_self_preserves_explicit_self(store):
    msg(store, 1, "你这场直播讲得太快了", sender="大鹏")
    item = memory("大鹏对我这场直播的反馈表明我讲得太快", ["我", "P1"], speaker="我", stance="推断")
    batch(store, FakeGateway({"memories": [item]}), count=1)
    with store.read() as conn:
        assert "self" in {r[0] for r in conn.execute("SELECT subject_id FROM memory_subjects")}


def test_distinct_facts_stay_separate_without_inferred_gender(store):
    assert PROMPT_VERSION == "learning_v7"
    assert "不推断性别" in PROMPT and "不要为避免重复自行加“他”“她”" in PROMPT
    assert "不同命题分开" in PROMPT and "不同人的独立观点" in PROMPT
    msg(store, 1, "我带望远镜，活动在周六下午三点", sender="大鹏")
    items = [memory("大鹏带望远镜", ["P1"]), memory("观星活动在周六下午三点", ["P1"])]
    _, result = batch(store, FakeGateway({"memories": items}), count=1)
    assert len(result["created"]) == 2


def test_update_body_cannot_persist_batch_participant_number(store):
    msg(store, 1, "我带望远镜", sender="大鹏")
    batch(store, FakeGateway({"memories": [memory("大鹏带望远镜", ["P1"])]}), count=1)
    msg(store, 2, "改带相机", sender="大鹏")
    _, result = batch(store, FakeGateway({"updates": [{"ref": "M1", "action": "修正",
                          "content": "P1 大鹏改带相机", "evidence": [2]}]}), count=1)
    assert len(result["updated"]) == 1
    with store.read() as conn:
        assert conn.execute("SELECT content FROM memories").fetchone()[0] == "大鹏改带相机"
