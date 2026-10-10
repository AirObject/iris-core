"""HTTP v1 compatibility baseline: public requests, projections and errors.

Handwritten fixtures use no model provider. Additional response fields are allowed;
required fields and types are frozen here, independently of the OpenAPI builder.
"""
import base64
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import authorize_host, msg
from iris.api import create_app, HostRetrieval
from iris.media import DEFAULT_MAX_BYTES, save_media
from test_retrieval import put

STAMP = '2026-10-10T00:00:00+00:00'
PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR' + b'\x00' * 20
MESSAGE = {'sender': '小林', 'content': '今晚看星星', 'occurred_at': STAMP, 'dedupe_key': 'm1'}
UPLOAD = {'entry_id': 'A', 'content_type': 'image/png', 'data_base64': base64.b64encode(PNG).decode()}
OPERATIONS = [
    ('post', '/api/v1/entries/{entry_id}/messages'),
    ('post', '/api/v1/entries/{entry_id}/learn'),
    ('post', '/api/v1/entries/{entry_id}/prepare'),
    ('post', '/api/v1/media'), ('post', '/api/v1/memories/search'),
    ('post', '/api/v1/feedback'), ('get', '/api/v1/state'), ('put', '/api/v1/state'),
    ('patch', '/api/v1/state'), ('delete', '/api/v1/state'),
    ('get', '/api/v1/goals'), ('post', '/api/v1/goals'),
    ('patch', '/api/v1/goals/{goal_id}'), ('get', '/api/v1/notifications'),
    ('get', '/api/v1/status'),
]


def assert_schema(value, schema, document):
    """Validate the subset used by the response contract, including real examples."""
    if "$ref" in schema:
        node = document
        for segment in schema["$ref"].removeprefix("#/").split("/"):
            node = node[segment]
        return assert_schema(value, node, document)
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                assert_schema(value, branch, document)
                return
            except AssertionError:
                pass
        raise AssertionError(f"No response schema branch matched {type(value).__name__}")
    types = {"object": (dict,), "array": (list,), "string": (str,), "integer": (int,),
             "number": (int, float), "boolean": (bool,), "null": (type(None),)}
    if "type" in schema:
        assert type(value) in types[schema['type']], schema
    if "enum" in schema:
        assert value in schema['enum']
    if isinstance(value, dict):
        assert set(schema.get('required', [])) <= value.keys()
        assert len(value) <= schema.get('maxProperties', len(value))
        for key, item in value.items():
            child = schema.get('properties', {}).get(key, schema.get('additionalProperties', {}))
            if isinstance(child, dict):
                assert_schema(item, child, document)
    if isinstance(value, list):
        for item in value:
            assert_schema(item, schema.get('items', {}), document)


def validate_http_schema(client, response):
    # Check documented response types against actual HTTP, without response filtering.
    if not response.request.url.path.startswith('/api/v1/') or response.status_code >= 400:
        return
    response.read()
    path = response.request.url.path
    if path.startswith('/api/v1/entries/'):
        path = '/api/v1/entries/{entry_id}/' + path.rsplit('/', 1)[1]
    elif path.startswith('/api/v1/goals/'):
        path = '/api/v1/goals/{goal_id}'
    document = client.app.openapi()
    schema = document['paths'][path][response.request.method.lower()]['responses'][str(response.status_code)]['content']['application/json']['schema']
    assert_schema(response.json(), schema, document)


@pytest.fixture
def client(store):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1234)) as value:
        value.app.state.scheduler.stop()
        authorize_host(value)
        value.event_hooks["response"].append(lambda response: validate_http_schema(value, response))
        yield value


def scoped(client, scope):
    value = client.app.state.tokens.create(host='connector', scope=scope, actor='local_cli')
    return {'Authorization': 'Bearer ' + value['token']}


def fields(value, types):
    assert isinstance(value, dict)
    for name, expected in types.items():
        assert name in value, name
        assert type(value[name]) in (expected if isinstance(expected, tuple) else (expected,)), name


def error(response, status, *, field=None):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {'error'}
    e = body['error']
    fields(e, {'code': str, 'message': str, 'fields': list,
               'retry_after_seconds': (int, type(None)), 'retry_at': (str, type(None))})
    assert e['code'] and e['message']
    for item in e['fields']:
        fields(item, {'field': str, 'message': str})
    if status == 400:
        assert e['fields']
    if field:
        assert any(item['field'] == field for item in e['fields'])
    if status in (429, 503):
        assert int(response.headers['Retry-After']) == e['retry_after_seconds'] >= 1
        assert datetime.fromisoformat(e['retry_at']).tzinfo is not None
    else:
        assert e['retry_after_seconds'] is e['retry_at'] is None
    if status == 401:
        assert response.headers['WWW-Authenticate'] == 'Bearer'
    return e


