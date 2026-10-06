import pytest

from iris.models import ModelError
from iris.query_analysis import SubjectNames
from iris.retrieval import Retrieval
from test_recall_quality import people
from test_retrieval import Embeddings, entry, put


def retriever(store, path):
    if path == 'hybrid':
        return Retrieval(store, Embeddings(store), tokenizer='trigram', vector_min=.5)
    if path == 'trigram_fts':
        return Retrieval(store, tokenizer='trigram')
    if path == 'failed_embedding':
        class Failed(Embeddings):
            def embedding(self, text, purpose='embedding'):
                raise ModelError('transient', 'offline')
        return Retrieval(store, Failed(store), vector_min=.5)
    return Retrieval(store)


@pytest.mark.parametrize('path', ['hybrid', 'trigram_fts', 'default_fallback', 'failed_embedding'])
@pytest.mark.parametrize('filtered', [False, True])
@pytest.mark.parametrize('keyword,content', [('围棋', '江澄每周在棋社下围棋'), ('琴', '江澄把琴放在窗边')])
def test_short_keywords_keep_lexical_matches(store, path, filtered, keyword, content):
    people(store)
    right = put(store, content, about=['p'], vector=[0., 1.])
    other = put(store, f'江橙也提到过{keyword}', about=['q'], vector=[0., 1.])
    put(store, '两位朋友刚看过云朵', vector=[0., 1.])
    result = retriever(store, path).search(text=keyword, people=['p'] if filtered else [])
    assert {m['id'] for m in result['memories']} == ({right} if filtered else {right, other})
    assert all(m['reason'] == 'relevant' for m in result['memories'])


@pytest.mark.parametrize('path', ['hybrid', 'trigram_fts'])
def test_mixed_short_and_long_terms_keep_both_lanes_without_duplicates(store, path):
    short = put(store, '围棋使用黑白棋子', vector=[0., 1.])
    long = put(store, '红楼梦是一本小说', vector=[0., -1.])
    both = put(store, '我看完红楼梦后去下围棋', vector=[.4, .916515])
    r = retriever(store, path)
    # Exercise both lanes at 50% coverage; the calibrated default can be stricter.
    r.overrides['lexical_min'] = .5
    result = r.search(text='围棋 红楼梦')['memories']
    assert {m['id'] for m in result} == {short, long, both}
    assert len(result) == 3


@pytest.mark.parametrize('path', ['hybrid', 'trigram_fts', 'default_fallback'])
@pytest.mark.parametrize('location', ['about', 'speaker', 'body'])
def test_name_only_query_keeps_absent_subject_evidence(store, path, location):
    entry(store)
    people(store)
    kwargs = {'about': [], 'vector': [0., 1.]}
    text = '这位朋友在档案馆担任讲解员'
    if location == 'about':
        kwargs['about'] = ['p']
    elif location == 'speaker':
        kwargs['speaker'] = 'p'
    else:
        text = '来客说小江在档案馆担任讲解员'
    right = put(store, text, **kwargs)
    put(store, '江橙在钟表店维修手表', about=['q'], vector=[0., 1.])
    r = retriever(store, path)
    for query in ('江澄是谁？', '小江是谁来着'):
        result = r.prepare('A', text=query, participants=['q'])['memories']
        assert [(m['id'], m['reason']) for m in result] == [(right, 'relevant')]


def test_name_only_candidates_respect_filters_and_do_not_scan_all_prose(store, monkeypatch):
    people(store)
    right = put(store, '档案馆的讲解员', about=['p'], kind='事实')
    body = put(store, '江澄以前在钟表店工作', about=['q'], kind='事实')
    put(store, '江澄想学修表', about=['p'], kind='计划')
    put(store, '江澄早年的工作地点', about=['p'], lifecycle='forgotten')
    unrelated = '没有任何姓名的无关正文'
    put(store, unrelated)
    seen = []
    original = SubjectNames.mentioned
    def tracked(self, text):
        seen.append(text)
        return original(self, text)
    monkeypatch.setattr(SubjectNames, 'mentioned', tracked)
    r = Retrieval(store, tokenizer='trigram')
    result = r.search(text='江澄是谁', kinds=['事实'])['memories']
    assert {m['id'] for m in result} == {right, body}
    assert unrelated not in seen
    assert [m['id'] for m in r.search(text='江澄是谁', people=['p'], kinds=['事实'])['memories']] == [right]


