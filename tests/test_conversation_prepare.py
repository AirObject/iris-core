"""Automatic query focus without changing explicit host queries or learning."""
from pathlib import Path

import numpy as np
import pytest

from conftest import msg
from iris.models import ModelConfig
from iris.recall_evaluation import load_corpus, seed_corpus
from iris.retrieval import Retrieval
from test_retrieval import put


class FixedEmbedding:
    configs = {'embedding': ModelConfig('fake', '', 'fake-vector')}

    def __init__(self, store, on_call=None):
        self.store, self.on_call = store, on_call
        self.texts = []

    def embedding(self, text, purpose='embedding'):
        assert not self.store._writer.in_transaction
        self.texts.append(text)
        if self.on_call:
            self.on_call()
        return [1., 0.]


def scenario(store, *, latest='你研究哪种航海图？', trailing=None):
    data = {'memories': [
        {'id': 'old', 'content': '严澈研究航海图，尤其留意海岸线。', 'about': ['严澈'], 'speaker': '严澈'},
        {'id': 'new', 'content': '裴昭研究航海图，尤其留意洋流。', 'about': ['裴昭'], 'speaker': '裴昭'},
        {'id': 'self', 'content': '我研究十九世纪的航海图，比较旧港口的位置。', 'about': ['我']}],
        'queries': [{'id': 'q', 'text': None, 'participants': None, 'relevant': {}, 'recent_messages': [
            {'id': 'a', 'speaker': '访客', 'content': '严澈也在研究航海图。'},
            {'id': 'b', 'speaker': 'Iris', 'kind': 'self_output', 'content': '严澈提过这件事。'},
            {'id': 'c', 'speaker': '访客', 'content': latest}, *(trailing or [])]}]}
    ids, entries = seed_corpus(store, data)
    with store.write() as conn:
        conn.execute("UPDATE memories SET embedding=?,embedding_model='fake-vector'",
                     (np.asarray([1., 0.], dtype=np.float32).tobytes(),))
    return ids, entries['q']


@pytest.mark.parametrize('vector_enabled', [False, True])
def test_earlier_named_person_does_not_exclude_current_answer(store, vector_enabled):
    ids, entry = scenario(store)
    response = Retrieval(store, FixedEmbedding(store) if vector_enabled else None, vector_min=.35).prepare(entry)
    assert ids['self'] in [m['id'] for m in response['memories']]


@pytest.mark.parametrize('vector_enabled', [False, True])
@pytest.mark.parametrize('latest', ['裴昭研究哪种航海图？', '裴昭是谁？'])
def test_only_latest_named_person_limits_automatic_results(store, latest, vector_enabled):
    ids, entry = scenario(store, latest=latest)
    response = Retrieval(store, FixedEmbedding(store) if vector_enabled else None, vector_min=.35).prepare(entry)
    assert [m['id'] for m in response['memories']] == [ids['new']]


@pytest.mark.parametrize('vector_enabled', [False, True])
@pytest.mark.parametrize('kind', ['self_output', 'action_result', 'event'])
def test_nonhuman_trailing_message_never_sets_query_or_anchor(store, kind, vector_enabled):
    ids, entry = scenario(store, latest='裴昭研究哪种航海图？', trailing=[
        {'id': 'd', 'speaker': '系统', 'kind': kind, 'content': '严澈研究航海图。'}])
    gateway = FixedEmbedding(store)
    retrieval = Retrieval(store, gateway if vector_enabled else None, vector_min=.35)
    response = retrieval.prepare(entry)
    assert [m['id'] for m in response['memories']] == [ids['new']]
    if vector_enabled:
        assert '严澈研究航海图。' not in gateway.texts[0]
    assert response['recent_messages'][-1]['kind'] == kind


def test_explicit_host_text_keeps_its_own_anchors(store):
    ids, entry = scenario(store, latest='裴昭研究哪种航海图？')
    retrieval = Retrieval(store, FixedEmbedding(store), vector_min=.35)
    assert retrieval.prepare_query(entry, '严澈的航海图')[0] == '严澈的航海图'
    assert [m['id'] for m in retrieval.prepare(entry, text='严澈的航海图', participants=[])['memories']] == [ids['old']]
    assert retrieval.prepare_query(entry, '')[0] == ''