@pytest.mark.parametrize('method,path', OPERATIONS)
def test_v1_every_operation_auth_and_openapi(client, method, path):
    endpoint = path.replace('{entry_id}', 'A').replace('{goal_id}', '1')
    error(client.request(method, endpoint, content='{', headers={'Authorization': ''}), 401)
    operation = client.app.openapi()['paths'][path][method]
    assert operation['description'] and any('\u4e00' <= c <= '\u9fff' for c in operation['description'])
    assert operation['security']
    assert '422' not in operation['responses']
    success = operation['responses']['201' if path in ('/api/v1/media', '/api/v1/goals') and method == 'post' else '200']
    assert success['content']['application/json']['schema']
    assert success['content']['application/json']['example'] is not None
    assert_schema(success['content']['application/json']['example'], success['content']['application/json']['schema'], client.app.openapi())
    assert operation['x-request-example']
    for status in ('400', '401', '403', '404', '409', '413', '429', '503'):
        assert operation['responses'][status]['content']['application/json']['schema']


def test_v1_message_single_batch_dedupe_atomicity_and_learn(client, store):
    one = client.post('/api/v1/entries/A/messages', json=MESSAGE)
    assert one.status_code == 200
    fields(one.json(), {'message_ids': list, 'pending_count': int})
    assert type(one.json()['message_ids'][0]) is int
    duplicate = client.post('/api/v1/entries/A/messages', json={**MESSAGE, 'content': '重试不能覆盖'})
    assert duplicate.json() == one.json()
    multiple = client.post('/api/v1/entries/A/messages', json=[MESSAGE, {**MESSAGE, 'dedupe_key': 'm2'}])
    assert multiple.json()['pending_count'] == 2
    error(client.post('/api/v1/entries/A/messages', json=[{**MESSAGE, 'dedupe_key': 'rolled-back'}, {}]), 400)
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
    result = client.post('/api/v1/entries/A/learn').json()
    fields(result, {'accepted': bool, 'pending_count': int, 'paused': bool, 'reason': (str, type(None))})
    assert result['accepted'] and result['pending_count'] == 2
    error(client.post('/api/v1/entries/missing/learn'), 404)


def test_v1_prepare_search_feedback_and_deleted_memory(client, store):
    msg(store, 0, '本入口消息')
    mid = put(store, '天文摄影')
    result = client.post('/api/v1/entries/A/prepare', json={'text': '天文摄影', 'judge': False}).json()
    fields(result, {'persona': dict, 'memories': list, 'recent_messages': list, 'goals': list,
                    'state': dict, 'hints': list, 'judgment': dict, 'recall_id': str})
    fields(result['recent_messages'][0], {'id': int, 'entry_id': str, 'content': str, 'unlearned': bool})
    fields(result['memories'][0], {'id': int, 'content': str, 'revision': int, 'reason': str,
                                 'lifecycle': str, 'sources': list, 'about': list})
    assert result['judgment']['status'] == 'disabled'
    search = client.post('/api/v1/memories/search', json={'text': '天文摄影', 'include_goals': True, 'include_state': True}).json()
    fields(search, {'memories': list, 'hints': list, 'recall_id': str, 'goals': list, 'state': dict})
    feedback = {'recall_id': search['recall_id'], 'memory_ids': [mid]}
    used = client.post('/api/v1/feedback', json=feedback).json()
    assert used == {'recall_id': search['recall_id'], 'accepted': [mid], 'strengthened': [mid]}
    assert client.post('/api/v1/feedback', json=feedback).json()['strengthened'] == []
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='deleted' WHERE id=?", (mid,))
    error(client.post('/api/v1/feedback', json=feedback), 404)
    error(client.post('/api/v1/entries/missing/prepare', json={}), 404)
    error(client.post('/api/v1/feedback', json={'recall_id': 'missing', 'memory_ids': []}), 404)


