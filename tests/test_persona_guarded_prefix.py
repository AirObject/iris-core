"""A deterministically failed positive prefix rejects; missing evidence never passes."""
import copy
import json

import pytest

from iris.db import dumps
from iris.persona import PersonaEngine, _check_verdicts
from test_persona_focus_completion import PrefixModel, check
from test_persona_focus_windows import material_and_sentence
from test_persona_optimization import Model, prepared


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
@pytest.mark.parametrize('failure', ['no_proof', 'unbound', 'wrong_quote', 'single_date'])
def test_adverse_proof_in_a_valid_positive_prefix_rejects_without_completing(store, purpose, failure):
    def mutate(row):
        part = row['clauses'][0]
        if failure == 'no_proof':
            part['evidence'] = []
        elif failure == 'unbound':
            part['evidence'][0]['ref'] = 'M999'
        elif failure == 'wrong_quote':
            part['evidence'][0]['quote'] = 'This text does not occur in the source.'
        else:
            part['scene_qualified'] = False
    model = PrefixModel(first=mutate); outputs = {}
    result, material, sentences = check(store, model, purpose=purpose, outputs=outputs)
    assert len(model.calls) == 1 and not result['incomplete_checks']
    row = result['sentences'][0]
    assert not row['supported'] and row['guard_rejected_prefix']
    assert row['clauses'][0]['supported'] is True  # Preserve the actual model opinion.
    assert row['unchecked_clause_indices'] == [2, 3, 4]
    assert _check_verdicts(result, sentences, material=material)[2]
    assert result['normalizations'] == [{
        'batch': 1, 'sentence_index': 1, 'operation': 'positive_prefix_rejected_by_evidence_guard',
        'checked_clause_indices': [1], 'unchecked_clause_indices': [2]}]
    assert result['skipped_windows']
    raw = outputs[purpose + '_batches'][0]
    original = json.loads(raw[purpose])['sentences'][0]
    assert original['supported'] is True and len(original['clauses']) == 1
    assert 'guard_rejected_prefix' not in original
    assert purpose + '_focus_completions' not in raw


def general_material():
    material, sentence = material_and_sentence(4)
    text = sentence['text'].replace('在那次共读中', '多次共读后')
    sentence.update(text=text, dates=['2026-10-01', '2026-10-02'], date_count=2)
    memory = material['evidence']['memories'][0]
    memory.update(content=text, dates=sentence['dates'], date_count=2)
    memory['excerpts'][0]['excerpt'] = text
    memory['excerpts'].append({**memory['excerpts'][0], 'date': '2026-10-02'})
    return material, sentence


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
@pytest.mark.parametrize('failure', ['single_focus_date', 'scope_not_preserved'])
def test_prefix_guards_use_its_own_dates_and_scope_not_the_whole_memory(store, purpose, failure):
    material, sentence = general_material()
    def mutate(row):
        row['scope_check'] = {'candidate_scope': '共读',
            'source_scopes': [{'ref': 'M1', 'quote': '共读'}], 'preserved': failure != 'scope_not_preserved'}
        if failure == 'scope_not_preserved':
            proof = copy.deepcopy(row['clauses'][0]['evidence'][0])
            proof['source_index'] = 2
            row['clauses'][0]['evidence'].append(proof)
    model = PrefixModel(first=mutate); outputs = {}
    result = PersonaEngine(store, model)._check_all(purpose, material, [sentence],
        full_content=sentence['text'], outputs=outputs)
    assert len(model.calls) == 1 and not result['incomplete_checks']
    row = result['sentences'][0]
    assert not row['supported'] and row['unchecked_clause_indices'] == [2, 3, 4]
    assert row['clauses'][0]['supported'] is True
    expected = 'single-date claim needs an explicit bounded scene' if failure == 'single_focus_date' else 'activity scope not preserved'
    assert any(expected in e for e in row['clause_errors'])


@pytest.mark.parametrize('bad', ['wrong_index', 'boolean_index', 'missing_reason', 'wrong_bool',
    'bad_evidence_type', 'empty', 'duplicate', 'missing_row_reason', 'false_stop',
    'true_stop', 'integer_stop', 'negative_row', 'negative_part', 'task'])
