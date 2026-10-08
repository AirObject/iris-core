"""Batch-local subject labels must not become durable names or bypass evidence."""

import copy
import json

import pytest

from iris.queue import add_message
from conftest import FakeGateway, batch, msg
from test_batches import memory


def subject_output(field, value, *, evidence=None):
    evidence = evidence or [1]
    if field in ("about", "speaker"):
        item = memory("宋棠谈到家人的近况", evidence, "P1", ["P1"], "转述")
        item[field] = [value] if field == "about" else value
        return {"memories": [item]}
    item = {"name": "远客", "same_as": "P1", "evidence": evidence}
    if field == "name":
        item["name"] = value
    else:
        item = {"name": "P1", field: value, "evidence": evidence}
    return {"people": [item]}


def participant_id(store, message_id=1):
    with store.read() as conn:
        return conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (message_id,)).fetchone()[0]


def assert_audit(store, formed, result, original, field, before, after):
    assert any(n["field"] == field and n["before"] == before and n["after"] == after and n["reason"]
               for n in result["normalizations"])
    with store.read() as conn:
        assert json.loads(conn.execute("SELECT raw_output FROM batch_attempts WHERE batch_id=?",
                                       (formed.id,)).fetchone()[0]) == original
        saved = json.loads(conn.execute("SELECT result_json FROM batches WHERE id=?", (formed.id,)).fetchone()[0])
        assert saved["normalizations"] == result["normalizations"]


@pytest.mark.parametrize("field", ["about", "speaker", "name", "same_as", "roleplay"])
@pytest.mark.parametrize("template", ["P1 {}", "P1（{}）", "P1({})"])
@pytest.mark.parametrize("name", ["宋棠", "小棠"])
def test_participant_labels_resolve_display_name_and_known_alias(store, field, template, name):
    msg(store, 1, "大家叫我小棠，也有人叫我棠棠", sender="宋棠")
    owner = participant_id(store)
    with store.write() as conn:
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES(?,?)", (owner, "小棠"))
    label = template.format(name)
    output = subject_output(field, label)
    if field in ("same_as", "roleplay"):
        output["people"][0]["name"] = "远客"
    if field == "name":
        output["people"][0] = {"name": label, "alias": "棠棠", "evidence": [1]}
    original = copy.deepcopy(output)
    formed, result = batch(store, FakeGateway(output), count=1)
    assert not result["dropped"]
    with store.read() as conn:
        if field in ("about", "speaker"):
            assert len(result["created"]) == 1
            assert conn.execute("SELECT speaker_subject_id FROM memories").fetchone()[0] == owner
            assert {r[0] for r in conn.execute("SELECT subject_id FROM memory_subjects")} == {owner}
        elif field == "name":
            assert conn.execute("SELECT subject_id FROM subject_aliases WHERE alias='棠棠'").fetchone()[0] == owner
        else:
            link = conn.execute("SELECT subject_a,subject_b FROM subject_links").fetchone()
            assert owner in tuple(link) and link[0] != link[1]
        assert not any("P1" in r[0] for r in conn.execute("SELECT name FROM subjects"))
    assert output == original
    before, after = ([label], ["P1"]) if field == "about" else (label, "P1")
    assert_audit(store, formed, result, original, field, before, after)


@pytest.mark.parametrize("template", ["P1 {}", "P1（{}）", "P1({})"])
def test_same_name_accounts_keep_number_identity_and_evidence_boundary(store, template):
    common = dict(entry_id="A", entry_name="A", platform="test", entry_kind="group", kind="message",
                  occurred_at="2026-09-28T09:00:00+08:00", sender="宋棠")
    add_message(store, content="我学建筑", account_id="first", dedupe_key="first", **common)
    add_message(store, content="我学陶艺", account_id="second", dedupe_key="second", **common)
    first = template.format("宋棠")
    second = first.replace("P1", "P2")
    output = {"memories": [memory("宋棠学建筑", [1], first, [first]),
                            memory("宋棠学陶艺", [2], second, [second]),
                            memory("串号", [1], second, [second])]}
    _, result = batch(store, FakeGateway(output))
    assert len(result["created"]) == 2
    assert [item["reason"] for item in result["dropped"]] == ["speaker reference is not in evidence"]
    with store.read() as conn:
        assert {r[0] for r in conn.execute("SELECT speaker_subject_id FROM memories")} == {
            participant_id(store, 1), participant_id(store, 2)}
        assert {r[0] for r in conn.execute("SELECT name FROM subjects WHERE id NOT IN ('self','scene')")} == {"宋棠"}


