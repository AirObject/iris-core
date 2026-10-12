"""Quote indices resolve to immutable own-source spans, without weakening support."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.persona import PersonaEngine, _check_payload, _validate_clause_checks, _check_verdicts
from test_persona import store
from test_persona_focus_windows import material_and_sentence, FocusModel, run


def proof_request():
    material, sentence = material_and_sentence(2)
    request = _check_payload(material, [sentence], full_content=sentence['text'])
    row = {'index': 1, 'persona_kind': 'episode', 'supported': True, 'fabricated': False,
           'scene_qualified': True, 'violations': [], 'reason': '有据的当次活动。',
           'clauses': [{'index': i, 'supported': True, 'scene_qualified': True, 'reason': '当次原话。',
                        'evidence': [{'ref': 'M1', 'source_index': 1, 'quote_index': i}]}
                       for i in (1, 2)]}
    return request, {'sentences': [row], 'change_degree': 'small', 'reason': '局部。'}


def test_selection_is_expanded_to_exact_original_span_and_keeps_index():
    request, output = proof_request()
    assert request['evidence_by_ref']['M1']['excerpts'][0]['quote_spans'] == [
        {'index': 1, 'text': '在那次共读中'}, {'index': 2, 'text': '我核对了第1项。'}]
    assert _validate_clause_checks(output, request) == ([], {})
    proof = output['sentences'][0]['clauses'][1]['evidence'][0]
    assert proof == {'ref': 'M1', 'source_index': 1, 'quote_index': 2, 'quote': '我核对了第1项。'}


@pytest.mark.parametrize('change', [
    {'ref': 'M99'}, {'source_index': 0}, {'source_index': True},
    {'quote_index': True}, {'quote_index': 0}, {'quote_index': 9},
    {'quote': '冲突的原文'},
])
def test_bad_or_ambiguous_selection_cannot_supply_a_valid_proof(change):
    request, output = proof_request()
    output['sentences'][0]['clauses'][0]['evidence'][0].update(change)
    assert _validate_clause_checks(output, request)[1]


def test_selection_preserves_original_batch_and_only_enriches_checked_projection(store):
    class Indexed(FocusModel):
        def chat(self, *args, **kwargs):
            reply = super().chat(*args, **kwargs)
            value = json.loads(reply.content)
            for row in value['sentences']:
                row['persona_kind'] = 'episode'
                for part in row['clauses']:
                    part['evidence'] = [{'ref': 'M1', 'source_index': 1, 'quote_index': part['index']}]
            reply.content = dumps(value)
            return reply
    material, sentence = material_and_sentence(2); raw = {}
    checked = run(store, Indexed(), material, [sentence], outputs=raw)
    assert checked['sentences'][0]['clauses'][0]['evidence'][0]['quote'] == '在那次共读中'
    assert 'quote' not in checked['batches'][0]['sentences'][0]['clauses'][0]['evidence'][0]
    assert 'quote' not in json.loads(raw['persona_check'])['sentences'][0]['clauses'][0]['evidence'][0]


@pytest.mark.parametrize('kind', ['state', 'task', 'instruction'])
def test_excluded_content_is_not_allowed_by_true_supported_and_empty_violations(kind):
    request, output = proof_request(); sentence = request['candidate'][0]
    output['sentences'][0]['persona_kind'] = kind
    assert _check_verdicts(output, [sentence])[2]


def test_missing_content_classification_has_no_valid_check_even_when_recovered(store):
    class Missing(FocusModel):
        def chat(self, *args, **kwargs):
            reply = super().chat(*args, **kwargs); value = json.loads(reply.content)
            for row in value['sentences']:
                row.pop('persona_kind', None)
            reply.content = dumps(value); return reply
    material, sentence = material_and_sentence(2)
    result = run(store, Missing(), material, [sentence])
    assert result['incomplete_checks'] and not result['sentences'][0]['supported']


@pytest.mark.parametrize('kind', [None, [], {}, True, 1, 'unrecognized'])
def test_invalid_classification_is_recorded_without_crashing(store, kind):
    class BadKind(FocusModel):
        def chat(self, *args, **kwargs):
            reply = super().chat(*args, **kwargs); value = json.loads(reply.content)
            value['sentences'][0]['persona_kind'] = kind
            reply.content = dumps(value); return reply
    material, sentence = material_and_sentence(2)
    result = run(store, BadKind(), material, [sentence])
    assert result['incomplete_checks'] and not result['sentences'][0]['supported']
