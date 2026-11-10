"""Uncertain sources must not become assertions; deterministic and fake-model checks."""
from importlib.resources import files

import pytest

from iris.db import Store
from iris.memory_ops import setup_role
from iris.persona import (DEFAULT_PUBLISH_MODE, HANDWRITTEN_PROMPT_VERSION, PROMPT_VERSIONS, UNCERTAINTY,
                          UNCERTAINTY_ISSUE, _clause_parts, _proof_uncertainty_issues, _surface_issues,
                          _uncertainty_issues)
from test_persona import Clock, add_self, generate


OTHER = 'persona-eval:同伴'


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'uncertainty.db')
    setup_role(result, 'Iris', '我来自云城。')
    yield result
    result.close()


def memory(content, *excerpts, mid=1):
    pairs = [excerpt if isinstance(excerpt, tuple) else (excerpt, 'self') for excerpt in excerpts]
    return {'memory_id':mid, 'revision':1, 'content':content,
            'excerpts':[{'excerpt':text, 'speaker_subject_id':speaker} for text, speaker in pairs]}


def sentence(text, *memories, setting=False):
    return {'text':text, 'origin':'memory', 'basis':[{'memory_id':m['memory_id'], 'revision':1} for m in memories],
            'dates':['2026-10-01'], 'date_count':1, 'initial_setting':setting}


def flagged(text, *memories):
    return _uncertainty_issues(sentence(text, *memories), list(memories)) == [UNCERTAINTY_ISSUE]


@pytest.mark.parametrize('text,marked', [('我好像记得', True), ('大概三次', True), ('不太确定', True), ('也许吧', True),
    ('这不可能', False), ('有这种可能性', False), ('我喜欢雨天', False), ('我觉得很好', False)])
def test_uncertainty_is_a_closed_grammatical_class(text, marked):
    assert bool(UNCERTAINTY.search(text)) is marked


def test_dropped_hedge_is_flagged_and_any_hedge_of_the_class_keeps_it():
    source = memory('我好像更喜欢慢一点的节拍。', '我好像更喜欢慢一点的节拍')
    assert flagged('我更喜欢慢一点的节拍。', source)
    assert not flagged('我可能更喜欢慢一点的节拍。', source)
    assert not flagged('我似乎更喜欢慢一点的节拍。', source)


def test_hedge_in_an_unrelated_clause_does_not_constrain_this_claim():
    source = memory('我喜欢慢一点的节拍。', '我好像忘带谱子了，不过我喜欢慢一点的节拍')
    assert not flagged('我喜欢慢一点的节拍。', source)


@pytest.mark.parametrize('excerpt', ['好像吧，我喜欢慢一点的节拍', '我喜欢慢一点的节拍，大概吧'])
def test_bare_hedge_fragment_governs_its_neighbour(excerpt):
    assert flagged('我喜欢慢一点的节拍。', memory('我喜欢慢一点的节拍。', excerpt))


def test_definite_impossibility_is_not_uncertainty():
    source = memory('我不可能喜欢快节拍。', '我不可能喜欢快节拍')
    assert not flagged('我不喜欢快节拍。', source)


@pytest.mark.parametrize('content,excerpt', [
    ('我可能喜欢慢节拍。', '慢节拍我喜欢'),      # current text narrows a flat utterance
    ('我喜欢慢节拍。', '我好像喜欢慢节拍')])     # a confident summary cannot override it
def test_either_the_current_text_or_the_utterance_can_be_the_uncertain_one(content, excerpt):
    assert flagged('我喜欢慢节拍。', memory(content, excerpt))


def test_one_independent_certain_memory_ends_the_gate():
    hedged = memory('我好像喜欢慢节拍。', '我好像喜欢慢节拍', mid=1)
    certain = memory('我喜欢慢节拍。', '我喜欢慢节拍', mid=2)
    assert flagged('我喜欢慢节拍。', hedged)
    assert not flagged('我喜欢慢节拍。', hedged, certain)


def test_another_persons_hedge_says_nothing_about_this_speaker():
    assert not flagged('我喜欢慢节拍。', memory('我喜欢慢节拍。', ('你好像喜欢慢节拍', OTHER)))
    assert not flagged('我喜欢慢节拍。', memory('我喜欢慢节拍。', '我喜欢慢节拍\n引用原话：好像喜欢慢节拍'))


def test_hedge_scope_runs_rightwards_only():
    source = memory('我好像喜欢快节奏。', '我好像喜欢快节奏')
    assert flagged('我喜欢快节奏，可能也喜欢慢节拍。', source)
    assert not flagged('我好像喜欢快节奏，也喜欢慢节拍。', source)


def test_setting_sentences_are_checked_and_protected_rows_are_not():
    source = memory('设定中，我好像来自云城。')
    setting = sentence('设定中，我来自云城。', source, setting=True)
    assert UNCERTAINTY_ISSUE in _surface_issues(setting, memories=[source])
    assert _surface_issues({**setting, 'origin':'admin'}, memories=[source]) == []


