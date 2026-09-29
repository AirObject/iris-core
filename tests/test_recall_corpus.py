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
