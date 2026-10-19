"""Persona-only streaming must preserve budgets, complete envelopes and privacy."""
from contextlib import contextmanager
import json

import httpx
import pytest

from iris.model_health import ModelHealth
from iris.models import Gateway, ModelConfig, ModelError
from iris.persona import PersonaEngine
from test_persona_optimization import prepared
from test_persona_evaluation import material, tmp_path


def event(delta=None, finish=None, **extra):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta or {},
        'finish_reason': finish}], **extra}, ensure_ascii=False) + '\n\n').encode()


def envelope(content='{}', finish='stop'):
    return [event({'role': 'assistant'}), event({'reasoning_content': 'PRIVATE_REASONING'}),
            event({'content': content}), event(finish=finish),
            b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":20,'
            b'"completion_tokens_details":{"reasoning_tokens":15}}}\n\n',
            b'data: [DONE]\n\n']


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, now, delays=None):
        self.chunks, self.now = chunks, now
        self.delays = delays or [0] * len(chunks)
        self.closed = False

    def __iter__(self):
        for chunk, delay in zip(self.chunks, self.delays):
            self.now[0] += delay
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


@contextmanager
def engine_for(store, responder, *, model='glm-5.3-flash', health=False):
    now, requests = [0.0], []
    def respond(request):
        assert not store._writer.in_transaction
        requests.append(request)
        return responder(request, now, len(requests))
    configs = {'chat': ModelConfig('https://model.invalid', '', model, reasoning_effort='high')}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway = Gateway(configs, store, client=client,
            health=ModelHealth(store, configs) if health else None,
            monotonic=lambda: now[0], sleeper=lambda delay: now.__setitem__(0, now[0] + delay),
            jitter=lambda *args: 0)
        try:
            yield PersonaEngine(store, gateway), now, requests, client
            assert gateway.client is client and not client.is_closed
        finally:
            gateway.close()


def response(chunks, now, delays=None):
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                          stream=Chunks(chunks, now, delays))


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
@pytest.mark.parametrize('health', [False, True])
def test_stream_assembles_split_utf8_and_records_only_counts(prepared, purpose, health):
    def respond(request, now, number):
        body = json.loads(request.content)
        assert body['stream'] is True and body['stream_options'] == {'include_usage': True}
        assert body['reasoning_effort'] == 'high' and body['max_tokens'] == 16000
        assert request.extensions['timeout']['read'] == 90
        raw = b''.join(envelope('{"reason":"中文"}'))
        return response([raw[i:i+7] for i in range(0, len(raw), 7)], now)
    with engine_for(prepared, respond, health=health) as (engine, now, requests, client):
        outputs = {}
        assert engine._call(purpose, {}, outputs) == {'reason': '中文'}
    audit = outputs[purpose + '_transport'][0]
    assert audit['mode'] == 'sse' and audit['complete'] and audit['received_done']
    assert audit['reasoning_chars'] == len('PRIVATE_REASONING')
    assert audit['content_chars'] == len('{"reason":"中文"}')
    assert audit['first_reasoning_seconds'] == 0 and audit['first_content_seconds'] == 0
    assert 'PRIVATE_REASONING' not in json.dumps(outputs)
    with prepared.read() as conn:
        call = dict(conn.execute('SELECT * FROM model_calls').fetchone())
    assert call['completion_tokens'] == 20 and call['reasoning_tokens'] == 15
    assert call['reasoning_present'] == 1 and call['reasoning_chars'] == len('PRIVATE_REASONING')
    assert call['finish_reason'] == 'stop' and call['result_category'] == 'success'


