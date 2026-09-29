from conftest import FakeGateway, batch, msg
from test_batches import memory


def test_confirmation_does_not_rewrite_but_correction_preserves_added_details(store):
    msg(store, 1, "我周六在港口做志愿者")
    _, first = batch(store, FakeGateway({"memories": [memory("小林周六在港口做志愿者")]}), count=1)
    mid = first["created"][0]
    expanded = "小林从2021年起每周六在港口做志愿者，已持续五年"
    msg(store, 2, "从2021年开始，五年了")
    batch(store, FakeGateway({"updates": [{"ref": "M1", "action": "确认", "content": expanded,
                                           "belief": 90, "evidence": [2]}]}), count=1)
    with store.read() as conn:
        row = conn.execute("SELECT content,belief,revision,retention FROM memories WHERE id=?", (mid,)).fetchone()
    assert tuple(row) == ("小林周六在港口做志愿者", 60, 1, 59)
    msg(store, 3, "对，五年，每周六都去")
    batch(store, FakeGateway({"updates": [{"ref": "M1", "action": "修正", "content": expanded,
                                           "belief": 90, "evidence": [2]}]}), count=1)
    with store.read() as conn:
        row = conn.execute("SELECT content,belief,revision,retention FROM memories WHERE id=?", (mid,)).fetchone()
    assert tuple(row) == (expanded, 90, 2, 59)
