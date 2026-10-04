import numpy as np
import pytest

from iris.db import now
from iris.memory_ops import setup_role
from iris.queue import add_message
from iris.retrieval import Retrieval
from iris.search_text import query_terms
from test_retrieval import Embeddings, entry, put
from test_recall_quality import people


def conversation(store, kind, messages):
    setup_role(store, 'Iris')
    for index, (speaker, content) in enumerate(messages):
        add_message(store, entry_id='chat', entry_name='chat', platform='test', entry_kind=kind,
                    kind='message', sender=speaker, content=content,
                    occurred_at=f'2026-10-04T12:00:{index:02}+00:00', dedupe_key=str(index))
    with store.read() as conn:
        return {r['name']: r['id'] for r in conn.execute("SELECT id,name FROM subjects WHERE kind='person'")}


@pytest.mark.parametrize(('kind', 'question'), [
    ('private', '你常听哪种音乐？'), ('group', 'Iris 喜欢什么音乐？'),
    ('private', '小鸢最近听些什么？'), ('live', '主播爱听什么？')])
def test_default_prepare_recalls_self_without_excluding_others(store, kind, question):
    participants = conversation(store, kind, [('来客', question)])
    with store.write() as conn:
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('self','小鸢')")
    own = put(store, '我偏爱室内乐', vector=[.5, np.sqrt(.75)])
    other = put(store, '来客收藏了巴赫的唱片', about=[participants['来客']], vector=[.5, np.sqrt(.75)])
    response = Retrieval(store, Embeddings(store), vector_min=.35, vector_relative=.75).prepare('chat')
    reasons = {m['id']: m['reason'] for m in response['memories']}
    assert reasons[own] == reasons[other] == 'relevant'


def test_unmentioned_absent_person_is_not_filtered_by_participants(store):
    conversation(store, 'group', [('甲', '有没有人会照顾盆栽？'), ('乙', '记得以前认识一个能救活兰花的。')])
    people(store)
    absent = put(store, '江澄有园艺经验', about=['p'], vector=[.5, np.sqrt(.75)])
    r = Retrieval(store, Embeddings(store), vector_min=.35)
    assert absent in [m['id'] for m in r.prepare('chat')['memories']]
    assert absent in [m['id'] for m in r.prepare('chat', participants=['甲'])['memories']]


def test_named_anchors_allow_about_speaker_body_and_ambiguous_names(store):
    entry(store)
    people(store)
    with store.write() as conn:
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('p2','person','江澄',?)", (now(),))
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('parent','person','江澄妈妈',?)", (now(),))
    about = put(store, '这位朋友收集蓝色邮票', about=['p'], vector=[.5, np.sqrt(.75)])
    speaker = put(store, '我收集蓝色邮票', speaker='p', about=[], vector=[.5, np.sqrt(.75)])
    body = put(store, '江橙把蓝色邮票送给小江', speaker='q', about=['q'], vector=[.5, np.sqrt(.75)])
    same_name = put(store, '另一位江澄交换蓝色邮票', about=['p2'], vector=[.5, np.sqrt(.75)])
    put(store, '江橙收集蓝色邮票', about=['q'], vector=[1., 0.])
    put(store, '江澄妈妈整理蓝色邮票', about=['parent'], vector=[1., 0.])
    r = Retrieval(store, Embeddings(store), vector_min=.35)
    for result in (r.search(text='江澄收藏的蓝色邮票'), r.prepare('A', text='江澄收藏的蓝色邮票', participants=['q'])):
        assert {m['id'] for m in result['memories']} == {about, speaker, body, same_name}
        assert all(m['reason'] == 'relevant' for m in result['memories'])


def test_highlights_fill_after_relevance_in_recent_speaker_round_robin(store):
    ids = conversation(store, 'group', [('甲', '先来的'), ('乙', '到啦'), ('甲', '刚补了一句')])
    relevant = put(store, '彗星经过北方天空', about=[], importance=1)
    a1 = put(store, '甲需要无乳饮食', about=[ids['甲']], importance=95)
    a2 = put(store, '甲每月去河岸捡垃圾', about=[ids['甲']], importance=85)
    b1 = put(store, '乙照顾流浪狗', about=[ids['乙']], importance=90)
    put(store, '乙收藏陶片', about=[ids['乙']], importance=80)
    put(store, '我的重要背景', importance=100)
    r = Retrieval(store)
    response = r.prepare('chat', text='彗星')
    assert [(m['id'], m['reason']) for m in response['memories']] == [
        (relevant, 'relevant'), (a1, 'person_highlight'), (b1, 'person_highlight'), (a2, 'person_highlight')]
    explicit = r.prepare('chat', text='彗星', participants=[ids['乙'], 'self', ids['甲'], 'scene'])
    assert [m['id'] for m in explicit['memories'][:3]] == [relevant, b1, a1]
    assert all(m['reason'] == 'relevant' for m in r.search(text='彗星')['memories'])
    assert [m['id'] for m in r.search(text='彗星')['memories']] == [relevant]


