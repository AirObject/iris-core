import json

import pytest

from iris.memory_ops import edit_memory
from conftest import FakeGateway, batch, msg


def seed(store):
    msg(store, 1, "我定在十一月四日参观展馆", sender="沈砚", at="2026-11-02T10:00:00+08:00")
    _, result = batch(store, FakeGateway({"memories": [{
        "content": "沈砚计划2026年11月4日参观展馆", "type": "计划", "speaker": "P1",
        "stance": "亲历", "about": ["P1"], "event_time": "2026-11-04", "evidence": [1],
    }]}), count=1)
    return result["created"][0]


def update(store, **fields):
    msg(store, 2, "改到下周三，原来的日期作废", sender="沈砚", at="2026-11-02T10:05:00+08:00")
    item = {"ref": "M1", "action": "修正", "content": "沈砚计划2026年11月11日参观展馆", "evidence": [2], **fields}
    _, result = batch(store, FakeGateway({"updates": [item]}), count=1)
    return result


@pytest.mark.parametrize("value", ["2026-11-11", "下周三", None])
def test_correction_updates_event_time_with_content_and_revision(store, value):
    memory_id = seed(store)
    result = update(store, event_time=value)
    expected = None if value is None else "2026-11-11"
    assert result["updated"] == [memory_id]
    with store.read() as conn:
        memory = conn.execute("SELECT content,event_time,revision FROM memories WHERE id=?", (memory_id,)).fetchone()
        revision = conn.execute("SELECT before_json,after_json FROM memory_revisions WHERE memory_id=?", (memory_id,)).fetchone()
    assert tuple(memory) == ("沈砚计划2026年11月11日参观展馆", expected, 2)
    assert json.loads(revision["before_json"])["event_time"] == "2026-11-04"
    assert json.loads(revision["after_json"])["event_time"] == expected


def test_omitted_event_time_preserves_existing_value(store):
    memory_id = seed(store)
    assert update(store)["updated"] == [memory_id]
    with store.read() as conn:
        assert conn.execute("SELECT event_time FROM memories WHERE id=?", (memory_id,)).fetchone()[0] == "2026-11-04"


def test_confirmation_does_not_rewrite_event_time(store):
    memory_id = seed(store)
    result = update(store, action="确认", event_time="2026-11-11")
    assert result["confirmed"] == [memory_id]
    with store.read() as conn:
        row = conn.execute("SELECT event_time,revision FROM memories WHERE id=?", (memory_id,)).fetchone()
    assert tuple(row) == ("2026-11-04", 1)


@pytest.mark.parametrize("value", [["2026-11-11"], {"date": "2026-11-11"}])
def test_non_text_corrected_event_time_is_rejected(store, value):
    memory_id = seed(store)
    result = update(store, event_time=value)
    assert result["updated"] == []
    assert result["dropped"][0]["reason"] == "invalid corrected event time"
    with store.read() as conn:
        assert conn.execute("SELECT revision FROM memories WHERE id=?", (memory_id,)).fetchone()[0] == 1


def test_manual_content_protection_also_preserves_event_time(store):
    memory_id = seed(store)
    assert edit_memory(store, memory_id, 1, content="沈砚决定不去展馆")
    result = update(store, event_time="2026-11-11")
    assert result["dropped"][0]["reason"] == "memory manually edited"
    with store.read() as conn:
        row = conn.execute("SELECT event_time,revision FROM memories WHERE id=?", (memory_id,)).fetchone()
    assert tuple(row) == ("2026-11-04", 2)