@pytest.mark.parametrize("field", ["about", "speaker", "name", "alias", "same_as", "roleplay"])
@pytest.mark.parametrize("label", ["P1 闻舟", "P1（闻舟）", "P1(闻舟)", "P9 宋棠", "P9的妈妈", "P1和P9的朋友"])
def test_conflicting_or_unknown_number_is_dropped_without_guessing(store, field, label):
    msg(store, 1, f"我提到了{label}", sender="宋棠")
    msg(store, 2, "在这里", sender="闻舟")
    output = subject_output(field, label)
    _, result = batch(store, FakeGateway(output))
    assert not result["created"]
    assert len(result["dropped"]) == 1
    reason = result["dropped"][0]["reason"]
    assert reason == ("unknown participant number" if "P9" in label else "participant name does not match participant number")
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subjects WHERE id NOT IN ('self','scene')").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM subject_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM subject_aliases").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["about", "name", "same_as", "roleplay"])
@pytest.mark.parametrize(("label", "expected"), [("P1的妈妈", "宋棠的妈妈"), ("P2家的哥哥", "闻舟家的哥哥")])
def test_embedded_number_creates_and_reuses_readable_relative(store, field, label, expected):
    msg(store, 1, "我妈妈在医院工作，闻舟家的哥哥在读书", sender="宋棠")
    msg(store, 2, "嗯", sender="闻舟")
    output = subject_output(field, label)
    formed, result = batch(store, FakeGateway(output))
    assert not result["dropped"]
    with store.read() as conn:
        relative = conn.execute("SELECT id,parent_id FROM subjects WHERE name=?", (expected,)).fetchone()
        assert relative is not None
        if label == "P1的妈妈":
            assert relative["parent_id"] == participant_id(store)
        assert not any("P1" in r[0] or "P2" in r[0] for r in conn.execute("SELECT name FROM subjects"))
    before, after = ([label], [expected]) if field == "about" else (label, expected)
    assert_audit(store, formed, result, output, field, before, after)
    # A fresh entry reverses batch numbers while retaining platform identities.
    add_message(store, entry_id="B", entry_name="B", platform="test", entry_kind="group", kind="message",
                occurred_at="2026-09-28T10:00:00+08:00", sender="闻舟", account_id="A:闻舟", content="又见面了", dedupe_key="B-1")
    add_message(store, entry_id="B", entry_name="B", platform="test", entry_kind="group", kind="message",
                occurred_at="2026-09-28T10:01:00+08:00", sender="宋棠", account_id="A:宋棠", content="继续聊家人", dedupe_key="B-2")
    new_label = label.replace("P1", "P2") if label.startswith("P1") else label.replace("P2", "P1")
    _, again = batch(store, FakeGateway(subject_output(field, new_label)), entry="B")
    assert not again["dropped"]
    with store.read() as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM subjects WHERE name=?", (expected,))] == [relative["id"]]


