"""Omitted scope is unchecked, and never licenses a retained sentence."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.persona import PersonaEngine, _check_verdicts
from test_persona_focus_windows import FocusModel
from test_persona_guarded_prefix import general_material
from test_persona_focus_completion import PrefixModel
from test_persona import FakeGateway, add_self
from iris.memory_ops import setup_role
from iris.persona import current_persona, select_evidence


class MissingScope(FocusModel):
    def __init__(self, *, failure='single_date', mutation=None):
        super().__init__()
        self.failure, self.mutation = failure, mutation

    def chat(self, *args, **kwargs):
        reply = super().chat(*args, **kwargs)
        value = json.loads(reply.content)
        row = value['sentences'][0]
        part = row['clauses'][0]
        if self.failure == 'negative':
            row['supported'] = False
            part.update(supported=False, evidence=[], reason='The supplied claim lacks support.')
        elif self.failure == 'no_proof':
            part['evidence'] = []
        elif self.failure == 'unbound':
            part['evidence'][0]['ref'] = 'M999'
        elif self.failure == 'wrong_quote':
            part['evidence'][0]['quote'] = 'Absent source text.'
        elif self.failure == 'valid':
            for clause in row['clauses']:
                proof = copy.deepcopy(clause['evidence'][0]); proof['source_index'] = 2
                clause['evidence'].append(proof)
        if self.mutation:
            self.mutation(row)
        return ModelReply(dumps(value), 'stop', {})


def check_scope(store, model, purpose='persona_check', *, second_sentence=False):
    material, sentence = general_material()
    sentences = [sentence, sentence] if second_sentence else [sentence]
    outputs = {}
    result = PersonaEngine(store, model)._check_all(purpose, material, sentences,
        full_content=''.join(s['text'] for s in sentences), outputs=outputs)
    return result, material, sentences, outputs


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
@pytest.mark.parametrize('failure', ['single_date', 'negative', 'no_proof', 'unbound', 'wrong_quote'])
def test_independent_adverse_proof_rejects_without_claiming_the_missing_scope_passed(store, purpose, failure):
    model = MissingScope(failure=failure)
    result, material, sentences, outputs = check_scope(store, model, purpose)
    assert len(model.calls) == 1 and not result['incomplete_checks']
    row = result['sentences'][0]
    assert not row['supported'] and row['unchecked_scope_check'] is True
    assert 'scope_check' not in row
    assert row['unchecked_clause_indices'] == [3, 4]
    assert _check_verdicts(result, sentences, material=material)[2]
    assert result['normalizations'] == [{
        'batch': 1, 'sentence_index': 1, 'operation': 'scope_omitted_after_evidence_rejection',
        'checked_clause_indices': [1, 2]}]
    raw = json.loads(outputs[purpose + '_batches'][0][purpose])['sentences'][0]
    assert 'scope_check' not in raw and 'unchecked_scope_check' not in raw
    assert raw['supported'] is (failure != 'negative')


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
def test_complete_positive_proofs_still_require_the_scope_verdict(store, purpose):
    result, _, _, _ = check_scope(store, MissingScope(failure='valid'), purpose)
    assert result['incomplete_checks'] and not result['sentences'][0]['supported']
    assert not result['sentences'][0].get('unchecked_scope_check')
    assert not result['normalizations']


@pytest.mark.parametrize('bad', ['row_reason', 'row_bool', 'part_reason', 'part_bool', 'wrong_index',
    'extra_index', 'evidence_type', 'missing_focus', 'scope_null', 'scope_empty',
    'scope_bool', 'nested_partial'])
def test_existing_bad_fields_are_not_hidden_by_an_independent_proof_failure(store, bad):
    def mutate(row):
        part = row['clauses'][0]
        if bad == 'row_reason': row['reason'] = ''
        elif bad == 'row_bool': row['supported'] = 'true'
        elif bad == 'part_reason': part.pop('reason')
        elif bad == 'part_bool': part['supported'] = 1
        elif bad == 'wrong_index': part['index'] = 99
        elif bad == 'extra_index': row['clauses'].append({**copy.deepcopy(part), 'index': 99})
        elif bad == 'evidence_type': part['evidence'] = None
        elif bad == 'missing_focus': row['clauses'] = row['clauses'][1:]
        elif bad == 'scope_null': row['scope_check'] = None
        elif bad == 'scope_empty': row['scope_check'] = {}
        elif bad == 'scope_bool':
            row['scope_check'] = {'candidate_scope': '共读',
                'source_scopes': [{'ref': 'M1', 'quote': '共读'}], 'preserved': 'true'}
        else:
            part['scope_check'] = {'candidate_scope': '共读',
                'source_scopes': [{'ref': 'M1', 'quote': '共读'}], 'preserved': True}
    result, _, _, _ = check_scope(store, MissingScope(mutation=mutate))
    assert result['incomplete_checks'] and not result['sentences'][0]['supported']
    assert not result['sentences'][0].get('unchecked_scope_check')
    assert not result['normalizations']


@pytest.mark.parametrize('negative', [False, True])
def test_rejected_prefix_preserves_both_unchecked_suffix_and_scope(store, negative):
    def mutate(row):
        if negative:
            row['supported'] = False
            row['clauses'][0].update(supported=False, evidence=[], reason='Unsupported focus.')
    model = PrefixModel(first=mutate)
    result, material, sentences, outputs = check_scope(store, model)
    assert len(model.calls) == 1 and not result['incomplete_checks']
    row = result['sentences'][0]
    assert not row['supported'] and row['unchecked_scope_check']
    assert row['unchecked_clause_indices'] == [2, 3, 4]
    assert _check_verdicts(result, sentences, material=material)[2]
    assert 'persona_check_focus_completions' not in outputs['persona_check_batches'][0]


def test_complete_scope_cannot_be_bypassed_by_a_model_injected_unchecked_flag(store):
    def mutate(row):
        row['unchecked_scope_check'] = True
        row['scope_check'] = {'candidate_scope': '共读',
            'source_scopes': [{'ref': 'M1', 'quote': '共读'}], 'preserved': True}
    result, material, sentences, _ = check_scope(store, MissingScope(failure='valid', mutation=mutate))
    assert not result['incomplete_checks'] and result['sentences'][0]['supported']
    assert not result['sentences'][0].get('unchecked_scope_check')
    assert not _check_verdicts(result, sentences, material=material)[2]


def test_absent_scope_cannot_be_excused_by_the_model_injected_unchecked_flag(store):
    model = MissingScope(failure='valid', mutation=lambda row: row.update(unchecked_scope_check=True))
    result, _, _, _ = check_scope(store, model)
    assert result['incomplete_checks'] and not result['sentences'][0].get('unchecked_scope_check')


def test_a_negative_row_alone_cannot_stand_in_for_the_missing_scope(store):
    model = MissingScope(failure='valid', mutation=lambda row: row.update(supported=False))
    result, _, _, _ = check_scope(store, model)
    assert result['incomplete_checks'] and not result['sentences'][0].get('unchecked_scope_check')


def test_scope_omission_after_proof_failure_cannot_publish_the_claim(store):
    setup_role(store, 'Iris', '我来自云城。')
    source = '在合奏排练中，我先听了对方的节拍。'
    mids = [add_self(store, source, stamp=stamp) for stamp in (
        '2026-10-01T12:00:00+00:00', '2026-10-04T12:00:00+00:00')]
    refs = [m['ref'] for m in select_evidence(store)['memories'] if m['memory_id'] in mids]
    text = '在合奏排练中，我倾向先听对方的节拍。'
    class Omitted(FakeGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose in ('persona_check', 'persona_recheck'):
                value = json.loads(reply.content)
                for row in value['sentences']:
                    if 'scope_check' in row:
                        row.pop('scope_check')
                        row['clauses'][0]['evidence'] = []
                reply.content = dumps(value)
            return reply
    model = Omitted(store, {'sentences': [{'text': text, 'basis': refs}]}, scene=False)
    result = PersonaEngine(store, model).regenerate(expected_version=current_persona(store)['id'])
    checks = result['version']['checks']
    assert checks['passed'] and not checks['initial_model']['incomplete_checks']
    assert text not in result['version']['content']
    assert any('no valid original proof' in ' '.join(d['reasons']) for d in checks['deleted_sentences'])


def test_later_rejected_window_preserves_its_unchecked_scope_on_the_combined_row(store):
    def mutate(row):
        if len(model.calls) == 1:
            row['scope_check'] = {'candidate_scope': '共读',
                'source_scopes': [{'ref': 'M1', 'quote': '共读'}], 'preserved': True}
        else:
            row['supported'] = False
            row['clauses'][0].update(supported=False, evidence=[], reason='Later focus is unsupported.')
    model = MissingScope(failure='valid', mutation=mutate)
    result, _, _, _ = check_scope(store, model)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    assert not result['sentences'][0]['supported']
    assert result['sentences'][0]['unchecked_scope_check']
    assert result['normalizations'][-1]['checked_clause_indices'] == [3, 4]


def test_suffix_completion_records_omitted_scope_without_overwriting_the_proven_prefix(store):
    def prefix(row):
        part = row['clauses'][0]
        proof = copy.deepcopy(part['evidence'][0]); proof['source_index'] = 2
        part['evidence'].append(proof)
        row['scope_check'] = {'candidate_scope': '共读',
            'source_scopes': [{'ref': 'M1', 'quote': '共读'}], 'preserved': True}
    model = PrefixModel(first=prefix)
    result, material, sentences, outputs = check_scope(store, model)
    assert len(model.calls) == 2 and not result['incomplete_checks']
    assert not result['sentences'][0]['supported']
    assert result['sentences'][0]['scope_check']['preserved'] is True
    assert _check_verdicts(result, sentences, material=material)[2]
    audit = outputs['persona_check_batches'][0]['persona_check_focus_completions'][0]
    assert audit['completed'] and audit['unchecked_scope_check']
    assert audit['missing_clause_indices'] == [2]
