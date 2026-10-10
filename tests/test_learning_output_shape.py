"""Exercise the real learning gateway with local HTTP replies, never a model."""
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, msg
from iris.api import create_app
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.models import Gateway, ModelConfig
from iris.queue import form_batch, get_batch


SECTIONS = ('memories', 'updates', 'people', 'goals', 'questions')
EMPTY = {key: [] for key in SECTIONS}
VALID = json.dumps(EMPTY)
CHAT = '{"answer":"你好，我是Iris，很高兴见到你。"}'


@contextmanager
def learner(store, replies, *, monotonic=None, on_request=None):
    msg(store, 1, '我喜欢雨声')
    formed = form_batch(store, 'A', PROMPT_VERSION)
    remaining = iter(replies)
    requests = []

    def handler(request):
        assert not store._writer.in_transaction
        requests.append(json.loads(request.content))
        if on_request:
            on_request(request, len(requests))
        content, finish = next(remaining)
        return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': finish}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        options = {'monotonic': monotonic} if monotonic else {}
        gateway = Gateway({'chat': ModelConfig('https://test.invalid/v1', '', 'fake')},
                          store, client=client, **options)
        try:
            yield LearningEngine(store, gateway), formed, requests
        finally:
            gateway.close()


def attempt(store, batch_id):
    with store.read() as conn:
        return dict(conn.execute('SELECT * FROM batch_attempts WHERE batch_id=? ORDER BY id DESC', (batch_id,)).fetchone())


def test_chat_output_gets_one_shape_repair_and_keeps_original(store):
    with learner(store, [(CHAT, 'stop'), (VALID, 'stop')]) as (engine, formed, requests):
        result = engine.run_batch(formed.id)
    assert result['parse_status'] == 'repaired'
    assert get_batch(store, formed.id).state == 'succeeded'
    assert len(requests) == 2 and requests[0]['max_tokens'] == requests[1]['max_tokens'] == 16000
    assert requests[1]['messages'][:-2] == requests[0]['messages']
    assert requests[1]['messages'][-2] == {'role': 'assistant', 'content': CHAT}
    assert all(key in requests[1]['messages'][-1]['content'] for key in SECTIONS)
    saved = attempt(store, formed.id)
    assert saved['raw_output'] == CHAT and saved['repair_output'] == VALID
    assert 'missing' in saved['error'] and 'questions' in saved['error']
    with store.read() as conn:
        assert [r[0] for r in conn.execute('SELECT purpose FROM model_calls ORDER BY id')] == ['learning', 'learning_repair']
        assert conn.execute('SELECT learning_state FROM messages').fetchone()[0] == 'learned'


@pytest.mark.parametrize('missing', SECTIONS)
def test_even_one_missing_protocol_array_requires_repair(store, missing):
    partial = {key: [] for key in SECTIONS if key != missing}
    raw = json.dumps(partial)
    with learner(store, [(raw, 'stop'), (VALID, 'stop')]) as (engine, formed, requests):
        result = engine.run_batch(formed.id)
    assert result['parse_status'] == 'repaired' and len(requests) == 2
    assert missing in attempt(store, formed.id)['error']


@pytest.mark.parametrize('section', SECTIONS)
@pytest.mark.parametrize('bad', [None, {}, '', False, 0])
def test_array_type_errors_are_batch_errors_not_empty_sections(store, section, bad):
    raw = json.dumps({**EMPTY, section: bad})
    with learner(store, [(raw, 'stop'), (raw, 'stop')]) as (engine, formed, requests):
        result = engine.run_batch(formed.id)
    assert result['state'] == 'waiting' and len(requests) == 2
    assert section in result['error'] and 'array' in result['error']
    with store.read() as conn:
        assert conn.execute('SELECT learning_state FROM messages').fetchone()[0] == 'batched'
        assert conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0


@pytest.mark.parametrize('raw', ['[]', 'null', '"chat"', '['+VALID+']', '{"wrapper":'+VALID+'}', '{}'])
def test_non_protocol_root_cannot_be_an_empty_success(store, raw):
    with learner(store, [(raw, 'stop'), (VALID, 'stop')]) as (engine, formed, requests):
        result = engine.run_batch(formed.id)
    assert result['parse_status'] == 'repaired' and len(requests) == 2
    assert attempt(store, formed.id)['raw_output'] == raw


@pytest.mark.parametrize('first,finish', [('{broken', 'stop'), ('', 'stop'), (VALID, 'length'), (CHAT, 'stop')])
def test_syntax_and_shape_share_a_single_repair_attempt(store, first, finish):
    with learner(store, [(first, finish), (CHAT, 'stop')]) as (engine, formed, requests):
        result = engine.run_batch(formed.id)
    assert result['state'] == 'waiting' and len(requests) == 2
    saved = attempt(store, formed.id)
    assert saved['parse_status'] == 'failed'
    assert saved['raw_output'] == first and saved['repair_output'] == CHAT
    assert 'missing' in saved['error']