def test_no_other_message_does_not_use_role_output_as_query(store):
    from iris.memory_ops import setup_role
    setup_role(store, 'Iris')
    msg(store, 1, '航海图', kind='self_output')
    mid = put(store, '我研究航海图')
    gateway = FixedEmbedding(store)
    retrieval = Retrieval(store, gateway, vector_min=.35)
    assert retrieval.prepare_query('A')[0] == ''
    assert retrieval.prepare('A')['memories'] == []
    assert gateway.texts == []


def test_anchor_is_frozen_with_query_before_embedding(store):
    ids, entry = scenario(store, latest='裴昭研究哪种航海图？')
    def arrival():
        msg(store, 4, '严澈的航海图呢？', entry=entry)
    gateway = FixedEmbedding(store, arrival)
    response = Retrieval(store, gateway, vector_min=.35).prepare(entry)
    assert response['recent_messages'][-1]['content'] == '严澈的航海图呢？'
    assert [m['id'] for m in response['memories']] == [ids['new']]


@pytest.mark.parametrize('strategy,count', [('latest_1', 1), ('latest_2', 2), ('latest_3', 3), ('window_5', 2)])
def test_candidate_composition_uses_only_other_messages(store, strategy, count):
    from iris.memory_ops import setup_role
    setup_role(store, 'Iris')
    msg(store, 1, '旧话题')
    msg(store, 2, '旧回复', kind='self_output')
    msg(store, 3, '这里是上一条的上下文')
    msg(store, 4, '没有记录', kind='self_output')
    msg(store, 5, '那她呢？')
    msg(store, 6, '某人进入', kind='event')
    retrieval = Retrieval(store, conversation_query=strategy)
    expected = ['旧话题', '这里是上一条的上下文', '那她呢？'][-count:]
    assert retrieval.prepare_query('A')[0] == '\n'.join(expected)


def test_recall_corpus_kind_and_cross_entry_source_are_real(store):
    root = Path(__file__).resolve().parents[1]
    data = load_corpus(root / 'evals/recall_conversation_v2.json')
    ids, entries = seed_corpus(store, data)
    with store.read() as conn:
        row = conn.execute("SELECT kind,sender_subject_id FROM messages WHERE content='顾棠进入聊天室。'").fetchone()
        assert tuple(row) == ('event', 'scene')
        row = conn.execute("SELECT kind,sender_subject_id FROM messages WHERE content='链接没打开，我先回应刚才的问题。'").fetchone()
        assert tuple(row) == ('self_output', 'self')
        source_entry = conn.execute('SELECT m.entry_id FROM sources s JOIN messages m ON m.id=s.message_id WHERE s.memory_id=?', (ids['D08'],)).fetchone()[0]
        assert source_entry == entries['U21'] != entries['U03']
    response = Retrieval(store).prepare(entries['U03'], text='石阶旧影院的无障碍入口', participants=[])
    memory = next(m for m in response['memories'] if m['id'] == ids['D08'])
    assert memory['sources'][0]['entry_id'] == entries['U21']
    assert all(m['entry_id'] == entries['U03'] for m in response['recent_messages'])


def conversation(store, messages):
    _, entries = seed_corpus(store, {'memories': [], 'queries': [{
        'id': 'focus', 'text': None, 'participants': None, 'relevant': {},
        'recent_messages': messages}]})
    return entries['focus']


