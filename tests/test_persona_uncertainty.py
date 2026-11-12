"""Uncertain sources must not become assertions; deterministic and fake-model checks."""
from importlib.resources import files

import pytest

from iris.db import Store
from iris.memory_ops import setup_role
from iris.persona import (DEFAULT_PUBLISH_MODE, HANDWRITTEN_PROMPT_VERSION, PROMPT_VERSIONS, UNCERTAINTY,
                          UNCERTAINTY_ISSUE, _clause_parts, _epistemic_clauses, _proof_uncertainty_issues,
                          _surface_issues, _uncertainty_issues)
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


def test_doubt_about_a_judgement_does_not_reach_the_request_made_because_of_it():
    text = '我没看全那段，就请对方把两个人的顺序再说一遍。'
    utterance = '你把两个人的顺序再说一遍'
    assert cited(text, 2, proof(utterance, utterance, current='我不确定两个人的顺序，再请对方补上过程')) == []


def test_a_cited_doubt_does_not_reach_a_request_either():
    assert cited('我就请对方把顺序再说一遍。', 1, proof('我不确定谁先换牌', '我不确定谁先换牌')) == []


def test_doubt_still_constrains_the_judgement_it_is_about():
    assert cited('是小舒先换的牌。', 1, proof('是小舒先换的牌', '是小舒先换的牌',
                                            current='我不确定是小舒先换的牌')) == [UNCERTAINTY_ISSUE]


def test_a_request_that_is_itself_doubted_stays_constrained():
    assert cited('我请对方重说了一遍。', 1, proof('我请对方重说了一遍', '我请对方重说了一遍',
                                              current='我好像请对方重说了一遍')) == [UNCERTAINTY_ISSUE]


def test_saying_one_cannot_tell_already_keeps_the_doubt():
    text = '我没看全那段，不能直接判谁先换牌，就请对方再说一遍。'
    utterance = '不能直接判谁先换牌'
    assert cited(text, 2, proof(utterance, utterance, current='我不确定是谁先换牌')) == []


def test_a_doubt_ends_where_its_stated_consequence_begins():
    assert _epistemic_clauses('我不确定谁先换牌就请对方再说一遍') == [('我不确定谁先换牌', True), ('请对方再说一遍', False)]
    assert _epistemic_clauses('我因为不确定谁先换牌而请对方重说') == [('我因为不确定谁先换牌', True), ('请对方重说', False)]
    # 情态词紧跟连接词时还没有内容，后面的整段仍是它修饰的内容。
    assert _epistemic_clauses('我好像就喜欢慢节拍') == [('我好像就喜欢慢节拍', True)]
    assert cited('我就请对方把顺序再说一遍。', 1, proof('顺序再说一遍', '顺序再说一遍',
        current='我因为不确定谁先换牌而请对方把顺序再说一遍')) == []


def test_bundled_citation_needs_a_link_to_say_which_clause_supports_the_focus():
    bundle = '我好像累了，你把顺序再说一遍'
    assert cited('在那次桌游里，我认真听着。', 2, proof(bundle, bundle)) == []
    linked = '我好像喜欢慢节拍，你把顺序再说一遍'
    assert cited('我喜欢慢节拍。', 1, proof(linked, linked)) == [UNCERTAINTY_ISSUE]


def test_current_text_clause_must_hold_most_of_the_focus():
    assert cited('在排练里我把节拍和谱子都重新过了一遍。', 1, proof('都过了一遍', '都过了一遍',
                                                       current='我好像节拍有点快')) == []


@pytest.mark.parametrize('text', ['我好像累了，我喜欢慢节拍。', '我好像累了，不过喜欢慢节拍。',
                                  '那次我可能累了，那次我喜欢慢节拍。'])
def test_an_unrelated_hedge_elsewhere_does_not_excuse_a_claim(text):
    source = memory('我好像喜欢慢节拍。', '我好像喜欢慢节拍')
    assert flagged(text, source)
    assert cited(text, 2, proof('我好像喜欢慢节拍', '我好像喜欢慢节拍')) == [UNCERTAINTY_ISSUE]


@pytest.mark.parametrize('text', ['我好像喜欢快节奏，也喜欢慢节拍。', '好像吧，我喜欢慢节拍。'])
def test_a_hedge_still_reaches_its_continuation_and_a_bare_hedge_its_neighbour(text):
    source = memory('我好像喜欢慢节拍。', '我好像喜欢慢节拍')
    assert not flagged(text, source)


def test_a_clause_restarting_the_claim_stops_the_hedge_for_those_after_it():
    source = memory('我好像喜欢慢节拍。', '我好像喜欢慢节拍')
    assert flagged('我好像累了，我喜欢雨天，也喜欢慢节拍。', source)


def test_request_beside_a_doubt_is_not_rejected_end_to_end(store):
    add_self(store, '在这次桌游里，我不确定谁先换牌，再请对方补上过程。')
    with store.write() as conn:
        conn.execute("UPDATE messages SET content='这次桌游那段我没看全，不能直接判谁先换牌。你把两个人的顺序再说一遍'")
    text = '在这次桌游里，我没看全那段，不能直接判谁先换牌，就请对方把两个人的顺序再说一遍。'
    result, _ = generate(store, Clock(), text=text)
    checks = result['version']['checks']
    assert not any('uncertainty must be preserved' in error for error in checks['initial_model_errors'])
    assert text in result['version']['content']


def test_unrelated_hedge_does_not_save_a_flat_claim_end_to_end(store):
    add_self(store, '在这次合奏中，我好像喜欢慢节拍。')
    result, _ = generate(store, Clock(), text='在这次合奏中，我好像累了，我喜欢慢节拍。')
    assert any(UNCERTAINTY_ISSUE in row['reasons'] for row in result['version']['checks']['precheck_rejections'])
    assert '我好像累了，我喜欢慢节拍' not in result['version']['content']


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
    # The fake cites the whole excerpt, so keep it to one clause: a bundle would need a link.
    add_self(store, '这次合奏我好像喜欢慢节拍。')
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
    assert PROMPT_VERSIONS == {'persona_generate':'persona_generate_v24', 'persona_check':'persona_check_v37',
                               'persona_recheck':'persona_check_v37'}
    assert HANDWRITTEN_PROMPT_VERSION == 'persona_handwritten_v2'
    assert DEFAULT_PUBLISH_MODE == 'all_manual'
    for name in ('persona_generate_v21', 'persona_generate_v22', 'persona_generate_v23', 'persona_generate_v24',
                 'persona_check_v34', 'persona_check_v35', 'persona_check_v36', 'persona_check_v37',
                 'persona_handwritten_v1', 'persona_handwritten_v2'):
        assert files('iris').joinpath(f'prompts/{name}.md').is_file()
