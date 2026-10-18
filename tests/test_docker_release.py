"""Container deployment keeps Host, bootstrap and administrator boundaries separate."""
import hashlib
import json
import logging

import pytest
from fastapi.testclient import TestClient

from iris.api import create_app
from iris.cli import main
from iris.configuration import deployment
from iris import auth

CODE = 'synthetic-bootstrap-code-not-a-real-credential'
PASSWORD = 'synthetic-container-password'


@pytest.fixture(autouse=True)
def isolated_container_environment(monkeypatch):
    monkeypatch.delenv('IRIS_CONTAINER', raising=False)
    monkeypatch.delenv('IRIS_ALLOWED_HOSTS', raising=False)


def csrf(client):
    state = client.get('/admin/api/session')
    assert state.status_code == 200
    client.headers['X-Iris-CSRF'] = state.json()['csrf_token']
    return state.json()


def container_client(store, monkeypatch, *, local=False, hosts=('iris',)):
    monkeypatch.setenv('IRIS_CONTAINER', '1')
    monkeypatch.setattr(auth.secrets, 'token_hex', lambda n: CODE)
    return TestClient(create_app(store=store, allowed_hosts=hosts), base_url='http://127.0.0.1',
                      client=('127.0.0.1' if local else '172.20.0.5', 4000))


@pytest.mark.parametrize('flag', [None, '', '0', 'true', 'yes', ' 1', '01'])
def test_non_container_still_rejects_public_listener_before_database(tmp_path, monkeypatch, flag):
    if flag is not None:
        monkeypatch.setenv('IRIS_CONTAINER', flag)
    db = tmp_path / 'iris.db'
    assert main(['--db', str(db), 'serve', '--host', '0.0.0.0', '--no-open']) == 1
    assert not db.exists()


def test_container_listener_warning_and_explicit_allowlist(tmp_path, monkeypatch, capsys):
    import uvicorn
    monkeypatch.setenv('IRIS_CONTAINER', '1')
    monkeypatch.setenv('IRIS_ALLOWED_HOSTS', 'iris,iris.internal')
    seen = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: seen.update(app=app, **kwargs))
    from iris import runtime_logging
    logger = logging.getLogger('container-cli-test')
    monkeypatch.setattr(runtime_logging, 'configure_logging', lambda *a, **kw: logger)
    with pytest.MonkeyPatch.context() as patch:
        warnings = []
        patch.setattr(logger, 'warning', lambda message: warnings.append(message))
        assert main(['--data-dir', str(tmp_path), 'serve', '--no-open']) == 0
    assert seen['host'] == '0.0.0.0' and seen['port'] == 8080
    assert seen['proxy_headers'] is False
    assert any('不要直接暴露到公网' in text and '不提供 TLS' in text for text in warnings)
    assert '首次设置码' not in capsys.readouterr().out
    with TestClient(seen['app'], base_url='http://iris', client=('172.20.0.5', 1)) as client:
        assert client.get('/setup').status_code == 200
        assert client.get('/setup', headers={'Host': 'evil.example'}).status_code == 400


def test_allowed_hosts_config_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path / 'iris.toml'
    path.write_text('allowed_hosts=["iris", "iris.internal"]\n')
    assert deployment(config=path)['allowed_hosts'] == ['iris', 'iris.internal']
    monkeypatch.setenv('IRIS_ALLOWED_HOSTS', 'worker, iris-2')
    assert deployment(config=path)['allowed_hosts'] == ['worker', 'iris-2']
    monkeypatch.setenv('IRIS_ALLOWED_HOSTS', '')
    assert deployment(config=path)['allowed_hosts'] == []
    assert deployment(config=path)['host'] == '127.0.0.1'


@pytest.mark.parametrize('value', ['*', '*.example', 'http://iris', 'iris/path', 'iris?x',
                                  'iris#x', 'iris@host', 'iris\nother', 'iris:invalid', 'iris,'])
def test_unsafe_host_configuration_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setenv('IRIS_ALLOWED_HOSTS', value)
    with pytest.raises(ValueError):
        deployment(config=None, data_dir=tmp_path)


