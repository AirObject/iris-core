import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from conftest import msg
from iris.db import Store, now
from iris.memory_ops import edit_memory, setup_role
from iris.models import ModelConfig, ModelError
from iris.retrieval import Retrieval


def put(store, content, *, about=("self",), speaker="self", entry=None, evidence=(), vector=None,
        importance=60, lifecycle="active", kind="事实", stance="亲历", event_time=None):
    with store.write() as conn:
        stamp = now()
        mid = conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,
            retention,event_time,lifecycle,entry_id,embedding,embedding_model,created_at,updated_at,
            first_confirmed_at,last_confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (content, kind, speaker, stance, 70, importance, 50, event_time, lifecycle, entry,
             np.asarray(vector, dtype=np.float32).tobytes() if vector is not None else None,
             "fake-vector" if vector is not None else None, stamp, stamp, stamp, stamp)).lastrowid
        conn.executemany("INSERT INTO memory_subjects VALUES(?,?)", [(mid, sid) for sid in about])
        conn.executemany("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",
                         [(mid, i, stamp) for i in evidence])
    return mid


def entry(store):
    setup_role(store, "Iris")
    return msg(store, 1, "晚上好，正在等你", sender="来客")


@pytest.mark.parametrize("tokenizer", ["jieba", "trigram"])
def test_fts5_tokenizers_and_committed_edit_delete(store, tokenizer):
    r = Retrieval(store, tokenizer=tokenizer)
    mid = put(store, "我喜欢天文摄影和望远镜")
    assert r.search(text="天文摄影")["memories"][0]["id"] == mid
    assert edit_memory(store, mid, 1, content="我喜欢水彩绘画")
    assert r.search(text="天文摄影")["memories"] == []
    assert r.search(text="水彩绘画")["memories"][0]["id"] == mid
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='deleted' WHERE id=?", (mid,))
    assert r.search(text="水彩绘画", include_forgotten=True)["memories"] == []


def test_r01_r04_r05_r06_partitions_shared_memory_and_no_raw_leak(store):
    entry(store)
    other = msg(store, 2, "B 的私有原始正文", entry="B")
    mid = put(store, "我喜欢天文摄影", entry="B", evidence=[other])
    r = Retrieval(store)
    reply = r.prepare("A", text="天文摄影", participants=[])
    assert set(reply) >= {"persona", "memories", "recent_messages", "state", "goals", "hints", "recall_id"}
    assert reply["persona"]["version"] and reply["persona"]["generated_at"]
    assert reply["state"] == {}
    assert reply["memories"][0]["id"] == mid
    assert reply["memories"][0]["sources"][0]["entry_id"] == "B"
    assert all(m["entry_id"] == "A" and m["unlearned"] for m in reply["recent_messages"])
    assert "B 的私有原始正文" not in str(reply)
    assert r.prepare("A", text="火星飞船航班", participants=[])["memories"] == []
    assert r.search(text="正在等你")["memories"] == []


def test_r11_r12_r13_redundancy_budget_and_no_strength_for_read(store):
    recent = entry(store)
    redundant = put(store, "我喜欢天文摄影", evidence=[recent])
    old = put(store, "我每周练习天文摄影", importance=80)
    twin = put(store, "我每周练习天文摄影。", importance=70)
    unseen = put(store, "天文摄影使用赤道仪跟踪星星")
    r = Retrieval(store)
    reply = r.prepare("A", text="天文摄影", participants=[], known_memory_ids=[old])
    assert redundant not in [m["id"] for m in reply["memories"]]
    assert old not in [m["id"] for m in reply["memories"]]
    assert unseen in [m["id"] for m in reply["memories"]]
    ids = [m["id"] for m in r.prepare("A", text="天文摄影", participants=[])["memories"]]
    assert len(set(ids) & {old, twin}) == 1
    assert redundant in [m["id"] for m in r.prepare("A", text="天文摄影", participants=[], recent_limit=0)["memories"]]
    assert r.prepare("A", text="天文摄影", participants=[], token_budget=0)["memories"] == []
    with store.read() as conn:
        assert {row[0] for row in conn.execute("SELECT retention FROM memories")} == {50}