def test_short_topic_does_not_broaden_into_name_only_query(store):
    people(store)
    put(store, '江澄在档案馆工作', about=['p'], vector=[0., 1.])
    r = retriever(store, 'hybrid')
    assert r.search(text='江澄的租金')['memories'] == []
    assert r.search(text='小江的租金')['memories'] == []
    assert r.search(text='租金', people=['p'])['memories'] == []


def test_short_lane_applies_name_anchor_before_candidate_cap(store, monkeypatch):
    from iris import retrieval as module
    people(store)
    monkeypatch.setattr(module, 'CANDIDATES', 2)
    for number in range(5):
        put(store, f'江橙参加围棋比赛第{number}场', about=['q'])
    first = put(store, '江澄学围棋已经三年', about=['p'])
    second = put(store, '江澄的围棋棋盘在阁楼', about=['p'])
    result = Retrieval(store, tokenizer='trigram').search(text='小江的围棋')['memories']
    assert {m['id'] for m in result} == {first, second}


def test_learning_context_does_not_add_absent_person_highlights(store):
    people(store)
    own = put(store, '常去图书馆看建筑画册', about=['p'], vector=[0., 1.])
    third_party = put(store, '陶泥需要在阴凉处存放', about=['q'], vector=[0., 1.])
    result = Retrieval(store, Embeddings(store)).learning_context('江橙刚刚来过', ['p'])
    assert own in {m['id'] for m in result}
    assert third_party not in {m['id'] for m in result}


@pytest.mark.parametrize('path', ['hybrid', 'trigram_fts', 'default_fallback'])
def test_lexical_candidates_must_cover_more_than_one_incidental_short_word(store, path):
    weak = put(store, '围棋棋盘收在阁楼', vector=[0., 1.])
    strong = put(store, '露营时大家下围棋，后来又一起练习书法', vector=[0., -1.])
    result = retriever(store, path).search(text='围棋 露营 书法 骑行')['memories']
    assert strong in {m['id'] for m in result}
    assert weak not in {m['id'] for m in result}


@pytest.mark.parametrize('path', ['hybrid', 'trigram_fts'])
def test_short_lane_coverage_includes_long_query_terms(store, path):
    weak = put(store, '围棋是黑白棋子的游戏', vector=[0., 1.])
    strong = put(store, '围棋与红楼梦、交响乐都在这场文化活动中出现', vector=[0., -1.])
    result = retriever(store, path).search(text='围棋 红楼梦 交响乐')['memories']
    assert strong in {m['id'] for m in result}
    assert weak not in {m['id'] for m in result}


@pytest.mark.parametrize('path', ['hybrid', 'trigram_fts'])
@pytest.mark.parametrize('filtered', [False, True])
def test_rare_long_fragment_survives_long_query_but_common_fragment_does_not(store, path, filtered):
    people(store)
    rare = put(store, '显微镜的镜片要保持干燥', about=['p'], vector=[0., 1.])
    also_rare = put(store, '显微镜放在实验室的柜子里', about=['p'], vector=[0., -1.])
    common = [put(store, text, about=[who], vector=[0., 1.]) for who, text in (
        ('p', '天文台的门票在周三发售'), ('q', '天文台周末需要提前预约'), ('q', '天文台旁边有一座钟楼'))]
    r = retriever(store, path)
    r.overrides.update(lexical_min=.5, lexical_max_df=2)
    result = r.search(text='显微镜 天文台 陶艺馆 自行车', people=['p'] if filtered else [])['memories']
    assert {m['id'] for m in result} == {rare, also_rare}
    assert not {m['id'] for m in result}.intersection(common)
