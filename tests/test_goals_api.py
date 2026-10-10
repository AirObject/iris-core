"""Host goal commands and the authenticated management projection share one core."""
from conftest import authorize_host
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, msg
from iris.api import create_app
from iris.goals import Goals


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 9, 4, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **values):
        self.value += timedelta(**values)


@pytest.fixture
def client(store):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as client:
        authorize_host(client)
        client.app.state.scheduler.stop()
        client.app.state.goals = Goals(store, clock=Clock())
        yield client


def create(client, content='周五联系小林', **fields):
    response = client.post('/api/v1/goals', json={'content': content, **fields})
    assert response.status_code == 201, response.text
    return response.json()


def test_host_create_shared_list_idempotency_and_revision_conflict(client, store):
    msg(store, 0, '第一入口', entry='A')
    msg(store, 1, '第二入口', entry='B')
    first = create(client, content='  周五联系小林  ', entry_id='A', host_key='host-task-1')
    goal = first['goal']
    assert goal['content'] == '周五联系小林' and goal['origin'] == 'host'
    assert goal['state'] == 'open' and first['dedup']['status'] == 'created'
    assert create(client, content='重试不得改正文', host_key='host-task-1') == first
    duplicate = create(client, entry_id='B')
    assert duplicate['dedup']['status'] == 'merged'
    assert duplicate['goal']['id'] == goal['id']
    page = client.get('/api/v1/goals?state=open&kind=normal&limit=1').json()
    assert page['total'] == 1 and page['limit'] == 1 and page['offset'] == 0
    canonical = page['items'][0]
    conflict = client.patch(f"/api/v1/goals/{goal['id']}", json={'state': 'completed', 'expected_revision': 999})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'revision_conflict'
    merged = client.patch(f"/api/v1/goals/{duplicate['submitted_id']}", json={'state': 'completed'})
    assert merged.status_code == 409 and merged.json()['error']['code'] == 'goal_merged'
    assert merged.json()['error']['canonical_id'] == goal['id']
    complete = client.patch(f"/api/v1/goals/{goal['id']}", json={
        'state': 'completed', 'expected_revision': canonical['revision']})
    assert complete.status_code == 200 and complete.json()['state'] == 'completed'
    assert client.get('/api/v1/goals?state=open').json()['total'] == 0
    assert client.get('/api/v1/goals?state=completed').json()['total'] == 1
    assert create(client, content='another retry', host_key='host-task-1') == first
    assert client.patch('/api/v1/goals/999999', json={'state': 'abandoned'}).status_code == 404


def test_question_has_no_reminders_and_date_deadline_uses_role_timezone(client, store):
    store.set_setting('timezone', 'Europe/Berlin')
    question = create(client, content='想问小林最近读什么书', kind='question')['goal']
    assert question['deadline'] is None
    dated = create(client, content='整理书单', deadline='2026-10-10')['goal']
    assert dated['deadline'] == '2026-10-10T23:59:59+02:00'
    assert client.get('/api/v1/goals?kind=question').json()['items'][0]['id'] == question['id']
    rejected = client.patch(f"/api/v1/goals/{question['id']}", json={'deadline': '2026-10-11'})
    assert rejected.status_code == 400
    assert client.get('/api/v1/notifications').json()['items'] == []


def test_notification_cursor_publishes_late_deadline_and_taken_is_not_completed(client):
    later = create(client, content='晚到期事项', deadline='2026-10-10T10:00:00Z', reminder_minutes=60)['goal']
    immediate = create(client, content='已到期事项', deadline='2026-10-09T03:00:00Z')['goal']
    first = client.get('/api/v1/notifications?after=0&limit=1').json()
    assert len(first['items']) == 1 and first['items'][0]['goal_id'] == immediate['id']
    assert first['items'][0]['kind'] == 'goal_reminder'
    assert first['items'][0]['reminder_kind'] == 'immediate'
    assert client.get(f"/api/v1/notifications?after={first['next_cursor']}").json()['items'] == []
    assert client.get('/api/v1/goals?state=open').json()['total'] == 2
    client.app.state.goals.clock.advance(days=1, hours=5)
    client.app.state.goals.generate_notifications()
    following = client.get(f"/api/v1/notifications?after={first['next_cursor']}").json()
    assert later['id'] in [item['goal_id'] for item in following['items']]
    assert all(item['id'] > first['next_cursor'] for item in following['items'])
    login_admin(client)
    taken = client.get('/admin/api/notifications?status=taken').json()
    assert taken['total'] >= 2
    assert client.get('/admin/api/notifications?status=pending').json()['total'] == 0


