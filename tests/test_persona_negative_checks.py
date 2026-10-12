"""A witnessed negative may stop; an accepted sentence must have every proof."""
import json

import pytest

from iris.db import dumps
from iris.persona import _validate_clause_checks, _check_verdicts, PersonaEngine
from test_persona_proof_selection import proof_request
from test_persona_focus_windows import FocusModel, material_and_sentence, run
from test_persona_optimization import Model, prepared


def rejected_prefix():
    request, output = proof_request()
    row = output['sentences'][0]
    row.update(supported=False, stop_after_failure=True)
    row['clauses'] = row['clauses'][:1]
    row['clauses'][0].update(supported=False, evidence=[], reason='本焦点人物归属无据。')
    return request, output


def test_explicit_negative_prefix_is_a_complete_rejection_not_partial_acceptance():
    request, output = rejected_prefix()
    errors, problems = _validate_clause_checks(output, request)
    assert not errors and 1 in problems
    assert output['sentences'][0]['unchecked_clause_indices'] == [2]
    assert not output['sentences'][0]['supported']


@pytest.mark.parametrize('mutation', ['no_flag', 'integer_flag', 'positive_row',
    'positive_clause', 'empty', 'wrong_index', 'missing_reason', 'wrong_bool'])
def test_missing_checks_cannot_be_laundered_as_a_negative(mutation):
    request, output = rejected_prefix(); row = output['sentences'][0]
    if mutation == 'no_flag': row.pop('stop_after_failure')
    elif mutation == 'integer_flag': row['stop_after_failure'] = 1
    elif mutation == 'positive_row': row['supported'] = True
    elif mutation == 'positive_clause': row['clauses'][0]['supported'] = True
    elif mutation == 'empty': row['clauses'] = []
    elif mutation == 'wrong_index': row['clauses'][0]['index'] = 2
    elif mutation == 'missing_reason': row['clauses'][0]['reason'] = ''
    else: row['clauses'][0]['supported'] = 0
    assert _validate_clause_checks(output, request)[0]


def test_untrusted_unchecked_metadata_cannot_make_a_positive_incomplete_verdict_complete():
    request, output = proof_request(); row = output['sentences'][0]
    row['clauses'].pop(); row['unchecked_clause_indices'] = [2]
    assert _validate_clause_checks(output, request)[0]


def test_complete_positive_has_no_unchecked_metadata_from_model():
    request, output = proof_request()
    output['sentences'][0]['unchecked_clause_indices'] = [99]
    assert _validate_clause_checks(output, request) == ([], {})
    assert not output['sentences'][0].get('unchecked_clause_indices')


class NegativeFocus(FocusModel):
    def chat(self, *args, **kwargs):
        reply = super().chat(*args, **kwargs); value = json.loads(reply.content)
        for row in value['sentences']:
            if row['clauses']:
                row.update(supported=False, stop_after_failure=True)
                row['clauses'] = row['clauses'][:1]
                row['clauses'][0].update(supported=False, evidence=[], reason='本焦点无据。')
        reply.content = dumps(value); return reply


def test_negative_windows_retain_unchecked_indices_and_raw_output(store):
    material, sentence = material_and_sentence(4); model = NegativeFocus(); outputs = {}
    result = run(store, model, material, [sentence], outputs)
    assert not result['incomplete_checks']
    assert result['sentences'][0]['unchecked_clause_indices'] == [2, 4]
    assert not result['sentences'][0]['supported']
    assert len(result['short_circuits']) == 2
    assert _check_verdicts(result, [sentence], material=material)[2]
    assert all('unchecked_clause_indices' not in batch['sentences'][0] for batch in result['batches'])
    assert len(json.loads(outputs['persona_check'])['sentences'][0]['clauses']) == 1


def test_negative_with_unchecked_focus_is_deleted_never_retained(prepared):
    class Negative(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs); value = json.loads(reply.content)
            if purpose == 'persona_check':
                for row in value['sentences']:
                    if row['persona_kind'] == 'episode' and len(row.get('clauses', [])) > 1:
                        row.update(supported=False, stop_after_failure=True)
                        row['clauses'] = row['clauses'][:1]
                        row['clauses'][0].update(supported=False, evidence=[], reason='人物归属无据。')
            reply.content = dumps(value); return reply
    result = PersonaEngine(prepared, Negative(prepared, repair=None)).regenerate(expected_version=1)
    version = result['version']
    assert version['checks']['passed']
    assert not version['checks']['initial_model']['incomplete_checks']
    assert version['checks']['initial_model']['short_circuits']
    assert all(s['origin'] == 'initial_template' for s in version['sentences'])
    assert any('人物归属无据' in ' '.join(d['reasons']) for d in version['checks']['deleted_sentences'])
