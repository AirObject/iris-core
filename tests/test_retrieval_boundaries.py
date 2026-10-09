import json
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest

from conftest import FakeGateway, batch, msg
from iris.models import Gateway, ModelConfig
from iris.queue import estimate_tokens
from iris.retrieval import Retrieval
from test_retrieval import Embeddings, put, entry


def test_startup_snapshot_consumes_preexisting_vector_notifications(store, monkeypatch):
    mid = put(store, "我喜欢观测星空", vector=[1, 0])
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM vector_dirty").fetchone()[0] == 1
    r = Retrieval(store, Embeddings(store), vector_min=.5)
    assert r.index.contains(mid)
    # The first recall must not reread and normalize the entire startup corpus.
    monkeypatch.setattr(r.index, "upsert", lambda _: pytest.fail("startup vector read twice"))
    assert r.search(text="星空")["memories"][0]["id"] == mid
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM vector_dirty").fetchone()[0] == 0


def test_prepare_token_budget_includes_serialized_list_overhead(store):
    entry(store)
    put(store, "我喜欢天文摄影")
    r = Retrieval(store, clock=lambda: datetime(2026, 9, 29, tzinfo=timezone.utc))
    reply = r.prepare("A", text="天文摄影", participants=[])
    from iris.db import dumps
    # People and consolidation annotations are appended after the unchanged selection budget.
    selected = [{k: v for k, v in m.items() if k not in ("subject_annotations", "consolidation_annotations")} for m in reply["memories"]]
    budget = estimate_tokens(dumps(selected)) - 1
    bounded = r.prepare("A", text="天文摄影", participants=[], token_budget=budget)
    assert bounded["memories"] == []


def test_embedding_deadline_is_two_seconds_without_retries_or_waiting_in_transaction(store):
    entry(store)
    put(store, "我喜欢天文摄影")
    seen = []
    class Future:
        def result(self, timeout):
            assert timeout == pytest.approx(2, abs=.01)
            raise FutureTimeout()
        def cancel(self):
            seen.append("cancel")
    class Pool:
        def submit(self, *args, **kwargs):
            assert not store._writer.in_transaction
            assert kwargs["timeout"].read == kwargs["timeout"].connect == pytest.approx(2, abs=.01)
            seen.append("request")
            return Future()
    gateway = Gateway({"embedding": ModelConfig("https://example.invalid", "", "fake-vector")}, store,
                      sleeper=lambda _: pytest.fail("retrieval must not retry"))
    gateway._pool.shutdown(wait=False)
    gateway._pool = Pool()
    reply = Retrieval(store, gateway, vector_min=.5).prepare("A", text="天文摄影", participants=[])
    assert reply["memories"] and any(h["code"] == "embedding_fallback" for h in reply["hints"])
    assert seen == ["request", "cancel"]
    with store.read() as conn:
        row = conn.execute("SELECT purpose,result_category,error_summary FROM model_calls").fetchone()
        assert tuple(row) == ("retrieval_query", "retryable", "total timeout")
    gateway.client.close()


@pytest.mark.parametrize("vector", [[], [float("inf")], [0., 0.], "bad"])
def test_malformed_embedding_response_is_recorded_as_failure(store, vector):
    # httpx's JSON encoder rejects inf; provider bodies may still contain it.
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps({"data":[{"embedding":vector}]}))))
    gateway = Gateway({"embedding":ModelConfig("https://example.invalid","","fake-vector")},store,client=client)
    reply = Retrieval(store,gateway,vector_min=.5).search(text="天文摄影")
    assert reply["memories"] == [] and any(h["code"] == "embedding_fallback" for h in reply["hints"])
    with store.read() as conn:
        assert {r[0] for r in conn.execute("SELECT result_category FROM model_calls")} == {"configuration"}
    gateway.close()
    client.close()


def test_participant_highlights_budget_and_negative_claims_not_deduplicated(store):
    recent = entry(store)
    with store.read() as conn:
        sid = conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (recent,)).fetchone()[0]
    ids = [put(store, f"来客第{i}项重要经历" + "详细事实" * 20, about=[sid], importance=90-i) for i in range(12)]
    r = Retrieval(store)
    reply = r.prepare("A", participants=[sid], text="")
    assert [m["id"] for m in reply["memories"]] == ids[:3]
    assert sum(estimate_tokens(json.dumps(m,ensure_ascii=False,separators=(",",":"))) for m in reply["memories"]) <= 1500
    assert r.prepare("A", participants=[sid], text="", memory_limit=1)["memories"][0]["id"] == ids[0]
    a = put(store, "我喜欢在雨天听爵士音乐。")
    b = put(store, "我不喜欢在雨天听爵士音乐。")
    found = [m["id"] for m in r.search(text="雨天 爵士音乐")["memories"]]
    assert a in found and b in found


def test_fts_special_syntax_is_data_and_tags_remain_searchable(store):
    mid = put(store, "天文观测")
    with store.write() as conn:
        conn.execute("INSERT INTO memory_tags VALUES(?,'望远镜')", (mid,))
    r=Retrieval(store)
    assert r.search(text='望远镜 OR " - * (')["memories"][0]["id"] == mid
    with store.write() as conn:
        conn.execute("DELETE FROM memory_tags WHERE memory_id=?",(mid,))
    assert r.search(text="望远镜")["memories"] == []


def test_current_person_relation_does_not_merge_same_name_accounts(store):
    entry(store)
    with store.write() as conn:
        for sid in ("a","b"):
            conn.execute("INSERT INTO subjects(id,kind,name,parent_id,created_at) VALUES(?,'person','米粒',NULL,?)",(sid,datetime.now(timezone.utc).isoformat()))
    a=put(store,"米粒是陶艺老师",about=["a"],speaker="a")
    b=put(store,"米粒是建筑学生",about=["b"],speaker="b")
    r=Retrieval(store)
    assert [m["id"] for m in r.search(people=["a"])["memories"]] == [a]
    with pytest.raises(ValueError,match="ambiguous"):
        r.prepare("A",participants=["米粒"],text="")


def test_learning_and_retrieval_share_context_and_subject_index(store):
    msg(store,1,"我喜欢观星")
    fake=FakeGateway({"memories":[{"content":"小林喜欢天文摄影","type":"偏好","speaker":"小林","about":["小林"],"stance":"亲历","evidence":[1]}]})
    _, result=batch(store,fake,count=1)
    msg(store,2,"还喜欢用望远镜")
    fake.response={}
    queries=[]
    store._writer.set_trace_callback(queries.append)
    batch(store,fake,count=1)
    store._writer.set_trace_callback(None)
    assert "小林喜欢天文摄影" in fake.materials[-1]
    assert not any(q.strip() == "SELECT * FROM memories WHERE lifecycle='active'" for q in queries)
    with store.read() as conn:
        plan=" ".join(str(tuple(r)) for r in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM memories WHERE speaker_subject_id='self' AND kind='事实' AND stance='亲历' AND event_time IS NULL AND lifecycle='active'"))
    assert "memories_dedupe" in plan
