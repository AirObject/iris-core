"""Setup, sessions and hot configuration use only local fake model servers."""
import json
import stat

import pytest
from fastapi.testclient import TestClient

from iris.api import create_app
from iris.auth import hash_password, verify_password
from iris.configuration import RuntimeConfig, deployment
from iris.models import ModelConfig
from fake_openai import FakeOpenAI
from test_retrieval import put

PASSWORD = 'local-test-password-测试'


def csrf(client):
    data = client.get('/admin/api/session').json()
    client.headers['X-Iris-CSRF'] = data['csrf_token']
    return data


def start_setup(client):
    csrf(client)
    response = client.post('/admin/api/setup/password', json={'password': PASSWORD})
    assert response.status_code == 200, response.text
    csrf(client)


def finish_setup(client):
    response = client.post('/admin/api/setup/complete', json={'name': 'Iris', 'background': '喜欢观星。', 'timezone': 'Asia/Shanghai'})
    assert response.status_code == 200, response.text


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as c:
        yield c


def test_scrypt_is_salted_and_never_plaintext():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and PASSWORD not in first and first.startswith('scrypt$')
    assert verify_password(PASSWORD, first)
    assert not verify_password('wrong', first)
    assert not verify_password(PASSWORD, 'malformed')


def test_setup_redirect_session_cookie_and_host_api_unchanged(client, store):
    assert client.get('/', follow_redirects=False).headers['location'] == '/setup'
    response = client.get('/admin/api/memories')
    assert response.status_code == 409 and response.json()['error']['redirect'] == '/setup'
    assert client.get('/api/v1/status').status_code == 200
    start_setup(client)
    cookie = client.cookies.get('iris_session')
    assert cookie
    finish_setup(client)
    assert client.get('/').status_code == 200
    assert client.get('/admin/api/memories').status_code == 200
    assert client.app.state.health.snapshot()['chat']['state'] == 'configuration_error'
    assert client.get('/admin/api/session').json()['configured']
    with store.read() as conn:
        assert conn.execute("SELECT stance FROM memories WHERE content='喜欢观星'").fetchone()[0] == '设定'
        assert cookie not in '\n'.join(conn.iterdump())
    assert client.post('/admin/api/logout', json={}).status_code == 200
    assert client.get('/').url.path == '/login'
    assert client.get('/admin/api/memories').status_code == 401
    csrf(client)
    response = client.post('/admin/api/login', json={'password': PASSWORD})
    assert response.status_code == 200
    header = response.headers['set-cookie']
    assert 'HttpOnly' in header and 'SameSite=strict' in header and 'Path=/' in header
    assert client.cookies.get('iris_session') != cookie


