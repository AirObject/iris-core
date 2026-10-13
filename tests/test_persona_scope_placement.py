"""A complete misplaced scope verdict remains evidence, including every negative."""
import copy

import pytest

from iris.persona import _validate_clause_checks, _clause_parts
from test_persona_activity_scope import fixture


def nested_fixture(*, count=1, keep_row=False):
    payload, output = fixture()
    row = output['sentences'][0]
    scope = copy.deepcopy(row['scope_check'])
    if not keep_row:
        row.pop('scope_check')
    if count == 2:
        payload['candidate'][0]['clauses'] = _clause_parts(payload['candidate'][0]['text'])
        row['clauses'].append({**copy.deepcopy(row['clauses'][0]), 'index': 2})
    for part in row['clauses']:
        part['scope_check'] = copy.deepcopy(scope)
    return payload, output


@pytest.mark.parametrize('count,keep_row', [(1, False), (2, False), (1, True), (2, True)])
def test_complete_focus_scopes_are_checked_in_place_without_erasing_original_fields(count, keep_row):
    payload, output = nested_fixture(count=count, keep_row=keep_row)
    before = copy.deepcopy(output)
    assert _validate_clause_checks(output, payload) == ([], {})
    assert output == before


@pytest.mark.parametrize('keep_row', [False, True])
@pytest.mark.parametrize('bad', ['negative', 'negative_source', 'unbound', 'invented', 'broadened'])
def test_any_adverse_focus_scope_rejects_even_when_the_sentence_scope_is_positive(keep_row, bad):
    payload, output = nested_fixture(count=2, keep_row=keep_row)
    scope = output['sentences'][0]['clauses'][1]['scope_check']
    if bad == 'negative': scope['preserved'] = False
    elif bad == 'negative_source': scope['source_scopes'][0]['preserved'] = False
    elif bad == 'unbound': scope['source_scopes'][0]['ref'] = 'M999'
    elif bad == 'invented': scope['source_scopes'][0]['quote'] = '课堂表演'
    else: scope['candidate_scope'] = '节拍'
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'activity scope' in ' '.join(problems[1])


@pytest.mark.parametrize('keep_row', [False, True])
@pytest.mark.parametrize('bad', [None, {}, 'yes', {'preserved': True}])
def test_malformed_nested_scope_cannot_hide_behind_a_valid_sentence_scope(keep_row, bad):
    payload, output = nested_fixture(keep_row=keep_row)
    output['sentences'][0]['clauses'][0]['scope_check'] = bad
    assert _validate_clause_checks(output, payload)[0]


def test_partial_focus_scope_coverage_cannot_replace_the_missing_sentence_scope():
    payload, output = nested_fixture(count=2)
    output['sentences'][0]['clauses'][1].pop('scope_check')
    assert _validate_clause_checks(output, payload)[0]


def test_valid_sentence_scope_still_requires_every_supplied_nested_scope_to_pass():
    payload, output = nested_fixture(count=2, keep_row=True)
    output['sentences'][0]['clauses'][1].pop('scope_check')
    assert _validate_clause_checks(output, payload) == ([], {})
    output['sentences'][0]['clauses'][0]['scope_check']['preserved'] = False
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and problems


@pytest.mark.parametrize('bad', [None, {}, {'candidate_scope': '合奏排练', 'source_scopes': [], 'preserved': 1}])
def test_a_malformed_explicit_sentence_scope_cannot_be_replaced_by_valid_focus_scopes(bad):
    payload, output = nested_fixture()
    output['sentences'][0]['scope_check'] = bad
    assert _validate_clause_checks(output, payload)[0]


def test_negative_sentence_scope_is_not_overridden_by_all_positive_focus_scopes():
    payload, output = nested_fixture(count=2, keep_row=True)
    output['sentences'][0]['scope_check']['preserved'] = False
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and problems


def test_nested_scope_cannot_supply_the_missing_second_date_for_a_focus():
    payload, output = nested_fixture()
    output['sentences'][0]['clauses'][0]['evidence'].pop()
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'two distinct dates' in ' '.join(problems[1])


@pytest.mark.parametrize('negative', [False, True])
def test_nested_scope_contract_reaches_real_sentence_acceptance_or_deletion(store, negative):
    import json
    from iris.db import dumps
    from iris.memory_ops import setup_role
    from iris.persona import PersonaEngine, current_persona, select_evidence
    from test_persona import FakeGateway, add_self
    setup_role(store, 'Iris', '我来自云城。')
    source = '在合奏排练中，我先听了对方的节拍。'
    mids = [add_self(store, source, stamp=stamp) for stamp in (
        '2026-10-01T12:00:00+00:00', '2026-10-04T12:00:00+00:00')]
    refs = [m['ref'] for m in select_evidence(store)['memories'] if m['memory_id'] in mids]
    text = '在合奏排练中，我倾向先听对方的节拍。'

    class Nested(FakeGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose in ('persona_check', 'persona_recheck'):
                value = json.loads(reply.content)
                for row in value['sentences']:
                    if 'scope_check' in row:
                        scope = row.pop('scope_check')
                        for part in row['clauses']:
                            part['scope_check'] = copy.deepcopy(scope)
                        if negative: row['clauses'][-1]['scope_check']['preserved'] = False
                reply.content = dumps(value)
            return reply

    model = Nested(store, {'sentences': [{'text': text, 'basis': refs}]}, scene=False)
    result = PersonaEngine(store, model).regenerate(expected_version=current_persona(store)['id'])
    checks = result['version']['checks']
    assert checks['passed'] and not checks['initial_model']['incomplete_checks']
    assert (text in result['version']['content']) is (not negative)
    if negative:
        assert any('activity scope' in ' '.join(item['reasons']) for item in checks['deleted_sentences'])