@pytest.mark.parametrize('strategy', ['session_6', 'speaker_6', 'adaptive_6'])
@pytest.mark.parametrize('age,include', [(300, True), (301, False), (-1, False)])
def test_topic_time_boundary_uses_message_time_and_excludes_outputs(store, strategy, age, include):
    from datetime import datetime, timedelta, timezone
    latest = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    entry = conversation(store, [
        {'id': 'a', 'speaker': '客人', 'content': '前文对象', 'occurred_at': (latest-timedelta(seconds=age)).isoformat()},
        {'id': 'b', 'speaker': 'Iris', 'kind': 'self_output', 'content': '不应检索的回复', 'occurred_at': latest.isoformat()},
        {'id': 'c', 'speaker': '客人', 'content': '那件呢？', 'occurred_at': latest.isoformat()}])
    retrieval = Retrieval(store, conversation_query=strategy)
    query, _, anchor = retrieval._prepare_context(entry, None)
    assert query == ('前文对象\n那件呢？' if include else '那件呢？')
    assert anchor == '那件呢？'


def test_invalid_time_keeps_latest_without_crossing_unknown_boundary(store):
    entry = conversation(store, [
        {'id': 'a', 'speaker': '访客', 'content': '更早上文'},
        {'id': 'b', 'speaker': '访客', 'content': '边界'},
        {'id': 'c', 'speaker': '访客', 'content': '继续呢？'}])
    with store.write() as conn:
        conn.execute("UPDATE messages SET occurred_at='unknown' WHERE content='边界'")
    assert Retrieval(store, conversation_query='session_6').prepare_query(entry)[0] == '继续呢？'


@pytest.mark.parametrize('strategy', ['session_6', 'speaker_6', 'adaptive_6'])
def test_interruption_keeps_earlier_question_without_changing_latest_anchor(store, strategy):
    entry = conversation(store, [
        {'id': 'a', 'speaker': '访客', 'content': '齐峦的玻璃水壶应该用什么清洗？'},
        {'id': 'b', 'speaker': 'Iris', 'kind': 'self_output', 'content': '不记得了'},
        {'id': 'c', 'speaker': '插话者', 'content': '屏幕太暗了'},
        {'id': 'd', 'speaker': '插话者', 'content': '调亮一点'},
        {'id': 'e', 'speaker': '访客', 'content': '你记得吗？'}])
    query, _, anchor = Retrieval(store, conversation_query=strategy)._prepare_context(entry, None)
    assert '齐峦的玻璃水壶应该用什么清洗？' in query
    assert anchor == '你记得吗？'
    assert '不记得了' not in query
    if strategy == 'speaker_6':
        assert '屏幕太暗了' not in query and '调亮一点' in query


@pytest.mark.parametrize('latest,context', [
    ('清理铜版画的印刷滚筒需要什么溶剂，最后如何擦干存放？', False),
    ('她清理铜版画的印刷滚筒需要什么溶剂，最后如何擦干存放？', True),
    ('那台设备清理铜版画的印刷滚筒需要什么溶剂，最后如何擦干存放？', True),
    ('再想想？我跟你说过的。', True)])
def test_adaptive_query_preserves_references_and_reminders_but_focuses_new_question(store, latest, context):
    entry = conversation(store, [
        {'id': 'a', 'speaker': '访客', 'content': '较早介绍的对象'},
        {'id': 'b', 'speaker': '访客', 'content': '附带的一句'},
        {'id': 'c', 'speaker': '访客', 'content': latest}])
    query, _, anchor = Retrieval(store, conversation_query='adaptive_6')._prepare_context(entry, None)
    assert ('较早介绍的对象' in query) is context
    assert query.endswith(latest) and anchor == latest


def test_topic_history_is_bounded(store):
    messages = [{'id': str(i), 'speaker': '访客', 'content': f'第{i}句上文'} for i in range(22)]
    entry = conversation(store, messages)
    retrieval = Retrieval(store, conversation_query='session_6')
    assert retrieval.prepare_query(entry)[0].splitlines() == [m['content'] for m in messages[-6:]]


@pytest.mark.parametrize('strategy', ['session_6', 'speaker_6', 'adaptive_6'])
def test_new_compositions_leave_explicit_empty_and_nonempty_text_unchanged(store, strategy):
    _, entry = scenario(store)
    retrieval = Retrieval(store, conversation_query=strategy)
    for text in ('', '宿主自己写的查询'):
        query, _, anchor = retrieval._prepare_context(entry, text)
        assert query == text and anchor is None