@pytest.mark.parametrize('headers', [{}, {'X-Iris-CSRF': 'wrong'}, {'Origin': 'http://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}])
def test_csrf_protects_setup_login_and_mutations(client, headers):
    state = csrf(client)
    client.headers.pop('X-Iris-CSRF')
    attack = {'X-Iris-CSRF': state['csrf_token'], **headers} if ('Origin' in headers or 'Sec-Fetch-Site' in headers) else headers
    assert client.post('/admin/api/setup/password', json={'password': PASSWORD}, headers=attack).status_code == 403
    start_setup(client)
    finish_setup(client)
    client.headers.pop('X-Iris-CSRF')
    assert client.post('/admin/api/trial/entries', json={'name': 'forged'}, headers=headers).status_code == 403


def test_remote_initial_setup_is_denied_even_with_loopback_host(store):
    with TestClient(create_app(store=store), base_url='http://localhost', client=('192.0.2.10', 1000)) as c:
        assert c.get('/setup').status_code == 403
        assert c.get('/admin/api/session').status_code == 403
        assert c.post('/admin/api/setup/password', json={'password': PASSWORD}).status_code == 403


def test_login_rate_limit_and_expired_session(client, store):
    start_setup(client)
    finish_setup(client)
    client.post('/admin/api/logout', json={})
    csrf(client)
    for _ in range(5):
        assert client.post('/admin/api/login', json={'password': 'wrong-password'}).status_code == 401
    assert client.post('/admin/api/login', json={'password': PASSWORD}).status_code == 429
    with store.write() as conn:
        conn.execute('DELETE FROM admin_login_limits')
    assert client.post('/admin/api/login', json={'password': PASSWORD}).status_code == 200
    with store.write() as conn:
        conn.execute('UPDATE admin_sessions SET expires_at=0')
    assert client.get('/admin/api/settings').status_code == 401


def test_settings_presets_default_only_ark_glm_to_high(client):
    start_setup(client)
    snapshot = client.get('/admin/api/settings').json()
    assert {preset['id']: preset['reasoning_effort'] for preset in snapshot['presets']} == {
        'ark-glm': 'high', 'deepseek': None, 'local': None,
    }
    assert not snapshot['models']['chat']['enabled']
    assert snapshot['models']['chat']['reasoning_effort'] is None


def test_settings_hot_replace_secret_permissions_and_audit(client, store):
    start_setup(client)
    finish_setup(client)
    key = 'fake-credential-never-echo-this'
    response = client.put('/admin/api/settings/models/chat', json={
        'enabled': True, 'base_url': 'https://models.example/v1', 'model': 'glm-test', 'api_key': key, 'reasoning_effort': 'low'})
    assert response.status_code == 200 and key not in response.text
    assert client.app.state.gateway.configs['chat'].api_key == key
    secret = store.path.parent / 'secrets.json'
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert key in secret.read_text()
    snapshot = client.get('/admin/api/settings').json()
    assert snapshot['models']['chat']['key_set'] and 'api_key' not in snapshot['models']['chat']
    assert snapshot['models']['chat']['reasoning_effort'] == 'low'
    assert RuntimeConfig(store).load()['chat'].reasoning_effort == 'low'
    assert client.app.state.gateway.configs['chat'].reasoning_effort == 'low'
    assert key not in json.dumps(snapshot)
    with store.read() as conn:
        assert key not in '\n'.join(conn.iterdump())
        assert conn.execute('SELECT COUNT(*) FROM admin_operations').fetchone()[0] >= 2
    assert client.patch('/admin/api/settings/limits', json={'daily_token_limit': 10000, 'learning_concurrency': 1}).status_code == 200
    assert store.setting('learning_concurrency') == 1
    assert client.app.state.health.budget()['limit'] == 10000
    response = client.patch('/admin/api/settings/role', json={'name': '星星', 'background': '喜欢绘画', 'timezone': 'Europe/Paris'})
    assert response.status_code == 200
    assert store.setting('role_name') == '星星' and store.setting('timezone') == 'Europe/Paris'
    assert client.post('/api/v1/memories/search', json={}).status_code == 200


def test_u06_fake_401_pauses_learning_not_intake_or_recall_and_changes_recover(client, store):
    start_setup(client)
    finish_setup(client)
    mid = put(store, '喜欢天文摄影')
    with FakeOpenAI().serve() as server:
        payload = {'enabled': True, 'base_url': server.configs['chat'].base_url, 'model': 'stub-chat', 'api_key': 'fake-invalid'}
        assert client.put('/admin/api/settings/models/chat', json=payload).status_code == 200
        client.app.state.scheduler.stop()
        server.enqueue(401, {'error': {'message': 'secret fake-invalid', 'code': 'AuthenticationError'}})
        tested = client.post('/admin/api/settings/models/chat/test', json={}).json()
        assert not tested['ok'] and tested['message'] == '密钥无效' and tested['duration_ms'] >= 0
        assert client.app.state.health.snapshot()['chat']['state'] == 'invalid_key'
        assert not client.app.state.health.learning_allowed()
        assert client.post('/api/v1/entries/test/messages', json={'sender': '用户', 'content': '等待学习', 'occurred_at': '2026-10-07T10:00:00+08:00', 'dedupe_key': 'one'}).status_code == 200
        assert client.post('/api/v1/memories/search', json={'text': '天文摄影'}).json()['memories'][0]['id'] == mid
        assert client.put('/admin/api/settings/models/chat', json={**payload, 'api_key': 'fake-correct'}).status_code == 200
        assert client.app.state.health.learning_allowed()
        assert client.post('/admin/api/settings/models/chat/test', json={}).json()['ok']
        with store.read() as conn:
            assert 'fake-invalid' not in '\n'.join(conn.iterdump())


def test_connection_check_timeout_is_bounded_and_has_no_raw_response(client, monkeypatch):
    import iris.settings_api as module
    monkeypatch.setattr(module, 'CONNECTION_TIMEOUT', .05)
    start_setup(client)
    with FakeOpenAI().serve() as server:
        server.enqueue(delay=.2)
        response = client.post('/admin/api/settings/models/chat/test', json={
            'enabled': True, 'base_url': server.configs['chat'].base_url, 'model': 'stub', 'api_key': 'fake-draft'})
        assert response.status_code == 200
        assert response.json()['message'] == '连接超时'
        assert not response.json()['ok'] and 'fake-draft' not in response.text
        assert not client.get('/admin/api/settings').json()['models']['chat']['enabled']


def test_external_config_is_read_only_but_still_needs_admin(store):
    config = ModelConfig('http://127.0.0.1:9999/v1', 'fake-external', 'external-chat')
    with TestClient(create_app(store=store, configs={'chat': config}), base_url='http://127.0.0.1', client=('127.0.0.1', 1)) as c:
        assert c.get('/admin/api/settings').status_code == 409
        start_setup(c)
        settings = c.get('/admin/api/settings').json()
        assert settings['model_source'] == 'external' and settings['models']['chat']['key_set']
        assert 'fake-external' not in json.dumps(settings)
        assert c.put('/admin/api/settings/models/chat', json={'enabled': False}).status_code == 409
        assert not (store.path.parent / 'secrets.json').exists()


def test_deployment_precedence_and_config_relative_data_dir(tmp_path, monkeypatch):
    config = tmp_path / 'iris.toml'
    config.write_text('data_dir="files"\nhost="localhost"\nport=8090\n')
    monkeypatch.setenv('IRIS_PORT', '8091')
    d = deployment(config=config, port=8092)
    assert d['port'] == 8092 and d['host'] == 'localhost'
    assert d['data_dir'] == tmp_path / 'files'


def test_import_updates_db_and_secret_without_printing_and_reloads(client, store, monkeypatch, capsys, tmp_path):
    from iris.cli import main
    fixture = tmp_path / 'fixture.toml'
    fixture.write_text('[chat]\nbase_url="https://models.example/v1"\nmodel="glm-test"\napi_key="fake-import-only"\nreasoning_effort="low"\n')
    monkeypatch.setenv('IRIS_TEST_MODELS', str(fixture))
    assert main(['--data-dir', str(store.path.parent), 'models', 'import']) == 0
    assert 'fake-import-only' not in capsys.readouterr().out
    configs = client.app.state.runtime_config.load()
    assert configs['chat'].api_key == 'fake-import-only'
    client.app.state.scheduler.tick()
    assert client.app.state.gateway.configs['chat'].api_key == 'fake-import-only'


def test_session_survives_restart_but_logout_revokes_copied_cookie(store):
    kwargs = {'base_url': 'http://127.0.0.1', 'client': ('127.0.0.1', 1)}
    with TestClient(create_app(store=store), **kwargs) as first:
        start_setup(first)
        finish_setup(first)
        saved = dict(first.cookies)
    with TestClient(create_app(store=store), **kwargs) as second:
        second.cookies.update(saved)
        assert csrf(second)['authenticated']
        assert second.get('/admin/api/settings').status_code == 200
        assert second.post('/admin/api/logout', json={}).status_code == 200
        second.cookies.update(saved)
        assert second.get('/admin/api/settings').status_code == 401


def test_role_changes_preserve_history_and_do_not_duplicate_on_rename(client, store):
    start_setup(client)
    finish_setup(client)
    learned = put(store, '学习产生的记忆')
    store.set_setting('persona_goal', '已有的生成目标')
    store.set_setting('persona_rules', '已有的监管要求')
    payload = {'name': '星星', 'background': '喜欢观星。', 'timezone': 'UTC'}
    assert client.patch('/admin/api/settings/role', json=payload).status_code == 200
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content='喜欢观星' AND lifecycle='active'").fetchone()[0] == 1
    assert client.patch('/admin/api/settings/role', json={**payload, 'background': '喜欢画画。'}).status_code == 200
    with store.read() as conn:
        assert conn.execute("SELECT lifecycle FROM memories WHERE content='喜欢观星'").fetchone()[0] == 'deleted'
        assert conn.execute('SELECT lifecycle FROM memories WHERE id=?', (learned,)).fetchone()[0] == 'active'
        assert conn.execute('SELECT COUNT(*) FROM persona_versions').fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content='喜欢画画' AND stance='设定'").fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM memory_revisions').fetchone()[0] == 1
    assert store.setting('persona_goal') == '已有的生成目标'
    assert store.setting('persona_rules') == '已有的监管要求'


def test_key_rotation_keep_clear_and_crash_safe_references(client, store, monkeypatch):
    start_setup(client)
    payload = {'enabled': True, 'base_url': 'https://example.test/v1', 'model': 'chat'}
    assert client.put('/admin/api/settings/models/chat', json={**payload, 'api_key': 'fake-first'}).status_code == 200
    assert client.put('/admin/api/settings/models/chat', json={**payload, 'model': 'next'}).status_code == 200
    assert client.app.state.gateway.configs['chat'].api_key == 'fake-first'
    runtime = client.app.state.runtime_config
    original_write = runtime._write_secrets
    def interrupted(keys):
        original_write(keys)
        raise OSError('simulated interrupted import')
    monkeypatch.setattr(runtime, '_write_secrets', interrupted)
    with pytest.raises(OSError):
        runtime.save({'chat': ModelConfig('https://other.test/v1', 'fake-new', 'new')})
    assert runtime.load()['chat'].api_key == 'fake-first'
    monkeypatch.setattr(runtime, '_write_secrets', original_write)
    assert client.put('/admin/api/settings/models/chat', json={**payload, 'api_key': ''}).status_code == 200
    assert not client.get('/admin/api/settings').json()['models']['chat']['key_set']
    assert client.app.state.gateway.configs['chat'].api_key == ''


def test_secret_errors_logs_and_responses_never_echo_credentials(client, store, caplog):
    from iris.runtime_logging import configure_logging
    start_setup(client)
    finish_setup(client)
    with FakeOpenAI().serve() as server:
        key = 'fake-opaque-credential'
        payload = {'enabled': True, 'base_url': server.configs['chat'].base_url, 'model': 'stub', 'api_key': key}
        assert client.put('/admin/api/settings/models/chat', json=payload).status_code == 200
        client.app.state.scheduler.stop()
        configure_logging(store.path.parent, client.app.state.gateway.configs)
        server.enqueue(401, {'error': {'message': f'Authorization: Bearer {key}'}})
        response = client.post('/admin/api/settings/models/chat/test', json={})
        assert key not in response.text
        assert key not in client.get('/api/v1/status').text
        assert key not in caplog.text
        assert key not in (store.path.parent / 'logs/iris.log').read_text()
        # Validation failures must not echo body fields either.
        response = client.put('/admin/api/settings/models/chat', json={**payload, 'base_url': 'invalid', 'extra': key})
        assert response.status_code == 400 and key not in response.text
    (store.path.parent / 'secrets.json').write_text('{"key":"fake-opaque-credential", invalid')
    response = client.get('/admin/api/settings')
    assert response.status_code == 400 and 'fake-opaque-credential' not in response.text


def test_embedding_connection_uses_fixed_input_and_dimension(client):
    start_setup(client)
    with FakeOpenAI().serve() as server:
        response = client.post('/admin/api/settings/models/embedding/test', json={
            'base_url': server.configs['embedding'].base_url, 'model': 'stub', 'dimensions': 8})
        assert response.json()['ok']
        assert server.requests[-1][1]['input'] == 'Iris 测试连接'
        assert server.requests[-1][1]['dimensions'] == 8


def test_default_command_and_browser_options(tmp_path, monkeypatch):
    from iris.cli import main
    import uvicorn
    monkeypatch.delenv('IRIS_TEST_MODELS', raising=False)
    seen = []
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: seen.append((app, kwargs)))
    assert main(['--data-dir', str(tmp_path)]) == 0
    assert seen[-1][1]['host'] == '127.0.0.1' and seen[-1][1]['proxy_headers'] is False
    assert main(['--data-dir', str(tmp_path), 'serve', '--no-open']) == 0


def test_browser_opens_setup_once_and_never_after_completed(store, monkeypatch):
    import threading
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, 'open', opened.append)
    class ImmediateTimer:
        def __init__(self, delay, callback, args): self.callback, self.args = callback, args
        def start(self): self.callback(*self.args)
        def cancel(self): pass
    monkeypatch.setattr(threading, 'Timer', ImmediateTimer)
    with TestClient(create_app(store=store, open_browser_url='http://127.0.0.1:9999'),
                    base_url='http://127.0.0.1', client=('127.0.0.1', 1)) as c:
        assert opened == ['http://127.0.0.1:9999/setup']
        start_setup(c)
        finish_setup(c)
    with TestClient(create_app(store=store, open_browser_url='http://127.0.0.1:9999'), base_url='http://127.0.0.1'):
        assert len(opened) == 1


def test_serve_model_source_precedence_and_no_implicit_test_file(tmp_path, monkeypatch):
    from iris.cli import main
    import iris.api
    import uvicorn
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('IRIS_TEST_MODELS', raising=False)
    captured = []
    monkeypatch.setattr(iris.api, 'create_app', lambda *args, **kwargs: captured.append(kwargs))
    monkeypatch.setattr(uvicorn, 'run', lambda *args, **kwargs: None)
    implicit = tmp_path / 'test-models.toml'
    implicit.write_text('[chat]\nbase_url="https://implicit.test/v1"\nmodel="implicit"\n')
    assert main(['serve', '--no-open']) == 0
    assert captured[-1]['configs'] is None and captured[-1]['config_loader'] is None
    monkeypatch.setenv('IRIS_TEST_MODELS', str(implicit))
    assert main(['serve']) == 0
    assert captured[-1]['configs']['chat'].model == 'implicit'
    assert captured[-1]['open_browser_url'] is None
    explicit = tmp_path / 'explicit.toml'
    explicit.write_text('[chat]\nbase_url="https://explicit.test/v1"\nmodel="explicit"\n')
    assert main(['serve', '--models-config', str(explicit)]) == 0
    assert captured[-1]['config_loader']()['chat'].model == 'explicit'
