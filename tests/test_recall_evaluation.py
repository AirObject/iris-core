import json

import pytest

from iris.recall_evaluation import recall_metrics, run_recall_eval


def test_relevant_reason_metrics_separate_background_from_answers():
    rows = [
        {'relevant': {'a': 3}, 'returned': ['a', 'x', 'h'], 'reasons': {'a': 'relevant', 'x': 'relevant', 'h': 'person_highlight'}, 'local_ms': 1},
        {'relevant': {}, 'returned': ['h'], 'reasons': {'h': 'person_highlight'}, 'local_ms': 1},
        {'relevant': {}, 'returned': ['x'], 'reasons': {'x': 'relevant'}, 'local_ms': 1}]
    metrics = recall_metrics(rows)
    assert metrics['irrelevant_return_rate'] == .5
    assert metrics['average_returned'] == pytest.approx(5/3)
    assert metrics['relevant_precision'] == pytest.approx(1/3)
    assert metrics['recall_at_8'] == metrics['ndcg_at_8'] == 1


def test_public_corpora_exclude_historical_holdout_from_calibration(tmp_path):
    from iris.recall_evaluation import PUBLIC_CORPORA
    folder = tmp_path / 'evals'
    folder.mkdir()
    for name, topic in zip(PUBLIC_CORPORA, ('天文', '水彩', '陶艺', '围棋'), strict=True):
        (folder / name).write_text(json.dumps({
            'memories': [{'id': 'm', 'content': topic}],
            'queries': [
                {'id': 'dev', 'split': 'dev', 'text': topic, 'relevant': {'m': 3}},
                {'id': 'old', 'split': 'holdout', 'text': topic, 'relevant': {'m': 3}}]
        }, ensure_ascii=False), encoding='utf-8')
    _, report = run_recall_eval({}, tmp_path, 'dev', calibrate=True)
    variant = report['variants']['jieba_fts']
    assert variant['metrics']['queries'] == len(PUBLIC_CORPORA) and variant['metrics']['recall_at_8'] == 1
    assert len(variant['corpora']) == len(PUBLIC_CORPORA)
    assert all(q['id'] == 'dev' and q['returned'] == ['m'] for q in variant['queries'])
    _, final = run_recall_eval({}, tmp_path)
    assert final['variants']['jieba_fts']['groups']['holdout']['all']['queries'] == len(PUBLIC_CORPORA)
    assert all(json.loads((folder / name).read_text(encoding='utf-8'))['queries'][1]['split'] == 'holdout'
               for name in PUBLIC_CORPORA)


def test_selection_uses_global_quality_band_then_precision_then_false_returns():
    from itertools import permutations
    from iris.recall_evaluation import select_trial
    def trial(name, quality, precision, false):
        return {'name': name, 'metrics': {'recall_at_8': quality, 'ndcg_at_8': quality,
                'relevant_precision': precision, 'irrelevant_return_rate': false}}
    best_quality = trial('best quality', .90, .5, .8)
    higher_precision = trial('precision', .891, .8, .7)
    fewer_false = trial('false', .891, .8, .6)
    boundary = trial('exactly 0.01 behind', .89, 1., 0.)
    chained = trial('tie chaining is not allowed', .882, 1., 0.)
    for order in permutations([best_quality, higher_precision, fewer_false, boundary, chained]):
        assert select_trial(order) is fewer_false
    assert select_trial([best_quality, boundary, chained]) is best_quality
    # A large false-return reduction cannot compensate for lost retrieval quality.
    assert select_trial([best_quality, trial('loss', .88, 1., 0.)]) is best_quality
    # If no labelled relevant items were returned, precision is not treated as 100%.
    assert select_trial([trial('empty', .90, None, 0.), best_quality]) is best_quality


def test_null_prepare_and_embedding_prefetch_share_production_query(tmp_path, monkeypatch):
    from iris import recall_evaluation as evaluation
    from iris.models import ModelConfig
    seen = []
    def cache(configs, texts, store, path):
        seen.extend(texts)
        return evaluation.CachedEmbeddings(configs, {text: [1.] + [0.] * 2047 for text in texts}), 0
    monkeypatch.setattr(evaluation, 'cache_embeddings', cache)
    corpus = tmp_path / 'conversation.json'
    corpus.write_text(json.dumps({'subjects': [{'name': 'Iris', 'aliases': ['小鸢']}],
        'memories': [{'id': 'm', 'content': '我偏爱室内乐', 'about': ['我']}],
        'queries': [{'id': 'q', 'text': None, 'participants': None, 'entry_kind': 'live',
            'categories': ['对话中准备'], 'recent_messages': [
                {'id': 'a', 'speaker': '来客', 'content': '晚安。'},
                {'id': 'b', 'speaker': '新观众', 'content': '主播喜欢听什么音乐？'}],
            'relevant': {'m': 3}}]}, ensure_ascii=False), encoding='utf-8')
    _, report = run_recall_eval({'embedding': ModelConfig('fake', '', 'fake-vector', 2048)}, tmp_path, corpus=corpus,
                                out=tmp_path / 'out')
    hybrid = report['variants']['jieba_hybrid']
    assert hybrid['groups']['dev']['categories']['对话中准备']['recall_at_8'] == 1
    assert hybrid['queries'][0]['reasons']['m'] == 'relevant'
    assert any('晚安' in text and '我喜欢听什么音乐' in text for text in seen)


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
    assert report["variants"]["trigram_fts"]["metrics"]["recall_at_8"] == 1
    assert report["details"]["variants"]["trigram_fts"][0]["returned"] == ["b"]
    assert "M2" in path.read_text(encoding="utf-8")


def test_calibration_compares_full_prefix_grid(tmp_path, monkeypatch):
    from iris import recall_evaluation as evaluation
    from iris.models import ModelConfig
    seen = []
    def cache(configs, texts, store, path):
        seen.extend(texts)
        return evaluation.CachedEmbeddings(configs, {text: [1.] + [0.] * 2047 for text in texts}), 0
    monkeypatch.setattr(evaluation, 'cache_embeddings', cache)
    corpus = tmp_path / 'recall.json'
    corpus.write_text(json.dumps({'memories': [{'id': 'm', 'content': '看星星'}],
        'queries': [{'id': 'q', 'text': '天文', 'relevant': {'m': 3}}]}, ensure_ascii=False), encoding='utf-8')
    _, report = run_recall_eval({'embedding': ModelConfig('fake', '', 'fake-vector', 2048)},
                                tmp_path, 'dev', corpus=corpus, calibrate=True)
    trials = report['calibration']['trials']
    hybrids = [t for t in trials if 'hybrid' in t['variant']]
    assert len(hybrids) == 2 * 4 * 3 * 3 * 2 * 3 * 2
    assert {t["settings"]["lexical_min"] for t in hybrids} == {.35, .5, .75}
    assert {t["settings"]["lexical_max_df"] for t in hybrids} == {0, 2}
    assert len({tuple(t['settings'].values()) for t in hybrids}) == len(hybrids)
    assert {t['settings']['query_prefix'] for t in hybrids} == {'', '为这个问题检索能回答它的个人记忆：'}
    assert '天文' in seen and '为这个问题检索能回答它的个人记忆：天文' in seen
    chosen = evaluation.select_trial(trials)
    assert report['calibration']['recommended_settings'] == chosen['settings']
    assert report['variants'][chosen['variant']]['settings'] == chosen['settings']
