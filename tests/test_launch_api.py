"""Launch review regressions: public HTTP boundaries and scoped projections."""
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from conftest import authorize_host, login_admin, msg
from iris.api import create_app
from iris.configuration import RuntimeConfig
from iris.models import ModelConfig
from iris.tokens import Scope
from test_host_api_contract import error
from test_retrieval import put

MESSAGE = {'sender': '用户', 'content': '待处理', 'occurred_at': '2026-10-10T10:00:00+08:00', 'dedupe_key': 'one'}


@pytest.fixture
def client(store):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        authorize_host(client)
        yield client


def scoped(client, scope=None):
    issued = client.app.state.tokens.create(host='test-host', scope=scope or {'kind': 'entries', 'entries': ['A']})
    return {'Authorization': 'Bearer ' + issued['token']}


@pytest.mark.parametrize('entry', ['A#x', 'A?x', 'A\tx', 'A\nx', 'A\rx', 'A\x00x', 'A\x1fx', 'A\x7fx', 'A\x85x'])
@pytest.mark.parametrize('route', ['messages', 'prepare', 'learn'])
def test_decoded_entry_id_rejected_before_scope_and_routing(client, store, entry, route):
    response = client.post(f'/api/v1/entries/{quote(entry, safe="")}/{route}', json=MESSAGE if route == 'messages' else {}, headers=scoped(client))
    error(response, 400)
    with store.read() as conn:
        assert not conn.execute('SELECT 1 FROM entries').fetchone()


@pytest.mark.parametrize('entry', ['platform:room:1', '群聊:星空', 'A%literal', 'A+B'])
def test_legal_encoded_entry_ids_use_exact_scope(client, entry):
    headers = scoped(client, {'kind': 'entries', 'entries': [entry]})
    path = f'/api/v1/entries/{quote(entry, safe="")}/messages'
    assert client.post(path, json=MESSAGE, headers=headers).status_code == 200
    error(client.post(path, json=MESSAGE, headers=scoped(client)), 403)


@pytest.mark.parametrize('path,payload', [('/api/v1/memories/search', {'entry_id': 'A#x'}),
    ('/api/v1/state', {'activity': '阅读', 'entry_id': 'A?x'}),
    ('/api/v1/goals', {'content': '读完书', 'entry_id': 'A\nx'}),
    ('/api/v1/media', {'entry_id': 'A\tx', 'content_type': 'image/png', 'data_base64': ''})])
def test_body_entry_id_has_same_validation(client, path, payload):
    response = client.request('PUT' if path.endswith('/state') else 'POST', path, json=payload)
    error(response, 400)


def test_rejected_prepare_never_writes_journal(client, store):
    def count():
        with store.read() as conn:
            return conn.execute("SELECT COUNT(*) FROM runtime_events WHERE event='prepare'").fetchone()[0]
    original = count()
    for headers, status in [({'Host': 'evil.example'}, 400), ({'Authorization': ''}, 401), (scoped(client, {'kind': 'entries', 'entries': ['B']}), 403)]:
        error(client.post('/api/v1/entries/A/prepare', json={}, headers=headers), status)
        assert count() == original
    error(client.post('/api/v1/entries/A/prepare', json={'judge': 42}), 400)
    assert count() == original + 1