def test_admin_goal_crud_filters_settings_notifications_and_audit(client, store):
    msg(store, 0, '入口资料', entry='A')
    assert client.get('/admin/api/goals').status_code == 409
    login_admin(client)
    default = client.get('/admin/api/settings').json()['goals']
    assert default == {'default_reminder_minutes': 60, 'overdue_reminders': True}
    response = client.post('/admin/api/goals', json={'content': '管理员安排', 'entry_id': 'A',
                                                    'deadline': '2026-10-09T05:00:00Z'})
    assert response.status_code == 201, response.text
    goal = response.json()['goal']
    assert goal['origin'] == 'admin'
    detail = client.get(f"/admin/api/goals/{goal['id']}").json()
    assert detail['id'] == goal['id']
    for key in ('sources', 'promise_memories', 'merged_goals', 'notifications'):
        assert key in detail
    page = client.get('/admin/api/goals', params={'entry_id': 'A', 'deadline_from': '2026-10-09',
                       'deadline_to': '2026-10-09', 'possible_duplicate': False, 'limit': 1}).json()
    assert page['total'] == 1
    changed = client.patch(f"/admin/api/goals/{goal['id']}", json={'expected_revision': detail['revision'],
        'content': '修订后的安排', 'deadline': '2026-10-11', 'reminder_minutes': 15})
    assert changed.status_code == 200 and changed.json()['content'] == '修订后的安排'
    assert client.patch(f"/admin/api/goals/{goal['id']}", json={'content': '缺少修订号'}).status_code == 400
    assert client.patch(f"/admin/api/goals/{goal['id']}", json={'expected_revision': detail['revision'],
                                                            'content': '过期写入'}).status_code == 409
    abandoned = client.patch(f"/admin/api/goals/{goal['id']}", json={
        'expected_revision': changed.json()['revision'], 'state': 'abandoned'})
    assert abandoned.status_code == 200 and abandoned.json()['state'] == 'abandoned'
    assert client.get('/admin/api/goals?state=abandoned').json()['total'] == 1
    settings = client.patch('/admin/api/settings/goals', json={'default_reminder_minutes': 30,
                                                             'overdue_reminders': False})
    assert settings.status_code == 200
    assert settings.json()['goals'] == {'default_reminder_minutes': 30, 'overdue_reminders': False}
    assert client.patch('/admin/api/settings/goals', json={}).json()['goals'] == settings.json()['goals']
    with store.read() as conn:
        operations = [dict(row) for row in conn.execute("SELECT * FROM admin_operations WHERE object_type IN ('goal','settings')")]
    assert any(row['actor'] == 'admin' and row['object_id'] == str(goal['id']) for row in operations)
    assert any(row['object_id'] == 'goals' for row in operations)
    assert '修订后的安排' not in json.dumps(operations, ensure_ascii=False)


@pytest.mark.parametrize('payload,field', [
    ({}, 'content'), ({'content': ' '}, 'content'), ({'content': 1}, 'content'),
    ({'content': 'x' * 4001}, 'content'), ({'content': 'x', 'kind': 'task'}, 'kind'),
    ({'content': 'x', 'deadline': 'tomorrow'}, 'deadline'),
    ({'content': 'x', 'deadline': '2026-10-10T01:00:00'}, 'deadline'),
    ({'content': 'x', 'reminder_minutes': True}, 'reminder_minutes'),
    ({'content': 'x', 'reminder_minutes': -1}, 'reminder_minutes'),
    ({'content': 'x', 'reminder_minutes': 525601}, 'reminder_minutes'),
    ({'content': 'x', 'reminder_minutes': '60'}, 'reminder_minutes'),
    ({'content': 'x', 'people': ['self', 'self']}, 'people'),
    ({'content': 'x', 'people': [' ']}, 'people'),
    ({'content': 'x', 'people': ['missing-person']}, 'people'),
    ({'content': 'x', 'people': ['p'] * 101}, 'people'),
    ({'content': 'x', 'entry_id': ' '}, 'entry_id'),
    ({'content': 'x', 'host_key': ''}, 'host_key'),
    ({'content': 'x', 'host_key': 'x' * 201}, 'host_key'),
    ({'content': 'x', 'unknown': True}, 'unknown'),
])
def test_host_invalid_goal_fields_are_400_and_atomic(client, payload, field):
    response = client.post('/api/v1/goals', json=payload)
    assert response.status_code == 400, response.text
    assert any(field in item['field'] for item in response.json()['error']['fields'])
    assert client.get('/api/v1/goals').json()['total'] == 0


@pytest.mark.parametrize('payload', [
    {'content': '问题', 'kind': 'question', 'deadline': '2026-10-10'},
    {'content': '问题', 'kind': 'question', 'reminder_minutes': 0},
])
def test_question_rejects_deadlines_and_lead_time(client, payload):
    assert client.post('/api/v1/goals', json=payload).status_code == 400


