"""A missing positive suffix must be checked, within the original window budget."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelError
from iris.persona import PersonaEngine, _check_verdicts
from test_persona_focus_windows import FocusModel, material_and_sentence
from test_persona_clause_checks import batch_sentences


class PrefixModel(FocusModel):
    def __init__(self, *, first=None, completion=None, durations=()):
        super().__init__()
        self.first, self.completion, self.durations = first, completion, durations

    def chat(self, *args, **kwargs):
        self.duration = self.durations[len(self.calls)] if len(self.calls) < len(self.durations) else 1
        reply = super().chat(*args, **kwargs)
        value = json.loads(reply.content)
        memory_rows = [r for r in value['sentences'] if r['clauses']]
        if len(self.calls) == 1:
            row = memory_rows[0]
            row['clauses'] = row['clauses'][:1]
            value['change_degree'] = 'medium'
            if self.first:
                self.first(row)
        elif len(self.calls) == 2 and self.completion:
            self.completion(value)
        reply.content = dumps(value)
        return reply


def check(store, model, *, purpose='persona_check', protected=False, outputs=None):
    material, sentence = material_and_sentence(4)
    sentences = batch_sentences()[:1] + [sentence] if protected else [sentence]
    result = PersonaEngine(store, model)._check_all(purpose, material, sentences,
        full_content=''.join(s['text'] for s in sentences), outputs=outputs if outputs is not None else {}, assess_degree=False)
    return result, material, sentences


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
def test_positive_prefix_requires_a_real_check_of_only_the_missing_focus(store, purpose):
    model = PrefixModel(); outputs = {}
    result, material, sentences = check(store, model, purpose=purpose, outputs=outputs)
    assert len(model.calls) == 3
    assert [[p['index'] for p in r['candidate'][0]['clauses']] for r, _ in model.calls] == [[1, 2], [2], [3, 4]]
    assert [d for _, d in model.calls] == [360, 360, 362]
    assert model.calls[1][0]['task'] == 'check_facts_and_rules'
    assert all(r['candidate'][0]['text'] == sentences[0]['text'] for r, _ in model.calls)
    assert all(r['candidate'][0]['evidence_refs'] == ['M1'] for r, _ in model.calls)
    assert result['change_degree'] == 'large'
    assert result['degree_assessment']['status'] == 'deferred_to_recovery'  # Facts cannot supply a comparison.
    assert not result['incomplete_checks'] and not _check_verdicts(result, sentences, material=material)[2]
    assert [p['index'] for p in result['sentences'][0]['clauses']] == [1, 2, 3, 4]
    assert len(result['batches'][0]['sentences'][0]['clauses']) == 1  # Original remains incomplete.
    raw = outputs[purpose + '_batches'][0]
    assert len(json.loads(raw[purpose])['sentences'][0]['clauses']) == 1
    completion = raw[purpose + '_focus_completions'][0]
    assert completion['missing_clause_indices'] == [2] and completion['completed']
    assert json.loads(completion['outputs'][purpose])['sentences'][0]['clauses'][0]['index'] == 2


def test_completed_protected_rows_are_not_sent_again_or_reindexed_in_the_result(store):
    model = PrefixModel()
    result, _, _ = check(store, model, protected=True)
    assert len(model.calls) == 3
    assert [r['origin'] for r in model.calls[1][0]['candidate']] == ['memory']
    assert model.calls[1][0]['candidate'][0]['index'] == 1
    assert [r['index'] for r in result['sentences']] == [1, 2]
    assert len(result['sentences'][1]['clauses']) == 4 and not result['incomplete_checks']


@pytest.mark.parametrize('bad', ['negative', 'unbound', 'wrong_quote', 'task', 'violation'])
def test_valid_adverse_completion_rejects_the_sentence_without_rechecking_good_prefix(store, bad):
    def mutate(value):
        row = value['sentences'][0]; part = row['clauses'][0]
        if bad == 'negative': part.update(supported=False, evidence=[], reason='本动作无据。')
        elif bad == 'unbound': part['evidence'][0]['ref'] = 'M999'
        elif bad == 'wrong_quote': part['evidence'][0]['quote'] = '不存在的原话'
        elif bad == 'task': row['persona_kind'] = 'task'
        else: row['violations'] = ['当前状态或待办']
    model = PrefixModel(completion=mutate)
    result, material, sentences = check(store, model)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    assert not result['sentences'][0]['supported'] or result['sentences'][0]['violations']
    assert _check_verdicts(result, sentences, material=material)[2]
    assert result['skipped_windows']


@pytest.mark.parametrize('bad', ['missing', 'wrong_index', 'bool_type', 'missing_reason', 'bad_degree'])
def test_bad_completion_is_still_incomplete_and_never_loops(store, bad):
    def mutate(value):
        row = value['sentences'][0]
        if bad == 'missing': row['clauses'] = []
        elif bad == 'wrong_index': row['clauses'][0]['index'] = 99
        elif bad == 'bool_type': row['clauses'][0]['supported'] = 'true'
        elif bad == 'missing_reason': row['reason'] = ''
        else: value['change_degree'] = 'unknown'
    model = PrefixModel(completion=mutate); outputs = {}
    result, _, _ = check(store, model, outputs=outputs)
    assert len(model.calls) == 2
    assert result['incomplete_checks'] and not result['sentences'][0]['supported']
    assert not outputs['persona_check_batches'][0]['persona_check_focus_completions'][0]['completed']


@pytest.mark.parametrize('bad', ['empty', 'wrong_index', 'wrong_bool', 'no_proof', 'negative', 'bad_stop', 'task'])
def test_only_a_valid_unambiguously_positive_prefix_is_eligible_for_completion(store, bad):
    def mutate(row):
        if bad == 'empty': row['clauses'] = []
        elif bad == 'wrong_index': row['clauses'][0]['index'] = 2
        elif bad == 'wrong_bool': row['clauses'][0]['supported'] = 1
        elif bad == 'no_proof': row['clauses'][0]['evidence'] = []
        elif bad == 'negative': row['supported'] = False
        elif bad == 'bad_stop': row['stop_after_failure'] = True
        else: row['persona_kind'] = 'task'
    model = PrefixModel(first=mutate)
    result, _, _ = check(store, model)
    assert len(model.calls) == 1
    if bad == 'no_proof':
        assert not result['incomplete_checks'] and not result['sentences'][0]['supported']
        assert result['sentences'][0]['guard_rejected_prefix']
    else:
        assert result['incomplete_checks']


def test_full_sentence_completion_cannot_hide_an_adverse_extra_focus(store):
    def mutate(value):
        row = value['sentences'][0]; prototype = row['clauses'][0]
        row['clauses'] = [{**copy.deepcopy(prototype), 'index': i} for i in range(1, 5)]
        row['clauses'][0].update(supported=False, evidence=[], reason='此前前缀的主张无据。')
    model = PrefixModel(completion=mutate)
    result, material, sentences = check(store, model)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    assert not result['sentences'][0]['supported']
    assert _check_verdicts(result, sentences, material=material)[2]
    assert [p['index'] for p in result['sentences'][0]['additional_clause_checks']] == [3, 4]


def test_completion_shares_original_window_deadline_and_keeps_both_raw_outputs_on_timeout(store):
    model = PrefixModel(durations=[359, 2]); outputs = {}
    with pytest.raises(ModelError) as error:
        check(store, model, outputs=outputs)
    assert error.value.reason == 'timeout'
    assert [deadline for _, deadline in model.calls] == [360, 360]
    raw = outputs['persona_check_batches'][0]
    assert raw['persona_check'] and raw['persona_check_focus_completions'][0]['outputs']['persona_check']


@pytest.mark.parametrize('mutation', [None, 'memory', 'current'])
def test_completion_runs_outside_transactions_and_publication_rechecks_revisions(store, mutation):
    from iris.memory_ops import edit_memory, setup_role
    from iris.persona import PersonaConflict, admin_edit, current_persona, select_evidence
    from test_persona import FakeGateway, add_self
    setup_role(store, 'Iris', '我来自云城。')
    text = '在那次共读中，我核对了纸上的页码。'
    mid = add_self(store, text)
    ref = next(m['ref'] for m in select_evidence(store)['memories'] if m['memory_id'] == mid)
    old = current_persona(store)

    class Gateway(FakeGateway):
        shortened = False
        completed = False

        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)  # Asserts no write transaction.
            if purpose == 'persona_check':
                request = self.calls[-1][1]; value = json.loads(reply.content)
                if not self.shortened:
                    for row in value['sentences']:
                        if len(row['clauses']) == 2:
                            row['clauses'] = row['clauses'][:1]
                            self.shortened = True
                            break
                elif len(request['candidate']) == 1 and [p['index'] for p in request['candidate'][0]['clauses']] == [2]:
                    self.completed = True
                    if mutation == 'memory': edit_memory(store, mid, 1, content='这次没有核对页码。')
                    elif mutation == 'current': admin_edit(store, '新的手写正文。', expected_version=old['id'])
                reply.content = dumps(value)
            return reply

    model = Gateway(store, {'sentences': [{'text': text, 'basis': [ref]}]})
    engine = PersonaEngine(store, model)
    if mutation == 'current':
        with pytest.raises(PersonaConflict): engine.regenerate(expected_version=old['id'])
        assert current_persona(store)['content'] == '新的手写正文。'
    else:
        result = engine.regenerate(expected_version=old['id'])
        if mutation == 'memory':
            assert result['status'] == 'conflict' and current_persona(store)['id'] == old['id']
        else:
            assert result['version']['checks']['passed'] and text in result['version']['content']
            assert not result['version']['checks']['initial_model']['incomplete_checks']
    assert model.completed
