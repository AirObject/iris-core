import json

import pytest

from iris.learning import LearningEngine, PROMPT_VERSION
from iris.queue import add_message, form_batch

from conftest import FakeGateway, batch, msg
from test_batches import memory


@pytest.mark.parametrize(("speaker", "about", "stance"), [
    ("P1", ["P1"], "亲历"), ("P1", ["小林"], "亲历"),
    ("小林", ["P1"], "亲历"), ("P1", [], "亲历"),
    ("P1", ["小王"], "转述"), ("我", ["小林"], "亲历"),
    ("Iris", ["小林"], "亲历"),
])
def test_plan_stance_is_normalized_by_speaker_identity(store, speaker, about, stance):
    msg(store, 1, "我计划周五整理文件")
    msg(store, 2, "我答应小林周五整理文件", sender="我", kind="self_output")
    item = {**memory("周五整理文件", [1, 2], speaker, stance="计划"), "about": about}
    _, result = batch(store, FakeGateway({"memories": [item]}))
    assert len(result["created"]) == 1
    with store.read() as conn:
        assert conn.execute("SELECT stance FROM memories").fetchone()[0] == stance
    assert any(note["field"] == "stance" for note in result["normalizations"])


@pytest.mark.parametrize("stance", ["设定", "建议", "事实", "plan"])
def test_other_invalid_stances_are_still_rejected(store, stance):
    msg(store, 1, "我计划周五整理文件")
    _, result = batch(store, FakeGateway({"memories": [memory(stance=stance)]}))
    assert not result["created"]
    assert result["dropped"][0]["reason"] == "invalid stance"


def test_plan_normalization_does_not_bypass_self_evidence(store):
    msg(store, 1, "你答应过周五整理文件")
    _, result = batch(store, FakeGateway({"memories": [memory(speaker="我", stance="计划")]}))
    assert not result["created"]
    assert "self claim" in result["dropped"][0]["reason"]


@pytest.mark.parametrize("ref", [1, 1.0, "1", " 1 ", "M1"])
def test_numeric_update_and_derived_refs_keep_revision_sources(store, ref):
    msg(store, 1, "我下周到杭州培训")
    _, first = batch(store, FakeGateway({"memories": [memory("小林下周到杭州培训")]}), count=1)
    msg(store, 2, "改成宁波了，培训期间我不在家")
    item = {**memory("培训期间小林不在家", [2]), "derived_from": [ref, "M999", {}, None]}
    output = {"updates": [{"ref": ref, "action": "修正", "content": "小林下周到宁波培训", "evidence": [2]}],
              "memories": [item]}
    formed, result = batch(store, FakeGateway(output), count=1)
    assert len(result["created"]) == 1 and result["updated"] == first["created"]
    assert len([n for n in result["normalizations"] if n["reason"] == "unknown derived memory reference removed"]) == 3
    with store.read() as conn:
        original = conn.execute("SELECT content,revision FROM memories WHERE id=?", (first["created"][0],)).fetchone()
        source = conn.execute("SELECT source_memory_id,source_revision FROM sources WHERE kind='memory'").fetchone()
        saved = json.loads(conn.execute("SELECT result_json FROM batches WHERE id=?", (formed.id,)).fetchone()[0])
    assert tuple(original) == ("小林下周到宁波培训", 2)
    assert tuple(source) == (first["created"][0], 1)
    assert saved["normalizations"] == result["normalizations"]


def test_invalid_derived_refs_do_not_lose_collective_feedback(store):
    msg(store, 1, "术语说得太快了", sender="甲")
    msg(store, 2, "我也跟不上，请留点停顿", sender="乙")
    item = {**memory("我根据甲乙反馈归纳术语语速太快，需要停顿", [1, 2], "我", ["我"], "推断"),
            "derived_from": [8, "99", []]}
    _, result = batch(store, FakeGateway({"memories": [item]}))
    assert len(result["created"]) == 1 and not result["dropped"]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE kind='message'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE kind='memory'").fetchone()[0] == 0


def test_alias_unwraps_single_name_and_resolves_in_same_and_later_batches(store):
    msg(store, 1, "以后叫我阿灯，我喜欢薄荷茶")
    fake = FakeGateway({"people": [{"name": ["P1"], "alias": ["阿灯"], "evidence": [1]}],
                        "memories": [memory("阿灯喜欢薄荷茶", [1], "阿灯", ["阿灯"])]})
    _, first = batch(store, fake, count=1)
    assert len(first["created"]) == 1
    msg(store, 2, "阿灯周六要去义诊", sender="小王")
    fake.response = {"memories": [memory("小王说阿灯周六去义诊", [2], "小王", ["阿灯"], "转述")]}
    _, second = batch(store, fake, count=1)
    with store.read() as conn:
        owner = conn.execute("SELECT sender_subject_id FROM messages WHERE id=1").fetchone()[0]
        alias = conn.execute("SELECT subject_id,alias,source_message_id FROM subject_aliases").fetchone()
        assert tuple(alias) == (owner, "阿灯", 1)
        assert conn.execute("SELECT COUNT(*) FROM subjects WHERE name='阿灯'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM subject_links").fetchone()[0] == 0
        assert conn.execute("SELECT subject_id FROM memory_subjects WHERE memory_id=?", (second["created"][0],)).fetchone()[0] == owner
        assert conn.execute("SELECT speaker_subject_id FROM memories WHERE id=?", (first["created"][0],)).fetchone()[0] == owner
    assert "阿灯" in fake.materials[-1]