@pytest.mark.parametrize('query', ['limit=0', 'offset=-1', 'state=in_progress', 'kind=invalid',
                                    'overdue=maybe', 'unknown=true'])
def test_host_goal_query_validation(client, query):
    assert client.get('/api/v1/goals?' + query).status_code == 400


@pytest.mark.parametrize('payload', [{}, {'state': 'open'}, {'state': None}, {'content': '不可由宿主编辑'},
                                     {'expected_revision': True}, {'deadline': 'invalid'}])
def test_host_goal_patch_validation(client, payload):
    goal = create(client)['goal']
    assert client.patch(f"/api/v1/goals/{goal['id']}", json=payload).status_code == 400


def test_request_size_json_host_and_notification_query_errors(client):
    assert client.post('/api/v1/goals', content='{', headers={'Content-Type': 'application/json'}).status_code == 400
    oversized = client.post('/api/v1/goals', content='{"content":"x"}' + ' ' * 32768,
                            headers={'Content-Type': 'application/json'})
    assert oversized.status_code == 400 and oversized.json()['error']['fields'][0]['field'] == 'body'
    client.app.state.ready = False
    assert client.post('/api/v1/goals', json={'content': 'x'}, headers={'Host': 'evil.example'}).status_code == 400
    client.app.state.ready = True
    for query in ('after=-1', 'after=no', 'after=9223372036854775808', 'limit=0', 'unknown=true'):
        assert client.get('/api/v1/notifications?' + query).status_code == 400


@pytest.mark.parametrize('protection', ['csrf', 'origin', 'host', 'content_type', 'session'])
def test_admin_goal_writes_preserve_authentication(client, protection):
    login_admin(client)
    headers = {}
    if protection == 'csrf': headers['X-Iris-CSRF'] = 'wrong'
    if protection == 'origin': headers['Origin'] = 'https://evil.example'
    if protection == 'host': headers['Host'] = 'evil.example'
    if protection == 'content_type': headers['Content-Type'] = 'text/plain'
    if protection == 'session': client.cookies.clear()
    response = client.post('/admin/api/goals', json={'content': '不得创建'}, headers=headers)
    assert response.status_code in (400, 401, 403)
    assert client.get('/api/v1/goals').json()['total'] == 0


@pytest.mark.parametrize('payload', [
    {'default_reminder_minutes': True}, {'default_reminder_minutes': -1},
    {'default_reminder_minutes': 525601}, {'overdue_reminders': 'false'}, {'extra': 1},
])
def test_goal_settings_validation(client, payload):
    login_admin(client)
    assert client.patch('/admin/api/settings/goals', json=payload).status_code == 400


def test_admin_possible_duplicate_merge_and_dismiss_with_revisions(client):
    first = create(client, content='周五问小林面试结果')['goal']
    second_result = create(client, content='周五问小林的面试结果')
    assert second_result['dedup']['status'] == 'possible_duplicate'
    second = second_result['goal']
    login_admin(client)
    candidates = client.get('/admin/api/goals?possible_duplicate=true').json()
    assert {item['id'] for item in candidates['items']} == {first['id'], second['id']}
    first = client.get(f"/admin/api/goals/{first['id']}").json()
    second = client.get(f"/admin/api/goals/{second['id']}").json()
    path = f"/admin/api/goals/{first['id']}/duplicates/{second['id']}"
    assert client.post(path + '/merge', json={'expected_revision': 999,
                                               'other_revision': second['revision']}).status_code == 409
    dismissed = client.post(path + '/dismiss', json={'expected_revision': first['revision'],
                                                     'other_revision': second['revision']})
    assert dismissed.status_code == 200, dismissed.text
    assert client.get('/admin/api/goals?possible_duplicate=true').json()['total'] == 0
    third = create(client, content='周五问小林面试的结果')['goal']
    first = client.get(f"/admin/api/goals/{first['id']}").json()
    third = client.get(f"/admin/api/goals/{third['id']}").json()
    path = f"/admin/api/goals/{first['id']}/duplicates/{third['id']}"
    merged = client.post(path + '/merge', json={'expected_revision': first['revision'],
                                               'other_revision': third['revision']})
    assert merged.status_code == 200, merged.text
    assert merged.json()['id'] == first['id']
    detail = client.get(f"/admin/api/goals/{first['id']}").json()
    assert third['id'] in [item['id'] for item in detail['merged_goals']]
    assert client.get('/admin/api/goals').json()['total'] == 2


