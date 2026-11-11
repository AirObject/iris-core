"""PE-23: a truncated one-sentence window is split by focus; degenerate replies get one retry."""
import copy
import json

import pytest

from iris.db import dumps
from iris.persona import PersonaEngine, _check_verdicts, _output_shape
from iris.persona_evaluation import run_persona_eval
from test_persona import store
from test_persona_evaluation import FakeGateway, TIMELINE, tmp_path
from test_persona_focus_windows import FocusModel, material_and_sentence


class Truncating(FocusModel):
    """Ends a reply at the output limit whenever `rule(request, number)` holds."""
    def __init__(self, rule, *, content=None, duration=1, omit=None):
        super().__init__(duration=duration)
        self.rule, self.content, self.omit = rule, content, omit

    def chat(self, messages, purpose, **kwargs):
        reply = super().chat(messages, purpose, **kwargs)
        request = self.calls[-1][0]
        if self.omit is not None:
            output = json.loads(reply.content)
            for row in output['sentences']:
                row['clauses'] = [part for part in row['clauses'] if part['index'] != self.omit]
            reply.content = dumps(output)
        if self.rule(request, len(self.calls)):
            reply.finish_reason = 'length'
            if self.content is not None:
                reply.content = self.content
        return reply


def several_foci(request, number):
    return any(len(row['clauses']) > 1 for row in request['candidate'])


def single_focus_case():
    material, sentence = material_and_sentence(2)
    text = '在那次共读中我核对了第1项。'
    sentence['text'] = text
    memory = material['evidence']['memories'][0]
    memory['content'] = memory['excerpts'][0]['excerpt'] = text
    return material, sentence


def run(store, model, material, sentences, outputs=None, engine=PersonaEngine):
    outputs = {} if outputs is None else outputs
    result = engine(store, model)._check_all('persona_check', material, sentences,
        full_content=''.join(s['text'] for s in sentences), outputs=outputs, assess_degree=False)
    return result, outputs


def limits(outputs):
    return [event for raw in outputs['persona_check_batches']
            for event in raw.get('persona_check_output_limits', [])]


def test_truncated_one_sentence_window_is_split_by_focus_with_full_context(store):
    material, sentence = material_and_sentence(2)
    model = Truncating(several_foci)
    result, outputs = run(store, model, material, [sentence])
    requests = [request for request, _ in model.calls]
    assert [[p['index'] for p in r['candidate'][0]['clauses']] for r in requests] == [[1, 2], [1], [2]]
    assert all(r['candidate'][0]['text'] == sentence['text'] and r['evidence_by_ref'] == requests[0]['evidence_by_ref']
               for r in requests)
    assert not result['validation_errors']
    row = result['sentences'][0]
    assert row['supported'] and [p['index'] for p in row['clauses']] == [1, 2]
    assert not _check_verdicts(result, [sentence], material=material)[2]
    assert result['output_limit_diagnostics'] == {'truncations':1, 'splits':0, 'split_results':{},
        'terminal_truncations':0, 'focus_splits':1, 'focus_split_results':{'recovered':1},
        'output_retries':0, 'output_retry_results':{}}
    assert limits(outputs)[0]['handled'] == 'focus_split' and limits(outputs)[0]['focus_count'] == 2
    assert [call[1] for call in model.calls] == [360, 361, 362]  # Per-call caps within one stage.


def test_one_focus_still_truncated_is_incomplete_and_skips_its_sibling(store):
    material, sentence = material_and_sentence(2)
    model = Truncating(lambda request, number: True)
    result, outputs = run(store, model, material, [sentence])
    assert [[p['index'] for p in r['candidate'][0]['clauses']] for r, _ in model.calls] == [[1, 2], [1]]
    assert result['validation_errors'] and not result['sentences']
    diagnostics = result['output_limit_diagnostics']
    assert diagnostics['truncations'] == 2 and diagnostics['terminal_truncations'] == 1
    assert diagnostics['focus_split_results'] == {'incomplete_output_limit':1}
    assert diagnostics['output_retries'] == 0  # A complete JSON body is not a degenerate reply.


