"""Persona HTTP publication, background execution and read-only projections."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, msg
from iris.api import create_app
from iris.db import dumps
from iris.memory_ops import setup_role, edit_memory, delete_memory, manage_memory
from iris.models import ModelError
from iris.persona import current_persona, PersonaEngine, PersonaConflict
from test_persona import FakeGateway, add_self


@pytest.fixture
def client(store):
    setup_role(store, 'Iris', '我来自云城。')
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1234)) as value:
        value.app.state.scheduler.stop()
        login_admin(value)
        value.app.state.persona_jobs.engine.gateway = FakeGateway(store, {
            'sentences': [{'text': '初始设定中，我来自云城。', 'basis': ['M1']}]})
        yield value


def gateway(client):
    return client.app.state.persona_jobs.engine.gateway


def current(client):
    response = client.get('/admin/api/persona')
    assert response.status_code == 200, response.text
    return response.json()


def start(client, expected=None):
    expected = expected if expected is not None else current(client)['current']['id']
    response = client.post('/admin/api/persona/regenerate', json={'expected_version': expected})
    assert response.status_code == 202, response.text
    assert response.json()['accepted'] is True
    return response.headers['location']


def finished(client, url):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(url)
        assert response.status_code == 200, response.text
        result = response.json()
        if result['state'] not in ('queued', 'running'):
            return result
        time.sleep(.005)
    pytest.fail('background persona task did not finish')


def test_s01_initial_current_detail_and_reads_do_not_write(client, store):
    before = store._writer.total_changes
    result = current(client)
    v = result['current']
    assert v['content'] == '我是Iris。初始设定：我来自云城。'
    assert v['status'] == 'current' and v['source'] == 'initial_setting'
    assert v['generated_at'] and not result['needs_update']
    assert result['stale_basis'] == [] and result['stale_basis_count'] == 0
    assert result['pending'] is None and result['latest_attempt'] is None
    detail = client.get(f"/admin/api/persona/versions/{v['id']}").json()
    assert ''.join(s['text'] for s in detail['sentences']) == v['content']
    basis = detail['sentences'][-1]['basis'][0]
    assert basis['memory_id'] == 1 and basis['revision'] == 1
    assert basis['content_at_revision'] == '我来自云城'
    assert basis['memory']['revision'] == 1
    assert detail['checks']['passed'] and detail['checks']['model'] is None
    page = client.get('/admin/api/persona/versions?limit=1').json()
    assert page['total'] == 1 and len(page['items']) == 1 and page['offset'] == 0
    assert client.get('/admin/api/persona/attempts').json()['items'] == []
    assert store._writer.total_changes == before
    assert not gateway(client).calls


@pytest.mark.parametrize('degree,status', [('small', 'current'), ('medium', 'current'), ('large', 'pending')])
def test_s02_s03_background_publication(client, degree, status):
    gateway(client).degree = degree
    old = current(client)['current']
    attempt = finished(client, start(client))
    assert attempt['state'] == status and attempt['stage'] == 'finished'
    assert attempt['base_version_id'] == old['id'] and attempt['finished_at']
    result = current(client)
    assert result['current']['id'] == (attempt['version_id'] if status == 'current' else old['id'])
    v = client.get(f"/admin/api/persona/versions/{attempt['version_id']}").json()
    assert v['change_degree'] == degree and v['checks']['passed']
    assert len(v['checks']['model']['sentences']) == len(v['sentences'])
    assert [p for p, _ in gateway(client).calls] == ['persona_generate', 'persona_check']


def test_s04_rejection_reason_and_previous_preserved(client):
    gateway(client).supported = False
    old = current(client)['current']
    task = finished(client, start(client))
    assert task['state'] == 'rejected'
    detail = client.get(f"/admin/api/persona/versions/{task['version_id']}").json()
    assert detail['rejection_reasons'] and not detail['checks']['passed']
    assert current(client)['current'] == old


@pytest.mark.parametrize('action,reason', [('edit', 'modified'), ('forget', 'forgotten'), ('delete', 'deleted')])
def test_s05_stale_basis_and_prepare_projection(client, store, action, reason):
    old = current(client)['current']
    msg(store, 0, '需要准备回复', entry='A')
    before = client.post('/api/v1/entries/A/prepare', json={'judge': False}).json()
    if action == 'edit':
        assert edit_memory(store, 1, 1, content='我来自海城')
    elif action == 'forget':
        assert manage_memory(store, 1, 1, action='forget')
    else:
        assert delete_memory(store, 1, 1)
    result = current(client)
    assert result['current'] == old
    assert result['needs_update'] and result['stale_basis_count'] == 1
    assert result['stale_basis'][0]['reason'] == reason
    detail = client.get('/admin/api/persona/versions/1').json()
    basis = detail['sentences'][-1]['basis'][0]
    assert basis['revision'] == 1 and basis['content_at_revision'] == '我来自云城'
    after = client.post('/api/v1/entries/A/prepare', json={'judge': False}).json()
    assert after['persona'] == {'version': 1, 'content': old['content'], 'generated_at': old['generated_at'],
                                'needs_update': True, 'stale_basis_count': 1}
    for key in ('recent_messages', 'state', 'goals'):
        assert after[key] == before[key]
    assert not gateway(client).calls


def test_s17_deleted_basis_absent_from_new_generation(client, store):
    delete_memory(store, 1, 1)
    mid = add_self(store, '我在昨晚读书时选择了安静的角落。')
    gateway(client).generated = {'sentences': [{'text': '昨晚读书时，我选择了安静的角落。', 'basis': ['M1']}]}
    task = finished(client, start(client))
    assert task['state'] == 'current'
    payload = gateway(client).calls[0][1]
    assert '我来自云城' not in dumps(payload['evidence'])
    detail = client.get(f"/admin/api/persona/versions/{task['version_id']}").json()
    assert detail['sentences'][0]['basis'][0]['memory_id'] == mid
    assert '我来自云城' not in detail['content'] and not current(client)['needs_update']


def test_s19_manual_deletion_forces_pending_and_confirm_keeps_generation_time(client):
    old = current(client)['current']['id']
    edited = client.put('/admin/api/persona', json={'expected_version': old, 'content': '我习惯以一声问候开场。'}).json()
    assert edited['source'] == 'admin_edit' and edited['sentences'][0]['admin_written']
    task = finished(client, start(client))
    pending = current(client)['pending']
    assert task['state'] == 'pending' and pending['change_degree'] == 'large'
    detail = client.get(f"/admin/api/persona/versions/{pending['id']}").json()
    assert detail['checks']['admin_content_removed_or_changed']
    response = client.post(f"/admin/api/persona/versions/{pending['id']}/confirm", json={'expected_version': edited['id']})
    assert response.status_code == 200
    assert response.json()['generated_at'] == pending['generated_at']
    assert current(client)['current']['id'] == pending['id']
    assert client.post(f"/admin/api/persona/versions/{pending['id']}/confirm", json={'expected_version': edited['id']}).status_code == 409


def test_pending_replacement_rejection_and_admin_edit(client):
    gateway(client).degree = 'large'
    first = finished(client, start(client))['version_id']
    second = finished(client, start(client))['version_id']
    assert client.get(f'/admin/api/persona/versions/{first}').json()['status'] == 'superseded'
    response = client.post(f'/admin/api/persona/versions/{second}/reject', json={'expected_version': 1, 'reason': '暂不采用'})
    assert response.status_code == 200
    assert response.json()['rejection_reasons'] == ['暂不采用']
    third = finished(client, start(client))['version_id']
    old = current(client)['current']['content']
    edit = client.put('/admin/api/persona', json={'expected_version': 1, 'content': old + '我喜欢简洁的开场。'}).json()
    assert [s['admin_written'] for s in edit['sentences']] == [False, False, True]
    assert client.get(f'/admin/api/persona/versions/{third}').json()['status'] == 'superseded'
    assert current(client)['pending'] is None
    assert client.get('/admin/api/persona/versions?status=superseded').json()['total'] == 2


def test_rollback_diff_conflicts_and_admin_audit(client, store):
    edit = client.put('/admin/api/persona', json={'expected_version': 1, 'content': '管理员写入的新句子。'}).json()
    diff = client.get('/admin/api/persona/diff', params={'before_version': 1, 'after_version': edit['id']}).json()
    assert diff['changes'] and diff['before_version'] == 1
    rollback = client.post('/admin/api/persona/versions/1/rollback', json={'expected_version': edit['id']})
    assert rollback.status_code == 200
    v = rollback.json()
    assert v['id'] > edit['id'] and v['rollback_of'] == 1 and v['source'] == 'rollback'
    assert client.get(f"/admin/api/persona/versions/{edit['id']}").json()['status'] == 'history'
    for path, body in [('/admin/api/persona', {'content': '冲突句。'}), ('/admin/api/persona/versions/1/rollback', {}),
                       ('/admin/api/persona/regenerate', {})]:
        method = client.put if path == '/admin/api/persona' else client.post
        assert method(path, json={'expected_version': 1, **body}).status_code == 409
    with store.read() as conn:
        operations = [dict(r) for r in conn.execute("SELECT * FROM admin_operations WHERE object_type='persona' AND actor='admin'")]
    assert {r['action'] for r in operations} >= {'persona_edit', 'persona_rollback'}
    assert all('管理员写入' not in r['details_json'] for r in operations)


def test_self_memory_filter_and_pagination(client, store):
    included = add_self(store, '我在一场直播中选择慢慢讲。', pinned=1)
    add_self(store, '观众认为我慢条斯理', speaker='audience')
    add_self(store, '我谈到其他人的事情', about=False)
    setting = add_self(store, '设定里我是邮差', speaker='author', stance='设定')
    gone = add_self(store, '已遗忘')
    manage_memory(store, gone, 1, action='forget')
    deleted = add_self(store, '已删除')
    delete_memory(store, deleted, 1)
    page = client.get('/admin/api/persona/self-memories?limit=1').json()
    assert page['total'] == 3 and page['items'][0]['id'] == included
    rest = client.get('/admin/api/persona/self-memories?limit=100&offset=1').json()
    assert {m['id'] for m in rest['items']} == {1, setting}


def test_regenerate_returns_202_while_model_blocked_and_rejects_second_task(client, store):
    entered, release = threading.Event(), threading.Event()
    def block():
        assert not store._lock._is_owned()
        entered.set()
        assert release.wait(10)
    gateway(client).callback = block
    try:
        url = start(client)
        assert entered.wait(1)
        progress = client.get(url).json()
        assert progress['state'] == 'running' and progress['stage'] == 'generating'
        assert client.post('/admin/api/persona/regenerate', json={'expected_version': 1}).status_code == 409
        assert current(client)['current']['id'] == 1
        # Other writes proceed; completion must not overwrite this publication.
        response = client.put('/admin/api/persona', json={'expected_version': 1, 'content': '管理员在生成期间发布。'})
        assert response.status_code == 200
    finally:
        release.set()
    result = finished(client, url)
    assert result['state'] == 'conflict' and result['reason'] == 'current_version_changed'
    assert current(client)['current']['content'] == '管理员在生成期间发布。'
    assert len(client.get('/admin/api/persona/attempts').json()['items']) == 1


def test_simultaneous_regenerate_requests_reserve_one_task(client):
    entered, release = threading.Event(), threading.Event()
    gateway(client).callback = lambda: (entered.set(), release.wait(10))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(client.post, '/admin/api/persona/regenerate', json={'expected_version': 1}) for _ in range(2)]
            responses = [f.result(timeout=3) for f in requests]
        assert sorted(r.status_code for r in responses) == [202, 409]
        assert entered.wait(1)
    finally:
        release.set()
    accepted = next(r for r in responses if r.status_code == 202)
    assert finished(client, accepted.headers['location'])['state'] == 'current'


@pytest.mark.parametrize('kind,state,reason', [('paused', 'skipped', 'daily_token_limit'), ('error', 'failed', 'unexpected_error')])
def test_background_failures_and_pauses_are_queryable_without_exception_text(client, kind, state, reason):
    def fail():
        if kind == 'paused':
            raise ModelError('paused', 'must not echo provider detail', paused=True, reason='daily_token_limit')
        raise RuntimeError('must not echo provider detail')
    gateway(client).callback = fail
    result = finished(client, start(client))
    assert result['state'] == state and result['reason'] == reason
    assert 'must not echo' not in dumps(result) and 'outputs' not in result
    assert current(client)['current']['id'] == 1
    assert finished(client, start(client))['state'] == 'current'


def test_startup_marks_interrupted_attempt_failed_and_keeps_old_version(store):
    setup_role(store, 'Iris', '我来自云城。')
    with store.write() as conn:
        conn.execute("""INSERT INTO persona_attempts(source,base_version_id,state,material_json,created_at)
            VALUES('regenerate',1,'running','{}','2026-10-01T00:00:00Z')""")
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        result = client.get('/admin/api/persona/attempts/1').json()
        assert result['state'] == 'failed' and result['reason'] == 'interrupted'
        assert current(client)['current']['id'] == 1


@pytest.mark.parametrize('path', ['/persona', '/persona/versions', '/persona/versions/1', '/persona/attempts',
                                  '/persona/self-memories', '/persona/diff?before_version=1&after_version=1'])
def test_persona_reads_require_admin_session(client, path):
    client.cookies.clear()
    assert client.get('/admin/api' + path).status_code == 401


@pytest.mark.parametrize('path,method,extra', [('/persona', 'put', {'content': '新句。'}),
    ('/persona/regenerate', 'post', {}), ('/persona/versions/1/confirm', 'post', {}),
    ('/persona/versions/1/reject', 'post', {}), ('/persona/versions/1/rollback', 'post', {})])
def test_persona_writes_require_csrf(client, path, method, extra):
    del client.headers['X-Iris-CSRF']
    assert getattr(client, method)('/admin/api' + path, json={'expected_version': 1, **extra}).status_code == 403


@pytest.mark.parametrize('body', [{}, {'expected_version': True}, {'expected_version': '1'}, {'expected_version': 0},
                                 {'expected_version': 1, 'unexpected': True}])
def test_regenerate_rejects_invalid_bodies(client, body):
    assert client.post('/admin/api/persona/regenerate', json=body).status_code == 400


def test_missing_versions_and_invalid_edit(client):
    for path in ('/admin/api/persona/versions/999', '/admin/api/persona/attempts/999',
                 '/admin/api/persona/diff?before_version=1&after_version=999'):
        assert client.get(path).status_code == 404
    for content in (' ', 'x'*801, '遗留 M1 编号。'):
        assert client.put('/admin/api/persona', json={'expected_version': 1, 'content': content}).status_code == 400
    assert client.post('/admin/api/persona/versions/999/rollback', json={'expected_version': 1}).status_code == 404


def test_checking_progress_and_evidence_conflict(client, store):
    original = gateway(client).chat
    entered, release = threading.Event(), threading.Event()
    def chat(messages, purpose, **kwargs):
        if purpose == 'persona_check':
            assert not store._lock._is_owned()
            entered.set()
            assert release.wait(10)
        return original(messages, purpose, **kwargs)
    gateway(client).chat = chat
    try:
        url = start(client)
        assert entered.wait(1)
        assert client.get(url).json()['stage'] == 'checking'
        edit_memory(store, 1, 1, content='我来自海城')
    finally:
        release.set()
    result = finished(client, url)
    assert result['state'] == 'conflict' and result['reason'] == 'evidence_changed'
    assert result['version_id'] is None and current(client)['current']['id'] == 1


def test_queued_task_freezes_settings_and_excludes_second_sync_generation(client, store):
    release = threading.Event()
    jobs = client.app.state.persona_jobs
    jobs._executor.submit(release.wait, 10)
    old_rules = store.setting('persona_rules')
    try:
        url = start(client)
        assert client.get(url).json()['state'] == 'queued'
        from iris.persona import PersonaBusy
        with pytest.raises(PersonaBusy):
            PersonaEngine(store, gateway(client)).regenerate(expected_version=1)
        store.set_setting('persona_publish_mode', 'all_manual')
        store.set_setting('persona_rules', '之后候选的新监管要求。')
    finally:
        release.set()
    assert finished(client, url)['state'] == 'current'
    assert gateway(client).calls[0][1]['rules'] == old_rules
    assert finished(client, start(client))['state'] == 'pending'
    assert gateway(client).calls[-1][1]['rules'] == '之后候选的新监管要求。'


def test_pending_confirmation_rechecks_evidence(client, store):
    gateway(client).degree = 'large'
    vid = finished(client, start(client))['version_id']
    edit_memory(store, 1, 1, content='我来自海城')
    response = client.post(f'/admin/api/persona/versions/{vid}/confirm', json={'expected_version': 1})
    assert response.status_code == 409
    assert current(client)['current']['id'] == 1
    assert current(client)['pending']['id'] == vid


def test_no_self_evidence_skips_without_model_calls(client, store):
    delete_memory(store, 1, 1)
    result = finished(client, start(client))
    assert result['state'] == 'skipped' and result['reason'] == 'no_self_evidence'
    assert result['version_id'] is None and not gateway(client).calls


def test_prepare_uninitialized_persona_is_explicit_and_does_not_create_version(store):
    from iris.retrieval import Retrieval
    msg(store, 0, '准备', entry='A')
    result = Retrieval(store).prepare('A', judge=False)
    assert result['persona'] == {'version': None, 'content': '', 'generated_at': None,
                                 'needs_update': False, 'stale_basis_count': 0}
    assert current_persona(store) is None


def test_persona_shutdown_waits_for_accepted_task_before_closing_store(store):
    from iris.persona import PersonaJobs
    setup_role(store, 'Iris', '我来自云城。')
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    fake = FakeGateway(store, {'sentences': [{'text': '初始设定中，我来自云城。', 'basis': ['M1']}]},
                       callback=lambda: (entered.set(), release.wait(10)))
    jobs = PersonaJobs(store, fake)
    jobs.submit(expected_version=1)
    assert entered.wait(1)
    closer = threading.Thread(target=lambda: (jobs.close(), closed.set()))
    closer.start()
    try:
        assert not closed.wait(.05)
    finally:
        release.set()
        closer.join(3)
    assert closed.is_set()
    assert current_persona(store)['id'] == 2


def test_persona_settings_partial_updates_are_audited_without_republishing(client, store):
    from iris.persona import DEFAULT_GOAL, DEFAULT_RULES
    defaults = {'goal': DEFAULT_GOAL, 'rules': DEFAULT_RULES, 'publish_mode': 'small_medium_auto'}
    assert client.get('/admin/api/settings').json()['persona'] == defaults
    before = current(client)
    response = client.patch('/admin/api/settings/persona', json={'goal': '  保持简洁的自我描述。  '})
    assert response.status_code == 200
    assert response.json()['persona'] == {**defaults, 'goal': '保持简洁的自我描述。'}
    response = client.patch('/admin/api/settings/persona', json={'publish_mode': 'all_manual'})
    assert response.status_code == 200
    assert response.json()['persona'] == {**defaults, 'goal': '保持简洁的自我描述。', 'publish_mode': 'all_manual'}
    assert current(client) == before and not gateway(client).calls
    with store.read() as conn:
        rows = conn.execute("SELECT actor,object_type,object_id,details_json FROM admin_operations WHERE action='persona_settings_saved' ORDER BY id").fetchall()
    assert [json.loads(r['details_json']) for r in rows] == [{'fields': ['goal']}, {'fields': ['publish_mode']}]
    assert all((r['actor'], r['object_type'], r['object_id']) == ('admin', 'settings', 'persona') for r in rows)


@pytest.mark.parametrize('body', [{}, {'goal': ''}, {'goal': '  '}, {'goal': 'x'*4001},
    {'rules': '\n\t'}, {'rules': 'x'*16001}, {'rules': None}, {'goal': True},
    {'publish_mode': 'automatic'}, {'publish_mode': None}, {'unexpected': True}])
def test_persona_settings_reject_invalid_changes_without_writes(client, store, body):
    before = client.get('/admin/api/settings').json()['persona']
    changes = store._writer.total_changes
    assert client.patch('/admin/api/settings/persona', json=body).status_code == 400
    assert client.get('/admin/api/settings').json()['persona'] == before
    assert store._writer.total_changes == changes


@pytest.mark.parametrize('session', [True, False])
def test_persona_settings_require_session_and_csrf(client, session):
    if session:
        del client.headers['X-Iris-CSRF']
    else:
        client.cookies.clear()
    assert client.patch('/admin/api/settings/persona', json={'goal': '保持稳定。'}).status_code == (403 if session else 401)


def test_persona_settings_only_affect_future_candidates_and_keep_pending_checks(client):
    gateway(client).degree = 'large'
    vid = finished(client, start(client))['version_id']
    detail_url = f'/admin/api/persona/versions/{vid}'
    old = client.get(detail_url).json()
    response = client.patch('/admin/api/settings/persona', json={'publish_mode': 'all_auto', 'rules': '新的监管要求。'})
    assert response.status_code == 200
    assert client.get(detail_url).json() == old
    assert current(client)['current']['id'] == 1 and current(client)['pending']['id'] == vid
    confirmed = client.post(detail_url + '/confirm', json={'expected_version': 1})
    assert confirmed.status_code == 200
    assert confirmed.json()['settings'] == old['settings']
    assert finished(client, start(client))['state'] == 'current'
    assert gateway(client).calls[-1][1]['rules'] == '新的监管要求。'


def test_persona_settings_do_not_change_an_inflight_generation(client):
    entered, release = threading.Event(), threading.Event()
    gateway(client).callback = lambda: (entered.set(), release.wait(10))
    old = client.get('/admin/api/settings').json()['persona']
    try:
        url = start(client)
        assert entered.wait(1)
        response = client.patch('/admin/api/settings/persona', json={'publish_mode': 'all_manual', 'rules': '以后逐句审核。'})
        assert response.status_code == 200
    finally:
        release.set()
    result = finished(client, url)
    assert result['state'] == 'current'
    detail = client.get(f"/admin/api/persona/versions/{result['version_id']}").json()
    assert all(detail['settings'][key] == value for key, value in old.items())
    assert gateway(client).calls[-1][1]['rules'] == old['rules']
    assert finished(client, start(client))['state'] == 'pending'