def test_v1_state_all_four_methods(client):
    assert client.get('/api/v1/state').json() == {}
    created = client.put('/api/v1/state', json={'activity': '观星', 'details': {'location': '露台'}, 'entry_id': 'A'})
    assert created.status_code == 200
    fields(created.json(), {'activity': str, 'details': dict, 'host': str, 'started_at': str,
                           'updated_at': str, 'possibly_stale': bool, 'duration_seconds': float})
    fields(created.json()['details']['location'], {'value': str, 'updated_at': str})
    updated = client.patch('/api/v1/state', json={'mood': '平静'}).json()
    assert updated['activity'] == '观星' and updated['mood'] == '平静'
    assert client.get('/api/v1/state').json()['started_at'] == created.json()['started_at']
    assert client.delete('/api/v1/state').json() == {}
    assert client.get('/api/v1/state').json() == {}


def test_v1_goals_revision_and_notifications(client):
    payload = {'content': '整理观星照片', 'deadline': '2020-01-01', 'host_key': 'goal-1'}
    created = client.post('/api/v1/goals', json=payload)
    assert created.status_code == 201
    fields(created.json(), {'goal': dict, 'submitted_id': int, 'dedup': dict})
    goal = created.json()['goal']
    fields(goal, {'id': int, 'revision': int, 'content': str, 'state': str, 'kind': str, 'people': list,
                  'entry_id': (str, type(None)), 'overdue': bool, 'due_soon': bool})
    assert client.post('/api/v1/goals', json=payload).json() == created.json()
    listing = client.get('/api/v1/goals?state=open&limit=1').json()
    fields(listing, {'items': list, 'total': int, 'offset': int, 'limit': int})
    assert listing['items'][0]['id'] == goal['id']
    pull = client.get('/api/v1/notifications?after=0&limit=1').json()
    fields(pull, {'items': list, 'next_cursor': int, 'has_more': bool})
    fields(pull['items'][0], {'id': int, 'goal_id': int, 'kind': str, 'status': str, 'content': str})
    assert pull['items'][0]['status'] == 'taken'
    assert client.get('/api/v1/goals').json()['items'][0]['state'] == 'open'
    assert client.get(f"/api/v1/notifications?after={pull['next_cursor']}").json()['items'] == []
    error(client.patch(f"/api/v1/goals/{goal['id']}", json={'state': 'completed', 'expected_revision': 999}), 409)
    changed = client.patch(f"/api/v1/goals/{goal['id']}", json={'state': 'completed', 'expected_revision': goal['revision']})
    assert changed.status_code == 200 and changed.json()['revision'] > goal['revision']
    error(client.patch('/api/v1/goals/999999', json={'state': 'abandoned'}), 404)


def test_v1_service_status_fields_and_scope(client, store):
    msg(store, 0, '有权', entry='A')
    msg(store, 1, '无权', entry='B')
    response = client.get('/api/v1/status', headers=scoped(client, {'kind': 'entries', 'entries': ['A']}))
    assert response.status_code == 200
    value = response.json()
    fields(value, {'service': str, 'models': list, 'backlog': list, 'entries': list, 'batches': list,
                   'model_health': dict, 'usage': dict, 'scheduler': dict, 'timeouts_seconds': dict,
                   'learning_calls_24h': list, 'learning_latency_24h': dict, 'memory_gap_count': int})
    assert value['service'] == 'ready'
    assert {e['entry_id'] for e in value['entries']} == {'A'}


@pytest.mark.parametrize('method,path,payload,field', [
    ('POST', '/api/v1/entries/A/messages', {}, 'body.Message.sender'),
    ('POST', '/api/v1/entries/A/messages', [], 'body'),
    ('POST', '/api/v1/entries/A/prepare', {'recent_limit': 101}, 'body.recent_limit'),
    ('POST', '/api/v1/memories/search', {'entry_id': ''}, 'body.entry_id'),
    ('POST', '/api/v1/memories/search', {'entry_id': ' '}, 'body.entry_id'),
    ('POST', '/api/v1/memories/search', {'entry_id': 'A\x00'}, 'body.entry_id'),
    ('POST', '/api/v1/feedback', {'recall_id': 'r', 'memory_ids': [-1]}, 'body.memory_ids.0'),
    ('POST', '/api/v1/media', {**UPLOAD, 'extra': 1}, 'body.extra'),
    ('PUT', '/api/v1/state', {}, 'body.activity'),
    ('PATCH', '/api/v1/state', {'activity': 'no'}, 'body.activity'),
    ('DELETE', '/api/v1/state', {'extra': 1}, 'body.extra'),
    ('POST', '/api/v1/goals', {}, 'body.content'),
    ('PATCH', '/api/v1/goals/1', {'state': 'invalid'}, 'body.state'),
    ('GET', '/api/v1/goals?limit=0', None, 'query.limit'),
    ('GET', '/api/v1/notifications?after=-1', None, 'query.after'),
])
def test_v1_field_errors(client, method, path, payload, field):
    error(client.request(method, path, json=payload), 400, field=field)