@pytest.mark.parametrize('chunks', [
    envelope()[:-1],                         # no DONE, even though JSON and finish are present
    [event({'content': '{}'}), b'data: [DONE]\n\n'],  # no finish
    [b'data: not-json\n\n'],
    [b'data: {"error":{"message":"PRIVATE_PROVIDER_ERROR"}}\n\n'],
    [event({'content': '{}'}), event(finish='tool_calls'), b'data: [DONE]\n\n'],
    [event({'content': '{}'}), event(finish='stop'), event({'content': 'extra'}), b'data: [DONE]\n\n'],
    [b'data: {"choices":[{"index":1,"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n', b'data: [DONE]\n\n'],
])
def test_incomplete_or_malformed_stream_cannot_accept_parseable_prefix(prepared, chunks):
    with engine_for(prepared, lambda req, now, n: response(chunks, now)) as (engine, now, requests, client):
        outputs = {}
        with pytest.raises(ModelError) as failure:
            engine._call('persona_check', {}, outputs)
        assert failure.value.category == 'invalid_output'
        assert len(requests) == 1
    assert 'persona_check' not in outputs
    audit = outputs['persona_check_transport'][0]
    assert not audit['complete'] and audit['error_type'] in ('ValueError', 'JSONDecodeError')
    assert 'PRIVATE_PROVIDER_ERROR' not in json.dumps(outputs)


def test_length_keeps_usage_and_raw_content_but_never_repairs_json(prepared):
    with engine_for(prepared, lambda req, now, n: response(envelope('{}', 'length'), now)) as (engine, now, requests, client):
        outputs = {}
        with pytest.raises(ModelError) as failure:
            engine._call('persona_check', {}, outputs)
        assert failure.value.reason == 'output_limit' and len(requests) == 1
    assert outputs['persona_check'] == '{}' and outputs['persona_check_finish_reason'] == 'length'
    assert outputs['persona_check_transport'][0]['complete']


def test_stream_progress_does_not_extend_total_deadline_or_accept_late_json(prepared):
    chunks = [event({'reasoning_content':'slow'})] * 3 + envelope()
    with engine_for(prepared, lambda req, now, n: response(chunks, now, [70,70,70,0,70,70,11,0,0])) as (engine, now, requests, client):
        outputs = {}
        with pytest.raises(ModelError) as failure:
            engine._call('persona_check', {}, outputs)
        assert failure.value.reason == 'timeout' and len(requests) == 1
    audit = outputs['persona_check_transport'][0]
    assert not audit['complete'] and audit['first_reasoning_seconds'] == 70
    assert audit['first_content_seconds'] == 350 and audit['max_data_gap_seconds'] == 70
    assert not audit['stall_aborted']
    assert audit['error_type'] == 'ReadTimeout' and 'persona_check' not in outputs
    with prepared.read() as conn:
        call = conn.execute('SELECT * FROM model_calls').fetchone()
    assert call['timed_out'] == 1 and call['completion_tokens'] is None


def test_json_repair_keeps_same_stream_deadline_and_audits_separate_requests(prepared):
    def respond(req, now, n):
        if n == 1:
            return response([b': heartbeat\n\n'] * 4 + envelope('broken'),
                            now, [60,60,60,60,0,10,0,0,0,0])
        return response(envelope(), now, [0,60,0,0,0,0])
    with engine_for(prepared, respond) as (engine, now, requests, client):
        outputs = {}
        assert engine._call('persona_check', {}, outputs) == {}
        assert [r.extensions['timeout']['read'] for r in requests] == [90, 90]
        assert [r.extensions['timeout']['write'] for r in requests] == [360, 110]
    assert len(outputs['persona_check_transport']) == len(outputs['persona_check_repair_transport']) == 1
    assert outputs['persona_check'] == 'broken' and outputs['persona_check_repair'] == '{}'


def test_http_retry_after_and_transport_failures_keep_gateway_accounting(prepared):
    def respond(req, now, n):
        if n == 1:
            return httpx.Response(429, headers={'retry-after': '7'}, json={'error': {'message': 'PRIVATE_PROVIDER_ERROR'}})
        return response(envelope(), now)
    with engine_for(prepared, respond) as (engine, now, requests, client):
        outputs = {}
        assert engine._call('persona_check', {}, outputs) == {}
        assert now[0] == 7
        assert [r.extensions['timeout']['read'] for r in requests] == [90, 90]
        assert [r.extensions['timeout']['write'] for r in requests] == [360, 353]
    assert [a['status_code'] for a in outputs['persona_check_transport']] == [429, 200]
    assert 'PRIVATE_PROVIDER_ERROR' not in json.dumps(outputs)
    with prepared.read() as conn:
        calls = [dict(r) for r in conn.execute('SELECT * FROM model_calls ORDER BY id')]
    assert [r['status_code'] for r in calls] == [429, 200]
    from iris.persona_evaluation import _invalid_model_calls
    assert _invalid_model_calls(calls)