def test_derived_sources_and_initial_setting_are_not_overfiltered(store):
    recent = entry(store)
    parent = put(store, "摄影器材需要保养", evidence=[recent])
    child = put(store, "摄影器材保养要防潮")
    mixed = put(store, "摄影器材是我的兴趣", evidence=[recent])
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,created_at) VALUES(?,'memory',?,?)", (child, parent, now()))
        conn.execute("INSERT INTO sources(memory_id,kind,note,created_at) VALUES(?,'initial_setting','背景',?)", (mixed, now()))
    ids = [m["id"] for m in Retrieval(store).prepare("A", text="摄影器材", participants=[])["memories"]]
    assert parent not in ids and child not in ids and mixed in ids


class Embeddings:
    configs = {"embedding": ModelConfig("fake", "", "fake-vector")}

    def __init__(self, store, fail=False):
        self.store, self.fail = store, fail

    def embedding(self, text, purpose="embedding", **kwargs):
        assert not self.store._writer.in_transaction
        if self.fail:
            raise ModelError("retryable", "offline")
        return [1.0, 0.0]


def test_r07_vector_index_after_commit_and_fallback(store):
    entry(store)
    r = Retrieval(store, Embeddings(store), vector_min=0.7)
    first = put(store, "我偏爱观测星空", vector=[1, 0])
    assert r.search(text="天文摄影")["memories"][0]["id"] == first
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            conn.execute("UPDATE memories SET lifecycle='deleted' WHERE id=?", (first,))
            assert r.index.contains(first)
            raise RuntimeError("rollback")
    assert r.index.contains(first)
    assert edit_memory(store, first, 1, content="我喜欢天文摄影")
    assert not r.index.contains(first)  # Old embedding cannot describe new prose.
    r.gateway.fail = True
    reply = r.prepare("A", text="天文摄影", participants=[])
    assert reply["memories"][0]["id"] == first
    assert any(h["code"] == "embedding_fallback" for h in reply["hints"])


def test_r08_r09_feedback_idempotent_atomic_and_revision_independent(store):
    entry(store)
    mid = put(store, "我喜欢天文摄影")
    r = Retrieval(store)
    recall = r.search(text="天文摄影")["recall_id"]
    assert edit_memory(store, mid, 1, content="我喜欢黑白天文摄影")
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: r.feedback(recall, [mid, mid]), range(8)))
    with store.read() as conn:
        row = conn.execute("SELECT retention,belief,revision FROM memories WHERE id=?", (mid,)).fetchone()
        assert tuple(row) == (58, 70, 2)
        assert conn.execute("SELECT revision FROM recall_items WHERE recall_id=?", (recall,)).fetchone()[0] == 1
    other = put(store, "我喜欢园艺")
    with pytest.raises(ValueError):
        r.feedback(recall, [other])
    with store.write() as conn:
        conn.execute("UPDATE recalls SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (recall,))
    with pytest.raises(ValueError, match="expired"):
        r.feedback(recall, [mid])


def test_feedback_cannot_resurrect_deleted_memory(store):
    mid = put(store, "天文摄影")
    r = Retrieval(store)
    recall = r.search(text="天文摄影")["recall_id"]
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='deleted' WHERE id=?", (mid,))
    with pytest.raises(KeyError):
        r.feedback(recall, [mid])
    with store.read() as conn:
        assert tuple(conn.execute("SELECT lifecycle,retention FROM memories WHERE id=?", (mid,)).fetchone()) == ("deleted", 50)


def test_filtering_and_deep_read_does_not_revive(store):
    a = put(store, "天文摄影", kind="偏好", stance="观点", event_time="2026-09-01")
    b = put(store, "天文摄影旧记录", lifecycle="forgotten", event_time="2024-01-01")
    r = Retrieval(store)
    assert [m["id"] for m in r.search(people=["self"], kinds=["偏好"], stances=["观点"],
                time_from="2026-08-31", time_to="2026-09-02")["memories"]] == [a]
    assert b not in [m["id"] for m in r.search(text="天文摄影")["memories"]]
    results = r.search(text="天文摄影", include_forgotten=True)["memories"]
    assert next(m for m in results if m["id"] == b)["lifecycle"] == "forgotten"


def test_r10_local_prepare_latency_and_empty_messages(store):
    entry(store)
    for i in range(30):
        put(store, f"天文摄影第{i}个主题")
    r = Retrieval(store)
    samples = []
    for _ in range(20):
        start = time.perf_counter()
        result = r.prepare("A", text="天文摄影", participants=[], recent_limit=0)
        samples.append((time.perf_counter() - start) * 1000)
    assert np.percentile(samples, 95) < 500
    assert result["recent_messages"] == [] and len(result["memories"]) <= 8