@pytest.mark.parametrize("bad", [[], ["小林", "小王"], [3], {}, 7, True, None])
@pytest.mark.parametrize("field", ["name", "alias", "same_as", "roleplay"])
def test_non_string_people_values_are_rejected_without_junk_subjects(store, bad, field):
    msg(store, 1, "我是小林")
    relation = field if field != "name" else "same_as"
    item = {"name": "P1", relation: "阿灯", "evidence": [1], field: bad}
    _, result = batch(store, FakeGateway({"people": [item]}))
    assert result["dropped"] and field in result["dropped"][0]["reason"]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subjects WHERE id NOT IN ('self','scene')").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM subject_links").fetchone()[0] == 0


def test_alias_requires_owners_evidence_and_does_not_merge_known_accounts(store):
    msg(store, 1, "小林可能叫阿灯", sender="小王")
    msg(store, 2, "来了", sender="小林")
    _, result = batch(store, FakeGateway({"people": [{"name": "P2", "alias": "阿灯", "evidence": [1]}]}))
    assert "own evidence" in result["dropped"][0]["reason"]
    msg(store, 3, "叫我阿灯", sender="小林")
    msg(store, 4, "我也叫阿灯但不是他", sender="阿灯", entry="B")
    _, result = batch(store, FakeGateway({"people": [{"name": "小林", "alias": "阿灯", "evidence": [3]}]}), count=1)
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM platform_identities").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM subject_aliases").fetchone()[0] == 1


def test_material_numbers_sender_and_quote_author_for_same_name_accounts(store):
    common = dict(entry_id="A", entry_name="A", platform="test", entry_kind="group",
                  kind="message", occurred_at="2026-09-28T09:00:00+08:00")
    add_message(store, sender="小米", account_id="a", content="我学建筑", dedupe_key="a", **common)
    add_message(store, sender="小米", account_id="b", content="我学陶艺", dedupe_key="b", **common)
    add_message(store, sender="小周", account_id="c", content="引用第二位", dedupe_key="c",
                quote_author="小米", quote_author_account_id="b", quote_content="我学陶艺", **common)
    fake = FakeGateway({"memories": [memory("小米学建筑", [1], "P1", ["P1"]),
                                       memory("小米学陶艺", [3], "P2", ["P2"]),
                                       memory("串号", [1], "P2", ["P2"])]})
    _, result = batch(store, fake, count=3)
    assert len(result["created"]) == 2 and len(result["dropped"]) == 1
    material = fake.materials[0]
    assert "P1 小米（test a）" in material and "P2 小米（test b）" in material
    assert "[他人消息] P1 小米" in material and "[他人消息] P2 小米" in material
    assert "引用作者 P2 小米" in material
    with store.read() as conn:
        assert len({r[0] for r in conn.execute("SELECT speaker_subject_id FROM memories")}) == 2


def test_v4_promise_and_detail_preserving_summary_pass_through_learning(store):
    msg(store, 1, "我学制两年，明年二月入学，别在群里说")
    msg(store, 2, "我答应你，在你公开之前保密", sender="我", kind="self_output")
    output = {"memories": [memory("小林明年二月开始两年学制，要求在本人公开前保密", [1]),
                            memory("我答应小林在其公开前保密", [2], "我", ["我", "P1"], "亲历")],
              "goals": [{"content": "在小林公开前保密入学计划", "evidence": [1, 2]}], "questions": []}
    fake = FakeGateway(output)
    formed, result = batch(store, fake)
    assert PROMPT_VERSION == "learning_v6"
    assert fake.requests[0]["max_tokens"] == 16000
    assert fake.requests[0]["batch_id"] == formed.id
    assert len(result["created"]) == 2
    with store.read() as conn:
        promise = conn.execute("SELECT id FROM memories WHERE speaker_subject_id='self'").fetchone()[0]
        assert 'self' in {r[0] for r in conn.execute("SELECT subject_id FROM memory_subjects WHERE memory_id=?", (promise,))}
        assert conn.execute("SELECT COUNT(*) FROM goals WHERE kind='normal'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM goals WHERE kind='question'").fetchone()[0] == 0


def test_participant_named_self_cannot_bypass_speaker_id_check(store):
    msg(store, 1, "大家都觉得太快", sender="我", kind="message")
    _, result = batch(store, FakeGateway({"memories": [memory("从大家反馈归纳太快", [1], "P1", ["P1"], "推断")]}))
    assert not result["created"]
    assert "inference speaker" in result["dropped"][0]["reason"]


@pytest.mark.parametrize("stance", ["亲历", "观点"])
def test_self_attribution_fills_self_only_after_evidence_validation(store, stance):
    msg(store, 1, "我答应帮小林；我觉得小林的文章写得好", sender="我", kind="self_output")
    item = memory("我对小林的重要言行", [1], "我", ["小林"], stance)
    _, result = batch(store, FakeGateway({"memories": [item]}))
    with store.read() as conn:
        about = {r[0] for r in conn.execute("SELECT subject_id FROM memory_subjects")}
        raw = json.loads(conn.execute("SELECT raw_output FROM batch_attempts").fetchone()[0])
    assert "self" in about and len(about) == 2
    assert raw["memories"][0]["about"] == ["小林"]
    assert any(n["field"] == "about" for n in result["normalizations"])


def test_self_report_does_not_automatically_add_self_as_subject(store):
    msg(store, 1, "小林说自己喜欢猫", sender="我", kind="self_output")
    batch(store, FakeGateway({"memories": [memory("我转述小林喜欢猫", [1], "我", ["小林"], "转述")]}))
    with store.read() as conn:
        assert "self" not in {r[0] for r in conn.execute("SELECT subject_id FROM memory_subjects")}
