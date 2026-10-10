"""Independent image-purpose transport, retry, budget, health and settings."""
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris.configuration import RuntimeConfig
from iris.model_health import ModelHealth
from iris.models import Gateway, ModelConfig, ModelError, load_test_models
from test_media import PNG, STAMP
from test_models import response

KIND = 'image_understanding'
CONFIGS = {'chat': ModelConfig('https://example.invalid/v1', '', 'fake-chat'),
           KIND: ModelConfig('https://example.invalid/v1', '', 'fake-vision', reasoning_effort='high')}


def test_multimodal_payload_usage_and_no_image_content_in_logs(store):
    seen = []
    def handler(request):
        assert not store._writer.in_transaction
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response('窗边有猫。', usage={'prompt_tokens': 32, 'completion_tokens': 8}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Gateway(CONFIGS, store, client=client)
        try:
            assert gateway.image_understanding(PNG, 'image/png') == '窗边有猫。'
        finally:
            gateway.close()
    payload = seen[0]
    assert payload['model'] == 'fake-vision' and payload['reasoning_effort'] == 'high'
    assert 'response_format' not in payload
    parts = payload['messages'][-1]['content']
    assert parts[0]['type'] == 'text'
    assert parts[1]['type'] == 'image_url'
    assert parts[1]['image_url']['url'].startswith('data:image/png;base64,')
    with store.read() as conn:
        row = dict(conn.execute('SELECT * FROM model_calls').fetchone())
        assert row['model_kind'] == row['purpose'] == KIND
        assert (row['prompt_tokens'], row['completion_tokens']) == (32, 8)
        assert row['finish_reason'] == 'stop'
        assert 'data:image' not in json.dumps(row) and '窗边有猫' not in json.dumps(row)


@pytest.mark.parametrize('bad', [httpx.Response(503)])
def test_image_failure_retries_twice_at_two_and_eight_seconds(store, bad):
    sequence = [bad, bad, httpx.Response(200, json=response('有效描述'))]
    delays = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: sequence.pop(0))) as client:
        gateway = Gateway(CONFIGS, store, client=client, sleeper=delays.append)
        try:
            assert gateway.image_understanding(PNG, 'image/png') == '有效描述'
        finally:
            gateway.close()
    assert delays == [2, 8]
    with store.read() as conn:
        assert [r[0] for r in conn.execute('SELECT result_category FROM model_calls')] == ['retryable', 'retryable', 'success']


@pytest.mark.parametrize('bad', [httpx.Response(200, json=response('')),
                                httpx.Response(200, json={'choices': []}),
                                httpx.Response(200, json=response('截断', finish_reason='length'))])
def test_image_invalid_output_is_one_failed_item(store, bad):
    seen, delays = [], []
    def handler(request):
        seen.append(request)
        return bad
    health = ModelHealth(store, CONFIGS)
    gateway = Gateway(CONFIGS, store, health=health,
        client=httpx.Client(transport=httpx.MockTransport(handler)), sleeper=delays.append)
    try:
        with pytest.raises(ModelError) as caught:
            gateway.image_understanding(PNG, 'image/png')
        assert caught.value.category == 'invalid_output' and not caught.value.paused
        assert len(seen) == 1 and not delays
        assert health.snapshot()[KIND]['state'] == 'normal'
    finally:
        gateway.close()


@pytest.mark.parametrize('code', ['InputImageSensitiveContentDetected', 'InputImageRiskDetection',
                                  'InputImageSensitiveContentDetected.PrivacyInformation'])