def cited(text, focus_index, *entries):
    focus = _clause_parts(text)[focus_index - 1]
    return _proof_uncertainty_issues(text, focus, list(entries))


def proof(excerpt, quote, *, current=None, speaker='self'):
    view = {} if current is None else {'current_revision_text':current}
    return view, {'excerpt':excerpt, 'speaker_subject_id':speaker}, quote


def test_proof_gate_reads_the_cited_span_even_without_a_lexical_link():
    text = '在那次合奏里，我偏爱舒缓的速度。'
    assert cited(text, 2, proof('这次合奏我好像喜欢慢节拍', '这次合奏我好像喜欢慢节拍')) == [UNCERTAINTY_ISSUE]


def test_proof_gate_ignores_an_uncited_hedged_span_and_certain_second_proof():
    text = '我喜欢慢节拍。'
    assert cited(text, 1, proof('我好像累了，我喜欢慢节拍', '我喜欢慢节拍')) == []
    assert cited(text, 1, proof('我好像喜欢慢节拍', '我好像喜欢慢节拍'),
                 proof('我喜欢慢节拍', '我喜欢慢节拍')) == []


def test_proof_gate_applies_the_current_revision_constraint():
    assert cited('我喜欢慢节拍。', 1, proof('慢节拍我喜欢', '慢节拍我喜欢', current='我可能喜欢慢节拍')) == [UNCERTAINTY_ISSUE]


def test_proof_gate_skips_another_authors_quote():
    assert cited('我喜欢慢节拍。', 1, proof('你好像喜欢慢节拍', '你好像喜欢慢节拍', speaker=OTHER)) == []
    excerpt = '我喜欢慢节拍\n引用原话：好像喜欢慢节拍'
    assert cited('我喜欢慢节拍。', 1, proof(excerpt, '好像喜欢慢节拍')) == []


def test_generated_sentence_dropping_the_hedge_is_repaired_from_the_whole_memory(store):
    original = '在这次排练中，我好像更喜欢慢一点的节拍。'
    add_self(store, original)
    result, gateway = generate(store, Clock(), text='在这次排练中，我更喜欢慢一点的节拍。')
    checks = result['version']['checks']
    assert any(UNCERTAINTY_ISSUE in row['reasons'] for row in checks['precheck_rejections'])
    assert [s['text'] for s in result['version']['sentences'] if s['origin']=='memory'
            and not s['initial_setting']] == [original]
    # The rejection precedes any fact-check request for the failed wording.
    fact_texts = [row['text'] for purpose, payload in gateway.calls if purpose=='persona_check'
                  for row in payload.get('candidate', [])]
    assert '在这次排练中，我更喜欢慢一点的节拍。' not in fact_texts


def test_sentence_keeping_the_hedge_passes_without_rejection(store):
    add_self(store, '在这次排练中，我好像更喜欢慢一点的节拍。')
    result, _ = generate(store, Clock(), text='在这次排练中，我似乎更喜欢慢一点的节拍。')
    assert result['version']['checks']['precheck_rejections'] == []
    assert '我似乎更喜欢慢一点的节拍' in result['version']['content']


def test_paraphrase_citing_a_hedged_source_is_rejected_by_the_proof_gate(store):
    add_self(store, '在这次合奏中，我好像喜欢慢节拍。')
    result, _ = generate(store, Clock(), text='在这次合奏中，我偏爱舒缓的速度。')
    checks = result['version']['checks']
    assert checks['precheck_rejections'] == []
    assert any('uncertainty must be preserved' in error for error in checks['initial_model_errors'])
    assert '我偏爱舒缓的速度' not in result['version']['content']


def test_a_memory_whose_text_already_lost_the_hedge_cannot_repair_itself(store):
    add_self(store, '在这次排练中，我更喜欢慢一点的节拍。')
    with store.write() as conn:
        conn.execute("UPDATE messages SET content='这次排练我好像更喜欢慢一点的节拍'")
    result, _ = generate(store, Clock(), text='在这次排练中，我更喜欢慢一点的节拍。')
    assert '慢一点的节拍' not in result['version']['content']
    assert result['version']['checks']['deleted_sentences']


def test_new_prompt_versions_are_active_and_old_files_remain():
    assert PROMPT_VERSIONS == {'persona_generate':'persona_generate_v22', 'persona_check':'persona_check_v35',
                               'persona_recheck':'persona_check_v35'}
    assert HANDWRITTEN_PROMPT_VERSION == 'persona_handwritten_v2'
    assert DEFAULT_PUBLISH_MODE == 'all_manual'
    for name in ('persona_generate_v21', 'persona_generate_v22', 'persona_check_v34', 'persona_check_v35',
                 'persona_handwritten_v1', 'persona_handwritten_v2'):
        assert files('iris').joinpath(f'prompts/{name}.md').is_file()
