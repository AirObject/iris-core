import json

import pytest

from iris.db import dumps, now
from iris.memory_ops import edit_memory
from conftest import FakeGateway, batch, msg


def seed(store):
    msg(store, 1, "我喜欢参观展馆", sender="沈砚")
    _, result = batch(store, FakeGateway({"memories": [{
        "speaker": "P1", "stance": "亲历", "content": "沈砚喜欢参观展馆",
        "about": ["P1"], "type": "偏好", "belief": 80, "importance": 50,
        "evidence": [1],
    }]}), count=1)
    return result["created"][0]


def state(store, memory_id):
    with store.read() as conn:
        return dict(conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())


def learn_update(store, action="修正", *, hook=None):
    msg(store, 2, "我现在更喜欢参观博物馆", sender="沈砚")
    gateway = FakeGateway({"updates": [{"ref": "M1", "action": action,
        "content": "沈砚更喜欢参观博物馆", "belief": 20, "evidence": [2]}]}, hook=hook)
    formed, result = batch(store, gateway, count=1)
    assert "M1" in gateway.materials[0]
    with store.read() as conn:
        persisted = json.loads(conn.execute("SELECT result_json FROM batches WHERE id=?", (formed.id,)).fetchone()[0])
    assert persisted["dropped"] == result["dropped"]
    return result


def record_revision(store, memory_id, *, actor, reason, **changes):
    """Model an already recorded revision without changing the editing API."""
    before = state(store, memory_id)
    after = {**before, **changes}
    with store.write() as conn:
        conn.execute("UPDATE memories SET content=?,belief=?,revision=revision+1 WHERE id=?",
                     (after["content"], after["belief"], memory_id))
        conn.execute("""INSERT INTO memory_revisions(memory_id,revision_before,revision_after,
            before_json,after_json,reason,actor,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (memory_id, before["revision"], before["revision"] + 1,
             dumps({k: before[k] for k in ("content", "belief")}),
             dumps({k: after[k] for k in ("content", "belief")}), reason, actor, now()))


@pytest.mark.parametrize("action", ["修正", "反驳"])
@pytest.mark.parametrize("actor", ["admin", "reviewer-17"])
def test_learning_cannot_rewrite_last_manual_content_edit(store, action, actor):
    memory_id = seed(store)
    assert edit_memory(store, memory_id, 1, content="沈砚喜欢参观历史展馆", actor=actor)
    before = state(store, memory_id)
    result = learn_update(store, action)
    after = state(store, memory_id)
    assert result["updated"] == result["confirmed"] == []
    assert result["dropped"][0]["reason"] == "memory manually edited"
    assert after == before
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE memory_id=?", (memory_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?", (memory_id,)).fetchone()[0] == 1


def test_confirmation_of_manual_memory_still_increases_retention(store):
    memory_id = seed(store)
    assert edit_memory(store, memory_id, 1, content="沈砚喜欢参观历史展馆")
    before = state(store, memory_id)
    result = learn_update(store, "确认")
    after = state(store, memory_id)
    assert result["confirmed"] == [memory_id]
    assert result["updated"] == result["dropped"] == []
    assert after["retention"] == before["retention"] + 5
    for key in ("content", "belief", "revision", "updated_at", "embedding"):
        assert after[key] == before[key]
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE memory_id=?", (memory_id,)).fetchone()[0] == 2


def test_non_content_revision_does_not_remove_manual_protection(store):
    memory_id = seed(store)
    assert edit_memory(store, memory_id, 1, content="沈砚喜欢参观历史展馆")
    record_revision(store, memory_id, actor="learning", reason="belief only", belief=75)
    before = state(store, memory_id)
    result = learn_update(store)
    assert result["dropped"][0]["reason"] == "memory manually edited"
    assert state(store, memory_id) == before


def test_unchanged_manual_save_does_not_count_as_content_edit(store):
    memory_id = seed(store)
    assert edit_memory(store, memory_id, 1, content=state(store, memory_id)["content"])
    result = learn_update(store)
    assert result["updated"] == [memory_id]
    assert result["dropped"] == []


def test_latest_content_edit_is_used_not_an_older_manual_revision(store):
    memory_id = seed(store)
    assert edit_memory(store, memory_id, 1, content="沈砚喜欢参观历史展馆")
    record_revision(store, memory_id, actor="learning", reason="legacy correction",
                    content="沈砚喜欢参观自然展馆")
    result = learn_update(store)
    assert result["updated"] == [memory_id]
    assert result["dropped"] == []


def test_learning_supplied_reason_cannot_create_manual_protection(store):
    memory_id = seed(store)
    record_revision(store, memory_id, actor="learning", reason="manual edit",
                    content="沈砚喜欢参观自然展馆")
    result = learn_update(store)
    assert result["updated"] == [memory_id]
    assert result["dropped"] == []
