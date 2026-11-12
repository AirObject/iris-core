"""PE-23: a truncated one-sentence window is split by focus; truncated replies get one bounded retry."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelError, ModelReply
from iris.persona import (OUTPUT_RECOVERY_RESERVE_SECONDS, PersonaEngine, _check_verdicts, _output_shape,
                          admin_edit, current_persona)
from iris.persona_evaluation import run_persona_eval
from test_persona import FakeGateway as ProductGateway, add_self, store
from test_persona_evaluation import FakeGateway, TIMELINE, tmp_path
from test_persona_focus_windows import FocusModel, material_and_sentence


class Truncating(FocusModel):
    """Ends a reply at the output limit whenever `rule(request, number)` holds.

    `script` overrides one call by number: an infrastructure error, a broken but
    finished reply, or a reply missing its reason. `durations` sets call lengths.
    """
    def __init__(self, rule, *, content=None, duration=1, omit=None, script=None, durations=None):
        super().__init__(duration=duration)
        self.rule, self.content, self.omit = rule, content, omit
        self.script, self.durations, self.base = script or {}, durations or {}, duration

    def chat(self, messages, purpose, **kwargs):
        number = len(self.calls) + 1
        self.duration = self.durations.get(number, self.base)
        action = self.script.get(number)
        if action in ('timeout', 'rate_limited', 'transport_failure', 'paused'):
            self.calls.append((json.loads(messages[-1]['content']), kwargs['_deadline']))
            if action == 'timeout':
                self.now = kwargs['_deadline']
                raise ModelError('timeout', 'total timeout', reason='timeout')
            if action == 'paused':
                raise ModelError('paused', '达到每日 token 上限', paused=True, reason='usage_limit')
            self.now += self.duration
            raise ModelError('retryable', action, reason=action)
        if action in ('item_error', 'content_rejection', 'configuration'):
            self.calls.append((json.loads(messages[-1]['content']), kwargs['_deadline']))
            self.now += self.duration
            raise ModelError(action, action, reason=action)
        reply = super().chat(messages, purpose, **kwargs)
        request = self.calls[-1][0]
        if self.omit is not None:
            output = json.loads(reply.content)
            for row in output['sentences']:
                row['clauses'] = [part for part in row['clauses'] if part['index'] != self.omit]
            reply.content = dumps(output)
        if action == 'broken':
            reply.content = '{"sentences": [ {"index": 1,'
        elif action == 'no_reason':
            output = json.loads(reply.content)
            output['sentences'][0]['reason'] = ''
            reply.content = dumps(output)
        elif self.rule(request, number):
            reply.finish_reason = 'length'
            if self.content is not None:
                reply.content = self.content
        return reply


def several_foci(request, number):
    return any(len(row['clauses']) > 1 for row in request['candidate'])


def first_call(request, number):
    return number == 1


def single_focus_case():
    material, sentence = material_and_sentence(2)
    text = '在那次共读中我核对了第1项。'
    sentence['text'] = text
    memory = material['evidence']['memories'][0]
    memory['content'] = memory['excerpts'][0]['excerpt'] = text
    return material, sentence


def run(store, model, material, sentences, outputs=None, engine=PersonaEngine, assess_degree=False):
    outputs = {} if outputs is None else outputs
    result = engine(store, model)._check_all('persona_check', material, sentences,
        full_content=''.join(s['text'] for s in sentences), outputs=outputs, assess_degree=assess_degree)
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
        'terminal_truncations':0, 'recovered_truncations':1, 'focus_splits':1,
        'focus_split_results':{'recovered':1}, 'output_retries':0, 'output_retry_results':{},
        'output_retry_errors':{}, 'truncation_events':[{'order':1}]}
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
    assert diagnostics['recovered_truncations'] == 0
    assert diagnostics['focus_split_results'] == {'incomplete_output_limit':1}
    assert diagnostics['output_retries'] == 0  # A complete JSON body is not retried.


@pytest.mark.parametrize('durations,split', [({1:300, 2:300}, False), ({1:200, 2:150}, True)])
def test_focus_split_needs_room_in_the_stage(store, durations, split):
    # Window [3,4] truncates after d s; its two halves need 2d plus the reserve.
    material, sentence = material_and_sentence(4)
    model = Truncating(lambda request, number: number == 2, durations=durations)
    result, outputs = run(store, model, material, [sentence])
    assert len(model.calls) == (4 if split else 2)
    assert bool(result['validation_errors']) is not split
    event = [e for e in limits(outputs) if e['focus_count'] == 2][0]
    assert event['handled'] == ('focus_split' if split else 'terminal')
    assert ('focus_split_skipped' in event) is not split
    assert result['output_limit_diagnostics']['focus_splits'] == int(split)
    assert result['output_limit_diagnostics']['terminal_truncations'] == int(not split)


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
    ('', 'empty'),
])
def test_truncated_reply_of_an_unsplittable_request_is_retried_once(store, content, shape):
    material, sentence = single_focus_case()
    model = Truncating(first_call, content=content)
    result, outputs = run(store, model, material, [sentence])
    assert len(model.calls) == 2 and model.calls[0][0] == model.calls[1][0]
    assert not result['validation_errors'] and result['sentences'][0]['supported']
    event = limits(outputs)[0]
    assert event['output_shape'] == shape and event['handled'] == 'retry' and event['retry'] == 'recovered'
    diagnostics = result['output_limit_diagnostics']
    assert diagnostics['output_retries'] == 1 and diagnostics['output_retry_results'] == {'recovered':1}
    assert diagnostics['terminal_truncations'] == 0 and diagnostics['recovered_truncations'] == 1


@pytest.mark.parametrize('failure', ['length', 'timeout', 'rate_limited', 'transport_failure', 'broken'])
def test_failed_retry_restores_the_original_truncation(store, failure):
    material, sentence = single_focus_case()
    script = {} if failure == 'length' else {2:failure}
    model = Truncating(lambda request, number: number == 1 or failure == 'length',
                       content='{"sentences":[', script=script)
    outputs = {}
    result, _ = run(store, model, material, [sentence], outputs)  # Never a stage failure.
    assert len(model.calls) == 2  # No timeout retry for the retry, no second retry.
    assert result['validation_errors'] == ['output_limit; no complete judgment'] and not result['sentences']
    assert 'persona_check_incomplete' in outputs and outputs['persona_check_incomplete'] == 'output_limit'
    event = limits(outputs)[0]
    assert event['retry'] == 'failed' and event['handled'] == 'terminal'
    diagnostics = result['output_limit_diagnostics']
    assert diagnostics['output_retry_results'] == {'failed':1}
    assert diagnostics['terminal_truncations'] == 1 and diagnostics['recovered_truncations'] == 0
    assert diagnostics['truncations'] == (2 if failure == 'length' else 1)
    assert not any('persona_check_timeout_retries' in raw for raw in outputs['persona_check_batches'])


def test_retry_deadline_leaves_the_stage_reserve(store):
    # Window [1,2] ends at 350; window [3] truncates at 500 after 150 s.
    material, sentence = material_and_sentence(3)
    last = lambda request, number: number == 2
    model = Truncating(last, content='', durations={1:350, 2:150})
    result, _ = run(store, model, material, [sentence])
    assert len(model.calls) == 3 and not result['validation_errors']
    assert model.calls[2][1] == 900 - OUTPUT_RECOVERY_RESERVE_SECONDS


@pytest.mark.parametrize('durations,retried', [({1:300, 2:300}, False), ({1:300, 2:200}, True)])
def test_retry_only_while_the_stage_can_hold_another_request(store, durations, retried):
    # Window [3] truncates at 300+d after d s; it is retried only if 900-(300+d)-reserve >= d.
    material, sentence = material_and_sentence(3)
    last = lambda request, number: number == 2
    model = Truncating(last, content='{"sentences":[', durations=durations)
    result, outputs = run(store, model, material, [sentence])
    assert len(model.calls) == (3 if retried else 2)
    assert bool(result['validation_errors']) is not retried
    event = limits(outputs)[0]
    assert event['handled'] == ('retry' if retried else 'terminal')
    assert ('retry_skipped' in event) is not retried
    assert result['output_limit_diagnostics']['output_retries'] == int(retried)


@pytest.mark.parametrize('content', ['{"sentences":[]}', '还在分析这一句'])
def test_complete_or_prose_reply_is_not_retried(store, content):
    material, sentence = single_focus_case()
    model = Truncating(lambda request, number: True, content=content)
    result, _ = run(store, model, material, [sentence])
    assert len(model.calls) == 1 and result['validation_errors']
    assert result['output_limit_diagnostics']['output_retries'] == 0


def test_splittable_request_is_split_rather_than_retried(store):
    material, sentence = material_and_sentence(2)
    model = Truncating(several_foci, content='{"sentences":[')
    result, outputs = run(store, model, material, [sentence])
    assert len(model.calls) == 3 and not result['validation_errors']
    assert result['output_limit_diagnostics']['output_retries'] == 0
    assert limits(outputs)[0]['handled'] == 'focus_split'


def test_window_completed_by_a_structure_reask_counts_its_truncation_recovered(store):
    # The retry finishes but omits a reason; the sentence reask completes the window.
    material, sentence = single_focus_case()
    model = Truncating(first_call, content='', script={2:'no_reason'})
    result, outputs = run(store, model, material, [sentence])
    assert len(model.calls) == 3 and not result['validation_errors'] and result['sentences'][0]['supported']
    diagnostics = result['output_limit_diagnostics']
    assert diagnostics['recovered_truncations'] == 1 and diagnostics['terminal_truncations'] == 0
    assert limits(outputs)[0]['window'] == 'complete'


@pytest.mark.parametrize('retry,complete', [(None, True), ('broken', False), ('timeout', False)])
def test_degree_judgment_retry_completes_or_falls_back(store, retry, complete):
    material, sentence = single_focus_case()
    degree = lambda request, number: request.get('task') == 'degree_only' and number == 2
    model = Truncating(degree, content='', script={3:retry} if retry else {})
    result, _ = run(store, model, material, [sentence], assess_degree=True)
    assert len(model.calls) == 3
    assert result['degree_assessment']['status'] == ('complete' if complete else 'invalid')
    assert bool(result['validation_errors']) is not complete
    assert result['output_limit_diagnostics']['terminal_truncations'] == int(not complete)


def test_handwritten_review_retry_is_counted(store):
    add_self(store)
    previous = admin_edit(store, '我在直播里会耐心解释规则。', expected_version=current_persona(store)['id'])

    class HandwrittenTruncated(ProductGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            reviews = [c for c in self.calls if c[1].get('task') == 'handwritten_review']
            if self.calls[-1][1].get('task') == 'handwritten_review' and len(reviews) == 1:
                reply.finish_reason, reply.content = 'length', ''
            return reply
    model = HandwrittenTruncated(store, {'sentences':[]})
    result = PersonaEngine(store, model).regenerate(expected_version=previous['id'])
    review = result['version']['checks']['handwritten_review']
    assert review['output_limit_diagnostics']['output_retry_results'] == {'recovered':1}
    assert review['output_limit_diagnostics']['recovered_truncations'] == 1
    assert review['output_limit_diagnostics']['terminal_truncations'] == 0


@pytest.mark.parametrize('content,shape', [(None, 'empty'), ('', 'empty'), ('  \n ', 'blank'),
    ('{"a":1}', 'complete_json'), ('```json\n{"a":1}\n```', 'complete_json'), ('{"a":', 'unclosed_json'),
    ('[1,', 'unclosed_json'), ('{"a":1}' + ' ' * 64, 'blank_padding'), ('先说明', 'text')])
def test_output_shapes(content, shape):
    assert _output_shape(content) == shape


def evaluate(tmp_path, decide, *, one_focus=False, text=None):
    """Run the format timeline; `decide(purpose, payload, number)` may change a fact reply."""
    generated = '在那次直播中我耐心解释了规则。' if one_focus else text
    class Scripted(FakeGateway):
        numbers = 0
        def __init__(self, configs, store, **kwargs):
            super().__init__(configs, store, **kwargs)
            self.clock = kwargs['clock']
        def chat(self, messages, purpose, **kwargs):
            payload = json.loads(messages[-1]['content'])
            if purpose == 'persona_generate' and generated:
                evidence = payload['evidence']['memories'][0]
                reply = ModelReply(dumps({'sentences':[{'text':generated,
                                                        'basis':[evidence['ref']]}]}), 'stop', {})
            else:
                reply = super().chat(messages, purpose, **kwargs)
            failure = None
            if purpose == 'persona_check' and payload.get('task') == 'check_facts_and_rules':
                Scripted.numbers += 1
                failure = decide(reply, payload, Scripted.numbers)
            timed_out, limited = failure == 'timeout', failure == 'rate_limited'
            with self.store.write() as conn:
                conn.execute("""INSERT INTO model_calls
                    (purpose,model,duration_ms,result_category,status_code,timed_out,finish_reason,created_at)
                    VALUES(?,'fake',1,?,?,?,?,?)""",
                    (purpose, 'retryable' if failure else 'success', 429 if limited else None if timed_out else 200,
                     int(timed_out), None if failure else reply.finish_reason, self.clock().isoformat()))
            if timed_out:
                raise ModelError('timeout', 'total timeout', reason='timeout')
            if limited:
                raise ModelError('retryable', 'rate limited', reason='rate_limited')
            return reply
    case = copy.deepcopy(TIMELINE)
    case['role'] = {**case['role'], 'background':''}
    case['events'] = case['events'][:2]
    corpus = tmp_path/'fixture.json'
    corpus.write_text(dumps(case), encoding='utf-8')
    _, report = run_persona_eval({}, tmp_path, corpus=corpus, out=tmp_path/'out',
        judge_mode='external', gateway_factory=Scripted, runner_identity='pe23-recovery')
    return report['metrics']


def test_recovered_focus_split_keeps_the_run_valid(tmp_path):
    def decide(reply, payload, number):
        if any(len(row['clauses']) > 1 for row in payload['candidate'] if row['origin'] == 'memory'):
            reply.finish_reason = 'length'
    metrics = evaluate(tmp_path, decide)
    assert metrics['run_valid'] and metrics['incomplete_check_count'] == 0
    assert metrics['calls']['recovered_output_limited_calls'] == 1
    report = metrics['check_output_limits']
    assert report['focus_splits'] == 1 and report['focus_split_results'] == {'recovered':1}
    assert report['splits'] == 0 and report['terminal_truncations'] == 0 and report['recovered_truncations'] == 1


def test_recovered_retry_keeps_the_run_valid(tmp_path):
    def decide(reply, payload, number):
        if number == 1:
            reply.finish_reason, reply.content = 'length', ''
    metrics = evaluate(tmp_path, decide, one_focus=True)
    assert metrics['run_valid'] and metrics['incomplete_check_count'] == 0
    assert metrics['calls']['recovered_output_limited_calls'] == 1
    assert metrics['check_output_limits']['output_retry_results'] == {'recovered':1}


def test_failed_retry_is_a_retained_incomplete_checkpoint(tmp_path):
    def decide(reply, payload, number):
        if number == 1:
            reply.finish_reason, reply.content = 'length', '{"sentences":['
        elif number == 2:
            reply.content = '{"sentences": [ {"index": 1,'
    metrics = evaluate(tmp_path, decide, one_focus=True)
    assert metrics['run_valid'] and metrics['invalid_call_count'] == 0
    assert metrics['incomplete_check_count'] == 1
    assert metrics['incomplete_checks'][0]['outcome'] == 'unpublished_current_retained'
    assert metrics['calls']['tolerated_output_limited_calls'] == 1
    assert metrics['check_output_limits']['terminal_truncations'] == 1


# Round 3: a recovery leaves room for the rest of the stage; a failed split restores the truncation.

def two_windows_then_single():
    """A single-focus sentence (its own window) followed by a two-focus sentence."""
    material, later = material_and_sentence(2)
    single = {**later, 'text':'在那次共读中我核对了第1项。'}
    return material, [single, later]


@pytest.mark.parametrize('truncated,split', [(300, False), (100, True)])
def test_focus_split_must_fit_both_halves(store, truncated, split):
    # Two halves of a 300 s truncation (600 s) plus the reserve exceed the 600 s left.
    material, sentence = material_and_sentence(2)
    model = Truncating(first_call, durations={1:truncated})
    outputs = {}
    result, _ = run(store, model, material, [sentence], outputs)
    assert len(model.calls) == (3 if split else 1)
    assert bool(result['validation_errors']) is not split
    assert outputs.get('persona_check_incomplete') == (None if split else 'output_limit')


@pytest.mark.parametrize('failure', ['timeout', 'rate_limited', 'transport_failure', 'length'])
def test_failed_half_restores_the_original_truncation(store, failure):
    # A half that times out twice (its one timeout retry included), is refused or truncated.
    material, sentence = material_and_sentence(2)
    script = {} if failure == 'length' else {2:failure, 3:failure} if failure == 'timeout' else {2:failure}
    model = Truncating(lambda request, number: number in (1, 2) if failure == 'length' else number == 1,
                       durations={1:100}, script=script)
    outputs = {}
    result, _ = run(store, model, material, [sentence], outputs)  # Never a stage failure.
    assert len(model.calls) == (3 if failure == 'timeout' else 2)
    assert result['validation_errors'] == ['output_limit; no complete judgment']
    assert outputs['persona_check_incomplete'] == 'output_limit'
    split = outputs['persona_check_batches'][0]['persona_check_output_splits'][0]
    assert split['status'] == 'incomplete_' + ('output_limit' if failure == 'length' else failure)
    diagnostics = result['output_limit_diagnostics']
    assert diagnostics['terminal_truncations'] == 1 and diagnostics['recovered_truncations'] == 0
    assert limits(outputs)[0]['handled'] == 'terminal'


def test_halves_end_before_the_stage_reserve(store):
    material, sentence = material_and_sentence(4)
    model = Truncating(lambda request, number: number == 2, durations={1:359, 2:130})
    result, _ = run(store, model, material, [sentence])
    assert len(model.calls) == 4 and not result['validation_errors']
    assert all(deadline <= 900 - OUTPUT_RECOVERY_RESERVE_SECONDS for _, deadline in model.calls[2:])


@pytest.mark.parametrize('later,split', [(True, False), (False, True)])
def test_a_split_leaves_room_for_the_windows_still_to_run(store, later, split):
    # [1,2] truncates after 250 s: 2*250 + later windows*250 + reserve against 650 s left.
    material, sentence = material_and_sentence(3 if later else 2)
    model = Truncating(first_call, durations={1:250})
    outputs = {}
    result, _ = run(store, model, material, [sentence], outputs)
    assert len(model.calls) == (3 if split else 1)
    assert bool(result['validation_errors']) is not split
    assert outputs.get('persona_check_incomplete') == (None if split else 'output_limit')
    assert limits(outputs)[0]['recovery_estimate_seconds'] == 560 + 250 * later


@pytest.mark.parametrize('later,retried', [(True, False), (False, True)])
def test_a_retry_leaves_room_for_the_windows_still_to_run(store, later, retried):
    # The single-focus window truncates after 290 s: 290 + later windows*290 + reserve against 610 s left.
    material, sentences = two_windows_then_single()
    model = Truncating(first_call, content='', durations={1:290})
    result, outputs = run(store, model, material, sentences if later else sentences[:1])
    assert (len(model.calls) >= 2 and model.calls[1][0] == model.calls[0][0]) is retried
    assert bool(result['validation_errors']) is not retried


def test_failed_retry_timeout_is_tolerated_at_a_retained_checkpoint(tmp_path):
    def decide(reply, payload, number):
        if number == 1:
            reply.finish_reason, reply.content = 'length', ''
        elif number == 2:
            return 'timeout'
    metrics = evaluate(tmp_path, decide, one_focus=True)
    assert metrics['run_valid'] and metrics['invalid_call_count'] == 0
    assert metrics['incomplete_check_count'] == 1
    assert metrics['calls']['tolerated_timed_out_calls'] == 1 and metrics['calls']['unrecovered_timed_out_calls'] == 0
    assert metrics['check_output_limits']['output_retry_errors'] == {'timeout':1}


def test_an_ordinary_batch_timeout_is_still_invalid(tmp_path):
    # The timeout comes before the truncation: it is no failed output retry.
    def decide(reply, payload, number):
        if number == 1:
            return 'timeout'
        if number == 2:
            reply.finish_reason, reply.content = 'length', ''
    metrics = evaluate(tmp_path, decide, one_focus=True)
    assert not metrics['run_valid'] and metrics['invalid_call_count'] == 1
    assert metrics['calls']['tolerated_timed_out_calls'] == 0


# Round 4: sibling sub-batches, the retry's own call, and adverse verdicts across a reask.

def pair_of_single_foci():
    material, sentence = single_focus_case()
    return material, [sentence, copy.deepcopy(sentence)]


def test_a_sibling_overrunning_the_stage_after_a_retry_is_a_tolerable_incomplete_check(store):
    material, sentences = pair_of_single_foci()
    model = Truncating(lambda request, number: number in (1, 2), content='{"sentences":[',
                       durations={1:200, 2:200, 3:200, 4:350})
    outputs = {}
    result, _ = run(store, model, material, sentences, outputs)  # Never a stage failure.
    assert len(model.calls) == 4 and result['validation_errors']
    assert outputs['persona_check_incomplete'] == 'output_limit'
    split = outputs['persona_check_batches'][0]['persona_check_output_splits'][0]
    assert split['status'] == 'incomplete_timeout' and limits(outputs)[0]['handled'] == 'terminal'


@pytest.mark.parametrize('failure', ['rate_limited', 'transport_failure'])
def test_a_failed_sentence_split_child_restores_the_truncation(store, failure):
    material, sentences = pair_of_single_foci()
    model = Truncating(first_call, script={3:failure})
    outputs = {}
    result, _ = run(store, model, material, sentences, outputs)
    assert result['validation_errors'] and outputs['persona_check_incomplete'] == 'output_limit'
    assert result['output_limit_diagnostics']['terminal_truncations'] == 1


@pytest.mark.parametrize('second,retried', [(250, False), (100, True)])
def test_a_retry_inside_a_split_leaves_room_for_its_sibling(store, second, retried):
    # A retry of the first child after 250+d s needs d + (sibling) d + reserve.
    material, sentences = pair_of_single_foci()
    model = Truncating(lambda request, number: number in (1, 2), content='', durations={1:250, 2:second})
    result, outputs = run(store, model, material, sentences)
    child = outputs['persona_check_batches'][0]['persona_check_output_splits'][0]['children'][0]['outputs']
    event = child['persona_check_output_limits'][0]
    assert ('retry_skipped' in event) is not retried
    assert bool(result['validation_errors']) is not retried


def test_only_the_failed_retrys_own_timeout_is_tolerated(tmp_path):
    # A focus split whose half recovers from an ordinary timeout, then a later window's retry times out.
    def decide(reply, payload, number):
        if number in (1, 5):
            reply.finish_reason, reply.content = 'length', '{"sentences":['
        elif number in (2, 6):
            return 'timeout'
    metrics = evaluate(tmp_path, decide, text='在那次直播中，我耐心解释了规则，我回答了提问。')
    assert metrics['incomplete_check_count'] == 1
    assert metrics['calls']['tolerated_timed_out_calls'] == 1
    assert metrics['calls']['unrecovered_timed_out_calls'] == 1
    assert not metrics['run_valid'] and metrics['invalid_call_count'] == 1


def test_a_reask_for_one_item_cannot_clear_another_items_adverse_verdict(store):
    from test_persona_check_resilience import Checks, general_case
    material, sentence = general_case()
    text = '合奏排练中我总是先听节拍偶尔先看谱。'
    sentence['text'] = text
    memory = material['evidence']['memories'][0]
    memory['content'] = text
    for source in memory['excerpts']:
        source['excerpt'] = '合奏排练中我总是先听节拍偶尔先看谱'
    def item(word, source):
        return {'candidate_quantifier':word, 'candidate_scope':'', 'source_quantifier':source,
                'source_scopes':[], 'source_instances':2, 'preserved':True}
    def mutate(value, payload, number):
        first = value['sentences'][0]['clauses'][0]
        first['quantifier_checks'] = ([item('总是', '偶尔'), item('偶尔', '偶尔先看谱')] if number == 1
                                      else [item('总是', '总是'), item('偶尔', '偶尔')])
    model = Checks(store, mutate)
    result = PersonaEngine(store, model)._check_all('persona_check', material, [sentence],
        full_content=text, outputs={}, assess_degree=False)
    assert len(model.calls) == 2  # One reask, which on its own would pass.
    assert not result['sentences'][0]['supported']
    problems = _check_verdicts(result, [sentence], material=material)[2]
    assert 'frequency strength raised' in ' '.join(problems[1])


# Round 5: pauses keep their own path; a failed focus split's timeouts; names stay in code.

@pytest.mark.parametrize('case', ['retry', 'focus_split', 'sentence_split'])
def test_a_pause_is_never_absorbed_by_a_fallback(store, case):
    if case == 'retry':
        material, sentences = single_focus_case()
        sentences = [sentences]
        model = Truncating(first_call, content='', script={2:'paused'})
    elif case == 'focus_split':
        material, sentence = material_and_sentence(2)
        sentences = [sentence]
        model = Truncating(first_call, script={2:'paused'})
    else:
        material, sentences = pair_of_single_foci()
        model = Truncating(first_call, script={2:'paused'})
    with pytest.raises(ModelError) as caught:
        run(store, model, material, sentences)
    assert caught.value.paused and caught.value.reason == 'usage_limit'


def test_a_pause_during_a_retry_skips_the_attempt_instead_of_rejecting(store):
    from iris.persona import _generation_material, _settings
    text = '在那次直播中我耐心解释了规则。'
    mid = add_self(store, text)
    previous = current_persona(store)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), previous, '2026-11-12T12:00:00+00:00')
    ref = next(m['ref'] for m in material['evidence']['memories'] if m['memory_id'] == mid)

    class PausedRetry(ProductGateway):
        facts = 0
        def chat(self, messages, purpose, **kwargs):
            payload = json.loads(messages[-1]['content'])
            if purpose == 'persona_check' and payload.get('task') == 'check_facts_and_rules':
                self.facts += 1
                if self.facts == 2:
                    raise ModelError('paused', '达到每日 token 上限', paused=True, reason='usage_limit')
            reply = super().chat(messages, purpose, **kwargs)
            if self.facts == 1 and purpose == 'persona_check':
                reply.finish_reason, reply.content = 'length', ''
            return reply
    result = PersonaEngine(store, PausedRetry(store, {'sentences':[{'text':text, 'basis':[ref]}]})
                           ).regenerate(expected_version=previous['id'])
    assert result['status'] == 'skipped' and result['reason'] == 'usage_limit' and result['version'] is None
    assert current_persona(store)['id'] == previous['id']


def test_a_failed_focus_splits_own_timeouts_are_tolerated(tmp_path):
    def decide(reply, payload, number):
        if number == 1:
            reply.finish_reason, reply.content = 'length', '{"sentences":['
        elif number in (2, 3):
            return 'timeout'  # The half, then its own timeout retry.
    metrics = evaluate(tmp_path, decide, text='在那次直播中，我耐心解释了规则。')
    assert metrics['run_valid'] and metrics['invalid_call_count'] == 0
    assert metrics['incomplete_check_count'] == 1
    assert metrics['calls']['tolerated_timed_out_calls'] == 2


def test_a_focus_split_failed_by_a_rate_limit_is_not_tolerated(tmp_path):
    def decide(reply, payload, number):
        if number == 1:
            reply.finish_reason, reply.content = 'length', '{"sentences":['
        elif number == 2:
            return 'rate_limited'
    metrics = evaluate(tmp_path, decide, text='在那次直播中，我耐心解释了规则。')
    assert metrics['incomplete_check_count'] == 1
    assert not metrics['run_valid'] and metrics['invalid_call_count'] == 1


def test_subject_names_never_reach_a_model_request(store):
    from iris.persona import _generation_material, _request_messages, _settings
    stamp = '2026-10-01T12:00:00+00:00'
    with store.write() as conn:
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('p1','person','小舒秘名',?)", (stamp,))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('p1','舒舒秘名')")
    mid = add_self(store, '那次直播里我和对方都先问了要不要继续。')
    with store.write() as conn:
        conn.execute("INSERT INTO memory_subjects VALUES(?,'p1')", (mid,))
    previous = current_persona(store)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), previous, '2026-11-12T12:00:00+00:00')
    assert material['subject_names'] == ['小舒秘名', '舒舒秘名']
    ref = next(m['ref'] for m in material['evidence']['memories'] if m['memory_id'] == mid)
    model = ProductGateway(store, {'sentences':[{'text':'那次直播里，我和小舒秘名都先问了要不要继续。', 'basis':[ref]}]})
    PersonaEngine(store, model).regenerate(expected_version=previous['id'])
    checks = [payload for purpose, payload in model.calls if purpose in ('persona_check', 'persona_recheck')]
    assert checks and all('_subject_names' not in payload for payload in checks)
    # The alias is in no text of this material: only the code's name list holds it.
    assert all('舒舒秘名' not in json.dumps(payload, ensure_ascii=False) for _, payload in model.calls)
    messages = _request_messages('persona_check', {'task':'check_facts_and_rules', 'candidate':[],
                                                   '_subject_names':['只给代码的名字']})
    assert '只给代码的名字' not in messages[1]['content'] and '_subject_names' not in messages[1]['content']


# Round 6: a refusal or item error of the one request is contained; configuration is not.

@pytest.mark.parametrize('category', ['item_error', 'content_rejection'])
@pytest.mark.parametrize('case', ['retry', 'focus_split'])
def test_a_request_refusal_or_item_error_restores_the_truncation(store, category, case):
    if case == 'retry':
        material, sentence = single_focus_case()
        model = Truncating(first_call, content='{"sentences":[', script={2:category})
    else:
        material, sentence = material_and_sentence(2)
        model = Truncating(first_call, script={2:category})
    outputs = {}
    result, _ = run(store, model, material, [sentence], outputs)  # Never a stage failure.
    assert result['validation_errors'] == ['output_limit; no complete judgment']
    assert outputs['persona_check_incomplete'] == 'output_limit'
    assert result['output_limit_diagnostics']['terminal_truncations'] == 1


@pytest.mark.parametrize('case', ['retry', 'focus_split'])
def test_a_configuration_error_is_not_absorbed(store, case):
    if case == 'retry':
        material, sentence = single_focus_case()
        model = Truncating(first_call, content='{"sentences":[', script={2:'configuration'})
    else:
        material, sentence = material_and_sentence(2)
        model = Truncating(first_call, script={2:'configuration'})
    with pytest.raises(ModelError) as caught:
        run(store, model, material, [sentence])
    assert caught.value.category == 'configuration'
