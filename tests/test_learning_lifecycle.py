"""Learning confirmations share lifecycle hysteresis and R13 claim boundaries."""
import pytest

from conftest import FakeGateway, batch, msg
from iris.learning import LearningEngine, source_message_ids
from iris.memory_ops import adjust_retention, delete_memory
from iris.models import ModelConfig
from iris.retrieval import Retrieval
from test_batches import memory


def row(store, memory_id):
    with store.read() as conn:
        return dict(conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())


def seed(store, content="小林喜欢在安静的地方画水彩"):
    msg(store, 1, content)
    _, result = batch(store, FakeGateway({"memories": [memory(content)]}), count=1)
    return result["created"][0]


def forget_at(store, memory_id, strength):
    adjust_retention(store, memory_id, value=19)
    adjust_retention(store, memory_id, value=strength)
    assert row(store, memory_id)["lifecycle"] == "forgotten"


def test_repeat_accumulates_once_per_batch_and_restores_at_high_threshold(store):
    memory_id = seed(store)
    original = row(store, memory_id)
    forget_at(store, memory_id, 29)
    forgotten_at = row(store, memory_id)["forgotten_at"]
    for number, expected, state in ((2, 34, "forgotten"), (3, 39, "active")):
        msg(store, number, original["content"])
        proposed = memory(original["content"], [2])  # One previous target is the history segment.
        _, result = batch(store, FakeGateway({"memories": [proposed, proposed]}), count=1)
        assert result["created"] == [] and result["confirmed"] == [memory_id]
        current = row(store, memory_id)
        assert (current["retention"], current["lifecycle"]) == (expected, state)
        assert current["forgotten_at"] == (forgotten_at if state == "forgotten" else None)
        assert all(current[k] == original[k] for k in ("content", "belief", "revision"))
        assert source_message_ids(store, memory_id) == set(range(1, number + 1))
    assert memory_id in [m["id"] for m in Retrieval(store).search(text="水彩")["memories"]]


def test_duplicate_confirmation_uses_configured_lifecycle_threshold_and_increment(store):
    memory_id = seed(store)
    store.set_setting("lifecycle", {**store.setting("lifecycle"), "restore_threshold": 40,
                                    "confirmation_increment": 7})
    forget_at(store, memory_id, 33)
    content = row(store, memory_id)["content"]
    msg(store, 2, content)
    _, result = batch(store, FakeGateway({"memories": [memory(content, [2])] * 2}), count=1)
    assert result["confirmed"] == [memory_id] and not result["created"]
    current = row(store, memory_id)
    assert (current["retention"], current["lifecycle"], current["revision"]) == (40, "active", 1)


def test_explicit_and_duplicate_confirmation_share_one_lifecycle_increment(store):
    memory_id = seed(store)
    original = row(store, memory_id)
    store.set_setting("lifecycle", {**store.setting("lifecycle"), "confirmation_increment": 9})
    msg(store, 2, original["content"])
    output = {"updates": [{"ref": "M1", "action": "确认", "evidence": [2]}],
              "memories": [memory(original["content"], [2])] * 2}
    _, result = batch(store, FakeGateway(output), count=1)
    assert result["confirmed"] == [memory_id] and not result["created"]
    assert row(store, memory_id)["retention"] == original["retention"] + 9
    assert row(store, memory_id)["revision"] == original["revision"]


def test_confirmation_and_sources_roll_back_with_the_learning_transaction(store, monkeypatch):
    memory_id = seed(store)
    forget_at(store, memory_id, 30)
    before = row(store, memory_id)
    msg(store, 2, before["content"])
    def fail_after_confirmation(*args):
        raise RuntimeError("source write interrupted")
    monkeypatch.setattr(LearningEngine, "_source", fail_after_confirmation)
    with pytest.raises(RuntimeError, match="source write interrupted"):
        batch(store, FakeGateway({"memories": [memory(before["content"], [2])]}), count=1)
    assert row(store, memory_id) == before
    assert source_message_ids(store, memory_id) == {1}
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1


@pytest.mark.parametrize("action", ["确认", "修正"])
def test_inflight_forgetting_allows_confirmation_but_not_content_revision(store, action):
    memory_id = seed(store)
    original = row(store, memory_id)
    msg(store, 2, original["content"])
    def forget(_):
        forget_at(store, memory_id, 30)
    output = {"updates": [{"ref": "M1", "action": action, "content": "小林改为画油画", "evidence": [2]}]}
    _, result = batch(store, FakeGateway(output, hook=forget), count=1)
    current = row(store, memory_id)
    assert current["content"] == original["content"] and current["revision"] == 1
    if action == "确认":
        assert result["confirmed"] == [memory_id] and not result["dropped"]
        assert (current["retention"], current["lifecycle"]) == (35, "active")
    else:
        assert not result["confirmed"] and result["dropped"]
        assert (current["retention"], current["lifecycle"]) == (30, "forgotten")


def test_deleted_duplicate_gets_a_new_id_without_reviving_or_changing_the_tombstone(store):
    memory_id = seed(store)
    assert delete_memory(store, memory_id, 1)
    deleted = row(store, memory_id)
    msg(store, 2, deleted["content"])
    _, result = batch(store, FakeGateway({"memories": [memory(deleted["content"], [2])]}), count=1)
    assert len(result["created"]) == 1 and result["created"][0] != memory_id
    assert not result["confirmed"] and row(store, memory_id) == deleted
    assert source_message_ids(store, memory_id) == {1}


@pytest.mark.parametrize("with_vector", [False, True])
@pytest.mark.parametrize(("first", "second"), [
    ("小林计划在周三前往河边工作室参加木雕课程", "小林计划在周五前往河边工作室参加木雕课程"),
    ("小林已经连续练习木雕12年，每周去工作室学习", "小林已经连续练习木雕13年，每周去工作室学习"),
    ("小林不喜欢在闷热的教室里长时间练习木雕", "小林喜欢在闷热的教室里长时间练习木雕"),
    ("小林未同意每周都去工作室，认为没有必要", "小林没有同意每周都去工作室，认为未有必要"),
])
def test_number_and_negation_sequences_block_both_text_and_vector_duplicates(store, with_vector, first, second):
    gateway = FakeGateway({"memories": [memory(first)]})
    if with_vector:
        gateway.configs["embedding"] = ModelConfig("fake", "", "fake-embed")
    msg(store, 1, first)
    _, initial = batch(store, gateway, count=1)
    msg(store, 2, second)
    gateway.response = {"memories": [memory(second, [2])]}
    _, result = batch(store, gateway, count=1)
    assert len(result["created"]) == 1 and not result["confirmed"]
    assert row(store, initial["created"][0])["retention"] == 54


def test_r13_duplicate_does_not_depend_on_model_type_choice(store):
    memory_id = seed(store)
    content = row(store, memory_id)["content"]
    msg(store, 2, content)
    _, result = batch(store, FakeGateway({"memories": [{**memory(content, [2]), "type": "事实"}]}), count=1)
    assert not result["created"] and result["confirmed"] == [memory_id]
    assert row(store, memory_id)["kind"] == "偏好"


def test_r13_duplicate_requires_the_same_world(store):
    memory_id = seed(store)
    with store.write() as conn:
        conn.execute("UPDATE memories SET world='story' WHERE id=?", (memory_id,))
    content = row(store, memory_id)["content"]
    msg(store, 2, content)
    _, result = batch(store, FakeGateway({"memories": [memory(content, [2])]}), count=1)
    assert len(result["created"]) == 1 and not result["confirmed"]
    assert row(store, result["created"][0])["world"] == "real"
    assert row(store, memory_id)["retention"] == 54