def test_admin_queries_and_settings_require_valid_fields_and_csrf(client):
    login_admin(client)
    for path in ('/admin/api/goals?deadline_from=not-a-date', '/admin/api/goals?unknown=true',
                 '/admin/api/notifications?status=delivered', '/admin/api/notifications?goal_id=0'):
        assert client.get(path).status_code == 400
    response = client.patch('/admin/api/settings/goals', json={'overdue_reminders': False},
                            headers={'X-Iris-CSRF': ''})
    assert response.status_code == 403
    assert client.get('/admin/api/settings').json()['goals']['overdue_reminders'] is True
    oversized = client.post('/admin/api/goals', content='{"content":"x"}' + ' ' * 32768,
                            headers={'Content-Type': 'application/json'})
    assert oversized.status_code == 400


def test_admin_notification_browsing_is_read_only_and_shows_cancellation(client):
    goal = create(client, content='过期但未取走的提醒', deadline='2026-10-09T03:00:00Z')['goal']
    login_admin(client)
    path = f"/admin/api/notifications?goal_id={goal['id']}&status=pending&limit=1"
    first = client.get(path).json()
    assert first['total'] == 1 and first['items'][0]['status'] == 'pending'
    assert client.get(path).json() == first
    updated = client.patch(f"/admin/api/goals/{goal['id']}", json={'expected_revision': goal['revision'],
                                                                'deadline': '2026-10-11T03:00:00Z'})
    assert updated.status_code == 200
    assert client.get(path).json()['total'] == 0
    assert client.get(f"/admin/api/notifications?goal_id={goal['id']}&status=cancelled").json()['total'] == 1
    assert client.get('/api/v1/notifications').json()['items'] == []


@pytest.mark.parametrize('goal_id', ['0', '-1', '9223372036854775808'])
def test_goal_path_ids_reject_out_of_range_values(client, goal_id):
    response = client.patch(f'/api/v1/goals/{goal_id}', json={'state': 'completed'})
    assert response.status_code == 400
    assert any(item['field'] == 'path.goal_id' for item in response.json()['error']['fields'])
    login_admin(client)
    assert client.get(f'/admin/api/goals/{goal_id}').status_code == 400
    assert client.patch(f'/admin/api/goals/{goal_id}', json={'state': 'abandoned',
                                                           'expected_revision': 1}).status_code == 400
    for action in ('merge', 'dismiss'):
        assert client.post(f'/admin/api/goals/1/duplicates/{goal_id}/{action}', json={
            'expected_revision': 1, 'other_revision': 1}).status_code == 400
        assert client.post(f'/admin/api/goals/{goal_id}/duplicates/1/{action}', json={
            'expected_revision': 1, 'other_revision': 1}).status_code == 400


def test_changing_default_lead_preserves_unclaimed_overdue_reminder(client):
    goal = create(client, content='仍然需要处理的过期任务', deadline='2026-10-09T03:00:00Z')['goal']
    login_admin(client)
    path = f"/admin/api/notifications?goal_id={goal['id']}&status=pending"
    assert client.get(path).json()['total'] == 1
    response = client.patch('/admin/api/settings/goals', json={'default_reminder_minutes': 90})
    assert response.status_code == 200
    assert client.get(path).json()['total'] == 1
    notifications = client.get('/api/v1/notifications').json()['items']
    assert len(notifications) == 1 and notifications[0]['goal_id'] == goal['id']
    assert client.get('/api/v1/goals?state=open').json()['items'][0]['overdue'] is True


def test_question_patch_reports_the_invalid_reminder_field(client):
    goal = create(client, content='还想确认什么', kind='question')['goal']
    response = client.patch(f"/api/v1/goals/{goal['id']}", json={'reminder_minutes': 15})
    assert response.status_code == 400
    assert response.json()['error']['fields'][0]['field'] == 'body.reminder_minutes'


@pytest.mark.parametrize('resolution', ['merge', 'dismiss'])
def test_admin_exact_duplicate_waits_for_explicit_resolution(client, resolution):
    original = create(client, content='确认画展开放时间')['goal']
    login_admin(client)
    response = client.post('/admin/api/goals', json={'content': original['content']})
    assert response.status_code == 201
    receipt = response.json()
    assert receipt['dedup'] == {'status': 'possible_duplicate', 'target_id': original['id']}
    assert receipt['goal']['origin'] == 'admin'
    assert receipt['goal']['id'] != original['id']
    assert client.get('/api/v1/goals').json()['total'] == 2
    first = client.get(f"/admin/api/goals/{original['id']}").json()
    second = receipt['goal']
    result = client.post(f"/admin/api/goals/{first['id']}/duplicates/{second['id']}/{resolution}",
                         json={'expected_revision': first['revision'], 'other_revision': second['revision']})
    assert result.status_code == 200
    assert client.get('/api/v1/goals').json()['total'] == (1 if resolution == 'merge' else 2)
    assert client.get('/admin/api/goals?possible_duplicate=true').json()['total'] == 0
