"""Full-text fallback has its own settings without changing existing installations."""
import json

import pytest

from iris.models import ModelConfig, ModelError
from iris.retrieval import DEFAULTS, Retrieval
from test_retrieval import put


class ControlledEmbeddings:
    def __init__(self, outcome='ok'):
        self.outcome = outcome
        self.calls = 0
        self.configs = {'embedding': ModelConfig('fake', '', 'fake-vector', 2)}

    def embedding(self, text, purpose='embedding'):
        self.calls += 1
        if self.outcome in ('failure', 'timeout'):
            raise ModelError('transient', self.outcome)
        return [1., 0.]


def configure(store, **settings):
    store.set_setting('retrieval', {**DEFAULTS, 'embedding_model': 'fake-vector',
        'embedding_dimensions': 2, 'tokenizer': 'trigram', 'lexical_min': .75,
        'lexical_max_df': 0, 'fallback_tokenizer': 'jieba',
        'fallback_lexical_min': .1, 'fallback_lexical_max_df': 0, **settings})


@pytest.mark.parametrize('reason', ['unconfigured', 'uncalibrated', 'dimensions', 'failure', 'timeout'])
def test_fallback_uses_own_coverage_on_every_degradation_path(store, reason):
    configure(store)
    target = put(store, '围棋棋盘放在柜子里', vector=[0., 1.])
    gateway = ControlledEmbeddings(reason) if reason != 'unconfigured' else None
    if reason == 'uncalibrated':
        configure(store, embedding_model='previous-model')
    elif reason == 'dimensions':
        configure(store, embedding_dimensions=2048)
    retrieval = Retrieval(store, gateway)
    reply = retrieval.search(text='围棋 露营 书法 骑行')
    assert [m['id'] for m in reply['memories']] == [target]
    assert retrieval.settings['tokenizer'] == 'jieba'
    assert retrieval.settings['lexical_min'] == .1
    assert retrieval.settings['lexical_max_df'] == 0
    if reason in ('uncalibrated', 'dimensions'):
        assert gateway.calls == 0


def test_fallback_uses_own_long_fragment_frequency(store):
    configure(store, fallback_tokenizer='trigram', fallback_lexical_min=1., fallback_lexical_max_df=2)
    target = put(store, '显微镜的镜片需要保持干燥')
    result = Retrieval(store).search(text='显微镜 陶艺馆 自行车')
    assert [m['id'] for m in result['memories']] == [target]
    configure(store, lexical_max_df=2, fallback_lexical_min=1., fallback_lexical_max_df=0)
    assert Retrieval(store).search(text='显微镜 陶艺馆 自行车')['memories'] == []


def test_hybrid_thresholds_are_restored_after_temporary_fallback(store):
    configure(store)
    target = put(store, '围棋棋盘放在柜子里', vector=[0., 1.])
    gateway = ControlledEmbeddings('failure')
    retrieval = Retrieval(store, gateway)
    assert [m['id'] for m in retrieval.search(text='围棋 露营 书法 骑行')['memories']] == [target]
    gateway.outcome = 'ok'
    assert retrieval.search(text='围棋 露营 书法 骑行')['memories'] == []
    assert (retrieval.settings['tokenizer'], retrieval.settings['lexical_min'], retrieval.settings['lexical_max_df']) == ('trigram', .75, 0)


@pytest.mark.parametrize('legacy_coverage', [.1, .75])
def test_existing_settings_without_new_fields_keep_their_interpretation(store, legacy_coverage):
    saved = {key: value for key, value in DEFAULTS.items() if key not in ('fallback_lexical_min', 'fallback_lexical_max_df')}
    saved.update(lexical_min=legacy_coverage, lexical_max_df=0, fallback_tokenizer='jieba')
    store.set_setting('retrieval', saved)
    target = put(store, '围棋棋盘放在柜子里')
    retrieval = Retrieval(store)
    result = retrieval.search(text='围棋 露营 书法 骑行')
    assert [m['id'] for m in result['memories']] == ([target] if legacy_coverage == .1 else [])
    assert retrieval.settings['lexical_min'] == legacy_coverage
    assert retrieval.settings['lexical_max_df'] == 0
    assert store.setting('retrieval') == saved


