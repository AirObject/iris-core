"""Repeated explicit focus verdicts are all checked; none can fill a missing ID."""
import copy
import json

import pytest

from iris.persona import PersonaEngine, _check_verdicts
from test_persona_extra_focus import ExtraModel, check
from test_persona_focus_completion import PrefixModel, check as check_prefix
from test_persona_focus_windows import material_and_sentence


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
def test_repeated_tail_verdicts_are_checked_and_audited_without_a_new_call(store, purpose):
    model = ExtraModel([3, 3], on_call=2)
    result, material, sentence, outputs = check(store, model, count=3, purpose=purpose)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    assert not _check_verdicts(result, [sentence], material=material)[2]
    row = result['sentences'][0]
    assert [p['index'] for p in row['clauses']] == [1, 2, 3]
    assert [p['index'] for p in row['repeated_clause_checks']] == [3]
    assert result['normalizations'][-1] == {
        'batch': 2, 'sentence_index': 1, 'operation': 'repeated_focus_checks',
        'requested_clause_indices': [3], 'repeated_clause_indices': [3]}
    raw = json.loads(outputs[purpose + '_batches'][1][purpose])['sentences'][0]
    assert [p['index'] for p in raw['clauses']] == [3, 3]
    assert 'repeated_clause_checks' not in raw


@pytest.mark.parametrize('indices', [[1, 1, 2], [1, 2, 1], [1, 1, 2, 2], [4, 1, 1, 2, 4]])
def test_explicit_repeats_preserve_complete_requested_coverage_and_every_extra(store, indices):
    model = ExtraModel(indices)
    result, material, sentence, _ = check(store, model)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    assert not _check_verdicts(result, [sentence], material=material)[2]
    row = result['sentences'][0]
    assert [p['index'] for p in row['clauses']] == [1, 2, 3, 4]
    assert len(row['repeated_clause_checks']) == len([i for i in indices if i in (1, 2)]) - 2
    assert [p['index'] for p in row.get('additional_clause_checks', [])] == [
        i for i in indices if i not in (1, 2)]


@pytest.mark.parametrize('negative_position', [0, 1])
def test_a_negative_repeated_verdict_cannot_be_overwritten_by_a_positive_one(store, negative_position):
    def mutate(row):
        row['clauses'][negative_position].update(
            supported=False, evidence=[], reason='The repeated verdict rejects this action.')
    model = ExtraModel([3, 3], on_call=2, mutation=mutate)
    result, material, sentence, _ = check(store, model, count=3)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    row = result['sentences'][0]
    assert not row['supported'] and _check_verdicts(result, [sentence], material=material)[2]
    assert any('The repeated verdict rejects' in reason for reason in row['clause_errors'])
    assert row['clauses'][-1]['supported'] is (negative_position != 0)
    assert row['repeated_clause_checks'][0]['supported'] is (negative_position != 1)


@pytest.mark.parametrize('failure', ['unsupported', 'one_date', 'unbound', 'wrong_quote'])
def test_each_repeat_must_pass_its_own_proof_or_the_whole_sentence_is_rejected(store, failure):
    def mutate(row):
        part = row['clauses'][1]
        if failure == 'unsupported':
            part['supported'] = False
        elif failure == 'one_date':
            part['scene_qualified'] = False
        elif failure == 'unbound':
            part['evidence'][0]['ref'] = 'M999'
        else:
            part['evidence'][0]['quote'] = 'not present in source'
    model = ExtraModel([1, 1, 2], mutation=mutate)
    result, material, sentence, _ = check(store, model)
    assert len(model.calls) == 1 and not result['incomplete_checks']
    assert not result['sentences'][0]['supported']
    assert _check_verdicts(result, [sentence], material=material)[2]
    assert result['sentences'][0]['unchecked_clause_indices'] == [3, 4]
    assert result['sentences'][0]['repeated_clause_checks']


