"""Resultative wording is not a parser for abilities, comparisons or noun stems."""
import json

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.persona import PersonaEngine
from test_persona_evidence_guards import memory, problems, sentence, Permissive
from test_persona import add_self
from test_persona_optimization import Model, prepared


@pytest.mark.parametrize('text', [
    '在那次共读中，我能读完目录。',
    '在那次共读中，我能够读完目录。',
    '在那次共读中，我可以读完目录。',
    '在那次共读中，我能读完目录了。',
    '那次我觉得承认困难比假装读完更舒服。',
    '那次我觉得承认困难比装作读完更舒服。',
    '那次我把完整解法留给对方选择。',
    '那次我觉得这份目录完全可读。',
    '那次我提出完善目录结构的想法。',
    '那次我更看重解释过程而非一口气做完。',
])
def test_non_result_scopes_are_not_mistaken_for_completed_actions(text):
    assert not problems(sentence(text), memory(original=text, content=text))


@pytest.mark.parametrize('text', [
    '在那次共读中，我读完了目录。',
    '在那次共读中，我读完目录。',
    '在那次共读中，我不小心读完了目录。',
    '那次我本来想读完，后来读完了目录。',
    '那次我用智能朗读工具读完了目录。',
    '那次我装作镇定，随后读完了目录。',
])
def test_actual_results_still_require_source_results_in_the_same_predicate(text):
    assert problems(sentence(text), memory(original='我能读完目录。'))


def test_ability_remains_in_candidate_when_the_fake_checker_supports_it(prepared):
    text = '在那次共读中，我能读完目录。'
    mid = add_self(prepared, text)
    result = PersonaEngine(prepared, Permissive(prepared, mid, text, None)).regenerate(expected_version=1)
    assert result['version']['checks']['passed']
    assert text in result['version']['content']
    assert not result['version']['checks']['deleted_sentences']


class WrappedGateway:
    def __init__(self, body):
        self.body = body
    def chat(self, *args, **kwargs):
        return ModelReply(dumps(self.body), 'stop', {})


def test_exact_single_answer_envelope_unwraps_without_changing_fields(prepared):
    result = {'sentences': [], 'change_degree': 'medium', 'reason': '有限偏好变化'}
    body = {'answer': result}; outputs = {}
    actual = PersonaEngine(prepared, WrappedGateway(body))._call('persona_check', {}, outputs)
    assert actual == result
    assert json.loads(outputs['persona_check']) == body
    assert outputs['persona_check_normalizations'] == ['unwrap_single_answer']


@pytest.mark.parametrize('body', [
    {'answer': []}, {'answer': 'text'},
    {'answer': {'sentences': []}, 'sentences': ['conflicting answer']},
])
def test_ambiguous_or_non_object_envelope_is_not_selected_or_coerced(prepared, body):
    outputs = {}
    actual = PersonaEngine(prepared, WrappedGateway(body))._call('persona_check', {}, outputs)
    assert actual == body and 'persona_check_normalizations' not in outputs


def test_answer_unwrap_is_not_recursive(prepared):
    body = {'answer': {'answer': {'sentences': []}}}; outputs = {}
    actual = PersonaEngine(prepared, WrappedGateway(body))._call('persona_check', {}, outputs)
    assert actual == body['answer']
    assert outputs['persona_check_normalizations'] == ['unwrap_single_answer']


def test_degree_only_envelope_does_not_reject_an_otherwise_valid_candidate(prepared):
    class Wrapped(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_recheck':
                reply.content = dumps({'answer': json.loads(reply.content)})
            return reply
    result = PersonaEngine(prepared, Wrapped(prepared, repair=None)).regenerate(expected_version=1)
    assert result['version']['checks']['passed']
    assert result['status'] == 'current'
