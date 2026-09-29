import json
from pathlib import Path


def test_handwritten_recall_corpus_contract():
    corpus = json.loads((Path(__file__).resolve().parents[1] / "evals/recall_v1.json").read_text(encoding="utf-8"))
    ids = {m["id"] for m in corpus["memories"]}
    queries = corpus["queries"]
    assert len(queries) >= 30 and len({q["id"] for q in queries}) == len(queries)
    assert sum(not q["relevant"] for q in queries) >= 5
    assert all(set(q["relevant"]) <= ids and q["split"] == "dev" for q in queries)
    assert sum(bool(q.get("recent_messages")) for q in queries) >= 2
    message_ids = {m["id"] for q in queries for m in q.get("recent_messages", [])}
    assert all(set(m.get("source_message_ids", [])) <= message_ids for m in corpus["memories"])


def test_v2_frozen_corpus_contract():
    corpus = json.loads((Path(__file__).resolve().parents[1] / "evals/recall_v2.json").read_text(encoding="utf-8"))
    queries, memories = corpus["queries"], corpus["memories"]
    ids = {m["id"] for m in memories}
    assert len(ids) == len(memories) == 50
    assert len({q["id"] for q in queries}) == len(queries) == 66
    assert sum(q["split"] == "dev" for q in queries) == 44
    assert sum(q["split"] == "holdout" for q in queries) == 22
    assert sum(not q["relevant"] for q in queries) == 16
    required = {"换个说法", "别名", "无答案", "问已知的人的未知属性", "按参与者召回", "近期消息去冗余"}
    for split in ("dev", "holdout"):
        subset = [q for q in queries if q["split"] == split]
        assert required <= {c for q in subset for c in q["categories"]}
    sources = {m["id"] for q in queries for m in q.get("recent_messages", [])}
    assert all(set(m.get("source_message_ids", [])) <= sources for m in memories)
    for q in queries:
        assert set(q["relevant"]) <= ids
        assert not set(q.get("known_memory_ids", [])) & set(q["relevant"])
        assert all(1 <= grade <= 3 for grade in q["relevant"].values())