def test_refusal_and_missing_usage_are_not_invented(prepared):
    chunks = [event({'refusal': 'blocked'}), event(finish='content_filter'), b'data: [DONE]\n\n']
    with engine_for(prepared, lambda req, now, n: response(chunks, now)) as (engine, now, requests, client):
        with pytest.raises(ModelError) as failure:
            engine._call('persona_check', {}, {})
        assert failure.value.category == 'content_rejection'
    with prepared.read() as conn:
        call = conn.execute('SELECT * FROM model_calls').fetchone()
    assert call['completion_tokens'] is None and call['finish_reason'] == 'content_filter'


@pytest.mark.parametrize('purpose,model', [('persona_generate','glm-5.3-flash'),
    ('persona_recheck','another-model'), ('persona_check','another-model')])
def test_other_purposes_and_models_keep_existing_nonstream_transport(prepared, purpose, model):
    def respond(req, now, n):
        assert 'stream' not in json.loads(req.content)
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]})
    with engine_for(prepared, respond, model=model) as (engine, now, requests, client):
        outputs = {}
        assert engine._call(purpose, {}, outputs) == {}
    if purpose in ('persona_check', 'persona_recheck'):
        assert outputs.pop(purpose + '_request_bytes')[0] <= 32768
    assert outputs == {purpose: '{}', purpose + '_finish_reason': 'stop'}


def test_missing_usage_remains_unknown_after_complete_stream(prepared):
    chunks = [event({'content': '{}'}), event(finish='stop'), b'data: [DONE]\n\n']
    with engine_for(prepared, lambda req, now, n: response(chunks, now)) as (engine, now, requests, client):
        assert engine._call('persona_check', {}, {}) == {}
    with prepared.read() as conn:
        call = conn.execute('SELECT * FROM model_calls').fetchone()
    assert call['completion_tokens'] is None and call['prompt_tokens'] is None
    assert call['reasoning_present'] == 0 and call['reasoning_chars'] == 0


def test_partial_transport_failure_is_not_mixed_into_retry(prepared):
    def respond(req, now, n):
        if n == 1:
            return response([event({'content': '{"wrong":'}), httpx.ReadError('PRIVATE_TRANSPORT_ERROR')], now)
        return response(envelope('{"right":true}'), now)
    with engine_for(prepared, respond) as (engine, now, requests, client):
        outputs = {}
        assert engine._call('persona_check', {}, outputs) == {'right': True}
        assert len(requests) == 2
    audits = outputs['persona_check_transport']
    assert not audits[0]['complete'] and audits[0]['content_chars'] == len('{"wrong":')
    assert audits[1]['complete'] and outputs['persona_check'] == '{"right":true}'
    assert 'PRIVATE_TRANSPORT_ERROR' not in json.dumps(outputs)
    with prepared.read() as conn:
        calls = [dict(r) for r in conn.execute('SELECT * FROM model_calls ORDER BY id')]
    from iris.persona_evaluation import _invalid_model_calls
    assert _invalid_model_calls(calls)[0]['reason'] == 'transport_failure'


def test_future_deadline_closes_stream_worker_and_preserves_shared_client(prepared):
    import threading
    import time
    class Blocking(httpx.SyncByteStream):
        def __init__(self):
            self.closed = threading.Event()
        def __iter__(self):
            yield event({'reasoning_content': 'PRIVATE_REASONING'})
            assert self.closed.wait(2), 'deadline must close the active stream'
            yield event({'content': '{}'})
        def close(self):
            self.closed.set()
    stream = Blocking()
    def respond(req):
        return httpx.Response(200, stream=stream)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway = Gateway({'chat': ModelConfig('https://model.invalid', '', 'glm-5.3-flash')},
                          prepared, client=client)
        try:
            outputs = {}
            started = time.monotonic()
            with pytest.raises(ModelError) as failure:
                PersonaEngine(prepared, gateway)._call('persona_check', {}, outputs, deadline=started + .1)
            assert failure.value.reason == 'timeout'
            assert stream.closed.is_set() and not client.is_closed and gateway.client is client
            assert time.monotonic() - started < 1.5
            audit = outputs['persona_check_transport'][0]
            assert not audit['complete'] and audit['cancelled']
            assert audit['reasoning_chars'] == len('PRIVATE_REASONING')
            assert 'persona_check' not in outputs
        finally:
            gateway.close()


