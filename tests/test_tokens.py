"""Host credentials are issued once, hashed, revocable and scoped before routing."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, msg
from iris.api import create_app
from iris.cli import main
from iris.process_lock import StoreLease


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as value:
        value.app.state.scheduler.stop()
        login_admin(value)
        yield value


def issue(client, host='astrbot', scope=None):
    response = client.post('/admin/api/tokens', json={'host': host, 'scope': scope or {'kind': 'all'}})
    assert response.status_code == 201, response.text
    value = response.json()
    return value, {'Authorization': 'Bearer ' + value['token']}


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/v1/entries/A/messages'), ('POST', '/api/v1/entries/A/prepare'),
    ('POST', '/api/v1/entries/A/learn'), ('POST', '/api/v1/memories/search'),
    ('POST', '/api/v1/feedback'), ('GET', '/api/v1/status'), ('GET', '/api/v1/state'),
    ('PUT', '/api/v1/state'), ('PATCH', '/api/v1/state'), ('DELETE', '/api/v1/state'),
    ('GET', '/api/v1/goals'), ('POST', '/api/v1/goals'), ('PATCH', '/api/v1/goals/1'),
    ('GET', '/api/v1/notifications'), ('GET', '/api/v1/future-route'),
])
def test_every_host_route_requires_bearer_before_body_validation(client, method, path):
    response = client.request(method, path, content='{')
    assert response.status_code == 401
    assert response.headers['WWW-Authenticate'] == 'Bearer'
    assert response.json()['error']['code'] == 'invalid_token'


@pytest.mark.parametrize('authorization', ['', 'Basic invalid', 'Bearer invalid', 'Bearer', 'Bearer a b'])
def test_invalid_tokens_and_admin_cookie_do_not_authorize_host(client, authorization):
    assert client.get('/api/v1/status', headers={'Authorization': authorization}).status_code == 401


def test_one_time_issue_hash_only_audit_last_used_and_immediate_revoke(client, store, caplog):
    value, headers = issue(client)
    token = value['token']
    assert client.get('/api/v1/status', headers=headers).status_code == 200
    listing = client.get('/admin/api/tokens').json()['items']
    assert listing[0]['last_used_at'] and listing[0]['host'] == 'astrbot'
    assert all(k not in listing[0] for k in ('token', 'token_hash', 'salt'))
    assert client.post(f"/admin/api/tokens/{value['id']}/revoke", json={}).status_code == 200
    assert client.get('/api/v1/status', headers=headers).status_code == 401
    assert client.post(f"/admin/api/tokens/{value['id']}/revoke", json={}).status_code == 200
    with store.read() as conn:
        dump = '\n'.join(conn.iterdump())
        row = conn.execute('SELECT * FROM host_tokens').fetchone()
        assert len(row['token_hash']) == 64 and len(row['salt']) >= 32
        actions = [r[0] for r in conn.execute("SELECT action FROM admin_operations WHERE object_type='host_token'")]
    assert actions == ['token_create', 'token_revoke']
    assert token not in dump and token not in caplog.text
    assert token.encode() not in store.path.read_bytes()
    assert 'no-store' in client.get('/admin/api/tokens').headers['Cache-Control']


@pytest.mark.parametrize('scope,allowed,denied', [
    ({'kind': 'entries', 'entries': ['A', '中文:1']}, ['A', '中文:1'], ['a', 'AB', 'B']),
    ({'kind': 'prefix', 'prefix': 'bot:%_'}, ['bot:%_1', 'bot:%_'], ['bot:abc', 'BOT:%_1', 'bot:%']),
])
def test_R03_literal_list_prefix_scope_covers_reads_and_writes(client, store, scope, allowed, denied):
    _, headers = issue(client, scope=scope)
    for i, entry in enumerate(allowed + denied):
        msg(store, i, '范围测试原始消息', entry=entry)
    for entry in allowed:
        assert client.post(f'/api/v1/entries/{entry}/prepare', json={'judge': False}, headers=headers).status_code == 200
    for entry in denied:
        for suffix in ('prepare', 'learn', 'messages'):
            response = client.post(f'/api/v1/entries/{entry}/{suffix}', json={}, headers=headers)
            assert response.status_code == 403, response.text
            assert '范围测试原始消息' not in response.text
    status = client.get('/api/v1/status', headers=headers).json()
    assert {r['entry_id'] for r in status['entries']} == set(allowed)


@pytest.mark.parametrize('scope', [
    {'kind': 'all', 'entries': ['A']}, {'kind': 'prefix', 'prefix': ''},
    {'kind': 'entries', 'entries': []}, {'kind': 'entries', 'entries': ['A', 'A']},
    {'kind': 'entries', 'entries': [' ']}, {'kind': 'unknown'}, {'kind': 'all', 'extra': True},
])
def test_invalid_scope_rejected_atomically(client, store, scope):
    assert client.post('/admin/api/tokens', json={'host': 'bot', 'scope': scope}).status_code == 400
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM host_tokens').fetchone()[0] == 0


def test_token_bucket_is_per_token_refills_and_is_atomic(client, store):
    from iris.tokens import Tokens, TokenError
    first, headers = issue(client)
    other, other_headers = issue(client)
    store.set_setting('host_tokens', {'rate_per_second': 2, 'burst': 3})
    moment = [0.0]
    service = Tokens(store, clock=lambda: moment[0])
    client.app.state.tokens = service
    assert [client.get('/api/v1/status', headers=headers).status_code for _ in range(3)] == [200]*3
    rejected = client.get('/api/v1/status', headers=headers)
    assert rejected.status_code == 429 and rejected.headers['Retry-After'] == '1'
    assert client.get('/api/v1/status', headers=other_headers).status_code == 200
    moment[0] = .5
    assert client.get('/api/v1/status', headers=headers).status_code == 200
    moment[0] = 10
    def attempt(_):
        try:
            service.authenticate(headers['Authorization'])
            return 200
        except TokenError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(attempt, range(10)))
    assert statuses.count(200) == 3 and statuses.count(429) == 7


def test_host_name_and_compatibility_claim_are_not_spoofable(client, store):
    _, headers = issue(client, host='bound-host')
    response = client.put('/api/v1/state', json={'activity': '游戏', 'host': 'spoof'}, headers=headers)
    assert response.status_code == 200 and response.json()['host'] == 'bound-host'
    client.patch('/api/v1/state', json={'host': 'spoof'}, headers=headers)
    client.request('DELETE', '/api/v1/state', json={'host': 'spoof'}, headers=headers)
    msg(store, 1, '待学习', entry='A')
    client.post('/api/v1/entries/A/learn', headers=headers)
    with store.read() as conn:
        assert {r[0] for r in conn.execute('SELECT host FROM state_reports')} == {'bound-host'}
        assert conn.execute("SELECT actor FROM admin_operations WHERE action='learn_requested'").fetchone()[0] == 'bound-host'


def test_cli_create_list_revoke_and_store_lease(tmp_path, capsys):
    db = tmp_path / 'offline.db'
    args = ['--db', str(db), 'tokens']
    assert main(args + ['create', '--host', 'bot', '--prefix', 'bot:']) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued['token']
    assert main(args + ['list']) == 0
    assert issued['token'] not in capsys.readouterr().out
    with StoreLease(db):
        assert main(args + ['list']) == 1
        assert '请先停止服务' in capsys.readouterr().err
    assert main(args + ['revoke', issued['id']]) == 0
    assert issued['token'] not in capsys.readouterr().out


def test_admin_token_management_requires_session_and_csrf(client):
    assert client.post('/admin/api/tokens', json={'host': 'bot', 'scope': {'kind': 'all'}},
                       headers={'X-Iris-CSRF': ''}).status_code == 403
    _, headers = issue(client)
    client.cookies.clear()
    assert client.get('/admin/api/tokens', headers=headers).status_code == 401


def test_feedback_is_bound_to_issuing_token_and_audited_with_host(client, store):
    from test_retrieval import put
    mid = put(store, '天文摄影')
    _, first = issue(client, host='first')
    _, second = issue(client, host='second')
    recall = client.post('/api/v1/memories/search', json={'text': '天文摄影'}, headers=first).json()
    payload = {'recall_id': recall['recall_id'], 'memory_ids': [mid]}
    assert client.post('/api/v1/feedback', json=payload, headers=second).status_code == 403
    assert client.post('/api/v1/feedback', json=payload, headers=first).status_code == 200
    with store.read() as conn:
        assert conn.execute("SELECT actor FROM admin_operations WHERE action='feedback'").fetchone()[0] == 'first'


def test_goal_host_name_namespaces_idempotency_and_cannot_be_spoofed(client, store):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    _, first = issue(client, host='first')
    _, second = issue(client, host='second')
    payload = {'content': '周一买书', 'host_key': 'same-key', 'host': 'spoof'}
    a = client.post('/api/v1/goals', json=payload, headers=first)
    b = client.post('/api/v1/goals', json={**payload, 'content': '周二买茶'}, headers=second)
    assert a.status_code == b.status_code == 201
    assert a.json()['submitted_id'] != b.json()['submitted_id']
    assert a.json()['goal']['host'] == 'first' and b.json()['goal']['host'] == 'second'
    retry = client.post('/api/v1/goals', json={**payload, 'content': '不得替换'}, headers=first)
    assert retry.json() == a.json()
    with store.read() as conn:
        assert {r[0] for r in conn.execute("SELECT actor FROM admin_operations WHERE action='goal_create'")} == {'first', 'second'}


def test_durable_credentials_restart_and_no_secret_in_serve_logs(tmp_path):
    import httpx
    from iris.e2e_evaluation import ServeProcess
    service = ServeProcess(tmp_path / 'server', {})
    try:
        service.start()
        secret = service._token
        assert service.client.get('/api/v1/status').status_code == 200
        assert httpx.get(str(service.client.base_url) + '/api/v1/status', trust_env=False).status_code == 401
        service.stop()
        service.start()
        assert service._token == secret
        assert service.client.get('/api/v1/status').status_code == 200
    finally:
        service.stop()
    for path in service.directory.rglob('*'):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path.name


@pytest.mark.parametrize('scope', [
    {'kind': 'entries', 'entries': ['A']}, {'kind': 'prefix', 'prefix': 'A'},
])
def test_goal_scope_filters_before_pagination_ranking_and_taking(client, store, scope):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    for i, entry in enumerate(['A', 'B']):
        msg(store, i, '入口消息', entry=entry)
    goals = client.app.state.goals
    global_goal = goals.create(content='阅读通用手册', origin='admin')['goal']
    own = goals.create(content='整理书架', entry_id='A', origin='admin')['goal']
    hidden = goals.create(content='整理书架', entry_id='B', origin='admin')['goal']
    # Hidden urgent goals must not consume the visible partition's limit.
    for i in range(11):
        goals.create(content=f'秘密工作项目 {i}', entry_id='B', origin='admin', deadline='2020-01-01')
    _, headers = issue(client, scope=scope)
    listing = client.get('/api/v1/goals?limit=1', headers=headers).json()
    assert listing['total'] == 2 and listing['items'][0]['id'] == own['id']
    assert listing['items'][0]['possible_duplicate_ids'] == []
    for path, payload in [('/api/v1/entries/A/prepare', {'judge': False, 'goal_limit': 2}),
                          ('/api/v1/memories/search', {'include_goals': True})]:
        result = client.post(path, json=payload, headers=headers)
        assert result.status_code == 200, result.text
        assert {r['id'] for r in result.json()['goals']} == {own['id'], global_goal['id']}
        assert all(r['possible_duplicate_ids'] == [] for r in result.json()['goals'])
    assert client.get('/api/v1/goals?entry_id=B', headers=headers).status_code == 403
    response = client.patch(f"/api/v1/goals/{hidden['id']}", json={'state': 'completed'}, headers=headers)
    assert response.status_code == 403
    assert goals.get(hidden['id'])['state'] == 'open'
    # Place hidden notifications first so a post-LIMIT filter cannot pass.
    with store.write() as conn:
        conn.execute('DELETE FROM notifications')
        for gid in [hidden['id'], own['id'], global_goal['id']]:
            conn.execute("INSERT INTO notifications(kind,goal_id,scheduled_at,published_at,content) VALUES('goal_dedup_result',?,'2026-10-10','2026-10-10','复核结果')", (gid,))
    first = client.get('/api/v1/notifications?limit=1', headers=headers).json()
    assert [n['goal_id'] for n in first['items']] == [own['id']] and first['has_more']
    second = client.get('/api/v1/notifications', params={'after': first['next_cursor'], 'limit': 1}, headers=headers).json()
    assert [n['goal_id'] for n in second['items']] == [global_goal['id']] and not second['has_more']
    with store.read() as conn:
        assert conn.execute('SELECT status FROM notifications WHERE goal_id=?', (hidden['id'],)).fetchone()[0] == 'pending'


def test_host_goal_dedup_and_idempotent_receipts_cannot_cross_scope(client, store):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    for i, entry in enumerate(['A', 'B']):
        msg(store, i, '入口消息', entry=entry)
    _, broad = issue(client, host='bot')
    _, narrow = issue(client, host='bot', scope={'kind': 'entries', 'entries': ['A']})
    hidden = client.post('/api/v1/goals', json={'content': '整理操作手册', 'entry_id': 'B', 'host_key': 'key'}, headers=broad).json()
    result = client.post('/api/v1/goals', json={'content': '整理操作手册', 'entry_id': 'A'}, headers=narrow).json()
    assert result['dedup']['status'] == 'created'
    assert result['goal']['id'] != hidden['goal']['id']
    replay = client.post('/api/v1/goals', json={'content': '重试', 'entry_id': 'A', 'host_key': 'key'}, headers=narrow)
    assert replay.status_code == 403 and '整理操作手册' not in replay.text
    # A later administrator merge must not expose its out-of-scope redirect.
    with store.write() as conn:
        conn.execute('UPDATE goals SET merged_into=?,revision=revision+1 WHERE id=?', (hidden['goal']['id'], result['goal']['id']))
    assert client.patch(f"/api/v1/goals/{result['goal']['id']}", json={'state': 'completed'}, headers=narrow).status_code == 403


@pytest.mark.parametrize('scope_data', [
    {'kind': 'entries', 'entries': ['A']}, {'kind': 'prefix', 'prefix': 'A'},
])
def test_goal_review_persists_scope_across_restart_and_rechecks_before_write(client, store, scope_data):
    from iris.goals import Goals
    from iris.models import ModelError
    from iris.tokens import Scope
    from test_goal_dedup_apply import Judge
    for i, entry in enumerate(['A', 'B']):
        msg(store, i, '入口消息', entry=entry)
    store.set_setting('goal_dedup_judge', {'enabled': False})
    goals = client.app.state.goals
    hidden = goals.create(content='整理操作手册', entry_id='B', origin='admin')['goal']
    own = goals.create(content='整理操作说明手册', entry_id='A', origin='admin')['goal']
    store.set_setting('goal_dedup_judge', {'enabled': True, 'method': 'C', 'budget_seconds': 1})
    judge = Judge(store, verdict=ModelError('retryable', 'unit timeout', reason='timeout'))
    goals = Goals(store, gateway=judge)
    pending = goals.create(content='整理操作手册说明', entry_id='A', host='bot', actor='bot',
                           scope=Scope.model_validate(scope_data))
    assert pending['dedup']['status'] == 'pending'
    assert judge.calls and all({r['id'] for r in call['candidates']} == {own['id']} for call in judge.calls)
    judge.calls.clear()
    judge.verdict = 'same'
    def move_candidate():
        with store.write() as conn:
            conn.execute('UPDATE goals SET entry_id=?,revision=revision+1 WHERE id=?', ('B', own['id']))
    judge.hook = move_candidate
    restarted = Goals(store, gateway=judge)
    stale = restarted.review(pending['submitted_id'])
    assert stale['dedup']['status'] == 'pending' and stale['dedup']['reason'] == 'stale'
    judge.hook = None
    done = Goals(store, gateway=judge).review(pending['submitted_id'])
    assert done['dedup']['status'] == 'created'
    assert done['goal']['id'] == pending['submitted_id']
    assert goals.get(hidden['id'])['merged_into'] is None
    with store.read() as conn:
        actors = {r[0] for r in conn.execute("SELECT actor FROM admin_operations WHERE action='goal_dedup_judged' AND object_id=?", (str(pending['submitted_id']),))}
    assert actors == {'bot'}


def test_auth_database_failure_is_sanitized_503(client, monkeypatch):
    import sqlite3
    _, headers = issue(client)
    def unavailable(_):
        raise sqlite3.OperationalError('private connection details')
    monkeypatch.setattr(client.app.state.tokens, 'authenticate', unavailable)
    response = client.get('/api/v1/status', headers=headers)
    assert response.status_code == 503 and response.headers['Retry-After'] == '1'
    assert 'private connection details' not in response.text


def test_limits_patch_preserves_omitted_settings_and_rejects_invalid_values(client, store):
    assert client.patch('/admin/api/settings/host-tokens', json={'rate_per_second': 10, 'burst': 100}).status_code == 200
    assert client.patch('/admin/api/settings/host-tokens', json={'burst': 90}).json() == {'rate_per_second': 10, 'burst': 90}
    for payload in [{'rate_per_second': 0}, {'burst': True}, {'burst': 1.5}, {'unknown': 1}]:
        assert client.patch('/admin/api/settings/host-tokens', json=payload).status_code == 400
    assert store.setting('host_tokens') == {'rate_per_second': 10, 'burst': 90}


@pytest.mark.parametrize('scope', [{'kind': 'prefix', 'prefix': 'A\x00B'}, {'kind': 'entries', 'entries': ['A\x00B']}])
def test_scope_rejects_nul_for_consistent_sql_and_literal_matching(client, scope):
    assert client.post('/admin/api/tokens', json={'host': 'bot', 'scope': scope}).status_code == 400


def test_scoped_merge_does_not_rewrite_hidden_related_goals(client, store):
    from iris.goals import _possible
    from iris.tokens import Scope
    for i, entry in enumerate(['A', 'B']):
        msg(store, i, '入口消息', entry=entry)
    store.set_setting('goal_dedup_judge', {'enabled': False})
    goals = client.app.state.goals
    older = goals.create(content='整理操作手册', entry_id='A', origin='admin')['goal']
    hidden = goals.create(content='整理操作说明书', entry_id='B', origin='admin')['goal']
    newer = goals.create(content='整理操作手册', entry_id='A', origin='admin')['goal']
    with store.write() as conn:
        _possible(conn, newer['id'], hidden['id'], goals.clock())
        conn.execute('UPDATE goals SET merged_into=? WHERE id=?', (newer['id'], hidden['id']))
        before = dict(conn.execute('SELECT * FROM goals WHERE id=?', (hidden['id'],)).fetchone())
    # Exercise a deferred merge whose source already has cross-scope links.
    from iris.goals import _merge, _row
    with store.write() as conn:
        _merge(conn, _row(conn, older['id']), _row(conn, newer['id']), goals.clock(),
               actor='bot', scope=Scope(kind='prefix', prefix='A'))
        assert dict(conn.execute('SELECT * FROM goals WHERE id=?', (hidden['id'],)).fetchone()) == before
        assert conn.execute('SELECT status FROM goal_duplicates WHERE goal_a=? AND goal_b=?',
                            tuple(sorted((newer['id'], hidden['id'])))).fetchone()[0] == 'possible'