def test_relevant_highlight_overlap_and_redundancy_do_not_consume_slots(store):
    ids = conversation(store, 'private', [('甲', '水彩用棉浆纸')])
    with store.read() as conn:
        recent_id = conn.execute('SELECT id FROM messages').fetchone()[0]
    already = put(store, '水彩用棉浆纸', about=[ids['甲']], importance=100, evidence=[recent_id])
    relevant = put(store, '水彩颜料需要留出干燥时间', about=[ids['甲']], importance=90)
    known = put(store, '甲会拉大提琴', about=[ids['甲']], importance=80)
    highlight = put(store, '甲养了一只鹦鹉', about=[ids['甲']], importance=70)
    response = Retrieval(store).prepare('chat', text='水彩', known_memory_ids=[known])
    assert [(m['id'], m['reason']) for m in response['memories']] == [(relevant, 'relevant'), (highlight, 'person_highlight')]
    assert already not in [m['id'] for m in response['memories']]


def test_learning_has_own_unprefixed_thresholds_and_no_anchor_filter(store):
    people(store)
    class Recording(Embeddings):
        def embedding(self, text, purpose='embedding', **kwargs):
            self.text = text
            return super().embedding(text, purpose)
    gateway = Recording(store)
    store.set_setting('learning_retrieval', {'embedding_model': 'fake-vector'})
    strong = put(store, '江橙偏爱岩壁活动', about=['q'], vector=[.7, np.sqrt(.51)])
    weak = put(store, '我去过一座海岛', vector=[.6, .8])
    r = Retrieval(store, gateway, vector_min=.1, vector_relative=.99, query_prefix='问题检索前缀：')
    text = '江澄刚讨论了另一位朋友'
    result = r.learning_context(text, [])
    assert gateway.text == text
    assert strong in [m['id'] for m in result] and weak not in [m['id'] for m in result]


def test_learning_keeps_pr4_lexical_coverage_independently(store):
    weak = put(store, '苹果')
    strong = put(store, '苹果 梨 葡萄')
    r = Retrieval(store, vector_min=0)
    assert [m['id'] for m in r.learning_context('苹果 梨 葡萄 芒果', [])] == [strong]
    assert weak in [m['id'] for m in r.search(text='苹果 梨 葡萄 芒果')['memories']]


def test_query_stopwords_do_not_discard_content_words(store):
    for word in ('提前', '注意', '值得', '台', '哪家', '回想', '平时', '聊天', '事情'):
        assert word in query_terms(word, 'jieba')


def test_trigram_query_removes_grammar_without_stitching_terms():
    tokens = query_terms('能不能问问红楼梦是什么？', 'trigram')
    assert '能不能' not in tokens and '是什么' not in tokens
    assert '红楼梦' in tokens
    assert not query_terms('甲是乙', 'trigram')


def test_role_sharing_a_name_does_not_remove_one_of_the_ambiguous_subjects(store):
    entry(store)
    with store.write() as conn:
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('double','person','Iris',?)", (now(),))
    own = put(store, '我收集邮票')
    other = put(store, '这位朋友也收集邮票', about=['double'])
    assert {m['id'] for m in Retrieval(store).search(text='Iris 的邮票')['memories']} == {own, other}


@pytest.mark.parametrize('configured', [False, True])
def test_default_fulltext_fallback_keeps_short_chinese_words(store, configured):
    from iris.retrieval import DEFAULTS
    store.set_setting('retrieval', {**DEFAULTS, 'tokenizer': 'trigram', 'fallback_tokenizer': 'jieba'})
    mid = put(store, '我收集邮票')
    gateway = Embeddings(store, fail=True) if configured else None
    result = Retrieval(store, gateway, vector_min=.45).search(text='邮票')
    assert [m['id'] for m in result['memories']] == [mid]
    assert result['memories'][0]['reason'] == 'relevant'
