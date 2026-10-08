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
