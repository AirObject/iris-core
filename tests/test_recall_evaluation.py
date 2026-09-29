import json

import pytest

from iris.recall_evaluation import recall_metrics, run_recall_eval


def test_recall_ndcg_and_no_answer_denominators():
    rows = [{"relevant": {"a": 3, "b": 1}, "returned": ["b", "x", "a"], "local_ms": 2, "mode": "prepare"},
            {"relevant": {}, "returned": ["x"], "local_ms": 3, "mode": "prepare"},
            {"relevant": {}, "returned": [], "local_ms": 1, "mode": "prepare"}]
    metrics = recall_metrics(rows)
    assert metrics["recall_at_8"] == 1
    assert 0 < metrics["ndcg_at_8"] < 1
    assert metrics["irrelevant_return_rate"] == .5
    assert metrics["prepare_p95_ms"] == pytest.approx(2.9)
    assert recall_metrics(rows[1:])["recall_at_8"] is None


def test_external_recall_corpus_details_use_production_redundancy(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    corpus = tmp_path / "test.json"
    data = {"memories": [{"id":"a","content":"观星集合时间是晚上八点","about":["我"],"source_message_ids":["context"]},
                         {"id":"b","content":"观星需带保暖衣服","about":["我"]}],
            "queries": [{"id":"one","split":"dev","text":"观星","recent_messages":[{"id":"context","speaker":"来客","content":"观星集合时间是晚上八点"}],"relevant":{"b":3}}]}
    corpus.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
    path, report = run_recall_eval({}, root, "all", corpus=corpus, out=tmp_path / "reports")
    assert path.exists()
    assert report["variants"]["jieba_fts"]["metrics"]["recall_at_8"] == 1
    assert report["details"]["memories"] == data["memories"]
    row = report["details"]["variants"]["jieba_fts"][0]
    assert row["returned"] == ["b"] and row["response"]["recent_messages"]
    assert report["variants"]["trigram_fts"]["metrics"]["recall_at_8"] == 0
    assert "M2" in path.read_text(encoding="utf-8")
