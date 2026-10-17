"""Degree-only work has no unperformed factual judgments to invent."""
import json

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.persona import PersonaEngine, _check_verdicts
from test_persona_focus_windows import material_and_sentence


class DegreeModel:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def chat(self, messages, purpose, **kwargs):
        self.requests.append(json.loads(messages[-1]['content']))
        return ModelReply(dumps(self.answer), 'stop', {})


def check(store, answer, *, with_sentence=False):
    material, sentence = material_and_sentence(2)
    model = DegreeModel(answer)
    outputs = {}
    result = PersonaEngine(store, model)._check_all('persona_recheck', material,
        [sentence] if with_sentence else [], full_content='仍有完整正文。', outputs=outputs)
    return result, outputs, model


def test_degree_only_two_field_answer_is_complete_and_raw_output_is_preserved(store):
    answer = {'change_degree': 'small', 'reason': '只删除一条有界经历。'}
    result, outputs, model = check(store, answer)
    assert model.requests[0]['task'] == 'degree_only'
    assert not model.requests[0]['candidate']
    assert _check_verdicts(result, []) == ('small', [], {})
    assert result['sentences'] == [] and not result['incomplete_checks']
    assert result['normalizations'] == [{'batch': 1, 'operation': 'degree_only_empty_sentences'}]
    assert result['batches'] == [answer]
    assert json.loads(outputs['persona_recheck']) == answer


def test_omitted_factual_rows_remain_invalid_when_a_sentence_was_requested(store):
    result, outputs, _ = check(store, {'change_degree': 'small', 'reason': '未查句子。'}, with_sentence=True)
    assert result['validation_errors']
    assert 'degree_only_empty_sentences' not in dumps(outputs)


@pytest.mark.parametrize('answer', [
    {'change_degree': 'small'},
    {'change_degree': 'unknown', 'reason': '没有合法程度。'},
    {'change_degree': 'small', 'reason': ''},
    {'change_degree': 'small', 'reason': '坏的空值。', 'sentences': None},
    {'change_degree': 'small', 'reason': '伪造额外判定。', 'sentences': [{'index': 1}]},
    {'change_degree': 'small', 'reason': '额外内容不能被忽略。', 'unsupported': ['claim']},
])
def test_degree_protocol_does_not_hide_other_malformed_results(store, answer):
    result, _, _ = check(store, answer)
    assert result['validation_errors']
