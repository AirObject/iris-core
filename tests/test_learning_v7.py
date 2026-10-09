import json

import pytest

from iris.learning import PROMPT_VERSION, normalize_event_time, source_message_ids
from iris.models import ModelConfig
from iris.queue import add_message
from conftest import FakeGateway, batch, msg
from test_batches import memory


def test_current_prompt_is_selected_without_changing_output_budget(store):
    msg(store, 1, "收到")
    gateway = FakeGateway({})
    batch(store, gateway, count=1)
    assert PROMPT_VERSION == "learning_v8"
    assert gateway.requests[0]["max_tokens"] == 16000


def test_participant_routing_data_preserves_distinct_accounts_and_escapes_lines(store):
    accounts = ['room"blue\ncontinued', 'room-green']
    for index, account in enumerate(accounts):
        add_message(store, entry_id="A", entry_name="A", platform="routing-service", entry_kind="group",
                    kind="message", sender="程安", account_id=account, content="我喜欢慢跑。",
                    occurred_at="2026-11-02T18:00:00+08:00", dedupe_key=str(index))
    gateway = FakeGateway({})
    batch(store, gateway, count=2)
    lines = gateway.materials[0].splitlines()
    routes = []
    for number in (1, 2):
        prefix = f"P{number} 程安；定位数据（非人物属性）："
        line = next(line for line in lines if line.startswith(prefix))
        routes.append(json.loads(line.removeprefix(prefix)))
        assert any(f"[他人消息] P{number} 程安：数据：" in line for line in lines)
    assert routes == [{"platform": "routing-service", "account_id": account} for account in accounts]


def test_related_material_exposes_existing_attribution_and_event_time(store):
    msg(store, 1, "林峤准备去学木工", sender="小林")
    first = {**memory("小林转述林峤准备去学木工", speaker="小林", about=["林峤"], stance="转述"),
             "event_time": "2026-12-08"}
    batch(store, FakeGateway({"memories": [first]}), count=1)
    msg(store, 2, "林峤准备去学木工", sender="小林")
    gateway = FakeGateway({})
    batch(store, gateway, count=1)
    line = next(line for line in gateway.materials[0].splitlines() if line.startswith("[M1 元数据] "))
    metadata = json.loads(line.removeprefix("[M1 元数据] "))
    assert metadata == {"speaker": "P1 小林", "stance": "转述", "about": ["林峤"],
                        "event_time": "2026-12-08"}


def test_quoted_material_distinguishes_body_owner_from_quote_owner(store):
    add_message(store, entry_id="A", entry_name="A", platform="test", entry_kind="group",
                kind="message", sender="小林", account_id="forwarder", content="这是原话，我只负责转发。",
                quote_author="林峤", quote_author_account_id="author", quote_content='我准备学木工。\n“下个月”开始。',
                occurred_at="2026-11-02T18:00:00+08:00", dedupe_key="quote")
    gateway = FakeGateway({})
    batch(store, gateway, count=1)
    line = next(line for line in gateway.materials[0].splitlines() if line.startswith("#1 "))
    assert "引用作者 P2 林峤；引用原话（数据）=" in line
    assert json.dumps('我准备学木工。\n“下个月”开始。', ensure_ascii=False) in line
    assert "；正文作者 P1 小林：数据：这是原话，我只负责转发。" in line


@pytest.mark.parametrize("action", ["修正", "反驳"])
def test_correction_precedes_duplicate_new_memory_in_same_batch(store, action):
    msg(store, 1, "我喜欢木香")
    _, first = batch(store, FakeGateway({"memories": [memory("小林喜欢木香")]}), count=1)
    memory_id = first["created"][0]
    msg(store, 2, "我现在更喜欢海盐气味，旧口味已经变了")
    corrected = "小林现在更喜欢海盐气味，已经改变原先偏好木香的口味。"
    output = {"updates": [{"ref": "M1", "action": action, "content": corrected, "evidence": [2]}],
              "memories": [memory(corrected, [2]), memory(corrected, [2])]}
    _, result = batch(store, FakeGateway(output), count=1)
    assert result["created"] == []
    assert result["updated"] == [memory_id]
    assert result["confirmed"] == [memory_id]
    with store.read() as conn:
        rows = conn.execute("SELECT content,revision,retention FROM memories").fetchall()
    assert [tuple(row) for row in rows] == [(corrected, 2, 59)]
    assert source_message_ids(store, memory_id) == {1, 2}


def test_correction_invalidates_old_vector_before_new_memory_comparison(store):
    original = "小林最喜欢湿润森林中的木香。"
    corrected = "小林现在最喜欢晴天海滩上的咸味空气。"
    separate = "小林喜欢用旧木料制作简洁的书架。"
    gateway = FakeGateway({"memories": [memory(original)]})
    gateway.configs["embedding"] = ModelConfig("fake", "", "fake-embed")
    gateway.embedding = lambda text, purpose="embedding": [0.0, 1.0] if text == corrected else [1.0, 0.0]
    msg(store, 1, original)
    _, first = batch(store, gateway, count=1)
    msg(store, 2, corrected + separate)
    gateway.response = {"updates": [{"ref": "M1", "action": "修正", "content": corrected, "evidence": [2]}],
                        "memories": [memory(separate, [2])]}
    _, result = batch(store, gateway, count=1)
    assert result["updated"] == first["created"]
    assert len(result["created"]) == 1 and not result["confirmed"]
    with store.read() as conn:
        assert {row[0] for row in conn.execute("SELECT content FROM memories")} == {corrected, separate}


@pytest.mark.parametrize(("raw", "at", "expected"), [
    ("下周二", "2026-11-01T23:58:00+08:00", "2026-11-03"),
    ("明天", "2026-11-01T23:58:00+08:00", "2026-11-02"),
    ("今天", "2026-11-02T00:02:00+08:00", "2026-11-02"),
])
def test_relative_dates_use_each_evidence_message_and_monday_week(raw, at, expected):
    assert normalize_event_time(raw, at, "Asia/Shanghai") == expected
