import json

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris.state import CurrentState
from test_state import Clock


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as value:
        value.app.state.current_state = CurrentState(store, clock=Clock())
        yield value


def test_host_crud_and_no_admin_editor(client):
    assert client.get('/api/v1/state').json() == {}
    first = client.put('/api/v1/state', json={'activity': '游戏', 'host': 'game', 'entry_id': 'A'})
    assert first.status_code == 200
    assert first.json()['activity'] == '游戏'
    client.app.state.current_state.clock.advance(minutes=1)
    second = client.patch('/api/v1/state', json={})
    assert second.json()['started_at'] == first.json()['started_at']
    assert second.json()['duration_seconds'] == 60
    assert client.request('DELETE', '/api/v1/state', json={'host': 'game'}).json() == {}
    assert client.get('/api/v1/state').json() == {}
    assert client.delete('/api/v1/state').json() == {}
    login_admin(client)
    for method in ('PUT', 'PATCH', 'DELETE'):
        assert client.request(method, '/admin/api/state', json={}).status_code == 405


def test_admin_history_pagination_settings_csrf_and_read_only(client, store):
    client.put('/api/v1/state', json={'activity': '游戏', 'details': {'scene': '森林'}, 'host': 'game'})
    client.patch('/api/v1/state', json={'details': {'scene': '海岸'}, 'host': 'stream'})
    assert client.get('/admin/api/state').status_code == 409
    assert client.get('/admin/api/state/reports').status_code == 409
    login_admin(client)
    assert client.get('/admin/api/state').json() == client.get('/api/v1/state').json()
    page = client.get('/admin/api/state/reports?limit=1&offset=0').json()
    assert page['total'] == 2 and page['limit'] == 1 and page['offset'] == 0
    report = page['items'][0]
    assert report['host'] == 'stream' and report['method'] == 'PATCH'
    assert report['reported'] == {'details': {'scene': '海岸'}}
    assert report['changes']['details']['scene'] == {'before': '森林', 'after': '海岸'}
    assert report['reported_at'].endswith('+08:00')
    assert client.get('/admin/api/state/reports?limit=1&offset=1').json()['items'][0]['host'] == 'game'
    assert client.get('/admin/api/state/reports?limit=0').status_code == 400
    assert client.get('/admin/api/state/reports?extra=true').status_code == 400
    assert client.get('/admin/api/settings').json()['state'] == {'stale_after_minutes': 30}
    client.app.state.current_state.clock.advance(minutes=10)
    assert client.get('/api/v1/state').json()['possibly_stale'] is False
    assert client.patch('/admin/api/settings/state', json={'stale_after_minutes': 5},
                        headers={'X-Iris-CSRF': ''}).status_code == 403
    saved = client.patch('/admin/api/settings/state', json={'stale_after_minutes': 5})
    assert saved.status_code == 200 and saved.json()['state'] == {'stale_after_minutes': 5}
    assert client.get('/api/v1/state').json()['possibly_stale'] is True
    assert client.patch('/admin/api/settings/state', json={}).json()['state'] == {'stale_after_minutes': 5}
    for value in (0, -1, 1.5, True, '30', None, 525601):
        assert client.patch('/admin/api/settings/state', json={'stale_after_minutes': value}).status_code == 400
    with store.read() as conn:
        row = conn.execute("SELECT * FROM admin_operations WHERE action='state_settings_saved' ORDER BY id LIMIT 1").fetchone()
        assert row and json.loads(row['details_json']) == {'stale_after_minutes': 5}
        assert conn.execute('SELECT COUNT(*) FROM state_reports').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM recalls').fetchone()[0] == 0
    csrf = client.headers['X-Iris-CSRF']
    client.post('/admin/api/logout', json={})
    assert client.get('/admin/api/state').status_code == 401
    assert client.get('/admin/api/state/reports').status_code == 401
    assert client.patch('/admin/api/settings/state', json={'stale_after_minutes': 1},
                        headers={'X-Iris-CSRF': csrf}).status_code in (401, 403)


