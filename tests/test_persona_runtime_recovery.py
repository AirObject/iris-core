"""PE-12: only a completed operation can recover its own transport failures."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelError
from iris.persona_evaluation import run_persona_eval, _metrics
from test_persona_evaluation import FakeGateway, TIMELINE, tmp_path


def gateway_for(failure, *, recovered=True, complete=True, outer_retry=False):
    class Recorded(FakeGateway):
        def __init__(self, configs, store, **kwargs):
            super().__init__(configs, store, **kwargs)
            self.clock, self.now, self.failed = kwargs['clock'], 0., False

        def monotonic(self):
            return self.now

        def record(self, purpose, category, *, timeout=False, status=200, finish='stop'):
            with self.store.write() as conn:
                conn.execute("""INSERT INTO model_calls
                    (purpose,model,duration_ms,result_category,status_code,timed_out,finish_reason,created_at)
                    VALUES(?, 'fake', 1000, ?, ?, ?, ?, ?)""",
                    (purpose, category, status, int(timeout), finish, self.clock().isoformat()))

        def chat(self, messages, purpose, **kwargs):
            assert not self.store._writer.in_transaction
            if purpose == 'persona_check' and not self.failed:
                self.failed = True
                self.record(purpose, 'retryable' if failure != 'output_limit' else 'success',
                    timeout=failure == 'timeout', status=429 if failure == 'rate_limited' else None,
                    finish='length' if failure == 'output_limit' else None)
                if outer_retry or not recovered:
                    self.now = kwargs['_deadline']
                    raise ModelError('retryable', failure, reason=failure, paused=not recovered)
            reply = super().chat(messages, purpose, **kwargs)
            self.record(purpose, 'success')
            if not complete and purpose == 'persona_check':
                reply.finish_reason = 'length'
            return reply
    return Recorded


def evaluate(tmp_path, factory, *, identity='runtime'):
    corpus = tmp_path/'fixture.json'
    case = copy.deepcopy(TIMELINE)
    # Same timestamp for both checkpoints: recovery must use owned call IDs,
    # never a timestamp or a later unrelated successful call.
    case['events'] = case['events'][:2] + [
        {**case['events'][1], 'id':'c2', 'action':'regenerate'}]
    corpus.write_text(dumps(case), encoding='utf-8')
    return run_persona_eval({}, tmp_path, corpus=corpus, out=tmp_path/'out',
        judge_mode='external', gateway_factory=factory, runner_identity=identity)


@pytest.mark.parametrize('failure', ['timeout', 'rate_limited', 'transport_failure'])
def test_recovered_calls_continue_timeline_and_are_reusable(tmp_path, failure):
    factory = gateway_for(failure)
    for resumed in (0, 1):
        _, report = evaluate(tmp_path, factory)
        metrics, row = report['metrics'], report['rows'][0]
        assert metrics['run_valid'] and row['complete']
        assert len(row['checkpoints']) == 2 and report['resumed_cases'] == resumed
        assert metrics['invalid_call_count'] == 0 and metrics['recovered_call_count'] == 1
        assert len(row['recovered_calls']) == 1
        assert row['recovered_calls'][0]['reason'] == failure
        assert row['recovered_calls'][0]['checkpoint_id'] == 'c1'
        assert metrics['calls']['recovered_timed_out_calls'] == int(failure == 'timeout')
        assert metrics['calls']['unrecovered_timed_out_calls'] == 0
        assert metrics['calls']['recovered_rate_limited_calls'] == int(failure == 'rate_limited')
        assert metrics['calls']['unrecovered_rate_limited_calls'] == 0
        first, second = (c['call_ids'] for c in row['checkpoints'])
        assert first and second and not set(first).intersection(second)
        assert report['run_validity_policy'] == 'completed_operation_recovery_v1'
        assert report['check_stream_idle_seconds'] == 90


def test_product_stage_retry_recovers_a_call_under_the_same_budget(tmp_path):
    _, report = evaluate(tmp_path, gateway_for('timeout', outer_retry=True))
    assert report['metrics']['run_valid']
    assert report['metrics']['calls']['recovered_timed_out_calls'] == 1
    outputs = json.loads(report['rows'][0]['attempts'][0]['outputs_json'])
    retry = outputs['persona_check_batches'][0]['persona_check_timeout_retries'][0]
    assert retry['status'] == 'complete' and retry['remaining_seconds'] == 540


@pytest.mark.parametrize('failure', ['timeout', 'rate_limited', 'transport_failure'])
def test_failed_retry_stops_without_counting_the_call_as_recovered(tmp_path, failure):
    _, report = evaluate(tmp_path, gateway_for(failure, recovered=False))
    metrics = report['metrics']
    assert not metrics['run_valid'] and metrics['recovered_call_count'] == 0
    assert metrics['invalid_call_count'] == 1
    assert metrics['calls']['unrecovered_timed_out_calls'] == int(failure == 'timeout')
    assert metrics['calls']['unrecovered_rate_limited_calls'] == int(failure == 'rate_limited')
    assert len(report['rows'][0]['checkpoints']) == 1 and report['rows'][0]['remaining_events'] == 1


@pytest.mark.parametrize('failure', ['timeout', 'rate_limited'])
def test_a_successful_response_with_incomplete_checks_is_not_recovery(tmp_path, failure):
    _, report = evaluate(tmp_path, gateway_for(failure, complete=False))
    metrics = report['metrics']
    assert not metrics['run_valid'] and metrics['incomplete_check_count'] == 1
    assert metrics['recovered_call_count'] == 0 and metrics['invalid_call_count'] == 1


def test_output_truncation_is_never_recovered_by_a_later_success(tmp_path):
    _, report = evaluate(tmp_path, gateway_for('output_limit'))
    assert not report['metrics']['run_valid']
    assert report['metrics']['calls']['output_limited_calls'] == 1
    assert report['metrics']['recovered_call_count'] == 0
    assert report['rows'][0]['invalid_calls'][0]['reason'] == 'output_limit'


@pytest.mark.parametrize('damage', ['no_ownership', 'other_checkpoint', 'duplicate_ownership', 'failed_outcome', 'no_success'])
def test_offline_scoring_does_not_infer_recovery_from_other_checkpoints(tmp_path, damage):
    _, report = evaluate(tmp_path, gateway_for('timeout'))
    rows = copy.deepcopy(report['rows'])
    row = rows[0]
    first, second = row['checkpoints']
    failed_id = next(c['id'] for c in row['calls'] if c['timed_out'])
    if damage == 'no_ownership':
        first.pop('call_ids')
    elif damage == 'other_checkpoint':
        first['call_ids'] = [failed_id]
    elif damage == 'duplicate_ownership':
        second['call_ids'].append(failed_id)
    elif damage == 'failed_outcome':
        first['result'].update(status='failed', version=None)
    else:
        for call in row['calls']:
            if call['id'] in first['call_ids'] and call['id'] != failed_id:
                call['result_category'] = 'invalid_output'
    # The complete flag and saved recovered_calls are not authoritative.
    assert not _metrics(rows)['run_valid']
    assert _metrics(rows)['calls']['unrecovered_timed_out_calls'] == 1


def test_stall_diagnostic_does_not_double_count_root_first_batch_alias(tmp_path):
    _, report = evaluate(tmp_path, gateway_for('timeout'))
    rows = copy.deepcopy(report['rows'])
    attempt = rows[0]['attempts'][0]
    output = json.loads(attempt['outputs_json'])
    audit = {'stall_aborted': True, 'complete': False}
    output['persona_check_batches'][0]['persona_check_transport'] = [audit]
    output['persona_check_transport'] = [audit]
    attempt['outputs_json'] = dumps(output)
    assert _metrics(rows)['stream_stall_aborts'] == 1
