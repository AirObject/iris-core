"""A bounded event cannot inherit a concrete scene from another claim."""
import copy
import json

import pytest

from iris.persona import (
    PersonaEngine, _clause_parts, _surface_issues, _validate_clause_checks,
    current_persona, select_evidence,
)
from test_persona import FakeGateway, add_self, store


def fixture(text='在那次矿石直播中，我给每个标记补上了数字。', *, source='我给每个标记补上了数字。'):
    memory = {'content': '我给每个标记补上了数字。', 'entry_kind': 'live', 'excerpts': [
        {'excerpt': source, 'date': '2028-02-03', 'entry_kind': 'live'}]}
    sentence = {'index': 1, 'origin': 'memory', 'initial_setting': False,
        'text': text, 'evidence_refs': ['M1'], 'scope_check_required': False,
        'clauses': _clause_parts(text)}
    payload = {'candidate': [sentence], 'evidence_by_ref': {'M1': memory}}
    output = {'sentences': [{'index': 1, 'clauses': [
        {'index': part['index'], 'supported': True, 'scene_qualified': True,
         'reason': '假模型将动作和场景一起判真。',
         'evidence': [{'ref': 'M1', 'source_index': 1, 'quote': source}]}
        for part in sentence['clauses']]}]}
    return payload, output


@pytest.mark.parametrize('opening', [
    '在那次矿石直播中', '那次矿石直播里', '在这次矿石直播时',
    '在2028年2月3日的矿石直播里', '2028-02-03的矿石直播中',
    '在二月三日的矿石直播中',
])
def test_a_date_or_bounded_scene_does_not_authorize_an_unsourced_topic(opening):
    payload, output = fixture(opening + '，我给每个标记补上了数字。')
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'bounded context' in ' '.join(problems.get(1, []))


@pytest.mark.parametrize('source', [
    '在矿石直播中，我给每个标记补上了数字。',
    '我在直播里讲解矿石，给每个标记补上了数字。',
])
def test_context_terms_can_be_reordered_but_must_come_from_own_source(source):
    payload, output = fixture(source=source)
    assert _validate_clause_checks(output, payload) == ([], {})


def test_entry_metadata_supports_only_the_generic_entry_context():
    payload, output = fixture('在那次直播中，我给每个标记补上了数字。')
    assert _validate_clause_checks(output, payload) == ([], {})
    payload['evidence_by_ref']['M1']['entry_kind'] = 'game'
    payload['evidence_by_ref']['M1']['excerpts'][0]['entry_kind'] = 'game'
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'bounded context' in ' '.join(problems.get(1, []))


def test_current_memory_can_retain_context_omitted_in_an_elliptical_utterance():
    payload, output = fixture()
    memory = payload['evidence_by_ref']['M1']
    memory.pop('content')
    memory['current_revision_text'] = '我在矿石讲解中为标记补上数字。'
    assert _validate_clause_checks(output, payload) == ([], {})


def test_date_only_wording_does_not_invent_a_specific_scene():
    payload, output = fixture('在2028年2月3日，我给每个标记补上了数字。')
    assert _validate_clause_checks(output, payload) == ([], {})


@pytest.mark.parametrize('bind_other', [False, True])
def test_context_in_another_memory_cannot_replace_this_focus_proof(bind_other):
    payload, output = fixture()
    other = copy.deepcopy(payload['evidence_by_ref']['M1'])
    other['content'] = '我在矿石直播里解释了图例。'
    payload['evidence_by_ref']['M2'] = other
    if bind_other:
        payload['candidate'][0]['evidence_refs'].append('M2')
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'bounded context' in ' '.join(problems.get(1, []))


def test_unquoted_source_in_the_same_memory_cannot_supply_the_context():
    payload, output = fixture()
    payload['evidence_by_ref']['M1']['excerpts'].append({
        'excerpt': '我在矿石直播里讲解图例。', 'date': '2028-02-10', 'entry_kind': 'live'})
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'bounded context' in ' '.join(problems.get(1, []))


def test_the_quoted_source_entry_kind_takes_precedence_over_memory_entry_kind():
    payload, output = fixture('在那次直播中，我给每个标记补上了数字。')
    payload['evidence_by_ref']['M1']['excerpts'][0]['entry_kind'] = 'game'
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'bounded context' in ' '.join(problems.get(1, []))


