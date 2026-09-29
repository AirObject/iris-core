import json

import pytest

from iris.recall_evaluation import recall_metrics, run_recall_eval


@pytest.mark.parametrize('jsonl', [False, True])
def test_recall_alias_seed_and_split_category_report(tmp_path, jsonl):
    corpus = tmp_path / 'recall.json'
    corpus.write_text(json.dumps({'subjects':[{'name':'叶青','aliases':['青叶']}],
        'memories':[{'id':'m','content':'叶青喜欢摄影','about':['叶青']}],
        'queries':[{'id':'d','split':'dev','text':'青叶喜欢摄影吗','categories':['别名'],'relevant':{'m':3}},
                   {'id':'h','split':'holdout','text':'宇宙飞船','categories':['无答案'],'relevant':{}}]}, ensure_ascii=False), encoding='utf-8')
    if jsonl:
        data = json.loads(corpus.read_text(encoding='utf-8'))
        corpus.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in [
            {'subjects':data['subjects'], 'memories':data['memories']}, *data['queries']]), encoding='utf-8')
    _, report = run_recall_eval({}, tmp_path, 'all', corpus=corpus)
    groups = report['variants']['jieba_fts']['groups']
    assert groups['dev']['all']['recall_at_8'] == 1
    assert groups['dev']['categories']['别名']['queries'] == 1
    assert groups['holdout']['categories']['无答案']['irrelevant_return_rate'] == 0
    with pytest.raises(ValueError, match='dev'):
        run_recall_eval({}, tmp_path, 'all', corpus=corpus, compare_embeddings=True)


def test_embedding_cache_separates_dimensions(tmp_path, store, monkeypatch):
    from dataclasses import replace
    from iris import recall_evaluation as evaluation
    from iris.models import ModelConfig
    class Fake:
        def __init__(self, configs, store):
            self.dim = configs['embedding'].dimensions
        def embedding(self, *args):
            return [1.] + [0.] * (self.dim - 1)
        def close(self):
            pass
    monkeypatch.setattr(evaluation, 'Gateway', Fake)
    monkeypatch.setattr(evaluation.time, 'sleep', lambda _: None)
    configs = {'embedding': ModelConfig('fake', '', 'model', 1024)}
    cache = tmp_path / 'cache.db'
    first, misses = evaluation.cache_embeddings(configs, ['同一个文本'], store, cache)
    assert misses == 1 and len(first.vectors['同一个文本']) == 1024
    configs['embedding'] = replace(configs['embedding'], dimensions=2048)
    second, misses = evaluation.cache_embeddings(configs, ['同一个文本'], store, cache)
    assert misses == 1 and len(second.vectors['同一个文本']) == 2048
    _, misses = evaluation.cache_embeddings(configs, ['同一个文本'], store, cache)
    assert misses == 0


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
