"""Credential failures use synthetic fixtures and never contact real models."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import authorize_host, login_admin
from iris.api import create_app
from iris.configuration import RuntimeConfig
from iris.models import ModelConfig, MODEL_KINDS


@pytest.mark.parametrize('fault', ['missing', 'json', 'utf8', 'format', 'permission'])
def test_startup_credential_failure_isolated_and_repairable(store, monkeypatch, caplog, fault):
    runtime = RuntimeConfig(store)
    private = ModelConfig('https://model.example/v1', 'fake-recovery-only', 'private-model')
    runtime.save({'chat': private, 'recall_judge': private, 'image_understanding': private,
                  'embedding': ModelConfig('http://127.0.0.1:11434/v1', '', 'local-embedding', 8)})
    original_rows = store.setting('models')
    if fault == 'missing':
        runtime.path.unlink()
    elif fault == 'json':
        runtime.path.write_text('{"invalid":"fake-recovery-only",', encoding='utf-8')
    elif fault == 'utf8':
        runtime.path.write_bytes(b'\xff\xfe')
    elif fault == 'format':
        runtime.path.write_text('["fake-recovery-only"]', encoding='utf-8')
    else:
        original_read = Path.read_text
        unreadable_inode = runtime.path.stat().st_ino
        def denied(path, *args, **kwargs):
            if path == runtime.path and path.stat().st_ino == unreadable_inode:
                raise PermissionError('fake-recovery-only')
            return original_read(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', denied)
    calls = []
    monkeypatch.setattr('iris.models.Gateway._call', lambda *args, **kwargs: calls.append(kwargs) or {})
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        authorize_host(client)
        login_admin(client)
        response = client.get('/admin/api/settings')
        assert response.status_code == 200
        body = response.json()
        assert body['health']['embedding']['state'] == 'normal'
        for kind in ('chat', 'recall_judge', 'goal_dedup_judge', 'image_understanding'):
            assert body['health'][kind]['state'] == 'configuration_error'
            assert body['models'][kind]['enabled']
            assert body['models'][kind]['base_url'] == private.base_url
            assert body['models'][kind]['key_missing']
            assert not body['models'][kind]['key_set']
            assert not client.app.state.health.allowed(kind)
        assert store.setting('models') == original_rows
        assert client.get('/api/v1/status').json()['model_health']['image_understanding']['state'] == 'configuration_error'
        assert client.post('/api/v1/entries/A/messages', json={
            'sender': '测试', 'content': '仍可接收', 'dedupe_key': 'test-only', 'occurred_at': '2026-10-10T00:00:00+00:00'}).status_code == 200
        assert client.post('/api/v1/entries/A/prepare', json={'judge': False, 'text': ''}).status_code == 200
        assert client.post('/admin/api/settings/models/chat/test', json={}).json()['category'] == 'configuration'
        payload = {'base_url': private.base_url, 'model': private.model}
        for method, path in [('PUT', '/admin/api/settings/models/chat'), ('POST', '/admin/api/settings/models/chat/test')]:
            rejected = client.request(method, path, json=payload)
            assert rejected.status_code == 400
            assert rejected.json()['error']['fields'][0]['field'] == 'body.api_key'
        assert not calls
        repaired = client.put('/admin/api/settings/models/chat', json={**payload, 'api_key': 'fake-repaired-only'})
        assert repaired.status_code == 200
        assert repaired.json()['health']['chat']['state'] == 'normal'
        assert repaired.json()['health']['goal_dedup_judge']['state'] == 'normal'
        # Missing independent keys must never inherit the repaired chat credential.
        assert repaired.json()['health']['recall_judge']['state'] == 'configuration_error'
        assert repaired.json()['health']['image_understanding']['state'] == 'configuration_error'
        assert runtime.load()['chat'].api_key == 'fake-repaired-only'
        assert 'fake-recovery-only' not in response.text + repaired.text + caplog.text
        assert 'fake-repaired-only' not in repaired.text + caplog.text


def test_one_missing_reference_does_not_disable_other_purposes(store):
    runtime = RuntimeConfig(store)
    for kind in MODEL_KINDS:
        runtime.save({kind: ModelConfig('https://model.example/v1', 'fake-partial-only', 'stub')})
    rows = store.setting('models')
    rows['recall_judge']['api_key_ref'] = 'missing-reference'
    store.set_setting('models', rows)
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        body = client.get('/admin/api/settings').json()
        for kind in MODEL_KINDS:
            assert body['health'][kind]['state'] == ('configuration_error' if kind == 'recall_judge' else 'normal')
        assert not body['models']['recall_judge']['inherited']
        assert client.put('/admin/api/settings/models/recall_judge', json={
            'base_url': 'https://model.example/v1', 'model': 'stub', 'api_key': 'fake-new-reference'}).status_code == 200
        assert client.app.state.health.allowed('recall_judge')


def test_runtime_loss_does_not_test_with_stale_cached_key(store, monkeypatch):
    runtime = RuntimeConfig(store)
    runtime.save({'chat': ModelConfig('https://model.example/v1', 'fake-stale-only', 'stub')})
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        runtime.path.unlink()
        calls = []
        monkeypatch.setattr('iris.models.Gateway._call', lambda *a, **kw: calls.append(kw) or {})
        assert client.post('/admin/api/settings/models/chat/test', json={}).json()['category'] == 'configuration'
        rejected = client.put('/admin/api/settings/models/chat', json={'base_url': 'https://model.example/v1', 'model': 'changed'})
        assert rejected.status_code == 400
        assert not calls
        # The normal scheduler loader returns a changed, unusable configuration;
        # existing replace_config then pauses it without changing scheduler logic.
        loaded = client.app.state.scheduler.config_loader()
        for kind in MODEL_KINDS:
            client.app.state.gateway.replace_config(kind, loaded.get(kind))
        assert not client.app.state.health.allowed('chat')
        assert not client.app.state.health.allowed('recall_judge')


def test_keyless_config_survives_unreadable_credentials(store, monkeypatch):
    runtime = RuntimeConfig(store)
    runtime.save({'chat': ModelConfig('http://127.0.0.1:11434/v1', '', 'local')})
    runtime.path.write_text('invalid', encoding='utf-8')
    assert runtime.load()['chat'].model == 'local'
    assert runtime.load()['chat'].base_url == 'http://127.0.0.1:11434/v1'


def test_cli_corrupt_credentials_is_actionable_and_preserves_data(store, tmp_path, monkeypatch, capsys):
    from iris.cli import main
    runtime = RuntimeConfig(store)
    runtime.save({'chat': ModelConfig('https://old.example/v1', 'fake-old-only', 'old')})
    rows = store.setting('models')
    runtime.path.write_text('invalid fake-old-only', encoding='utf-8')
    # Mock the input loader so no credential file needs to be opened by the test.
    monkeypatch.setattr('iris.cli.load_test_models', lambda _: {'chat': ModelConfig('https://new.example/v1', 'fake-new-only', 'new')})
    assert main(['--db', str(store.path), 'models', 'import', '--from', str(tmp_path / 'fixture.toml')]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert 'secrets.json' in captured.err and '设置页' in captured.err and '重新输入' in captured.err
    assert 'fake-old-only' not in captured.err and 'fake-new-only' not in captured.err
    assert store.setting('models') == rows
    assert runtime.path.stat().st_size == len(b'invalid fake-old-only')


def test_repair_never_follows_or_replaces_secret_symlink(store, tmp_path):
    runtime = RuntimeConfig(store)
    runtime.save({'chat': ModelConfig('https://model.example/v1', 'fake-symlink-only', 'stub')})
    runtime.path.unlink()
    target = tmp_path / 'unrelated-fixture'
    target.write_bytes(b'unrelated')
    runtime.path.symlink_to(target)
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        response = client.put('/admin/api/settings/models/chat', json={
            'base_url': 'https://model.example/v1', 'model': 'stub', 'api_key': 'fake-repair-only'})
        assert response.status_code == 400
    assert runtime.path.is_symlink() and target.stat().st_size == len(b'unrelated')