def test_deadline_expired_before_worker_starts_sends_nothing():
    from iris.persona import _PersonaCheckStreamClient
    def unexpected(req):
        raise AssertionError('cancelled work must not start a request')
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        adapter = _PersonaCheckStreamClient(client, lambda: 0, 360)
        adapter.cancel()
        with pytest.raises(httpx.ReadTimeout):
            adapter.post('https://model.invalid', headers={}, json={}, timeout=httpx.Timeout(360))
        assert not client.is_closed
    assert adapter.snapshot()[0]['cancelled']


def test_report_declares_transport_without_changing_other_model_metadata(material):
    from iris.persona import check_transport
    assert check_transport(ModelConfig('https://model.invalid', '', 'glm-5.3-flash')) == 'sse'
    assert check_transport(ModelConfig('https://model.invalid', '', 'another-model')) == 'json'
    _, report, _, _ = material
    assert report['check_transport'] == {'persona_check': 'json', 'persona_recheck': 'json'}


@pytest.mark.parametrize('stop', [False, True])
def test_maintenance_wrapper_retains_stop_hook_and_counts_each_http_attempt(prepared, stop):
    from datetime import datetime, timezone
    from iris.consolidation import _PersonaGateway
    def respond(req, now, n):
        assert json.loads(req.content)['stream'] is True
        if n == 1:
            return httpx.Response(503, json={'error': {'message': 'temporary'}})
        return response(envelope(), now)
    with engine_for(prepared, respond) as (engine, now, requests, client):
        class Owner:
            store = prepared
            gateway = engine.gateway
            clock = staticmethod(lambda: datetime.now(timezone.utc))
            def __init__(self):
                self.reservations, self.finishes = [], []
            def reserve(self, work, purpose):
                self.reservations.append((work, purpose))
                return len(self.reservations)
            def finish_call(self, *args):
                self.finishes.append(args)
        owner = Owner()
        facade = _PersonaGateway(owner, stop=lambda: stop)
        inner, health = facade.gateway, facade.gateway.health
        outputs = {}
        call = lambda: PersonaEngine(prepared, facade)._call('persona_check', {}, outputs)
        if stop:
            with pytest.raises(ModelError) as failure:
                call()
            assert failure.value.reason == 'interrupted'
            assert not requests and not owner.reservations
        else:
            assert call() == {}
            assert [p for _, p in owner.reservations] == ['persona_check', 'persona_check']
            assert [r[1] for r in owner.finishes] == ['retryable', 'success']
            assert len(outputs['persona_check_transport']) == 2
        assert facade.gateway is inner and inner.client is client and inner.health is health
        assert not inner._pool._shutdown and not client.is_closed


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
def test_idle_gap_aborts_and_gateway_retry_recovers_without_partial_output(prepared, purpose):
    def respond(req, now, n):
        assert req.extensions['timeout']['read'] == 90
        if n == 1:
            return response([event({'reasoning_content':'private'}), event({'content':'WRONG'})],
                            now, [1, 91])
        return response(envelope('{"ok":true}'), now)
    with engine_for(prepared, respond) as (engine, now, requests, client):
        outputs = {}
        assert engine._call(purpose, {}, outputs) == {'ok': True}
        assert len(requests) == 2 and now[0] < 360
    bad, good = outputs[purpose + '_transport']
    assert bad['stall_aborted'] and not bad['complete'] and bad['content_chars'] == 0
    assert bad['abort_reason'] == 'stream_stalled' and bad['max_receive_gap_seconds'] == 91
    assert good['complete'] and not good['stall_aborted']