@pytest.mark.parametrize('host', ['iris', 'iris:8080', '127.0.0.1:8080', 'localhost', '[::1]:8080'])
def test_container_hosts_gate_every_route_and_keep_csrf(store, monkeypatch, host):
    with container_client(store, monkeypatch) as client:
        for path in ('/setup', '/admin/api/session'):
            assert client.get(path, headers={'Host': host}).status_code == 200
        client.app.state.ready = False
        assert client.get('/setup', headers={'Host': 'foreign', 'X-Forwarded-Host': host}).status_code == 400
        assert client.get('/api/v1/status', headers={'Host': 'foreign'}).json()['error']['fields'][0]['field'] == 'header.host'
        client.app.state.ready = True
        csrf(client)
        response = client.post('/admin/api/setup/password', json={'password': PASSWORD, 'setup_code': CODE},
                               headers={'Origin': 'http://foreign'})
        assert response.status_code == 403
        assert not client.app.state.sessions.exists()


def test_container_does_not_trust_arbitrary_host_without_configuration(store, monkeypatch):
    with container_client(store, monkeypatch, hosts=()) as client:
        assert client.get('/setup', headers={'Host': 'iris'}).status_code == 400
        assert client.get('/setup').status_code == 200


@pytest.mark.parametrize('provided', [None, '', 'incorrect', '带中文的错误设置码'])
def test_non_loopback_requires_correct_setup_code(store, monkeypatch, provided):
    with container_client(store, monkeypatch) as client:
        assert csrf(client)['setup_code_required'] is True
        payload = {'password': PASSWORD}
        if provided is not None:
            payload['setup_code'] = provided
        response = client.post('/admin/api/setup/password', json=payload,
                               headers={'X-Forwarded-For': '127.0.0.1'})
        assert response.status_code == 403
        assert not client.app.state.sessions.exists()
        assert CODE not in response.text


@pytest.mark.parametrize('with_code', [False, True])
def test_loopback_bootstrap_remains_compatible(store, monkeypatch, with_code):
    with container_client(store, monkeypatch, local=True) as client:
        assert csrf(client)['setup_code_required'] is False
        payload = {'password': PASSWORD, **({'setup_code': CODE} if with_code else {})}
        assert client.post('/admin/api/setup/password', json=payload).status_code == 200
        assert client.app.state.sessions.setup_code_hash is None


def test_code_is_digest_only_one_use_and_never_in_responses_or_audit(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='iris.auth')
    with container_client(store, monkeypatch) as client:
        sessions = client.app.state.sessions
        assert sessions.setup_code_hash == hashlib.sha256(CODE.encode()).hexdigest()
        assert CODE not in repr(vars(sessions))
        state = csrf(client)
        assert CODE not in json.dumps(state) and 'setup_code_hash' not in state
        response = client.post('/admin/api/setup/password', json={'password': PASSWORD, 'setup_code': CODE})
        assert response.status_code == 200 and CODE not in response.text
        assert sessions.setup_code_hash is None
        assert not csrf(client)['setup_code_required']
        assert client.get('/admin/api/settings').status_code == 200
        assert client.post('/admin/api/setup/complete', json={'name': 'Iris', 'timezone': 'UTC'}).status_code == 200
        assert client.get('/').status_code == 200
        client.post('/admin/api/logout', json={})
        csrf(client)
        assert client.post('/admin/api/setup/password', json={'password': PASSWORD, 'setup_code': CODE}).status_code == 409
        assert client.post('/admin/api/login', json={'password': PASSWORD}).status_code == 200
        with store.read() as conn:
            data = '\n'.join(conn.iterdump())
        assert CODE not in data and hashlib.sha256(CODE.encode()).hexdigest() not in data
        assert PASSWORD not in data
        messages = [row.getMessage() for row in caplog.records]
        assert sum(CODE in text for text in messages) == 1
        assert not any(PASSWORD in text for text in messages)
    caplog.clear()
    with container_client(store, monkeypatch) as client:
        assert client.app.state.sessions.setup_code_hash is None
    assert not any(CODE in row.getMessage() for row in caplog.records)


def test_restart_before_password_rotates_code(store, monkeypatch):
    with container_client(store, monkeypatch) as client:
        first = client.app.state.sessions.setup_code_hash
    monkeypatch.setattr(auth.secrets, 'token_hex', lambda n: CODE + '-new')
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('172.20.0.5', 4000)) as client:
        assert client.app.state.sessions.setup_code_hash != first
        csrf(client)
        assert client.post('/admin/api/setup/password', json={'password': PASSWORD, 'setup_code': CODE}).status_code == 403
        assert client.post('/admin/api/setup/password', json={'password': PASSWORD, 'setup_code': CODE+'-new'}).status_code == 200


def test_cli_version_does_not_start_service(capsys):
    with pytest.raises(SystemExit) as result:
        main(['--version'])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == 'iris 0.0.2'