@pytest.mark.parametrize('method,payload,field', [
    ('PUT', {}, 'activity'), ('PUT', {'activity': ' '}, 'activity'),
    ('PUT', {'activity': 'a' * 201}, 'activity'),
    ('PUT', {'activity': 123}, 'activity'),
    ('PUT', {'activity': '游戏', 'started_at': '2026-10-09T00:00:00'}, 'started_at'),
    ('PUT', {'activity': '游戏', 'started_at': 'tomorrow'}, 'started_at'),
    ('PUT', {'activity': '游戏', 'started_at': '2027-01-01T00:00:00Z'}, 'started_at'),
    ('PUT', {'activity': '游戏', 'started_at': None}, 'started_at'),
    ('PATCH', {'activity': '休息'}, 'activity'),
    ('PATCH', {'details': {'scene': {'nested': 'value'}}}, 'details'),
    ('PATCH', {'details': None}, 'details'),
    ('PATCH', {'details': {' ': 'value'}}, 'details'),
    ('PATCH', {'details': {'scene': 'x' * 1001}}, 'details'),
    ('PATCH', {'details': {f'k{i}': i for i in range(33)}}, 'details'),
    ('PATCH', {'mood': 'x' * 201}, 'mood'),
    ('PATCH', {'host': 'x' * 101}, 'host'),
    ('PATCH', {'entry_id': ' '}, 'entry_id'),
    ('PATCH', {'extra': True}, 'extra'),
    ('DELETE', {'mood': '平静'}, 'mood'),
])
def test_invalid_fields_are_400_and_atomic(client, store, method, payload, field):
    original = client.put('/api/v1/state', json={'activity': '游戏'}).json()
    response = client.request(method, '/api/v1/state', json=payload)
    assert response.status_code == 400, response.text
    assert any(field in e['field'] for e in response.json()['error']['fields'])
    assert client.get('/api/v1/state').json() == original
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM state_reports').fetchone()[0] == 1


def test_missing_activity_invalid_json_body_size_host_and_timezone(client, store):
    empty = client.patch('/api/v1/state', json={})
    assert empty.status_code == 400 and empty.json()['error']['fields'][0]['field'] == 'body.activity'
    assert client.put('/api/v1/state', content='{', headers={'Content-Type': 'application/json'}).status_code == 400
    oversized = client.put('/api/v1/state', content=json.dumps({'activity': '游戏'}) + ' ' * 32768,
                           headers={'Content-Type': 'application/json'})
    assert oversized.status_code == 400 and oversized.json()['error']['fields'][0]['field'] == 'body'
    client.app.state.ready = False
    assert client.put('/api/v1/state', json={'activity': '游戏'}, headers={'Host': 'evil.example'}).status_code == 400
    client.app.state.ready = True
    store.set_setting('timezone', 'Europe/Berlin')
    result = client.put('/api/v1/state', json={'activity': '游戏', 'started_at': '2026-10-09T10:00:00+08:00'}).json()
    assert result['started_at'] == '2026-10-09T04:00:00+02:00'
    assert result['updated_at'] == '2026-10-09T06:00:00+02:00'
    assert result['duration_seconds'] == 7200


def test_details_limits_apply_after_merging_and_values_keep_json_types(client):
    details = {f'key{i}': i for i in range(32)}
    client.put('/api/v1/state', json={'activity': '游戏', 'details': details})
    rejected = client.patch('/api/v1/state', json={'details': {'new': 1}})
    assert rejected.status_code == 400 and rejected.json()['error']['fields'][0]['field'] == 'body.details'
    replaced = client.patch('/api/v1/state', json={'details': {'key0': None, 'new': False}}).json()
    assert len(replaced['details']) == 32 and replaced['details']['new']['value'] is False
    client.patch('/api/v1/state', json={'details': {'new': 0}})
    login_admin(client)
    changes = client.get('/admin/api/state/reports?limit=1').json()['items'][0]['changes']
    assert changes['details']['new']['before'] is False and type(changes['details']['new']['after']) is int
    assert client.put('/api/v1/state', json={'activity': '游戏', 'details': {f'k{i}': '中' * 1000 for i in range(12)}}).status_code == 400


def test_settings_origin_and_content_type_checks(client):
    login_admin(client)
    assert client.patch('/admin/api/settings/state', json={'stale_after_minutes': 5},
                        headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.patch('/admin/api/settings/state', content='{}',
                        headers={'Content-Type': 'text/plain'}).status_code == 403
    assert client.get('/admin/api/settings').json()['state']['stale_after_minutes'] == 30