@pytest.mark.parametrize('heartbeat', [b': heartbeat\n\n', b'data: {"choices":[]}' + b'\n\n'])
def test_slow_stream_heartbeats_keep_it_alive_even_without_content(prepared, heartbeat):
    chunks = [event({'reasoning_content':'thinking'}), heartbeat, heartbeat, heartbeat] + envelope()
    delays = [0, 70, 70, 70] + [0] * len(envelope())
    with engine_for(prepared, lambda req, now, n: response(chunks, now, delays)) as (engine, now, requests, client):
        outputs = {}
        assert engine._call('persona_check', {}, outputs) == {}
        assert now[0] == 210 and len(requests) == 1
    audit = outputs['persona_check_transport'][0]
    assert audit['complete'] and not audit['stall_aborted']
    assert audit['max_receive_gap_seconds'] == 70
    assert audit['first_content_seconds'] == 210


def test_partial_utf8_and_sse_lines_count_as_received_data(prepared):
    raw = event({'reasoning_content':'中文推理'}) + b''.join(envelope())
    chunks = [raw[:5], raw[5:10], raw[10:15], raw[15:20], raw[20:]]
    with engine_for(prepared, lambda req, now, n: response(chunks, now, [0,70,70,70,0])) as (engine, now, requests, client):
        outputs = {}
        assert engine._call('persona_check', {}, outputs) == {}
    audit = outputs['persona_check_transport'][0]
    assert audit['first_data_seconds'] == 210 and not audit['stall_aborted']
    assert audit['receive_chunks'] == 5 and audit['received_bytes'] == len(raw)


def test_no_body_progress_uses_the_socket_idle_timeout_not_the_total_budget(prepared):
    def respond(req, now, n):
        if n == 1:
            idle = req.extensions['timeout']['read']
            assert idle == 90
            return response([httpx.ReadTimeout('PRIVATE_SOCKET_ERROR')], now, [idle])
        return response(envelope(), now)
    with engine_for(prepared, respond) as (engine, now, requests, client):
        outputs = {}
        assert engine._call('persona_check', {}, outputs) == {}
        assert now[0] == 92 and len(requests) == 2
    audit = outputs['persona_check_transport'][0]
    assert audit['stall_aborted'] and audit['received_bytes'] == 0
    assert audit['elapsed_seconds'] == 90
    assert 'PRIVATE_SOCKET_ERROR' not in json.dumps(outputs)


def test_idle_detection_never_extends_a_shorter_stage_deadline(prepared):
    def respond(req, now, n):
        assert req.extensions['timeout']['read'] == 30
        return response([httpx.ReadTimeout('socket timeout')], now, [30])
    with engine_for(prepared, respond) as (engine, now, requests, client):
        outputs = {}
        with pytest.raises(ModelError) as failure:
            engine._call('persona_check', {}, outputs, deadline=30)
        assert failure.value.reason == 'timeout' and len(requests) == 1
    assert not outputs['persona_check_transport'][0]['stall_aborted']


def test_socket_stall_is_interrupted_before_total_deadline(prepared, monkeypatch):
    """A local fake SSE server stops yielding; HTTPX must abort the blocked read."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from iris import persona
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            chunk = event({'reasoning_content':'private'})
            self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
            self.wfile.flush()
            release.wait(3)
            self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
    thread.start()
    monkeypatch.setattr(persona, 'CHECK_STREAM_IDLE_SECONDS', .2)
    gateway = Gateway({'chat':ModelConfig(f'http://127.0.0.1:{server.server_port}', '', 'glm-5.3-flash')},
                      prepared, jitter=lambda *args:0)
    try:
        outputs = {}
        started = time.monotonic()
        with pytest.raises(ModelError) as failure:
            PersonaEngine(prepared, gateway)._call('persona_check', {}, outputs, deadline=started + 2)
        assert failure.value.reason == 'timeout'
        assert time.monotonic() - started < 1.5
        audit = outputs['persona_check_transport'][0]
        assert audit['stall_aborted'] and not audit['complete']
        assert audit['reasoning_chars'] == len('private')
        assert audit['receive_chunks'] == 1
        assert audit['max_receive_gap_seconds'] >= .19
    finally:
        release.set()
        gateway.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