@pytest.mark.parametrize('path', ['/api/v1/entries/A/messages', '/api/v1/media', '/api/v1/goals'])
def test_v1_json_syntax_content_type_and_utf8(client, path):
    error(client.post(path, content='{', headers={'Content-Type': 'application/json'}), 400)
    error(client.post(path, content='{}', headers={'Content-Type': 'text/plain'}), 400)
    error(client.post(path, content=b'\xff', headers={'Content-Type': 'application/json'}), 400)


def test_v1_error_envelope_unknown_route_host_readiness_and_database(client, monkeypatch):
    error(client.get('/api/v1/unknown'), 404)
    error(client.get('/api/v1/status', headers={'Host': 'evil.example'}), 400, field='header.host')
    client.app.state.ready = False
    error(client.get('/api/v1/status'), 503)
    error(client.get('/api/v1/status', headers={'Host': 'evil.example'}), 400)
    client.app.state.ready = True
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('private path must not escape')
    monkeypatch.setattr('iris.api.service_status', unavailable)
    response = client.get('/api/v1/status')
    error(response, 503)
    assert 'private path' not in response.text


def test_v1_rate_limit_and_unauthorized_scope(client, store):
    headers = scoped(client, {'kind': 'entries', 'entries': ['A']})
    for suffix in ('messages', 'learn', 'prepare'):
        error(client.post('/api/v1/entries/B/' + suffix, json={}, headers=headers), 403)
    error(client.post('/api/v1/memories/search', json={'entry_id': 'B'}, headers=headers), 403)
    error(client.post('/api/v1/media', json={**UPLOAD, 'entry_id': 'B'}, headers=headers), 403)
    store.set_setting('host_tokens', {'rate_per_second': 1, 'burst': 1})
    client.app.state.tokens.clock = lambda: 0.0
    headers = scoped(client, {'kind': 'all'})
    assert client.get('/api/v1/status', headers=headers).status_code == 200
    error(client.get('/api/v1/status', headers=headers), 429)


@pytest.mark.parametrize('method,path', [('PUT', '/api/v1/state'), ('POST', '/api/v1/goals'),
                                       ('POST', '/api/v1/entries/A/messages')])
def test_v1_oversized_requests_are_413(client, method, path):
    payload = {**MESSAGE, 'content': '中' * 10923} if path.endswith('messages') else {'padding': 'x' * 32769}
    error(client.request(method, path, json=payload), 413)


def test_v1_media_upload_references_sharing_and_host_understanding(client, store):
    first = client.post('/api/v1/media', json={**UPLOAD, 'understanding_text': '星空照片'})
    assert first.status_code == 201
    value = first.json()
    fields(value, {'id': str, 'sha256': str, 'content_type': str, 'size_bytes': int,
                   'kind': str, 'understanding_source': str, 'understanding_text': str})
    assert value['understanding_source'] == 'host' and value['understanding_text'] == '星空照片'
    second = client.post('/api/v1/media', json=UPLOAD).json()
    assert second['id'] != value['id'] and second['sha256'] == value['sha256']
    accepted = client.post('/api/v1/entries/A/messages', json={**MESSAGE, 'media_ids': [value['id'], second['id']]})
    assert accepted.status_code == 200
    # A retry never replaces the already committed message or its references.
    retry = client.post('/api/v1/entries/A/messages', json={**MESSAGE, 'media_ids': ['expired-object']})
    assert retry.json() == accepted.json()
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM media_files').fetchone()[0] == 1
        assert [r[0] for r in conn.execute('SELECT media_id FROM message_media ORDER BY position')] == [value['id'], second['id']]
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0


@pytest.mark.parametrize('change,code,field', [
    ({'data_base64': '%%%='}, 'invalid_request', 'body.data_base64'),
    ({'content_type': 'image/svg+xml'}, 'unsupported_media_type', 'body.content_type'),
    ({'content_type': 'image/jpeg'}, 'unsupported_media_type', 'body.content_type'),
    ({'data_base64': ''}, 'unsupported_media_type', 'body.content_type'),
    ({'understanding_text': '中' * 10923}, 'invalid_understanding', 'body.understanding_text'),
])
def test_v1_media_invalid_payloads(client, change, code, field):
    assert error(client.post('/api/v1/media', json={**UPLOAD, **change}), 400, field=field)['code'] == code


