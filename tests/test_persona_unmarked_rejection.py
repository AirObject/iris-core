"""An explicit negative prefix remains a rejection when its redundant flag is absent."""
import json

import pytest

from iris.db import dumps
from iris.persona import PersonaEngine, _check_verdicts, _validate_clause_checks
from test_persona_clause_checks import batch_sentences
from test_persona_focus_windows import FocusModel, material_and_sentence
from test_persona_negative_checks import rejected_prefix
from test_persona_optimization import Model, prepared


def unmarked():
    request, output = rejected_prefix()
    output['sentences'][0].pop('stop_after_failure')
    return request, output


def test_unmarked_negative_prefix_preserves_rejection_and_unchecked_spans():
    request, output = unmarked()
    errors, problems = _validate_clause_checks(output, request)
    assert not errors and 1 in problems
    row = output['sentences'][0]
    assert row['supported'] is False and row['unchecked_clause_indices'] == [2]
    assert 'stop_after_failure' not in row


@pytest.mark.parametrize('bad', ['positive_row', 'positive_clause', 'empty', 'wrong_index',
    'duplicate', 'missing_reason', 'wrong_bool', 'false_flag', 'integer_flag', 'null_flag'])
def test_missing_flag_does_not_excuse_incomplete_positive_or_malformed_checks(bad):
    request, output = unmarked(); row = output['sentences'][0]
    if bad == 'positive_row': row['supported'] = True
    elif bad == 'positive_clause': row['clauses'][0]['supported'] = True
    elif bad == 'empty': row['clauses'] = []
    elif bad == 'wrong_index': row['clauses'][0]['index'] = 99
    elif bad == 'duplicate': row['clauses'].append(dict(row['clauses'][0]))
    elif bad == 'missing_reason': row['clauses'][0]['reason'] = ''
    elif bad == 'wrong_bool': row['clauses'][0]['supported'] = 0
    elif bad == 'false_flag': row['stop_after_failure'] = False
    elif bad == 'integer_flag': row['stop_after_failure'] = 1
    else: row['stop_after_failure'] = None
    assert _validate_clause_checks(output, request)[0]


class UnmarkedNegative(FocusModel):
    def chat(self, *args, **kwargs):
        reply = super().chat(*args, **kwargs)
        value = json.loads(reply.content)
        if len(self.calls) == 1:
            row = next(r for r in value['sentences'] if r['clauses'])
            row.update(supported=False, fabricated=True)
            row['clauses'] = row['clauses'][:1]
            row['clauses'][0].update(supported=False, evidence=[], reason='The first claim is unsupported.')
        reply.content = dumps(value)
        return reply


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
@pytest.mark.parametrize('protected', [False, True])
def test_unmarked_negative_skips_only_rejected_sentence_and_preserves_raw_response(store, purpose, protected):
    material, sentence = material_and_sentence(4)
    sentences = batch_sentences()[:1] + [sentence] if protected else [sentence]
    model = UnmarkedNegative(); outputs = {}
    result = PersonaEngine(store, model)._check_all(purpose, material, sentences,
        full_content=''.join(s['text'] for s in sentences), outputs=outputs)
    assert len(model.calls) == 1 and not result['incomplete_checks']
    row = result['sentences'][-1]
    assert not row['supported'] and row['unchecked_clause_indices'] == [2, 3, 4]
    assert _check_verdicts(result, sentences, material=material)[2]
    if protected:
        assert result['sentences'][0]['supported'] is True
    normalization = result['normalizations'][0]
    assert normalization == {'batch': 1, 'sentence_index': len(sentences),
        'operation': 'negative_prefix_without_stop_marker',
        'checked_clause_indices': [1], 'unchecked_clause_indices': [2]}
    raw = json.loads(outputs[purpose + '_batches'][0][purpose])
    assert 'stop_after_failure' not in raw['sentences'][-1]
    assert 'unchecked_clause_indices' not in raw['sentences'][-1]


def test_unmarked_rejection_does_not_skip_another_sentences_proofs(store):
    material, sentence = material_and_sentence(4); model = UnmarkedNegative()
    result = PersonaEngine(store, model)._check_all('persona_check', material, [sentence, sentence],
        full_content=sentence['text'] * 2, outputs={})
    assert len(model.calls) == 3 and not result['incomplete_checks']
    assert [r['supported'] for r in result['sentences']] == [False, True]
    assert [p['index'] for p in result['sentences'][1]['clauses']] == [1, 2, 3, 4]


def test_unmarked_negative_cannot_publish_the_rejected_claim(prepared):
    class Negative(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs); value = json.loads(reply.content)
            if purpose == 'persona_check':
                for row in value['sentences']:
                    if row['persona_kind'] == 'episode' and len(row.get('clauses', [])) > 1:
                        row.update(supported=False, fabricated=True)
                        row['clauses'] = row['clauses'][:1]
                        row['clauses'][0].update(supported=False, evidence=[], reason='The actor has no evidence.')
            reply.content = dumps(value)
            return reply
    result = PersonaEngine(prepared, Negative(prepared, repair=None)).regenerate(expected_version=1)
    version = result['version']
    assert version['checks']['passed']
    assert not version['checks']['initial_model']['incomplete_checks']
    assert version['checks']['initial_model']['short_circuits']
    assert all(s['origin'] == 'initial_template' for s in version['sentences'])
    assert any('The actor has no evidence.' in ' '.join(d['reasons'])
               for d in version['checks']['deleted_sentences'])
