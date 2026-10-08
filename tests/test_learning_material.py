import copy
import json

import pytest

from iris.learning import LearningEngine, PROMPT_VERSION
from iris.queue import form_batch

from conftest import FakeGateway, batch, msg
from test_batches import memory


@pytest.mark.parametrize(("zone", "display"), [
    (None, "2026-10-08 周四 00:30"),
    ("Asia/Shanghai", "2026-10-08 周四 00:30"),
    ("UTC", "2026-10-07 周三 16:30"),
    ("America/Los_Angeles", "2026-10-07 周三 09:30"),
])
def test_material_uses_role_timezone_without_changing_format_or_stored_time(store, zone, display):
    if zone is not None:
        store.set_setting("timezone", zone)
    occurred_at = "2026-10-07T16:30:00+00:00"
    msg(store, 1, "今天开始在宁波工作", at=occurred_at)
    fake = FakeGateway({"memories": [{**memory("小林开始在宁波工作"), "event_time": "今天"}]})
    _, result = batch(store, fake, count=1)
    assert result["created"]
    assert fake.materials == ["\n".join([
        "角色与 persona（数据）：", "名字：Iris", "", "参与者：", 'P1 小林；定位数据（非人物属性）：{"platform": "test", "account_id": "A:小林"}',
        "相关已有记忆（数据）：", "—— 历史段（仅供理解） ——", "—— 目标段（只从这里学习） ——",
        f"#1 [{display}] [他人消息] P1 小林：数据：今天开始在宁波工作",
        "—— 后续段（仅供理解） ——",
    ])]
    with store.read() as conn:
        assert conn.execute("SELECT occurred_at FROM messages").fetchone()[0] == occurred_at
        assert conn.execute("SELECT event_time FROM memories").fetchone()[0] == display[:10]


def test_timezone_change_applies_to_history_target_and_future_on_same_engine(store):
    msg(store, 1, "今天开始在宁波工作", at="2026-10-07T16:30:00+00:00")
    batch(store, FakeGateway({}), count=1)
    msg(store, 2, "我记住了", sender="我", kind="self_output", at="2026-10-07T16:31:00+00:00")
    msg(store, 3, "门铃响了", kind="event", at="2026-10-07T16:32:00+00:00")
    formed = form_batch(store, "A", PROMPT_VERSION, target_count=1, history_count=1, future_count=1)
    assert formed.history_ids and formed.target_ids and formed.future_ids
    engine = LearningEngine(store, FakeGateway({}))
    snapshot = engine._snapshot(formed)
    store.set_setting("timezone", "Asia/Shanghai")
    shanghai, numbers, refs = engine._material(formed, snapshot, [])
    for number, minute in enumerate((30, 31, 32), 1):
        assert f"#{number} [2026-10-08 周四 00:{minute}]" in shanghai
    store.set_setting("timezone", "America/Los_Angeles")
    los_angeles, new_numbers, new_refs = engine._material(formed, snapshot, [])
    assert los_angeles == shanghai.replace("2026-10-08 周四 00:", "2026-10-07 周三 09:")
    assert (new_numbers, new_refs) == (numbers, refs)


ANNOTATION = "（说话人 我（用户）；相信 90）"
CLEAN_CONTENT = "我（用户）周四去宁波（带资料）。"


def seed_user_memory(store):
    msg(store, 1, "我周四去上海出差", sender="我（用户）")
    _, result = batch(store, FakeGateway({"memories": [
        memory("我（用户）周四去上海出差", speaker="P1", about=["P1"]),
    ]}), count=1)
    return result["created"][0]