@pytest.mark.parametrize('raw', [VALID, json.dumps({**EMPTY, 'extra': 'ignored'}),
    '<think>discarded</think>\n```json\n'+VALID+'\n```'])
def test_all_five_empty_arrays_remain_success_without_repair(store, raw):
    with learner(store, [(raw, 'stop')]) as (engine, formed, requests):
        result = engine.run_batch(formed.id)
    assert result['parse_status'] == 'direct' and len(requests) == 1
    assert all(result[key] == [] for key in ('created', 'updated', 'confirmed', 'dropped'))
    assert get_batch(store, formed.id).state == 'succeeded'
    assert attempt(store, formed.id)['repair_output'] is None


def test_bad_item_still_uses_existing_item_validation_without_shape_repair(store):
    raw = json.dumps({**EMPTY, 'memories': [None]})
    with learner(store, [(raw, 'stop')]) as (engine, formed, requests):
        result = engine.run_batch(formed.id)
    assert result['parse_status'] == 'direct' and len(requests) == 1
    assert result['dropped'][0]['reason'] == 'not an object'


def test_failed_shape_retries_then_records_gap_and_admin_failure_detail(store):
    with learner(store, [(CHAT, 'stop'), (CHAT, 'stop')]*4) as (engine, formed, requests):
        for number, state in enumerate(('waiting', 'waiting', 'waiting', 'abandoned'), 1):
            result = engine.run_batch(formed.id, force=True)
            assert result['state'] == state
            assert get_batch(store, formed.id).attempt_count == number
        assert len(requests) == 8
    with store.read() as conn:
        assert conn.execute('SELECT learning_state FROM messages').fetchone()[0] == 'abandoned'
        gap = conn.execute('SELECT batch_id,reason FROM memory_gaps').fetchone()
        assert tuple(gap) == (formed.id, 'attempts_exhausted')
        assert conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        login_admin(client)
        detail = client.get(f'/admin/api/batches/{formed.id}').json()
    assert detail['last_error'] == result['error']
    assert len(detail['attempts']) == 4
    for row in detail['attempts']:
        assert row['raw_output'] == row['repair_output'] == CHAT
        assert row['parse_status'] == 'failed' and 'missing' in row['error']


@pytest.mark.parametrize('elapsed,expected_calls', [(179, 2), (181, 1)])
def test_shape_repair_uses_remaining_learning_budget(store, elapsed, expected_calls):
    tick = [0.0]
    timeouts = []
    def on_request(request, number):
        timeouts.append(request.extensions['timeout']['read'])
        if number == 1:
            tick[0] += min(elapsed, 179)
    with learner(store, [(CHAT, 'stop'), (VALID, 'stop')], monotonic=lambda: tick[0],
                 on_request=on_request) as (engine, formed, requests):
        if elapsed > 180:
            original = engine.gateway.json_chat
            def exhaust_after_parse(*args, **kwargs):
                result = original(*args, **kwargs)
                tick[0] = elapsed  # Parsing used the remainder before shape repair.
                return result
            engine.gateway.json_chat = exhaust_after_parse
        result = engine.run_batch(formed.id)
    assert len(requests) == expected_calls
    if expected_calls == 2:
        assert result['parse_status'] == 'repaired' and timeouts[1] <= 1
    else:
        assert result['state'] == 'waiting' and 'timeout' in result['error']
        assert attempt(store, formed.id)['raw_output'] == CHAT


@pytest.mark.parametrize('raw,finish', [('{broken', 'stop'), (VALID, 'length'), ('['+VALID+']', 'stop')])
def test_shape_repair_must_itself_be_complete_and_valid(store, raw, finish):
    with learner(store, [(CHAT, 'stop'), (raw, finish)]) as (engine, formed, requests):
        result = engine.run_batch(formed.id)
    assert result['state'] == 'waiting' and len(requests) == 2
    saved = attempt(store, formed.id)
    assert saved['raw_output'] == CHAT and saved['repair_output'] == raw
    assert saved['parse_status'] == 'failed'


def test_partial_envelope_never_writes_its_valid_items_before_repair(store):
    partial = {'memories': [{'content': '小林喜欢雨声', 'speaker': 'P1', 'about': ['P1'],
                            'type': '偏好', 'stance': '亲历', 'evidence': [1]}]}
    with learner(store, [(json.dumps(partial), 'stop'), (CHAT, 'stop')]) as (engine, formed, requests):
        result = engine.run_batch(formed.id)
    assert result['state'] == 'waiting' and len(requests) == 2
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
        assert conn.execute('SELECT learning_state FROM messages').fetchone()[0] == 'batched'