def test_v1_media_file_and_wire_limits(client):
    data = PNG + b'x' * (DEFAULT_MAX_BYTES - len(PNG))
    assert client.post('/api/v1/media', json={**UPLOAD, 'data_base64': base64.b64encode(data).decode()}).status_code == 201
    error(client.post('/api/v1/media', json={**UPLOAD, 'data_base64': base64.b64encode(data + b'x').decode()}), 413)
    # No Content-Length: actual streamed bytes are bounded too.
    error(client.post('/api/v1/media', content=iter([b' ' * (14 * 1024 * 1024), b'x']),
                      headers={'Content-Type': 'application/json'}), 413)
    error(client.post('/api/v1/media', content=b'{}', headers={
        'Content-Type': 'application/json', 'Content-Length': str(14 * 1024 * 1024 + 1)}), 413)


def test_v1_media_scope_reference_missing_and_atomic_batch(client, store):
    value = client.post('/api/v1/media', json={**UPLOAD, 'entry_id': 'B'}).json()
    headers = scoped(client, {'kind': 'entries', 'entries': ['A']})
    payload = {**MESSAGE, 'media_ids': [value['id']]}
    error(client.post('/api/v1/entries/A/messages', json=payload, headers=headers), 403)
    error(client.post('/api/v1/entries/A/messages', json={**MESSAGE, 'media_ids': ['missing']}), 404)
    # Internal objects without a host upload grant are not public capabilities.
    internal = save_media(store, PNG, content_type='image/png')
    error(client.post('/api/v1/entries/A/messages', json={**MESSAGE, 'media_ids': [internal['id']]}), 404)
    error(client.post('/api/v1/entries/A/messages', json=[MESSAGE, {**payload, 'dedupe_key': 'second'}], headers=headers), 403)
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
    assert client.post('/api/v1/entries/A/messages', json=payload).status_code == 200


@pytest.mark.parametrize('scope,expected', [
    ({'kind': 'all'}, 3),
    ({'kind': 'entries', 'entries': ['A', 'B']}, 1),
    ({'kind': 'prefix', 'prefix': 'bot:%_'}, 1),
    ({'kind': 'entries', 'entries': ['A']}, 0),
])
def test_R02_only_authorized_other_entry_count_and_received_range(client, store, scope, expected):
    entries = ['A', 'B', 'bot:%_1', 'bot:abc', 'learned', 'missed', 'skipped']
    for i, entry in enumerate(entries):
        msg(store, i, '绝不能外泄的正文和人物', entry=entry)
    msg(store, 9, '同一入口第二条不增加入口数', entry='B')
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at=?", (STAMP,))
        conn.execute("UPDATE messages SET learning_state='batched' WHERE entry_id='B'")
        for state in ('learned', 'missed', 'skipped'):
            conn.execute('UPDATE messages SET learning_state=? WHERE entry_id=?', (state, state))
    # The prefix token needs its own existing current entry, outside the three others.
    current = 'bot:%_current' if scope['kind'] == 'prefix' else 'A'
    if current != 'A':
        msg(store, 8, '当前入口', entry=current)
    result = client.post(f'/api/v1/entries/{current}/prepare', json={'judge': False}, headers=scoped(client, scope))
    assert result.status_code == 200
    hints = [h for h in result.json()['hints'] if h['code'] == 'other_entries_pending']
    if not expected:
        assert hints == []
        return
    assert len(hints) == 1
    assert hints[0] == {'code': 'other_entries_pending', 'entry_count': expected, 'time_from': STAMP,
                        'time_to': STAMP, 'time_basis': 'received_at',
                        'message': f'另有 {expected} 个入口有尚未学习的新消息（{STAMP}—{STAMP}）'}