def test_ark_image_refusal_is_not_retried_or_paused(store, code):
    seen = []
    health = ModelHealth(store, CONFIGS)
    def handler(request):
        seen.append(request)
        return httpx.Response(400, json={'error': {'code': code, 'message': 'untrusted detail'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Gateway(CONFIGS, store, health=health, client=client)
        try:
            with pytest.raises(ModelError) as caught:
                gateway.image_understanding(PNG, 'image/png')
            assert caught.value.category == 'content_rejection'
            assert len(seen) == 1 and health.snapshot()[KIND]['state'] == 'normal'
        finally:
            gateway.close()


def test_image_pause_persists_probe_contains_image_and_chat_unaffected(store):
    current = [STAMP]
    health = ModelHealth(store, CONFIGS, clock=lambda: current[0])
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(503) if len(seen) <= 3 else httpx.Response(200, json=response('连接成功'))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Gateway(CONFIGS, store, health=health, client=client, clock=lambda: current[0], sleeper=lambda _: None)
        try:
            with pytest.raises(ModelError):
                gateway.image_understanding(PNG, 'image/png')
            assert health.snapshot()[KIND]['state'] == 'temporarily_unavailable'
            assert health.snapshot()[KIND]['timeout_seconds'] == 120
            assert health.learning_allowed()
            restored = ModelHealth(store, CONFIGS, clock=lambda: current[0])
            assert restored.snapshot()[KIND]['state'] == 'temporarily_unavailable'
            current[0] += timedelta(seconds=60)
            assert gateway.probe(KIND)
            assert any(part['type'] == 'image_url' for part in seen[-1]['messages'][-1]['content'])
            assert health.snapshot()[KIND]['state'] == 'normal'
        finally:
            gateway.close()


def test_image_budget_and_config_independent_and_late_result_discarded(store):
    clock = [0.0]
    health = ModelHealth(store, CONFIGS, clock=lambda: STAMP)
    def handler(request):
        clock[0] = 121.0
        return httpx.Response(200, json=response('迟到的描述'))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Gateway(CONFIGS, store, health=health, client=client, clock=lambda: STAMP, monotonic=lambda: clock[0])
        try:
            assert Gateway.timeout_for(KIND, KIND) == 120
            with pytest.raises(ModelError) as caught:
                gateway.image_understanding(PNG, 'image/png')
            assert caught.value.reason == 'timeout'
            token = health.check(KIND, KIND)
            health.observe(KIND, token, 'authentication', 'HTTP 401')
            gateway.replace_config('chat', replace(CONFIGS['chat'], model='other-chat'))
            assert health.snapshot()[KIND]['state'] == 'invalid_key'
            gateway.replace_config(KIND, replace(CONFIGS[KIND], model='other-image'))
            assert health.snapshot()[KIND]['state'] == 'normal'
            health.set_daily_token_limit(1)
            gateway._record(KIND, 'fake-vision', 1, 'success', None, {'prompt_tokens': 1, 'completion_tokens': 0})
            assert health.snapshot()[KIND]['state'] == 'usage_limit'
            with pytest.raises(ModelError) as caught:
                health.check(KIND, KIND)
            assert caught.value.reason == 'usage_limit'
        finally:
            gateway.close()
    with store.read() as conn:
        assert conn.execute('SELECT timed_out FROM model_calls ORDER BY id LIMIT 1').fetchone()[0] == 1


def test_optional_test_config_and_runtime_public_fields(tmp_path, store):
    path = tmp_path/'dummy-model-config.toml'
    path.write_text('[chat]\nbase_url="http://example.invalid/v1"\nmodel="chat"\n', encoding='utf-8')
    configs = load_test_models(path)
    assert not configs.get(KIND)
    path.write_text(path.read_text()+'\n[image_understanding]\nbase_url="http://example.invalid/v1"\nmodel="vision"\napi_key=""\nreasoning_effort="high"\n', encoding='utf-8')
    assert load_test_models(path)[KIND].model == 'vision'
    runtime = RuntimeConfig(store)
    runtime.save({KIND: CONFIGS[KIND]})
    assert runtime.load()[KIND] == CONFIGS[KIND]
    assert runtime.public()[KIND]['enabled'] and not runtime.public()[KIND]['inherited']
    runtime.save({KIND: None})
    assert not runtime.public()[KIND]['enabled']


def test_authenticated_image_settings_connection_and_disable(store, monkeypatch):
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        login_admin(client)
        route = '/admin/api/settings/models/image_understanding'
        got = client.put(route, json={'base_url': 'https://example.invalid/v1', 'model': 'fake-vision', 'reasoning_effort': 'high'})
        assert got.status_code == 200
        assert got.json()['models'][KIND]['enabled']
        seen = []
        def call(kind, purpose, payload, **kwargs):
            seen.append((kind, payload, kwargs))
            return response('连接成功')
        monkeypatch.setattr(client.app.state.gateway, '_call', call)
        assert client.post(route+'/test', json={}).json()['ok']
        assert seen[0][0] == KIND
        assert any(part['type'] == 'image_url' for part in seen[0][1]['messages'][-1]['content'])
        assert client.put(route, json={'enabled': False}).status_code == 200
        assert not client.get('/admin/api/settings').json()['models'][KIND]['enabled']
        assert KIND not in client.app.state.health.snapshot()
        from iris.service_status import add_health_hints
        assert all(h.get('kind') != KIND for h in add_health_hints({'hints': []}, client.app.state.health)['hints'])


def test_probe_image_meets_ark_dimensions_and_is_complete_png():
    import base64
    import struct
    import zlib

    payload = Gateway.image_probe_payload()
    image = next(part for part in payload['messages'][-1]['content'] if part['type'] == 'image_url')
    prefix, encoded = image['image_url']['url'].split(',', 1)
    assert prefix == 'data:image/png;base64'
    data = base64.b64decode(encoded, validate=True)
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    width, height = struct.unpack('>II', data[16:24])
    # Ark rejects either dimension <= 14 rather than upscaling such inputs.
    assert width > 14 and height > 14
    assert 196 <= width * height <= 36_000_000 and 1/150 <= width/height <= 150
    chunks, offset = [], 8
    while offset < len(data):
        size = struct.unpack('>I', data[offset:offset+4])[0]
        kind = data[offset+4:offset+8]
        body = data[offset+8:offset+8+size]
        crc = struct.unpack('>I', data[offset+8+size:offset+12+size])[0]
        assert crc == zlib.crc32(kind+body) & 0xffffffff
        chunks.append((kind, body))
        offset += 12+size
    assert offset == len(data) and chunks[0][0] == b'IHDR' and chunks[-1] == (b'IEND', b'')
    assert chunks[0][1][8:] == bytes([8, 2, 0, 0, 0])  # RGB, no interlace
    pixels = zlib.decompress(b''.join(body for kind, body in chunks if kind == b'IDAT'))
    assert pixels == (b'\x00'+b'\xff'*width*3)*height  # complete, fixed white image