def test_repeats_do_not_pool_dates_to_justify_a_general_claim(store):
    material, sentence = material_and_sentence(4)
    memory = material['evidence']['memories'][0]
    memory['excerpts'].append({**memory['excerpts'][0], 'date': '2026-10-02'})

    def mutate(row):
        for position in (0, 1):
            part = row['clauses'][position]
            part['scene_qualified'] = False
            part['evidence'][0]['source_index'] = position + 1
    model = ExtraModel([1, 1, 2], mutation=mutate)
    result = PersonaEngine(store, model)._check_all('persona_check', material, [sentence],
        full_content=sentence['text'], outputs={})
    assert len(model.calls) == 1 and not result['incomplete_checks']
    row = result['sentences'][0]
    assert not row['supported']
    assert sum('general claim needs at least two distinct dates' in x for x in row['clause_errors']) == 2


@pytest.mark.parametrize('bad', ['missing_reason', 'wrong_bool', 'bad_evidence', 'bad_scope'])
def test_malformed_repeat_is_never_discarded(store, bad):
    def mutate(row):
        part = row['clauses'][1]
        if bad == 'missing_reason':
            part.pop('reason')
        elif bad == 'wrong_bool':
            part['supported'] = 'true'
        elif bad == 'bad_evidence':
            part['evidence'] = None
        else:
            row['scope_check'] = {'candidate_scope': 'Reading group',
                'source_scopes': [{'ref': 'M1', 'quote': 'Reading group'}], 'preserved': True}
            part['scope_check'] = {'preserved': 'true'}
    model = ExtraModel([1, 1, 2], mutation=mutate)
    if bad == 'bad_scope':
        material, sentence = material_and_sentence(4)
        prefix = sentence['text'].split('\uFF0C')[0]
        sentence['text'] = sentence['text'].replace(prefix, 'Reading group')
        memory = material['evidence']['memories'][0]
        memory['content'] = sentence['text']
        memory['excerpts'][0]['excerpt'] = sentence['text']
        result = PersonaEngine(store, model)._check_all('persona_check', material, [sentence],
            full_content=sentence['text'], outputs={})
        assert model.calls[0][0]['candidate'][0]['scope_check_required']
        assert any('invalid activity scope verdict' in e for e in result['incomplete_checks'])
    else:
        result, _, _, _ = check(store, model)
    assert result['incomplete_checks'] and not result['sentences'][0]['supported']
    assert not result['normalizations']


@pytest.mark.parametrize('indices', [[1, 1], [1, 1, 3], [2, 2, 1], [1, True, 2], [1, '1', 2], [1, 1, 99]])
def test_repeats_cannot_supply_missing_reordered_or_malformed_requested_ids(store, indices):
    result, _, _, _ = check(store, ExtraModel(indices))
    assert result['incomplete_checks'] and not result['sentences'][0]['supported']
    assert not result['normalizations']


@pytest.mark.parametrize('negative', [False, True])
def test_suffix_completion_keeps_all_repeated_verdicts_and_the_original_deadline(store, negative):
    def mutate(value):
        row = value['sentences'][0]
        row['clauses'].append(copy.deepcopy(row['clauses'][0]))
        if negative:
            row['clauses'][0].update(supported=False, evidence=[], reason='First repeated verdict is adverse.')
    model = PrefixModel(completion=mutate); outputs = {}
    result, material, sentences = check_prefix(store, model, outputs=outputs)
    assert not result['incomplete_checks']
    assert result['sentences'][0]['supported'] is (not negative)
    assert bool(_check_verdicts(result, sentences, material=material)[2]) is negative
    assert [d for _, d in model.calls[:2]] == [360, 360]
    audit = outputs['persona_check_batches'][0]['persona_check_focus_completions'][0]
    assert audit['completed'] and audit['repeated_clause_indices'] == [2]
    raw = json.loads(audit['outputs']['persona_check'])['sentences'][0]
    assert [p['index'] for p in raw['clauses']] == [2, 2]
    row = result['sentences'][0]
    assert [p['index'] for p in row['repeated_clause_checks']] == [2]
    if negative:
        assert len(model.calls) == 2 and result['skipped_windows']
        assert any('First repeated verdict is adverse.' in x for x in row['clause_errors'])
    else:
        assert len(model.calls) == 3 and [p['index'] for p in row['clauses']] == [1, 2, 3, 4]


def test_model_cannot_inject_derived_repeat_audit(store):
    def mutate(row):
        row['repeated_clause_checks'] = [{'index': 99, 'supported': True}]
    result, _, _, _ = check(store, ExtraModel([1, 2], mutation=mutate))
    assert not result['incomplete_checks']
    assert not result['sentences'][0].get('repeated_clause_checks')