def test_adverse_verdict_of_the_first_half_is_kept_and_the_rest_left_unchecked(store):
    material, sentence = material_and_sentence(2)

    class FirstHalfRejects(Truncating):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if [p['index'] for p in self.calls[-1][0]['candidate'][0]['clauses']] == [1]:
                output = json.loads(reply.content)
                output['sentences'][0]['clauses'][0]['supported'] = False
                output['sentences'][0]['supported'] = False
                reply.content = dumps(output)
            return reply
    model = FirstHalfRejects(several_foci)
    result, outputs = run(store, model, material, [sentence])
    assert len(model.calls) == 2
    row = result['sentences'][0]
    assert not row['supported'] and row['unchecked_clause_indices'] == [2]
    split = outputs['persona_check_batches'][0]['persona_check_output_splits'][0]
    assert split['unit'] == 'focus' and split['status'] == 'recovered' and split['unchecked_clause_indices'] == [2]
    assert 1 in _check_verdicts(result, [sentence], material=material)[2]


def test_merged_result_missing_a_focus_never_passes(store):
    material, sentence = material_and_sentence(2)

    class Gap(PersonaEngine):
        def _check_fact_batch(self, purpose, material, subset, raw, **kwargs):
            if [p['index'] for p in subset[0]['_check_focus']] == [2]:
                row = {'index':1, 'supported':True, 'fabricated':False, 'scene_qualified':True, 'violations':[],
                       'persona_kind':'episode', 'reason':'没有给出焦点。', 'clauses':[], 'clause_errors':[]}
                return {'sentences':[row], 'change_degree':'small', 'reason':'缺焦点'}, {}, {}, {}
            return super()._check_fact_batch(purpose, material, subset, raw, **kwargs)
    result, outputs = run(store, Truncating(several_foci), material, [sentence], engine=Gap)
    row = result['sentences'][0]
    assert not row['supported']
    assert 'focus split left spans without a verdict' in row['clause_errors']
    assert 1 in _check_verdicts(result, [sentence], material=material)[2]
    split = outputs['persona_check_batches'][0]['persona_check_output_splits'][0]
    assert split['missing_clause_indices'] == [2]


def test_child_that_omits_its_focus_is_rejected_after_the_split(store):
    material, sentence = material_and_sentence(2)
    model = Truncating(several_foci, omit=2)
    result, _ = run(store, model, material, [sentence])
    assert not result['validation_errors']
    assert not result['sentences'][0]['supported']
    assert 1 in _check_verdicts(result, [sentence], material=material)[2]


@pytest.mark.parametrize('content,shape', [
    ('{"sentences":[{"index":1,"clauses":[', 'unclosed_json'),
    ('\n' * 300, 'blank'),
    ('{"sentences":[{"index":1,' + ' ' * 200, 'blank_padding'),
])
def test_degenerate_reply_of_an_unsplittable_request_is_retried_once(store, content, shape):
    material, sentence = single_focus_case()
    model = Truncating(lambda request, number: number == 1, content=content)
    result, outputs = run(store, model, material, [sentence])
    assert len(model.calls) == 2 and model.calls[0][0] == model.calls[1][0]
    assert not result['validation_errors'] and result['sentences'][0]['supported']
    event = limits(outputs)[0]
    assert event['output_shape'] == shape and event['handled'] == 'retry'
    diagnostics = result['output_limit_diagnostics']
    assert diagnostics['output_retries'] == 1 and diagnostics['output_retry_results'] == {'recovered':1}
    assert diagnostics['terminal_truncations'] == 0


def test_retried_reply_truncated_again_is_incomplete_without_a_second_retry(store):
    material, sentence = single_focus_case()
    model = Truncating(lambda request, number: True, content='{"sentences":[')
    result, _ = run(store, model, material, [sentence])
    assert len(model.calls) == 2 and result['validation_errors']
    diagnostics = result['output_limit_diagnostics']
    assert diagnostics['output_retry_results'] == {'failed':1} and diagnostics['terminal_truncations'] == 1