def test_embedded_speaker_name_still_requires_that_subjects_evidence(store):
    msg(store, 1, "我妈妈在医院工作", sender="宋棠")
    msg(store, 2, "我下周去培训", sender="宋棠的妈妈")
    _, result = batch(store, FakeGateway({"memories": [
        memory("宋棠的妈妈下周去培训", [2], "P1的妈妈", ["P1的妈妈"]),
        memory("不能冒充妈妈原话", [1], "P1的妈妈", ["P1的妈妈"])]}))
    assert len(result["created"]) == 1
    assert [item["reason"] for item in result["dropped"]] == ["speaker reference is not in evidence"]
    with store.read() as conn:
        assert conn.execute("SELECT speaker_subject_id FROM memories").fetchone()[0] == participant_id(store, 2)


@pytest.mark.parametrize(("label", "expected", "reason"), [
    ("P1（小棠）", "小棠", None),
    ("P1 宋棠", "宋棠", "alias duplicates subject name"),
    ("P1的妈妈", "宋棠的妈妈", None),
])
def test_alias_fields_keep_literal_names_and_existing_evidence_checks(store, label, expected, reason):
    msg(store, 1, "大家叫我小棠，也叫我宋棠的妈妈", sender="宋棠")
    owner = participant_id(store)
    with store.write() as conn:
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES(?,?)", (owner, "小棠"))
    output = {"people": [{"name": "P1", "alias": [label], "evidence": [1]}]}
    formed, result = batch(store, FakeGateway(output), count=1)
    assert [item["reason"] for item in result["dropped"]] == ([reason] if reason else [])
    assert_audit(store, formed, result, output, "alias", label, expected)
    with store.read() as conn:
        assert not any("P1" in r[0] for r in conn.execute("SELECT alias FROM subject_aliases"))
        if reason is None:
            assert conn.execute("SELECT subject_id FROM subject_aliases WHERE alias=?", (expected,)).fetchone()[0] == owner


def test_plain_and_ascii_embedded_names_are_unchanged(store):
    msg(store, 1, "我和宋棠的妈妈、XP10、P1X、_P1一起出门", sender="宋棠")
    names = ["宋棠", "宋棠的妈妈", "XP10", "P1X", "_P1"]
    _, result = batch(store, FakeGateway({"memories": [memory("一起出门", [1], "宋棠", names)]}), count=1)
    assert len(result["created"]) == 1 and not result["normalizations"]
    with store.read() as conn:
        assert {r[0] for r in conn.execute("SELECT name FROM subjects WHERE id NOT IN ('self','scene')")} == set(names)


def test_decorated_alias_declared_in_same_output_keeps_evidence_rules(store):
    msg(store, 1, "叫我小棠，我喜欢猫", sender="宋棠")
    msg(store, 2, "我叫闻舟", sender="闻舟")
    output = {"people": [{"name": ["P1（宋棠）"], "alias": ["小棠"], "evidence": [1]},
                         {"name": "P2（闻舟）", "alias": "小棠", "evidence": [1]}],
              "memories": [memory("小棠喜欢猫", [1], "P1（小棠）", ["P1 小棠"])]}
    formed, result = batch(store, FakeGateway(output))
    assert len(result["created"]) == 1
    assert [item["reason"] for item in result["dropped"]] == ["alias subject has no own evidence in target segment"]
    assert_audit(store, formed, result, output, "speaker", "P1（小棠）", "P1")
    with store.read() as conn:
        assert conn.execute("SELECT speaker_subject_id FROM memories").fetchone()[0] == participant_id(store)


def test_ambiguous_relative_parent_is_dropped_before_subject_creation(store):
    common = dict(entry_id="A", entry_name="A", platform="test", entry_kind="group", kind="message",
                  occurred_at="2026-09-28T09:00:00+08:00", sender="宋棠", content="我妈妈在医院工作")
    add_message(store, account_id="first", dedupe_key="first", **common)
    add_message(store, account_id="second", dedupe_key="second", **common)
    _, result = batch(store, FakeGateway(subject_output("about", "P1的妈妈")))
    assert not result["created"]
    assert [item["reason"] for item in result["dropped"]] == ["subject is ambiguous; use participant number"]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subjects WHERE id NOT IN ('self','scene')").fetchone()[0] == 2
