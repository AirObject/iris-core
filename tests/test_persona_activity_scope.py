"""General claims must retain their own named activity scope, not just dates."""
import copy

import pytest

from iris.persona import _check_payload, _validate_clause_checks
from test_persona_source_payload import prepare, store


def fixture():
    text = '在合奏排练时，我倾向先听对方的节拍。'
    memory = {'content': '合奏排练中，我倾向先听节拍。', 'excerpts': [
        {'excerpt': '排练时我先听了对方的节拍。', 'date': '2026-01-03'},
        {'excerpt': '再次合奏时我先听了对方的节拍。', 'date': '2026-01-06'}]}
    sentence = {'index': 1, 'origin': 'memory', 'initial_setting': False,
        'text': text, 'evidence_refs': ['M1'], 'scope_check_required': True,
        'clauses': [{'index': 1, 'text': text}]}
    payload = {'candidate': [sentence], 'evidence_by_ref': {'M1': memory}}
    output = {'sentences': [{'index': 1, 'scope_check': {
        'candidate_scope': '在合奏排练时', 'source_scopes': [{'ref': 'M1', 'quote': '合奏排练'}],
        'preserved': True}, 'clauses': [{'index': 1, 'supported': True,
        'scene_qualified': False, 'reason': '两个独立日期有相应行为。', 'evidence': [
            {'ref': 'M1', 'source_index': i, 'quote': source['excerpt']}
            for i, source in enumerate(memory['excerpts'], 1)]}]}]}
    return payload, output


def test_general_claim_has_explicit_own_activity_anchor():
    payload, output = fixture()
    assert _validate_clause_checks(output, payload) == ([], {})


def test_two_dates_and_positive_fact_checks_cannot_override_scope_rejection():
    payload, output = fixture()
    output['sentences'][0]['scope_check']['preserved'] = False
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'activity scope' in ' '.join(problems[1])


@pytest.mark.parametrize('mutation', ['broad_candidate', 'unbound', 'invented', 'empty_candidate', 'no_sources', 'punctuation'])
def test_positive_scope_claim_needs_literal_bound_qualifier(mutation):
    payload, output = fixture()
    scope = output['sentences'][0]['scope_check']
    if mutation == 'broad_candidate':
        payload['candidate'][0]['text'] = '和人相处时，我倾向先听对方。'
        scope['candidate_scope'] = '和人相处时'
    elif mutation == 'unbound':
        payload['evidence_by_ref']['M2'] = copy.deepcopy(payload['evidence_by_ref']['M1'])
        scope['source_scopes'][0]['ref'] = 'M2'
    elif mutation == 'invented':
        scope['source_scopes'][0]['quote'] = '合奏排练时'
    elif mutation == 'empty_candidate':
        scope['candidate_scope'] = ''
    elif mutation == 'punctuation':
        scope['candidate_scope'] = '。'
        scope['source_scopes'][0]['quote'] = '。'
    else:
        scope['source_scopes'] = []
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'activity scope' in ' '.join(problems[1])


@pytest.mark.parametrize('mutation', ['missing', 'non_bool', 'non_string', 'non_array', 'bad_proof'])
def test_scope_protocol_is_complete_and_typed(mutation):
    payload, output = fixture()
    row = output['sentences'][0]
    if mutation == 'missing': row.pop('scope_check')
    elif mutation == 'non_bool': row['scope_check']['preserved'] = 1
    elif mutation == 'non_string': row['scope_check']['candidate_scope'] = None
    elif mutation == 'non_array': row['scope_check']['source_scopes'] = None
    else: row['scope_check']['source_scopes'] = [{'ref': 'M1'}]
    errors, _ = _validate_clause_checks(output, payload)
    assert any('activity scope' in error for error in errors)


def test_scope_quote_may_constrain_from_current_memory_without_becoming_fact_proof():
    payload, output = fixture()
    memory = payload['evidence_by_ref']['M1']
    memory['current_revision_text'] = memory.pop('content')
    assert _validate_clause_checks(output, payload) == ([], {})
    output['sentences'][0]['clauses'][0]['evidence'] = [
        {'ref': 'M1', 'source_index': 0, 'quote': '合奏排练'}]
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'original proof' in ' '.join(problems[1])


def test_real_payload_requires_scope_only_without_bounded_event(store):
    _, material, _, sentence = prepare(store)
    bounded = _check_payload(material, [sentence], full_content=sentence['text'])
    assert bounded['candidate'][0]['scope_check_required'] is False
    general = copy.deepcopy(sentence)
    general['text'] = '在配音中，我倾向先确认对白。'
    payload = _check_payload(material, [general], full_content=general['text'])
    assert payload['candidate'][0]['scope_check_required'] is True


