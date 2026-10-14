"""A recognition keeps its domain; a reported act does not inherit utterance time."""
import pytest

from iris.persona import _check_payload, _clause_parts, _validate_clause_checks
from test_persona_source_payload import prepare, store


@pytest.mark.parametrize('text', [
    '2026年4月7日，我认同乔禾说我细心，但不是什么事都细心。',
    '那次排练后，这一点我认同，但不能泛指所有事情。',
    '在那次演示中，我认可对方对我的评价。',
])
def test_bounded_recognition_still_requests_activity_scope(store, text):
    _, material, _, sentence = prepare(store)
    sentence['text'] = text
    request = _check_payload(material, [sentence], full_content=text)
    assert request['candidate'][0]['scope_check_required'] is True


def report_case(text, source, *, speaker='我', subject='self', source_index=1):
    parts = _clause_parts(text)
    # Focus on the reported action, retaining the complete candidate context.
    focus = next(part for part in parts if '乔禾' in part['text'])
    memory = {'content': '合奏排练后的自我认识。', 'excerpts': [
        {'excerpt': source, 'date': '2026-04-07', 'speaker': speaker,
         'speaker_subject_id': subject, 'entry_kind': 'private'}]}
    row = {'index': 1, 'text': text, 'origin': 'memory', 'initial_setting': False,
           'evidence_refs': ['M1'], 'clauses': [focus]}
    proof = {'ref': 'M1', 'source_index': source_index, 'quote': source}
    verdict = {'index': 1, 'persona_kind': 'episode', 'supported': True,
               'fabricated': False, 'scene_qualified': True, 'violations': [],
               'reason': '模拟检查器放行。', 'clauses': [
                   {'index': focus['index'], 'supported': True, 'scene_qualified': True,
                    'evidence': [proof], 'reason': '模拟元数据当作被转述事件日期。'}]}
    return {'candidate': [row], 'evidence_by_ref': {'M1': memory}}, {'sentences': [verdict]}


@pytest.mark.parametrize('text,source', [
    ('合奏排练那次，乔禾夸我细心，这一点我认同。',
     '今天又参加了合奏排练。我发现自己会先听节拍。乔禾夸我细心，这一点我现在认同。'),
    ('2026年4月7日，乔禾说我细心，我认同这点。',
     '乔禾说我细心，这一点我现在认同。'),
    ('那次排练后，乔禾称赞我细心，我认同这点。',
     '排练后我回顾了自己的表现。乔禾称赞我细心，这一点我认同。'),
    ('那次演示时，乔禾告诉我动作很稳，我认同这点。',
     '这次演示我先把步骤拆开。乔禾告诉我动作很稳，我觉得可以这样认识自己。'),
    ('2026年4月7日，乔禾认为我很细心，我认同这点。',
     '乔禾认为我很细心，我现在认同这点。'),
])
def test_reported_act_needs_its_own_event_context(text, source):
    payload, output = report_case(text, source)
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors
    assert 'reported event' in ' '.join(problems.get(1, []))


@pytest.mark.parametrize('text,source,speaker,subject', [
    ('那次合奏排练中，乔禾夸我细心，我认同这点。',
     '在那次合奏排练中，乔禾夸我细心，我认同这点。', '我', 'self'),
    ('2026年4月7日，乔禾说我细心，我认同这点。',
     '今天乔禾说我细心。', '我', 'self'),
    ('2026年4月7日，乔禾说我细心，我认同这点。',
     '我觉得你很细心。', '乔禾', 'person:qiaohe'),
    ('那次演示中，乔禾告诉我动作很稳，我认同这点。',
     '那次演示中，乔禾告诉我动作很稳。', '我', 'self'),
    ('那次我向乔禾说明了规则，我还做了示范。',
     '我向乔禾说明了规则。', '我', 'self'),
])
def test_explicit_report_context_or_direct_speaker_remains_eligible(text, source, speaker, subject):
    payload, output = report_case(text, source, speaker=speaker, subject=subject)
    assert _validate_clause_checks(output, payload) == ([], {})


def test_other_excerpt_cannot_supply_the_reported_actions_context():
    payload, output = report_case('那次排练中，乔禾夸我细心，我认同这点。', '乔禾夸我细心。')
    payload['evidence_by_ref']['M1']['excerpts'].append({
        'excerpt': '那次排练中，乔禾夸我细心。', 'date': '2026-04-07'})
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'reported event' in ' '.join(problems[1])


def test_current_summary_cannot_date_an_undated_report():
    payload, output = report_case('那次排练中，乔禾夸我细心，我认同这点。', '乔禾夸我细心。')
    payload['evidence_by_ref']['M1']['current_revision_text'] = '那次排练中乔禾夸我细心。'
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'reported event' in ' '.join(problems[1])


def test_known_negative_report_stays_negative_without_an_extra_opinion():
    payload, output = report_case('那次排练中，乔禾夸我细心，我认同这点。', '乔禾夸我细心。')
    output['sentences'][0]['clauses'][0].update(supported=False, evidence=[], reason='未证明评价发生场景。')
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and '未证明评价发生场景' in ' '.join(problems[1])


def test_bound_recognition_scope_cannot_be_satisfied_by_date_alone():
    payload, output = report_case('2026年4月7日，乔禾夸我细心，我认同这点。', '今天乔禾夸我细心。')
    payload['candidate'][0]['scope_check_required'] = True
    output['sentences'][0]['scope_check'] = {
        'candidate_scope': '2026年4月7日', 'source_scopes': [{'ref': 'M1', 'quote': '合奏排练'}],
        'preserved': True}
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'activity scope' in ' '.join(problems[1])