@pytest.mark.parametrize('content', ['', '{"sentences":[]}', '还在分析这一句'])
def test_empty_complete_or_prose_reply_is_not_retried(store, content):
    material, sentence = single_focus_case()
    model = Truncating(lambda request, number: True, content=content)
    result, _ = run(store, model, material, [sentence])
    assert len(model.calls) == 1 and result['validation_errors']
    assert result['output_limit_diagnostics']['output_retries'] == 0


@pytest.mark.parametrize('duration,retried', [(350, False), (250, True)])
def test_retry_only_while_the_stage_can_hold_another_request(store, duration, retried):
    # Window [1,2] passes; window [3] truncates at 2*duration, leaving 900-2*duration.
    material, sentence = material_and_sentence(3)
    last_focus = lambda request, number: [p['index'] for p in request['candidate'][0]['clauses']] == [3] \
        and number == 2
    model = Truncating(last_focus, content='{"sentences":[', duration=duration)
    result, outputs = run(store, model, material, [sentence])
    assert len(model.calls) == (3 if retried else 2)
    assert bool(result['validation_errors']) is not retried
    event = limits(outputs)[0]
    assert event['handled'] == ('retry' if retried else 'terminal')
    assert ('retry_skipped' in event) is not retried
    assert result['output_limit_diagnostics']['output_retries'] == int(retried)


def test_splittable_request_is_split_rather_than_retried(store):
    material, sentence = material_and_sentence(2)
    model = Truncating(several_foci, content='{"sentences":[')
    result, outputs = run(store, model, material, [sentence])
    assert len(model.calls) == 3 and not result['validation_errors']
    assert result['output_limit_diagnostics']['output_retries'] == 0
    assert limits(outputs)[0]['handled'] == 'focus_split'


@pytest.mark.parametrize('content,shape', [(None, 'empty'), ('', 'empty'), ('  \n ', 'blank'),
    ('{"a":1}', 'complete_json'), ('```json\n{"a":1}\n```', 'complete_json'), ('{"a":', 'unclosed_json'),
    ('[1,', 'unclosed_json'), ('{"a":1}' + ' ' * 64, 'blank_padding'), ('先说明', 'text')])
def test_output_shapes(content, shape):
    assert _output_shape(content) == shape


def test_recovered_focus_split_keeps_the_run_valid(tmp_path):
    class FocusLimited(FakeGateway):
        def __init__(self, configs, store, **kwargs):
            super().__init__(configs, store, **kwargs)
            self.clock = kwargs['clock']
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            payload = json.loads(messages[-1]['content'])
            if purpose == 'persona_check' and any(len(row['clauses']) > 1 for row in payload.get('candidate', [])
                                                  if row['origin'] == 'memory'):
                reply.finish_reason = 'length'
            with self.store.write() as conn:
                conn.execute("""INSERT INTO model_calls
                    (purpose,model,duration_ms,result_category,status_code,timed_out,finish_reason,created_at)
                    VALUES(?,'fake',1,'success',200,0,?,?)""",
                    (purpose, reply.finish_reason, self.clock().isoformat()))
            return reply
    case = copy.deepcopy(TIMELINE)
    case['role'] = {**case['role'], 'background':''}
    case['events'] = case['events'][:2]
    corpus = tmp_path/'fixture.json'
    corpus.write_text(dumps(case), encoding='utf-8')
    _, report = run_persona_eval({}, tmp_path, corpus=corpus, out=tmp_path/'out',
        judge_mode='external', gateway_factory=FocusLimited, runner_identity='focus-split')
    metrics = report['metrics']
    assert metrics['run_valid'] and metrics['incomplete_check_count'] == 0
    assert metrics['calls']['recovered_output_limited_calls'] == 1
    limits_report = metrics['check_output_limits']
    assert limits_report['focus_splits'] == 1 and limits_report['focus_split_results'] == {'recovered':1}
    assert limits_report['splits'] == 0 and limits_report['terminal_truncations'] == 0