def test_anonymous_session_cross_site_bounded_and_expired_cleanup(client, store):
    for headers in [{'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'https://other.example'}]:
        assert client.get('/admin/api/session', headers=headers).status_code == 403
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM admin_sessions').fetchone()[0] == 0
    for _ in range(140):
        client.cookies.clear()
        assert client.get('/admin/api/session').status_code == 200
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM admin_sessions').fetchone()[0] <= 128
    with store.write() as conn:
        conn.execute('UPDATE admin_sessions SET expires_at=?', (time.time()-1,))
    client.cookies.clear()
    assert client.get('/admin/api/session').status_code == 200
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM admin_sessions WHERE expires_at<=?', (time.time(),)).fetchone()[0] == 0


def test_restricted_tokens_read_but_cannot_create_update_or_merge_global_goals(client, store):
    msg(store, 1, '待处理')
    global_goal = client.post('/api/v1/goals', json={'content': '读完天文图册'}).json()['goal']
    headers = scoped(client)
    assert global_goal['id'] in [g['id'] for g in client.get('/api/v1/goals', headers=headers).json()['items']]
    error(client.post('/api/v1/goals', json={'content': '全局写入'}, headers=headers), 403)
    error(client.patch(f"/api/v1/goals/{global_goal['id']}", json={'expected_revision': global_goal['revision'], 'state': 'completed'}, headers=headers), 403)
    entry_goal = client.post('/api/v1/goals', json={'content': '读完天文图册', 'entry_id': 'A'}, headers=headers)
    assert entry_goal.status_code == 201
    assert entry_goal.json()['goal']['id'] != global_goal['id']
    with store.read() as conn:
        row = conn.execute('SELECT revision,state,merged_into FROM goals WHERE id=?', (global_goal['id'],)).fetchone()
        assert tuple(row) == (global_goal['revision'], 'open', None)


@pytest.mark.parametrize('field,limit', [('entry_name', 200), ('entry_kind', 100), ('account_id', 200), ('scene_identity', 200), ('quote_author', 200), ('quote_author_account_id', 200), ('quote_content', 32768), ('occurred_at', 100)])
def test_message_field_bounds_are_atomic(client, store, field, limit):
    response = client.post('/api/v1/entries/A/messages', json=[MESSAGE, {**MESSAGE, 'dedupe_key': 'two', field: 'x' * (limit+1)}])
    assert response.status_code in (400, 413)
    error(response, response.status_code)
    with store.read() as conn:
        assert not conn.execute('SELECT 1 FROM messages').fetchone()


def test_quote_content_utf8_limit(client):
    error(client.post('/api/v1/entries/A/messages', json={**MESSAGE, 'quote_content': '中'*10923}), 413)
    assert client.post('/api/v1/entries/A/messages', json={**MESSAGE, 'quote_content': 'x'*32768}).status_code == 200


@pytest.mark.parametrize('method,path', [('POST','/api/v1/entries/A/prepare'), ('POST','/api/v1/memories/search')])
def test_host_source_metadata_redacted_but_shared_memory_unchanged(client, store, method, path):
    a = msg(store, 1, '喜欢天文摄影', entry='A')
    b = msg(store, 1, '喜欢天文摄影', entry='B')
    mid = put(store, '喜欢天文摄影')
    with store.write() as conn:
        conn.executemany("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,'2026-10-10T00:00:00+00:00')", [(mid,a),(mid,b)])
    payload = {'text': '天文摄影', 'judge': False} if path.endswith('prepare') else {'text': '天文摄影'}
    complete = client.request(method, path, json=payload).json()['memories']
    masked = client.request(method, path, json=payload, headers=scoped(client)).json()['memories']
    assert [m['id'] for m in masked] == [m['id'] for m in complete] == [mid]
    source = next(s for s in masked[0]['sources'] if s['message_id'] == b)
    assert source == {'message_id': b, 'entry_id': None, 'entry_name': None, 'occurred_at': None}
    assert next(s for s in masked[0]['sources'] if s['message_id'] == a)['entry_id'] == 'A'
    assert next(s for s in complete[0]['sources'] if s['message_id'] == b)['entry_id'] == 'B'
    from iris.retrieval import Retrieval
    with store.read() as conn:
        assert next(s for s in Retrieval(store)._hydrate(conn,[mid])[mid]['sources'] if s['message_id'] == b)['entry_id'] == 'B'


@pytest.mark.parametrize('route', ['save', 'test'])
@pytest.mark.parametrize('url', ['https://new.example/v1', 'http://old.example/v1', 'https://old.example:8443/v1'])
def test_model_origin_change_requires_explicit_key(client, store, monkeypatch, route, url):
    login_admin(client)
    RuntimeConfig(store).save({'chat': ModelConfig('https://old.example/v1', 'fake-origin-only', 'stub')})
    client.app.state.gateway.replace_config('chat', RuntimeConfig(store).load()['chat'])
    calls = []
    monkeypatch.setattr('iris.models.Gateway._call', lambda *a, **kw: calls.append(kw) or {})
    payload = {'base_url': url, 'model': 'stub'}
    response = client.put('/admin/api/settings/models/chat', json=payload) if route == 'save' else client.post('/admin/api/settings/models/chat/test', json=payload)
    assert response.status_code == 400
    assert any(f['field'] == 'body.api_key' for f in response.json()['error']['fields'])
    assert not calls
    assert RuntimeConfig(store).load()['chat'].base_url == 'https://old.example/v1'
    assert 'fake-origin-only' not in response.text


def test_same_origin_and_explicit_empty_key_are_supported():
    from iris.settings_api import Model
    saved = ModelConfig('https://old.example/v1', 'fake-origin-only', 'stub')
    assert Model(base_url='https://OLD.example:443/another', model='stub').config(saved,'chat').api_key == saved.api_key
    assert Model(base_url='http://127.0.0.1:11434/v1', model='stub', api_key='').config(saved,'chat').api_key == ''


@pytest.mark.parametrize('path', ['/admin/api/login', '/admin/api/setup/password', '/admin/api/setup/complete'])
@pytest.mark.parametrize('declared', [True, False])
def test_prelogin_body_limit_including_streaming(client, store, path, declared):
    state = client.get('/admin/api/session').json()
    client.headers['X-Iris-CSRF'] = state['csrf_token']
    if path.endswith('/complete'):
        assert client.post('/admin/api/setup/password', json={'password': 'test-only-password'}).status_code == 200
        client.headers['X-Iris-CSRF'] = client.get('/admin/api/session').json()['csrf_token']
    content = b' ' * (16384+1)
    response = client.post(path, content=content if declared else iter([content[:8192],content[8192:]]), headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'payload_too_large'


def test_status_bounds_finished_batches_per_entry(client, store):
    for entry in ('A','B'):
        msg(store, 1, '积压', entry=entry)
    with store.write() as conn:
        for entry in ('A','B'):
            for n in range(26):
                conn.execute("INSERT INTO batches(entry_id,state,prompt_version,created_at,history_ids,target_ids,future_ids) VALUES(?,?,?,?,'[]','[]','[]')", (entry, 'waiting' if n==0 else 'succeeded', 'test', '2026-10-10T00:00:00+00:00'))
    body = client.get('/api/v1/status').json()
    assert len(body['batches']) == 42
    for entry in body['entries']:
        assert entry['current_batch']['state'] == 'waiting'
        assert entry['latest_batch']['id'] == max(b['id'] for b in body['batches'] if b['entry_id']==entry['entry_id'])
    assert len(client.get('/api/v1/status', headers=scoped(client)).json()['batches']) == 21


def test_unready_prepare_does_not_write_journal(client, store):
    with store.read() as conn:
        before = conn.execute("SELECT COUNT(*) FROM runtime_events WHERE event='prepare'").fetchone()[0]
    client.app.state.ready = False
    try:
        error(client.post('/api/v1/entries/A/prepare', json={}), 503)
    finally:
        client.app.state.ready = True
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_events WHERE event='prepare'").fetchone()[0] == before


@pytest.mark.parametrize('kind', ['entries', 'prefix'])
def test_background_goal_review_excludes_global_candidates_and_stale_redirects(client, store, kind):
    from iris.goals import Goals
    from iris.models import ModelError
    from test_goal_dedup_apply import Judge
    msg(store, 1, '待处理')
    goals = client.app.state.goals
    global_goal = goals.create(content='整理操作手册', origin='admin')['goal']
    own = goals.create(content='整理操作说明手册', entry_id='A', origin='admin')['goal']
    store.set_setting('goal_dedup_judge', {'enabled': True, 'method': 'C', 'budget_seconds': 1})
    scope = Scope(kind='entries', entries=('A',)) if kind=='entries' else Scope(kind='prefix', prefix='A')
    judge = Judge(store, verdict=ModelError('retryable', 'test timeout', reason='timeout'))
    pending = Goals(store,gateway=judge).create(content='整理操作手册说明', entry_id='A', host='test-host', host_key='retry-key', scope=scope)
    assert pending['dedup']['status'] == 'pending'
    assert judge.calls and all({r['id'] for r in c['candidates']} == {own['id']} for c in judge.calls)
    judge.calls.clear()
    judge.verdict = 'same'
    result = Goals(store,gateway=judge).review(pending['submitted_id'])
    assert result['goal']['id'] == own['id']
    assert all(global_goal['id'] not in {r['id'] for r in c['candidates']} for c in judge.calls)
    # An administrator can later redirect an entry goal to a global goal.
    with store.write() as conn:
        conn.execute('UPDATE goals SET merged_into=? WHERE id=?', (global_goal['id'], own['id']))
    headers = scoped(client, scope.model_dump())
    error(client.patch(f"/api/v1/goals/{own['id']}", json={'state': 'completed'}, headers=headers), 403)
    error(client.post('/api/v1/goals', json={'content': '重试', 'entry_id': 'A', 'host_key': 'retry-key'}, headers=headers), 403)


def test_status_usage_windows_match_previous_aggregation_and_calls_keep_history(client, store, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from iris.model_health import usage_window
    from iris.service_status import service_status
    current = datetime(2026,10,10,12,tzinfo=timezone.utc)
    start,_ = usage_window(store,current)
    store.set_setting('timezone','Asia/Shanghai')
    msg(store,1,'待处理')
    with store.write() as conn:
        for n in range(25):
            conn.execute("INSERT INTO batches(entry_id,state,prompt_version,created_at,history_ids,target_ids,future_ids) VALUES('A','succeeded','test',?,'[]','[]','[]')",(current.isoformat(),))
        for offset,prompt,completion,category in [(0,10,3,'success'),(-1,None,None,'retryable'),(-86400,4,None,'success'),(-604800,100,100,'success'),(60,200,200,'success')]:
            conn.execute("INSERT INTO model_calls(purpose,model,duration_ms,prompt_tokens,completion_tokens,result_category,created_at,batch_id) VALUES('learning','fake',1,?,?,?,?,1)", (prompt,completion,category,(start+timedelta(seconds=offset)).isoformat() if offset!=60 else (current+timedelta(seconds=60)).isoformat()))
    result = service_status(store,client.app.state.scheduler,client.app.state.health,clock=lambda:current)
    assert result['usage']['today']['calls'] == 1
    assert result['usage']['today']['tokens'] == 13
    assert result['usage']['week']['calls'] == 3
    assert result['usage']['week']['tokens'] == 17
    assert result['usage']['week']['calls_without_usage'] == 1
    assert result['usage']['week']['failure_rate'] == 1/3
    # Freeze the HTTP clock too; the oldest batch is absent but its calls stay.
    import iris.api as api
    original = api.service_status
    monkeypatch.setattr(api,'service_status',lambda *args: original(*args,clock=lambda:current))
    result = client.get('/api/v1/status',headers=scoped(client)).json()
    assert 1 not in {b['id'] for b in result['batches']}
    assert result['learning_calls_24h'] and {c['batch_id'] for c in result['learning_calls_24h']} == {1}


@pytest.mark.parametrize('scope', [{'kind': 'entries', 'entries': ['A#legacy']}, {'kind': 'prefix', 'prefix': 'A?legacy'}])
def test_legacy_scope_with_now_forbidden_characters_is_invalid_token(client, store, scope):
    from iris.db import dumps
    issued = client.app.state.tokens.create(host='test-host', scope={'kind': 'all'})
    with store.write() as conn:
        conn.execute('UPDATE host_tokens SET scope_json=? WHERE id=?', (dumps(scope), issued['id']))
    error(client.get('/api/v1/status', headers={'Authorization': 'Bearer '+issued['token']}), 401)


def test_source_redaction_happens_after_prepare_token_budget(client, store):
    from iris.db import dumps
    from iris.queue import estimate_tokens
    msg(store, 1, '待处理', entry='A')
    source_id = msg(store, 1, '喜欢天文摄影', entry='B')
    mid = put(store, '喜欢天文摄影')
    with store.write() as conn:
        conn.execute('UPDATE entries SET name=? WHERE id=?', ('长'*180, 'B'))
        conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,'2026-10-10T00:00:00+00:00')", (mid,source_id))
    payload = {'text': '天文摄影', 'judge': False}
    complete = client.post('/api/v1/entries/A/prepare', json=payload).json()['memories']
    assert [m['id'] for m in complete] == [mid]
    budget = estimate_tokens(dumps(complete))-50
    assert 1 <= budget < 1500
    payload['token_budget'] = budget
    original = client.post('/api/v1/entries/A/prepare', json=payload).json()['memories']
    restricted = client.post('/api/v1/entries/A/prepare', json=payload, headers=scoped(client)).json()['memories']
    assert original == []
    assert restricted == original
