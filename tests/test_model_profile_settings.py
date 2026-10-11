"""Capability persistence, inheritance visibility and independent image health."""
from dataclasses import replace

from fastapi.testclient import TestClient

from conftest import authorize_host, login_admin
from iris.api import create_app
from iris.configuration import RuntimeConfig
from iris.model_health import ModelHealth
from iris.models import ModelConfig


def test_old_database_configuration_defaults_vision_to_false(store):
    store.set_setting('models', {'chat': {'base_url': 'https://chat.invalid/v1', 'model': 'chat'}})
    runtime = RuntimeConfig(store)
    assert runtime.load()['chat'].vision is False
    assert runtime.public()['chat']['vision'] is False
    assert not runtime.public()['image_understanding']['enabled']


def test_chat_vision_roundtrip_api_audit_and_status(store):
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        login_admin(client)
        authorize_host(client)
        client.app.state.scheduler.stop()
        route = '/admin/api/settings/models/chat'
        payload = {'base_url': 'https://chat.invalid/v1', 'model': 'chat', 'api_key': '', 'vision': True}
        saved = client.put(route, json=payload)
        assert saved.status_code == 200
        models = saved.json()['models']
        assert models['chat']['vision'] is True
        assert models['image_understanding']['inherited'] is True
        assert models['image_understanding']['model'] == 'chat'
        assert models['image_understanding']['reasoning_effort'] is None
        assert store.setting('models')['chat']['vision'] is True
        assert RuntimeConfig(store).load()['chat'].vision is True
        assert client.app.state.gateway.configs['chat'].vision is True
        for path in ('/admin/api/status', '/api/v1/status'):
            assert client.get(path).json()['model_health']['image_understanding']['inherited'] is True
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='models_saved'").fetchone()[0] == 1
        # The current UI does not know this field yet; an omitted flag keeps it.
        del payload['vision']
        assert client.put(route, json=payload).json()['models']['chat']['vision'] is True
        assert client.put(route, json={**payload, 'vision': 'true'}).status_code == 400
        assert client.put(route, json={**payload, 'vision': False}).json()['models']['chat']['vision'] is False
        assert client.app.state.gateway.configs['chat'].vision is False
        assert not client.app.state.gateway.configs.get('image_understanding')


def test_inherited_image_health_is_independent_and_tracks_capability_reload(store):
    chat = ModelConfig('https://chat.invalid/v1', '', 'chat')
    health = ModelHealth(store, {'chat': chat})
    health.replace_config('chat', replace(chat, vision=True))
    assert health.configs['chat'].vision is True
    token = health.check('image_understanding', 'image_understanding')
    health.observe('image_understanding', token, 'authentication', 'HTTP 401')
    assert health.snapshot()['image_understanding']['state'] == 'invalid_key'
    assert health.snapshot()['chat']['state'] == 'normal'
    health.replace_config('chat', replace(chat, vision=True, model='second'))
    assert health.configs['image_understanding'].model == 'second'
    assert health.snapshot()['image_understanding']['state'] == 'normal'
    explicit = replace(chat, model='independent')
    health.replace_config('image_understanding', explicit)
    assert health.snapshot()['image_understanding']['inherited'] is False
    health.replace_config('chat', chat)
    assert health.configs['chat'].vision is False
    assert health.configs['image_understanding'] == explicit
    health.replace_config('image_understanding', None)
    assert 'image_understanding' not in health.snapshot()


def test_missing_chat_key_is_visible_on_inherited_image(store):
    store.set_setting('models', {'chat': {'base_url': 'https://chat.invalid/v1', 'model': 'chat',
                                        'vision': True, 'api_key_ref': 'missing-reference'}})
    runtime = RuntimeConfig(store)
    assert runtime.public()['image_understanding']['key_missing'] is True
    _, missing = runtime.editable('image_understanding')
    assert missing
    health = ModelHealth(store, runtime.load())
    assert health.configs['image_understanding'].base_url == ''