def test_context_presence_does_not_override_a_semantic_negative():
    payload, output = fixture(source='矿石直播里，我没有给标记补数字。')
    output['sentences'][0]['clauses'][-1]['supported'] = False
    output['sentences'][0]['clauses'][-1]['reason'] = '来源明确否认了这个动作。'
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and '来源明确否认' in ' '.join(problems[1])


def test_surface_guard_uses_only_current_bound_revisions():
    sentence = {'text': '在那次矿石直播中，我给标记补上了数字。',
        'origin': 'memory', 'initial_setting': False, 'basis': [{'memory_id': 1, 'revision': 2}], 'date_count': 1}
    memories = [{'memory_id': 1, 'revision': 2, 'content': '我给标记补上了数字。', 'entry_kind': 'live', 'excerpts': []},
                {'memory_id': 1, 'revision': 1, 'content': '我在矿石直播中给标记补上了数字。', 'entry_kind': 'live', 'excerpts': []}]
    assert any('bounded context' in x for x in _surface_issues(sentence, memories=memories))
    memories[0]['content'] = '我在矿石直播中给标记补上了数字。'
    assert not _surface_issues(sentence, memories=memories)


@pytest.mark.parametrize('origin,setting', [('admin', False), ('memory', True)])
def test_handwritten_and_setting_exemptions_keep_their_separate_semantic_checks(origin, setting):
    sentence = {'text': '在那次矿石直播中，我给标记补上了数字。', 'origin': origin,
        'initial_setting': setting, 'basis': [], 'date_count': 1}
    assert not _surface_issues(sentence, memories=[])


def test_an_optimistic_checker_cannot_publish_context_borrowed_from_a_neighbour(store):
    first = '在那次矿石直播中，我把图例放到了画面左边。'
    plain = '在那次直播中，我给每个标记补上了数字。'
    invented = '在那次矿石直播中，我给每个标记补上了数字。'
    first_id = add_self(store, first)
    numbered_id = add_self(store, plain, stamp='2026-10-04T12:00:00+00:00')
    refs = {m['memory_id']: m['ref'] for m in select_evidence(store)['memories']}
    gateway = FakeGateway(store, {'sentences': [
        {'text': first, 'basis': [refs[first_id]]},
        {'text': invented, 'basis': [refs[numbered_id]]},
    ]})
    result = PersonaEngine(store, gateway).regenerate(expected_version=current_persona(store)['id'])
    assert result['version']['checks']['passed']
    assert first in result['version']['content'] and invented not in result['version']['content']
    with store.read() as conn:
        outputs = json.loads(conn.execute('SELECT outputs_json FROM persona_attempts ORDER BY id DESC LIMIT 1').fetchone()[0])
    assert invented in outputs['persona_generate']


@pytest.mark.parametrize('relation', ['与', '和', '跟', '同'])
def test_context_relation_words_do_not_need_literal_evidence(relation):
    source = '我在青岚的私聊里给每个标记补上了数字。'
    payload, output = fixture(f'在2028年2月3日{relation}青岚的私聊里，我给每个标记补上了数字。', source=source)
    memory = payload['evidence_by_ref']['M1']
    memory.update(content=source, entry_kind='private')
    memory['excerpts'][0]['entry_kind'] = 'private'
    assert _validate_clause_checks(output, payload) == ([], {})


@pytest.mark.parametrize('context,missing', [('与江鹭的私聊', '江'), ('与青岚的矿石私聊', '矿石')])
def test_ignoring_relation_words_does_not_license_people_or_topics(context, missing):
    source = '我在青岚的私聊里给每个标记补上了数字。'
    payload, output = fixture(f'在2028年2月3日{context}里，我给每个标记补上了数字。', source=source)
    memory = payload['evidence_by_ref']['M1']
    memory.update(content=source, entry_kind='private')
    memory['excerpts'][0]['entry_kind'] = 'private'
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and any(missing in issue for issue in problems[1])


@pytest.mark.parametrize('context', ['《与》的直播', '“与”的直播', '「与」的直播'])
def test_quoted_titles_do_not_gain_the_relation_word_exemption(context):
    payload, output = fixture(f'在那次{context}中，我给每个标记补上了数字。')
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and any('与' in issue for issue in problems[1])