def test_new_database_persists_independent_fallback_defaults(store):
    saved = store.setting('retrieval')
    for field in ('tokenizer', 'lexical_min', 'lexical_max_df'):
        assert 'fallback_' + field in saved
        assert saved['fallback_' + field] == DEFAULTS['fallback_' + field]


def test_dev_calibration_selects_fulltext_independently_with_low_coverage_grid(tmp_path):
    from iris.recall_evaluation import run_recall_eval, select_trial
    corpus = tmp_path / 'fixture.json'
    corpus.write_text(json.dumps({'memories': [{'id': 'm', 'content': '围棋棋盘放在柜子里'}],
        'queries': [{'id': 'q', 'text': '围棋 露营 书法 骑行', 'relevant': {'m': 3}}]}, ensure_ascii=False), encoding='utf-8')
    _, report = run_recall_eval({}, tmp_path, 'dev', corpus=corpus, calibrate=True)
    calibration = report['calibration']
    trials = [t for t in calibration['trials'] if t['variant'].endswith('_fts')]
    assert {t['settings']['lexical_min'] for t in trials} >= {0., .1, .25, .35, .5, .75}
    chosen = select_trial(trials)
    assert calibration['recommended_fallback_variant'] == chosen['variant']
    assert calibration['recommended_fallback_settings'] == {
        'fallback_' + field: chosen['settings'][field] for field in ('tokenizer', 'lexical_min', 'lexical_max_df')}


def test_zero_coverage_accepts_any_matching_term_but_never_an_unmatched_memory(store):
    configure(store, fallback_lexical_min=0.)
    target = put(store, '围棋棋盘放在柜子里')
    put(store, '显微镜的镜片需要保持干燥')
    assert [m['id'] for m in Retrieval(store).search(text='围棋 露营 书法 骑行')['memories']] == [target]
    assert Retrieval(store).search(text='红楼梦 陶艺馆')['memories'] == []


def test_explicit_fallback_override_takes_precedence_over_lane_comparison_override(store):
    configure(store)
    target = put(store, '围棋棋盘放在柜子里')
    r = Retrieval(store, lexical_min=1., fallback_lexical_min=0.)
    assert [m['id'] for m in r.search(text='围棋 露营 书法 骑行')['memories']] == [target]


@pytest.mark.parametrize('saved', [{}, {'tokenizer': 'trigram', 'lexical_min': .75, 'lexical_max_df': 2}])
def test_older_settings_without_any_fallback_fields_inherit_shared_values(store, saved):
    store.set_setting('retrieval', saved)
    put(store, '围棋棋盘放在柜子里')
    r = Retrieval(store)
    assert r.search(text='围棋 露营 书法 骑行')['memories'] == []
    for field in ('tokenizer', 'lexical_min', 'lexical_max_df'):
        assert r.settings[field] == saved.get(field, DEFAULTS[field])
    assert store.setting('retrieval') == saved


@pytest.mark.parametrize('include_forgotten', [False, True])
def test_named_fts_stream_preserves_lifecycle_before_candidate_cap(store, monkeypatch, include_forgotten):
    from iris import retrieval as module
    from test_recall_quality import people
    people(store)
    monkeypatch.setattr(module, 'CANDIDATES', 2)
    put(store, '围棋', about=['p'], lifecycle='deleted')
    forgotten = put(store, '围棋', about=['p'], lifecycle='forgotten')
    first = put(store, '围棋 山林', about=['p'])
    second = put(store, '围棋 城堡 海风', about=['p'])
    result = Retrieval(store).search(text='江澄的围棋', include_forgotten=include_forgotten)
    assert {m['id'] for m in result['memories']} == ({forgotten, first} if include_forgotten else {first, second})


def test_named_prefilter_keeps_body_aliases_and_longest_label_boundaries(store):
    from test_recall_quality import people
    from iris.db import now
    people(store)
    with store.write() as conn:
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('longer','person','江澄妈妈',?)", (now(),))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('p','Ann')")
    alias = put(store, '小江带来了围棋棋盘', about=['q'])
    latin = put(store, 'ANN 说围棋棋盘在门口', about=['q'])
    put(store, '江澄妈妈带来了围棋棋盘', about=['q'])
    put(store, 'annual 围棋活动的通知', about=['q'])
    result = Retrieval(store).search(text='江澄的围棋')
    assert {m['id'] for m in result['memories']} == {alias, latin}
