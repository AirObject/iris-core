"""Entry-local UI projections, optimistic writes and durable background tasks."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris.entry_profile import EntryProfileJobs, current_profile
from test_entry_profile import Clock, Model, TEXTS, messages

BASE = '/admin/api/entries/group/profile'


@pytest.fixture
def client(store):
    clock = Clock()
    messages(store, clock)
    messages(store, clock, entry='other')
    messages(store, clock, entry='private', kind='private')
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        engine = client.app.state.entry_profile_jobs.engine
        engine.gateway, engine.clock = Model(store), clock
        yield client


def engine(client):
    return client.app.state.entry_profile_jobs.engine


def start(client, expected=0):
    response = client.post(BASE + '/regenerate', json={'expected_version': expected})
    assert response.status_code == 202, response.text
    assert response.json()['accepted']
    return response.headers['location']


def finished(client, path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = client.get(path).json()
        if value['state'] != 'running':
            assert value['stage'] == 'finished'
            return value
        time.sleep(.005)
    pytest.fail('entry profile task did not finish')


def test_read_only_snapshot_history_and_evidence(client, store):
    before = store._writer.total_changes
    snapshot = client.get(BASE).json()
    assert snapshot['current'] is None and snapshot['pending_count'] == 0
    assert snapshot['settings']['enabled'] and snapshot['settings']['publish_mode'] == 'check_auto'
    assert snapshot['supported'] and not snapshot['possibly_stale']
    assert client.get(BASE + '/versions').json()['total'] == 0
    assert not client.get('/admin/api/entries/private/profile').json()['supported']
    assert store._writer.total_changes == before
    assert finished(client, start(client))['state'] == 'published'
    before = store._writer.total_changes
    snapshot = client.get(BASE).json()
    version = snapshot['current']['version']
    detail = client.get(BASE + f'/versions/{version}').json()
    assert detail['checks']['passed'] and detail['checks']['model']['sentences']
    assert detail['sentences'][0]['basis'][0]['evidence']['content'].startswith('测试消息')
    assert detail['sentences'][0]['admin_written'] is False
    assert 'material' not in detail and 'model_input' not in detail['checks']
    assert client.get(BASE + '/versions?limit=1').json()['total'] == 1
    assert client.get('/admin/api/entries/other/profile/versions/1').status_code == 404
    attempt = snapshot['latest_attempt']
    assert client.get(f"/admin/api/entries/other/profile/attempts/{attempt['id']}").status_code == 404
    assert 'outputs_json' not in attempt and 'material_json' not in attempt
    assert store._writer.total_changes == before


def test_edit_rollback_diff_conflicts_and_audit(client, store):
    first = client.put(BASE, json={'expected_version': 0, 'content': ''.join(TEXTS)}).json()
    assert all(s['admin_written'] for s in first['sentences'])
    second = client.put(BASE, json={'expected_version': 1, 'content': ''.join(TEXTS[::-1])}).json()
    assert second['version'] == 2
    diff = client.get(BASE + '/diff?before_version=1&after_version=2').json()
    assert diff['changed'] and diff['sentences']
    rollback = client.post(BASE + '/versions/1/rollback', json={'expected_version': 2}).json()
    assert rollback['version'] == 3 and rollback['content'] == first['content']
    assert rollback['checks']['rollback_from'] == 1
    for method, suffix, body in [('PUT', '', {'content': ''.join(TEXTS)}), ('PATCH', '/settings', {'enabled': False}),
                                  ('POST', '/versions/1/rollback', {}), ('POST', '/regenerate', {})]:
        response = client.request(method, BASE + suffix, json={'expected_version': 0, **body})
        assert response.status_code == 409
    with store.read() as conn:
        records = [dict(r) for r in conn.execute("SELECT * FROM admin_operations WHERE object_type='entry_profile'")]
    assert {r['action'] for r in records} >= {'entry_profile_edit', 'entry_profile_rollback'}
    assert all(TEXTS[0] not in r['details_json'] for r in records)


def test_manual_candidate_confirm_reject_and_supersession(client):
    assert client.patch(BASE + '/settings', json={'expected_version': 0, 'publish_mode': 'all_manual'}).status_code == 200
    assert finished(client, start(client))['state'] == 'candidate'
    snapshot = client.get(BASE).json()
    assert snapshot['current'] is None and snapshot['pending_count'] == 1
    generated_at = snapshot['pending']['generated_at']
    confirmed = client.post(BASE + '/versions/1/confirm', json={'expected_version': 0})
    assert confirmed.status_code == 200 and confirmed.json()['generated_at'] == generated_at
    assert client.get(BASE).json()['pending_count'] == 0
    assert client.get(BASE).json()['latest_attempt']['version_status'] == 'published'
    engine(client).clock.advance(days=1)
    assert finished(client, start(client, 1))['state'] == 'candidate'
    assert client.post(BASE + '/versions/2/reject', json={'expected_version': 1, 'reason': '暂不采用'}).status_code == 200
    assert client.get(BASE + '/versions/2').json()['checks']['reason'] == '暂不采用'
    engine(client).clock.advance(days=1)
    assert finished(client, start(client, 1))['state'] == 'candidate'
    edited = client.put(BASE, json={'expected_version': 1, 'content': ''.join(TEXTS)}).json()
    assert edited['version'] == 4
    assert client.get(BASE + '/versions/3').json()['checks']['reason'] == 'superseded'
    assert client.get(BASE).json()['pending_count'] == 0


def test_stale_and_admin_sentences_protected(client, store):
    finished(client, start(client))
    first = client.get(BASE + '/versions/1').json()
    # Keep model-derived sentences, append an admin sentence; preserve evidence snapshots.
    edited = client.put(BASE, json={'expected_version': 1, 'content': first['content'] + '管理员说明：请将画像作为交流背景。'}).json()
    assert edited['sentences'][-1]['admin_written']
    assert edited['sentences'][0]['basis'][0]['evidence']['content']
    engine(client).clock.advance(days=1)
    assert finished(client, start(client, 2))['state'] == 'published'
    detail = client.get(BASE + '/versions/3').json()
    assert detail['sentences'][-1]['text'] == edited['sentences'][-1]['text']
    mid = detail['sentences'][0]['basis'][0]['message_id']
    with store.write() as conn:
        conn.execute('DELETE FROM messages WHERE id=?', (mid,))
    snapshot = client.get(BASE).json()
    assert snapshot['possibly_stale'] and snapshot['stale_basis'][0]['reason'] == 'message_cleaned'


def test_rejected_check_keeps_current_and_shows_deleted_sentences(client):
    first = client.put(BASE, json={'expected_version': 0, 'content': ''.join(TEXTS)}).json()
    # Different model sentences avoid duplicate admin sentences.
    engine(client).gateway = Model(engine(client).store, texts=[TEXTS[0].replace('这次', '当次')], bad=(0,))
    task = finished(client, start(client, first['version']))
    assert task['state'] == 'rejected'
    detail = client.get(BASE + f"/versions/{task['version']}").json()
    assert detail['checks']['deleted_sentences'] and not detail['checks']['passed']
    assert client.get(BASE).json()['current']['version'] == first['version']


def test_background_immediate_concurrency_and_compare_and_swap(client, store):
    entered, release = threading.Event(), threading.Event()
    engine(client).gateway.hook = lambda: (entered.set(), release.wait(5))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(client.post, BASE + '/regenerate', json={'expected_version': 0}) for _ in range(2)]
            responses = [r.result(timeout=2) for r in results]
        assert sorted(r.status_code for r in responses) == [202, 409]
        assert entered.wait(1)
        accepted = next(r for r in responses if r.status_code == 202)
        assert client.get(accepted.headers['location']).json()['stage'] == 'generating'
        assert client.put(BASE, json={'expected_version': 0, 'content': ''.join(TEXTS)}).status_code == 200
    finally:
        release.set()
    assert finished(client, accepted.headers['location'])['state'] == 'conflict'
    assert current_profile(store, 'group')['author'] == 'admin'


def test_settings_change_inflight_does_not_publish(client):
    entered, release = threading.Event(), threading.Event()
    engine(client).gateway.hook = lambda: (entered.set(), release.wait(5))
    try:
        path = start(client)
        assert entered.wait(1)
        assert client.patch(BASE + '/settings', json={'expected_version': 0, 'enabled': False}).status_code == 200
    finally:
        release.set()
    assert finished(client, path)['reason'] == 'settings_changed'
    assert client.get(BASE).json()['current'] is None


def test_skips_and_failures_are_queryable_without_exception_text(client):
    engine(client).gateway = None
    assert finished(client, start(client))['reason'] == 'unconfigured'
    def fail():
        raise RuntimeError('must-not-be-exposed')
    engine(client).gateway = Model(engine(client).store, hook=fail)
    value = finished(client, start(client))
    assert value['state'] == 'failed' and value['reason'] == 'unexpected_error'
    assert 'must-not-be-exposed' not in json.dumps(value)


def test_recovery_and_shutdown(store):
    clock = Clock()
    messages(store, clock)
    with store.write() as conn:
        conn.execute("""INSERT INTO entry_profile_attempts(entry_id,base_version,state,local_day,started_at)
            VALUES('group',0,'running','2026-10-09','2026-10-09T00:00:00+00:00')""")
    jobs = EntryProfileJobs(store, Model(store), clock=clock)
    with store.read() as conn:
        assert conn.execute('SELECT reason FROM entry_profile_attempts').fetchone()[0] == 'interrupted'
    attempt = jobs.submit('group', expected_version=0)
    jobs.close()
    with store.read() as conn:
        assert conn.execute('SELECT state FROM entry_profile_attempts WHERE id=?', (attempt,)).fetchone()[0] == 'published'


@pytest.mark.parametrize('method,suffix,body', [('PUT','',{'content':'x'*150}),
    ('PATCH','/settings',{'enabled':False}),('POST','/regenerate',{}),
    ('POST','/versions/1/confirm',{}),('POST','/versions/1/reject',{}),('POST','/versions/1/rollback',{})])
def test_write_auth_csrf_and_expected_version(client, method, suffix, body):
    assert client.request(method, BASE+suffix, json=body).status_code == 400
    csrf = client.headers.pop('X-Iris-CSRF')
    assert client.request(method, BASE+suffix, json={**body,'expected_version':0}).status_code == 403
    client.headers['X-Iris-CSRF'] = csrf
    client.cookies.clear()
    assert client.request(method, BASE+suffix, json={**body,'expected_version':0}).status_code == 401
    assert client.get(BASE).status_code == 401


@pytest.mark.parametrize('payload', [{'expected_version':True,'enabled':True},
    {'expected_version':0,'enabled':'false'}, {'expected_version':0,'enabled':None},
    {'expected_version':0}, {'expected_version':0,'publish_mode':'all_auto'}])
def test_settings_validation(client, payload):
    assert client.patch(BASE+'/settings', json=payload).status_code == 400


def test_switch_to_auto_does_not_confirm_existing_candidate(client):
    client.patch(BASE + '/settings', json={'expected_version': 0, 'publish_mode': 'all_manual'})
    finished(client, start(client))
    client.patch(BASE + '/settings', json={'expected_version': 0, 'publish_mode': 'check_auto'})
    snapshot = client.get(BASE).json()
    assert snapshot['current'] is None and snapshot['pending_count'] == 1
    assert client.get('/admin/api/entries').json()['items'][0]['entry_profile_pending_count'] == 1


def test_background_daily_limit_skip_does_not_make_model_calls(client):
    finished(client, start(client))
    calls = len(engine(client).gateway.calls)
    result = finished(client, start(client, 1))
    assert result['state'] == 'skipped' and result['reason'] == 'daily_limit'
    assert len(engine(client).gateway.calls) == calls
