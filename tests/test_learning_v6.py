import json

import pytest

from iris.learning import PROMPT, PROMPT_VERSION
from iris.queue import add_message
from conftest import FakeGateway, batch, msg


def test_learning_uses_v6_with_original_output_budget(store):
    msg(store, 1, "收到", sender="沈砚")
    gateway = FakeGateway({})
    batch(store, gateway, count=1)
    assert PROMPT_VERSION == "learning_v6"
    assert gateway.requests[0]["messages"][0]["content"] == PROMPT
    assert gateway.requests[0]["max_tokens"] == 16000


@pytest.mark.parametrize(("name", "alias"), [("沈砚", "沈砚"), ("Mora", "mora")])
def test_existing_display_name_is_not_an_alias(store, name, alias):
    msg(store, 1, f"叫我{alias}", sender=name)
    formed, result = batch(store, FakeGateway({"people": [
        {"name": "P1", "alias": alias, "evidence": [1]}]}), count=1)
    assert result["dropped"][0]["reason"] == "alias duplicates subject name"
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subject_aliases").fetchone()[0] == 0
        saved = json.loads(conn.execute("SELECT result_json FROM batches WHERE id=?", (formed.id,)).fetchone()[0])
    assert saved["dropped"] == result["dropped"]


@pytest.mark.parametrize("text", ["我开始学篆刻了", "我用这个账号加入", "大家说我的昵称好记"])
def test_alias_must_appear_in_owners_actual_evidence(store, text):
    msg(store, 1, text, sender="沈砚")
    _, result = batch(store, FakeGateway({"people": [
        {"name": "P1", "alias": "小篆", "evidence": [1]}]}), count=1)
    assert result["dropped"][0]["reason"] == "alias absent from own target evidence"
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subject_aliases").fetchone()[0] == 0


def quoted(store, content, quote):
    return add_message(store, entry_id="A", entry_name="A", platform="test", entry_kind="group",
                       sender="沈砚", account_id="sender", content=content, dedupe_key="quoted",
                       quote_author="闻秋", quote_author_account_id="author", quote_content=quote,
                       kind="message", occurred_at="2026-10-07T09:00:00+08:00")


@pytest.mark.parametrize(("owner", "content", "quote"), [
    ("P1", "收到", "叫我小篆"),
    ("P2", "你是不是叫小篆", "我开始学篆刻了"),
])
def test_alias_cannot_borrow_another_authors_words(store, owner, content, quote):
    quoted(store, content, quote)
    _, result = batch(store, FakeGateway({"people": [
        {"name": owner, "alias": "小篆", "evidence": [1]}]}), count=1)
    assert result["dropped"][0]["reason"] == "alias absent from own target evidence"
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subject_aliases").fetchone()[0] == 0


@pytest.mark.parametrize(("owner", "content", "quote"), [
    ("P1", "以后叫我小篆", "收到"),
    ("P2", "这是原话", "我的昵称是小篆"),
])
def test_actual_owner_declaration_is_preserved(store, owner, content, quote):
    quoted(store, content, quote)
    _, result = batch(store, FakeGateway({"people": [
        {"name": owner, "alias": "小篆", "evidence": [1]}]}), count=1)
    assert not result["dropped"]
    with store.read() as conn:
        source = conn.execute("SELECT sender_subject_id,quote_author_subject_id FROM messages").fetchone()
        saved = conn.execute("SELECT subject_id,alias,source_message_id FROM subject_aliases").fetchone()
    assert tuple(saved) == (source[0 if owner == "P1" else 1], "小篆", 1)


def test_alias_source_uses_target_with_alias_not_unrelated_own_message(store):
    msg(store, 1, "我开始学篆刻了", sender="沈砚")
    msg(store, 2, "以后叫我小篆", sender="沈砚")
    _, result = batch(store, FakeGateway({"people": [
        {"name": "P1", "alias": "小篆", "evidence": [1, 2]}]}))
    assert not result["dropped"]
    with store.read() as conn:
        assert conn.execute("SELECT source_message_id FROM subject_aliases").fetchone()[0] == 2
