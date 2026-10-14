"""Extra numbered checks cannot hide a missing focus or an adverse judgment."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.persona import PersonaEngine, _check_verdicts
from test_persona_focus_windows import FocusModel, material_and_sentence
from test_persona_focus_completion import PrefixModel, check as check_prefix


class ExtraModel(FocusModel):
    def __init__(self, indices, *, on_call=1, mutation=None):
        super().__init__()
        self.indices, self.on_call, self.mutation = indices, on_call, mutation

    def chat(self, *args, **kwargs):
        reply = super().chat(*args, **kwargs)
        value = json.loads(reply.content)
        if len(self.calls) == self.on_call:
            row = value['sentences'][0]
            prototype = row['clauses'][0]
            row['clauses'] = [{**copy.deepcopy(prototype), 'index': i} for i in self.indices]
            if self.mutation:
                self.mutation(row)
        return ModelReply(dumps(value), 'stop', {})


def check(store, model, *, count=4, purpose='persona_check'):
    material, sentence = material_and_sentence(count)
    outputs = {}
    result = PersonaEngine(store, model)._check_all(purpose, material, [sentence],
        full_content=sentence['text'], outputs=outputs)
    return result, material, sentence, outputs


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
def test_tail_focus_with_an_explicit_earlier_extra_is_validated_and_audited(store, purpose):
    model = ExtraModel([3, 1], on_call=2)
    result, material, sentence, outputs = check(store, model, count=3, purpose=purpose)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    assert not _check_verdicts(result, [sentence], material=material)[2]
    assert [p['index'] for p in result['sentences'][0]['clauses']] == [1, 2, 3]
    assert [p['index'] for p in result['sentences'][0]['additional_clause_checks']] == [1]
    assert result['normalizations'][-1]['operation'] == 'additional_focus_checks'
    assert result['normalizations'][-1]['requested_clause_indices'] == [3]
    raw = json.loads(outputs[purpose + '_batches'][1][purpose])
    assert [p['index'] for p in raw['sentences'][0]['clauses']] == [3, 1]


@pytest.mark.parametrize('indices', [[1, 2, 4], [4, 1, 2], [1, 4, 2], [1, 2, 4, 3]])
def test_extras_need_clear_identity_but_do_not_have_to_cover_unrequested_spans(store, indices):
    model = ExtraModel(indices)
    result, material, sentence, _ = check(store, model)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    assert not _check_verdicts(result, [sentence], material=material)[2]
    assert [p['index'] for p in result['sentences'][0]['clauses']] == [1, 2, 3, 4]
    assert [p['index'] for p in result['sentences'][0]['additional_clause_checks']] == [
        i for i in indices if i not in (1, 2)]


@pytest.mark.parametrize('failure', ['unsupported', 'one_date', 'unbound', 'wrong_quote'])
def test_partial_extra_failure_rejects_the_whole_sentence_and_preserves_unchecked_tail(store, failure):
    def mutate(row):
        part = row['clauses'][-1]
        if failure == 'unsupported':
            part['supported'] = False
        elif failure == 'one_date':
            part['scene_qualified'] = False
        elif failure == 'unbound':
            part['evidence'][0]['ref'] = 'M999'
        else:
            part['evidence'][0]['quote'] = 'not present in source'
    model = ExtraModel([1, 2, 3], mutation=mutate)
    result, material, sentence, _ = check(store, model)
    assert len(model.calls) == 1 and not result['incomplete_checks']
    assert not result['sentences'][0]['supported']
    assert _check_verdicts(result, [sentence], material=material)[2]
    assert result['sentences'][0]['unchecked_clause_indices'] == [4]
    assert result['skipped_windows'][0]['already_checked_clause_indices'] == [3]
    assert any('clause 3:' in e for e in result['sentences'][0]['clause_errors'])


@pytest.mark.parametrize('indices', [
    [1, 3, 4], [2, 1, 3], [1, 2, 99], [1, 2, True], [1, 2, '3'],
])
def test_extra_checks_cannot_cover_missing_reordered_or_invalid_indices(store, indices):
    result, _, _, _ = check(store, ExtraModel(indices))
    assert result['incomplete_checks'] and not result['sentences'][0]['supported']
    assert not result['normalizations']


def test_malformed_extra_is_not_dropped_even_when_all_requested_checks_are_present(store):
    def mutate(row):
        row['clauses'][-1]['supported'] = 'true'
    result, _, _, _ = check(store, ExtraModel([1, 2, 3], mutation=mutate))
    assert result['incomplete_checks'] and not result['sentences'][0]['supported']
    assert not result['normalizations']


def test_later_extra_can_reverse_an_earlier_positive_verdict(store):
    def mutate(row):
        row['clauses'][-1].update(supported=False, evidence=[], reason='Earlier claim is unsupported.')
    model = ExtraModel([3, 1], on_call=2, mutation=mutate)
    result, material, sentence, _ = check(store, model, count=3)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    assert not result['sentences'][0]['supported']
    assert _check_verdicts(result, [sentence], material=material)[2]
    assert result['sentences'][0]['additional_clause_checks'][0]['supported'] is False


@pytest.mark.parametrize('extra,negative', [(3, False), (1, False), (1, True)])
def test_suffix_completion_keeps_the_checked_prefix_and_all_adverse_extras(store, extra, negative):
    def mutate(value):
        row = value['sentences'][0]
        added = {**copy.deepcopy(row['clauses'][0]), 'index': extra}
        if negative:
            added.update(supported=False, evidence=[], reason='Earlier claim is unsupported.')
        row['clauses'].append(added)
    model = PrefixModel(completion=mutate); outputs = {}
    result, material, sentences = check_prefix(store, model, outputs=outputs)
    assert not result['incomplete_checks']
    assert result['sentences'][0]['supported'] is (not negative)
    assert bool(_check_verdicts(result, sentences, material=material)[2]) is negative
    audit = outputs['persona_check_batches'][0]['persona_check_focus_completions'][0]
    assert audit['completed'] and audit['additional_clause_indices'] == [extra]
    if negative:
        assert len(model.calls) == 2 and result['skipped_windows']
    else:
        assert len(model.calls) == 3
        assert [p['index'] for p in result['sentences'][0]['clauses']] == [1, 2, 3, 4]