@pytest.mark.parametrize('failure', ['omitted_qualifier', 'negative_source'])
def test_scope_omission_reaches_sentence_deletion_even_with_positive_fact_model(store, failure):
    import json
    from iris.db import dumps
    from iris.models import ModelReply
    from iris.persona import PersonaEngine, _generation_material, _settings, current_persona
    from test_persona import FakeGateway, add_self

    source = '在合奏排练中，我先听了对方的节拍。'
    mids = [add_self(store, source, stamp=stamp) for stamp in (
        '2026-10-01T12:00:00+00:00', '2026-10-04T12:00:00+00:00')]
    previous = current_persona(store)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), previous, '2026-10-13T12:00:00+00:00')
    refs = [m['ref'] for m in material['evidence']['memories'] if m['memory_id'] in mids]
    broad = ('与人交流时，我倾向先听对方的节拍。' if failure == 'omitted_qualifier'
             else '在合奏排练中，我先听了对方的节拍。')

    class ScopeGateway(FakeGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose in ('persona_check', 'persona_recheck'):
                output = json.loads(reply.content)
                for row, sentence in zip(output['sentences'], self.calls[-1][1]['candidate']):
                    if sentence.get('scope_check_required'):
                        row['scope_check'] = {'candidate_scope': '与人交流时',
                            'source_scopes': [{'ref': refs[0], 'quote': '合奏排练'}], 'preserved': True}
                        if failure == 'negative_source':
                            row['scope_check']['candidate_scope'] = '合奏排练'
                            row['scope_check']['source_scopes'][0]['preserved'] = False
                return ModelReply(dumps(output), 'stop', {})
            return reply

    model = ScopeGateway(store, {'sentences': [{'text': broad, 'basis': refs}]}, scene=False)
    result = PersonaEngine(store, model).regenerate(expected_version=previous['id'])
    assert result['version'] and broad not in result['version']['content']
    assert not result['version']['checks']['initial_model']['incomplete_checks']
    deleted = result['version']['checks']['deleted_sentences']
    assert any('activity scope' in reason for item in deleted for reason in item['reasons'])


def test_elliptical_utterance_cannot_replace_current_memory_scope_constraint():
    payload, output = fixture()
    memory = payload['evidence_by_ref']['M1']
    memory['current_revision_text'] = memory.pop('content')
    payload['candidate'][0]['text'] = '我倾向先听对方的节拍。'
    output['sentences'][0]['scope_check'].update(candidate_scope='对方的节拍',
        source_scopes=[{'ref': 'M1', 'quote': '对方的节拍'}])
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'activity scope' in ' '.join(problems[1])


def test_scope_can_use_original_when_no_current_text_is_present():
    payload, output = fixture()
    payload['evidence_by_ref']['M1'].pop('content')
    output['sentences'][0]['scope_check'].update(candidate_scope='合奏排练',
        source_scopes=[{'ref': 'M1', 'quote': '排练'}])
    assert _validate_clause_checks(output, payload) == ([], {})


@pytest.mark.parametrize('overall,source', [(True, True), (True, False), (False, True), (False, False)])
def test_optional_per_source_scope_verdict_is_typed_and_combined_adversely(overall, source):
    payload, output = fixture()
    scope = output['sentences'][0]['scope_check']
    scope['preserved'] = overall
    scope['source_scopes'][0]['preserved'] = source
    before = copy.deepcopy(output)
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors
    assert bool(problems) == (not overall or not source)
    assert output == before  # Keep the extra verdict, never erase a negative.


@pytest.mark.parametrize('value', [None, 1, 'true', [], {}])
def test_per_source_scope_verdict_never_coerces_non_boolean_values(value):
    payload, output = fixture()
    output['sentences'][0]['scope_check']['source_scopes'][0]['preserved'] = value
    errors, _ = _validate_clause_checks(output, payload)
    assert any('invalid activity scope verdict' in error for error in errors)


@pytest.mark.parametrize('target', ['scope', 'proof'])
def test_per_source_scope_compatibility_does_not_accept_unknown_fields(target):
    payload, output = fixture()
    scope = output['sentences'][0]['scope_check']
    record = scope if target == 'scope' else scope['source_scopes'][0]
    record['unknown_verdict'] = True
    errors, _ = _validate_clause_checks(output, payload)
    assert any('invalid activity scope verdict' in error for error in errors)


def test_positive_per_source_flag_cannot_override_missing_literal_qualifier():
    payload, output = fixture()
    scope = output['sentences'][0]['scope_check']
    scope['candidate_scope'] = '节拍'
    scope['source_scopes'][0]['preserved'] = True
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'omitted or broadened' in ' '.join(problems[1])