def test_R02_range_and_prepare_other_partitions_unchanged(client, store, monkeypatch):
    msg(store, 0, '本入口', entry='A')
    msg(store, 1, '别处新消息', entry='B')
    msg(store, 2, '更晚的消息', entry='B')
    end = '2026-10-10T01:00:00+00:00'
    with store.write() as conn:
        conn.execute('UPDATE messages SET received_at=? WHERE id=2', (STAMP,))
        conn.execute('UPDATE messages SET received_at=? WHERE id=3', (end,))
    original = HostRetrieval.prepare
    observed = []
    def capture(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        observed.append(json.loads(json.dumps(value)))
        return value
    monkeypatch.setattr(HostRetrieval, 'prepare', capture)
    value = client.post('/api/v1/entries/A/prepare', json={'judge': False}).json()
    from iris.service_status import add_health_hints
    before = add_health_hints(observed[0], client.app.state.health)
    assert {k: v for k, v in value.items() if k != 'hints'} == {k: v for k, v in before.items() if k != 'hints'}
    assert value['hints'][:-1] == before['hints']
    assert value['hints'][-1]['time_to'] == end and value['hints'][-1]['time_from'] == STAMP


@pytest.mark.parametrize('supports_entry', [False, True])
def test_v1_search_entry_id_forwarding_and_omission(client, monkeypatch, supports_entry):
    observed = []
    original = HostRetrieval.search
    if supports_entry:
        def search(self, *, entry_id='not-passed', **kwargs):
            observed.append(entry_id)
            return original(self, **kwargs)
    else:
        def search(self, **kwargs):
            assert 'entry_id' not in kwargs
            observed.append('legacy')
            return original(self, **kwargs)
    monkeypatch.setattr(HostRetrieval, 'search', search)
    headers = scoped(client, {'kind': 'entries', 'entries': ['A']})
    assert client.post('/api/v1/memories/search', json={'entry_id': 'A'}, headers=headers).status_code == 200
    assert client.post('/api/v1/memories/search', json={}, headers=headers).status_code == 200
    assert observed == (['A', 'not-passed'] if supports_entry else ['legacy', 'legacy'])
    error(client.post('/api/v1/memories/search', json={'entry_id': 'B'}, headers=headers), 403)
    assert len(observed) == 2


@pytest.mark.parametrize('content_type,data,kind', [
    ('image/png', PNG, 'image'), ('image/jpeg', b'\xff\xd8\xff\xe0image', 'image'),
    ('image/gif', b'GIF89aimage', 'image'), ('image/webp', b'RIFF0000WEBPimage', 'image'),
    ('audio/mpeg', b'ID3audio', 'audio'), ('audio/wav', b'RIFF0000WAVEaudio', 'audio'),
    ('audio/ogg', b'OggSaudio', 'audio'), ('audio/flac', b'fLaCaudio', 'audio'),
    ('video/mp4', b'0000ftypisomvideo', 'video'), ('video/webm', b'\x1aE\xdf\xa3webmvideo', 'video'),
])
def test_v1_media_complete_allowlist_and_placeholders(client, content_type, data, kind):
    response = client.post('/api/v1/media', json={**UPLOAD, 'content_type': content_type,
                                                'data_base64': base64.b64encode(data).decode()})
    assert response.status_code == 201
    assert response.json()['kind'] == kind
    assert response.json()['understanding_source'] == 'unprocessed'
    assert response.json()['understanding_text'] == {'image': '[图片，未理解]', 'audio': '[音频，未理解]',
                                                     'video': '[视频，未理解]'}[kind]


def test_v1_media_unavailable_storage_and_duplicate_ids(client, monkeypatch):
    error(client.post('/api/v1/entries/A/messages', json={**MESSAGE, 'media_ids': ['same', 'same']}),
          400, field='body.Message.media_ids')
    def unavailable(*args, **kwargs):
        raise OSError('/private/storage/location')
    monkeypatch.setattr('iris.api.save_host_media', unavailable)
    response = client.post('/api/v1/media', json=UPLOAD)
    error(response, 503)
    assert '/private/storage' not in response.text


def test_v1_media_grant_survives_service_restart(store):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1') as client:
        client.app.state.scheduler.stop()
        authorize_host(client)
        media_id = client.post('/api/v1/media', json=UPLOAD).json()['id']
        headers = scoped(client, {'kind': 'entries', 'entries': ['A']})
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1') as client:
        client.app.state.scheduler.stop()
        response = client.post('/api/v1/entries/A/messages', json={**MESSAGE, 'media_ids': [media_id]}, headers=headers)
        assert response.status_code == 200


def test_v1_openapi_over_http_after_admin_login(client):
    from conftest import login_admin
    login_admin(client)
    response = client.get('/openapi.json')
    assert response.status_code == 200
    schema = response.json()
    actual = {(method, path) for path, methods in schema['paths'].items() if path.startswith('/api/v1/')
              for method in methods if method in ('get', 'post', 'put', 'patch', 'delete')}
    assert actual == set(OPERATIONS)
    assert schema['info']['version'] == '1.0.0'
    assert 'entry_id' in schema['components']['schemas']['Search']['properties']
    assert 'media_ids' in schema['components']['schemas']['Message']['properties']