@pytest.mark.parametrize(("section", "action"), [
    ("memories", None), ("updates", "修正"), ("updates", "反驳"),
])
@pytest.mark.parametrize("content", [
    ANNOTATION + CLEAN_CONTENT,
    "我（用户）" + ANNOTATION + "周四去宁波（带资料）。",
    CLEAN_CONTENT + ANNOTATION,
    ANNOTATION + CLEAN_CONTENT + "（说话人 小林；相信 0）",
    "（说话人 我（用户（本机））；相信 100）" + CLEAN_CONTENT,
])
def test_copied_memory_annotations_are_removed_before_writing_and_audited(store, section, action, content):
    original_id = seed_user_memory(store)
    msg(store, 2, "改到宁波，要带资料", sender="我（用户）")
    if section == "memories":
        item = memory(content, [2], "P1", ["P1"])
    else:
        item = {"ref": "M1", "action": action, "content": content, "evidence": [2]}
    output = {section: [item]}
    original_output = copy.deepcopy(output)
    formed, result = batch(store, FakeGateway(output), count=1)
    assert not result["dropped"]
    if section == "memories":
        assert len(result["created"]) == 1
        memory_id = result["created"][0]
    else:
        assert result["updated"] == [original_id]
        memory_id = original_id
    with store.read() as conn:
        assert conn.execute("SELECT content FROM memories WHERE id=?", (memory_id,)).fetchone()[0] == CLEAN_CONTENT
        raw = conn.execute("SELECT raw_output FROM batch_attempts WHERE batch_id=?", (formed.id,)).fetchone()[0]
        saved = json.loads(conn.execute("SELECT result_json FROM batches WHERE id=?", (formed.id,)).fetchone()[0])
        if section == "updates":
            revision = json.loads(conn.execute(
                "SELECT after_json FROM memory_revisions WHERE memory_id=?", (memory_id,)).fetchone()[0])
            assert revision["content"] == CLEAN_CONTENT
    assert raw == json.dumps(original_output, ensure_ascii=False)
    assert output == original_output
    notes = [note for note in result["normalizations"] if note["field"] == "content"]
    assert len(notes) == 1
    assert notes[0]["section"] == section and notes[0]["index"] == 0
    assert notes[0]["before"] == content and notes[0]["after"] == CLEAN_CONTENT
    assert notes[0]["reason"]
    assert saved["normalizations"] == result["normalizations"]


@pytest.mark.parametrize("content", [
    "我（用户）周四去宁波（带资料），相信这次能完成。",
    "（说话人 我（用户））只表明是谁说话；（相信 90）只提相信程度。",
    "（说话人 我（用户）；相信 很多）是原话。",
    "（讨论记录（保留内部括号））和 (普通括号)。",
])
@pytest.mark.parametrize("section", ["memories", "updates"])
def test_other_parenthetical_content_is_unchanged(section, content):
    output, notes = LearningEngine._normalize_output({section: [{"content": content}]}, {})
    assert output[section][0]["content"] == content
    assert not notes


@pytest.mark.parametrize("section", ["memories", "updates"])
def test_incomplete_annotation_does_not_swallow_other_parenthetical_content(section):
    kept = "（说话人 小林）说我（用户）去宁波（带资料）。"
    output, notes = LearningEngine._normalize_output({section: [{"content": kept + ANNOTATION}]}, {})
    assert output[section][0]["content"] == kept
    assert len(notes) == 1


@pytest.mark.parametrize(("section", "action", "reason"), [
    ("memories", None, "empty or overlong content"),
    ("updates", "修正", "missing corrected content"),
    ("updates", "反驳", "missing corrected content"),
])
def test_annotation_only_content_is_dropped_by_existing_validation(store, section, action, reason):
    memory_id = seed_user_memory(store)
    msg(store, 2, "补充一下出差安排", sender="我（用户）")
    content = " \n" + ANNOTATION + ANNOTATION + "\n "
    item = memory(content, [2], "P1", ["P1"]) if section == "memories" else {
        "ref": "M1", "action": action, "content": content, "evidence": [2],
    }
    output = {section: [item]}
    formed, result = batch(store, FakeGateway(output), count=1)
    assert not result["created"] and not result["updated"]
    assert len(result["dropped"]) == 1
    assert result["dropped"][0]["reason"] == reason
    assert result["dropped"][0]["item"]["content"].strip() == ""
    assert any(note["field"] == "content" and note["before"] == content for note in result["normalizations"])
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert conn.execute("SELECT revision FROM memories WHERE id=?", (memory_id,)).fetchone()[0] == 1
        raw = conn.execute("SELECT raw_output FROM batch_attempts WHERE batch_id=?", (formed.id,)).fetchone()[0]
        saved = json.loads(conn.execute("SELECT result_json FROM batches WHERE id=?", (formed.id,)).fetchone()[0])
    assert raw == json.dumps(output, ensure_ascii=False)
    assert saved["normalizations"] == result["normalizations"]
