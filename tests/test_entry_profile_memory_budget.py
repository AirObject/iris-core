"""Memory-profile request deadlines, virtual clocks only; no real providers."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from iris import entry_profile as ep
from iris.models import Gateway, ModelConfig
from test_entry_profile_memory_live import CLOCK, RuleGateway, context, message
from test_entry_profile_memory_task import run
from fake_openai import completion


@contextmanager
def scripted_gateway(store, steps):
    script = SimpleNamespace(now=1000.0, timeouts=[], deadlines=[], sleeps=[])
    fake = RuleGateway(store)

    def sleep(seconds):
        script.sleeps.append(seconds)
        script.now += seconds

    def respond(request):
        assert not store._writer.in_transaction
        timeout = request.extensions['timeout']['read']
        script.timeouts.append(timeout)
        script.deadlines.append(script.now + timeout)
        elapsed, answer = steps[len(script.timeouts) - 1]
        script.now += elapsed
        if isinstance(answer, int):
            return httpx.Response(answer, json={'error': {'code': 'InternalServerError'}})
        payload = json.loads(request.content)
        if answer in ('generate', 'check', 'missing_violations'):
            stage = 'check' if answer == 'missing_violations' else answer
            reply = fake.chat(payload['messages'][:2], 'consolidation_entry_profile_' + stage)
            content = reply.content
            if answer == 'missing_violations':
                output = json.loads(content)
                for row in output['operations']:
                    row.pop('violations')
                content = json.dumps(output, ensure_ascii=False)
        else:
            content = answer
        return httpx.Response(200, json=completion(content))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway = Gateway(
            {'chat': ModelConfig('https://example.invalid', '', 'fake')}, store, client,
            monotonic=lambda: script.now, sleeper=sleep, jitter=lambda low, high: 0,
        )
        try:
            yield gateway, script
        finally:
            gateway.close()


def execute(store, gateway):
    return ep.EntryProfileEngine(store, gateway, clock=CLOCK).regenerate('group', expected_version=0)


def assert_no_writeback(store, result):
    assert result['status'] == 'failed', result
    assert context(store) is None
    with store.read() as conn:
        state = conn.execute('SELECT cursor,version FROM entry_profile_memory_state').fetchone()
        assert tuple(state) == (0, 0)
        for table in ('memories', 'entry_profile_memory_plans'):
            assert conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0


def test_generate_and_check_each_allow_180_seconds(store):
    message(store)
    with scripted_gateway(store, [(150, 'generate'), (150, 'check')]) as (gateway, script):
        result = execute(store, gateway)
    assert script.timeouts == [180, 180]
    assert result['status'] == 'published', result
    assert context(store)['memory_ids']


def test_http_retries_keep_the_original_request_deadline(store):
    message(store)
    with scripted_gateway(store, [(130, 503), (10, 'generate'), (150, 'check')]) as (gateway, script):
        result = execute(store, gateway)
    assert script.timeouts == [180, 48, 180]
    assert script.deadlines == [1180, 1180, 1322]
    assert script.sleeps == [2]
    assert result['status'] == 'published', result


@pytest.mark.parametrize('steps,remaining', [
    ([(180.001, 'generate')], [180]),
    ([(40, 503), (138.001, 'generate')], [180, 138]),
    ([(1, 'generate'), (180.001, 'check')], [180, 180]),
], ids=['generate', 'retry', 'check'])
def test_real_gateway_expiry_does_not_commit(store, steps, remaining):
    message(store)
    with scripted_gateway(store, steps) as (gateway, script):
        result = execute(store, gateway)
    assert script.timeouts == remaining
    assert result['reason'] == 'timeout'
    assert_no_writeback(store, result)
    with store.read() as conn:
        assert conn.execute('SELECT timed_out FROM model_calls ORDER BY id DESC LIMIT 1').fetchone()[0] == 1


@pytest.mark.parametrize('stage', ['generate', 'check'])
@pytest.mark.parametrize('overrun', [0, 0.001])
def test_late_success_from_custom_gateway_is_rejected_and_retained(store, stage, overrun):
    message(store)

    class LateGateway(RuleGateway):
        now = 1000.0

        def monotonic(self):
            return self.now

        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            self.now = kwargs['_deadline'] + overrun if purpose.endswith('_' + stage) else self.now + 1
            return reply

    result = execute(store, LateGateway(store))
    assert result['reason'] == 'timeout'
    assert_no_writeback(store, result)
    trace = result['result']
    assert stage in trace['outputs']
    assert trace['calls'][-1]['reason'] == 'timeout'
    assert len(trace['calls']) == (1 if stage == 'generate' else 2)
    with store.read() as conn:
        attempt = conn.execute('SELECT state,reason,outputs_json FROM entry_profile_attempts').fetchone()
        assert tuple(attempt)[:2] == ('failed', 'timeout')
        assert stage in json.loads(attempt['outputs_json'])['outputs']


def test_local_json_processing_cannot_extend_the_deadline(store, monkeypatch):
    message(store)
    original = ep.json_object
    with scripted_gateway(store, [(179, 'generate')]) as (gateway, script):
        def parse(content):
            value = original(content)
            script.now += 2
            return value
        monkeypatch.setattr(ep, 'json_object', parse)
        result = execute(store, gateway)
    assert result['reason'] == 'timeout'
    assert script.timeouts == [180]
    assert_no_writeback(store, result)


@pytest.mark.parametrize('answer,reason', [('{', None), ('missing_violations', 'invalid_check_verdict')])
def test_complete_bad_output_gets_only_one_format_repair_within_budget(store, answer, reason):
    message(store)
    steps = [(1, 'generate'), (1, answer), (1, answer)] if reason else [(1, answer), (1, answer)]
    with scripted_gateway(store, steps) as (gateway, script):
        result = execute(store, gateway)
    assert result['reason'] != 'timeout'
    assert result['reason'] == 'invalid_output'
    assert len(script.timeouts) == len(steps)
    assert_no_writeback(store, result)
    assert result['result']['calls'][-1]['result'] == 'invalid_output'


def test_saved_legacy_timeouts_are_preserved_and_memory_limits_are_separate(store):
    saved = {'generate_timeout_seconds': 75, 'check_timeout_seconds': 90}
    store.set_setting('entry_profile', saved)
    message(store)
    with scripted_gateway(store, [(150, 'generate'), (150, 'check')]) as (gateway, script):
        result = execute(store, gateway)
    assert result['status'] == 'published', result
    assert script.timeouts == [180, 180]
    assert store.setting('entry_profile') == saved
    with store.read() as conn:
        assert all(ep._runtime(conn)[key] == value for key, value in saved.items())


def test_explicit_memory_limits_keep_shorter_budget_and_reject_over_180(monkeypatch):
    assert ep.MemoryProfileLimits().timeout_seconds == 180
    assert ep.MemoryProfileLimits(timeout_seconds=180).timeout_seconds == 180
    for value in (0, 181, True, 1.5):
        with pytest.raises(ValueError):
            ep.MemoryProfileLimits(timeout_seconds=value)
    # The injected limit still applies to each call; no user setting is overwritten.
    monkeypatch.setattr(ep, 'time', SimpleNamespace(monotonic=lambda: 1000.0))
    repo, gateway, result = run(limits=ep.MemoryProfileLimits(timeout_seconds=45))
    assert result['status'] == 'committed'
    assert len(gateway.requests) == 2
    assert all(request[2]['_deadline'] == 1045 for request in gateway.requests)