def test_proof_failure_cannot_excuse_a_malformed_or_ineligible_prefix(store, bad):
    def mutate(row):
        part = row['clauses'][0]; part['evidence'] = []
        if bad == 'wrong_index': part['index'] = 2
        elif bad == 'boolean_index': part['index'] = True
        elif bad == 'missing_reason': part.pop('reason')
        elif bad == 'wrong_bool': part['supported'] = 1
        elif bad == 'bad_evidence_type': part['evidence'] = None
        elif bad == 'empty': row['clauses'] = []
        elif bad == 'duplicate': row['clauses'].append(copy.deepcopy(part))
        elif bad == 'missing_row_reason': row['reason'] = ''
        elif bad == 'false_stop': row['stop_after_failure'] = False
        elif bad == 'true_stop': row['stop_after_failure'] = True
        elif bad == 'integer_stop': row['stop_after_failure'] = 1
        elif bad == 'negative_row': row['supported'] = False
        elif bad == 'negative_part': part['supported'] = False
        else: row['persona_kind'] = 'task'
    model = PrefixModel(first=mutate)
    result, _, _ = check(store, model)
    assert len(model.calls) == 2 and not result['incomplete_checks'] and result['invalid_sentence_checks']
    assert not result['sentences'][0]['supported']
    assert not result['sentences'][0].get('guard_rejected_prefix')
    assert not result['normalizations']


def test_bad_scope_structure_is_rejected_after_correction_even_with_an_adverse_date_guard(store):
    material, sentence = general_material()
    model = PrefixModel(first=lambda row: row.update(scope_check={'preserved': 'true'}))
    result = PersonaEngine(store, model)._check_all('persona_recheck', material, [sentence],
        full_content=sentence['text'], outputs={})
    assert len(model.calls) == 2 and not result['incomplete_checks'] and result['invalid_sentence_checks']
    assert not result['sentences'][0].get('guard_rejected_prefix')


def test_valid_prefix_still_checks_the_missing_suffix_and_discards_injected_guard_audit(store):
    model = PrefixModel(first=lambda row: row.update(guard_rejected_prefix=True))
    result, material, sentences = check(store, model)
    assert len(model.calls) == 3 and not result['incomplete_checks']
    assert result['sentences'][0]['supported']
    assert not result['sentences'][0].get('guard_rejected_prefix')
    assert not _check_verdicts(result, sentences, material=material)[2]


def test_guard_rejection_never_skips_the_other_sentences_checks(store):
    material, sentence = material_and_sentence(4)
    model = PrefixModel(first=lambda row: row['clauses'][0].update(evidence=[]))
    result = PersonaEngine(store, model)._check_all('persona_check', material, [sentence, sentence],
        full_content=sentence['text'] * 2, outputs={})
    assert len(model.calls) == 3 and not result['incomplete_checks']
    assert [r['supported'] for r in result['sentences']] == [False, True]
    assert [p['index'] for p in result['sentences'][1]['clauses']] == [1, 2, 3, 4]


def test_guard_rejected_prefix_cannot_publish_the_unchecked_claim(prepared):
    class NoProof(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs); value = json.loads(reply.content)
            if purpose in ('persona_check','persona_recheck'):
                for row in value['sentences']:
                    if row['persona_kind'] == 'episode' and (len(row.get('clauses', [])) > 1 or purpose=='persona_recheck'):
                        row['clauses'] = row['clauses'][:1]
                        row['clauses'][0]['evidence'] = []
            reply.content = dumps(value)
            return reply
    result = PersonaEngine(prepared, NoProof(prepared, repair=None)).regenerate(expected_version=1)
    version = result['version']
    assert version['checks']['passed']
    assert not version['checks']['initial_model']['incomplete_checks']
    assert all(s['origin'] == 'initial_template' for s in version['sentences'])
    assert any('no valid original proof' in ' '.join(d['reasons'])
               for d in version['checks']['deleted_sentences'])
