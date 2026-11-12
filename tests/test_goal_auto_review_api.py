"""Admin surface of the G line: two switches, auto actions, evidence and reopen."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import authorize_host, login_admin
from iris.api import create_app
from iris.goals import AUTO_REVIEW_KEY, DUPLICATE_AUTO_KEY, Goals
from goal_auto_helpers import Clock, Reviewer, basic, cite, enable, row, run_goals

PATH = '/admin/api/settings/goals'


@pytest.fixture
def client(store):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as client:
        client.app.state.scheduler.stop()
        client.app.state.goals = Goals(store, clock=Clock())
        login_admin(client)
        yield client


def test_switches_are_off_by_default_and_saved_only_when_set(client, store):
    goals = client.get('/admin/api/settings').json()['goals']
    assert goals[AUTO_REVIEW_KEY] is False and goals[DUPLICATE_AUTO_KEY] is False
    response = client.patch(PATH, json={'default_reminder_minutes': 30})
    assert response.status_code == 200
    assert AUTO_REVIEW_KEY not in store.setting('goals') and DUPLICATE_AUTO_KEY not in store.setting('goals')
    response = client.patch(PATH, json={AUTO_REVIEW_KEY: True})
    assert response.status_code == 200 and response.json()['goals'][AUTO_REVIEW_KEY] is True
    assert response.json()['goals'][DUPLICATE_AUTO_KEY] is False
    assert store.setting('goals')[AUTO_REVIEW_KEY] is True and DUPLICATE_AUTO_KEY not in store.setting('goals')
    response = client.patch(PATH, json={DUPLICATE_AUTO_KEY: True, AUTO_REVIEW_KEY: False})
    assert response.json()['goals'][DUPLICATE_AUTO_KEY] is True and response.json()['goals'][AUTO_REVIEW_KEY] is False
    with store.read() as conn:
        audit = [json.loads(r[0]) for r in conn.execute(
            "SELECT details_json FROM admin_operations WHERE action='settings_goals' ORDER BY id")]
    assert audit[-1] == {DUPLICATE_AUTO_KEY: True, AUTO_REVIEW_KEY: False}


@pytest.mark.parametrize('value', [1, 'true', None, []])
def test_switches_are_strict_booleans(client, store, value):
    before = store.setting('goals')
    assert client.patch(PATH, json={AUTO_REVIEW_KEY: value}).status_code == 400
    assert store.setting('goals') == before


def test_detail_list_actions_and_reopen(client, store):
    gid, source, done = basic(store)
    enable(store)
    run_goals(store, Reviewer(store, cite('completed')))
    detail = client.get(f'/admin/api/goals/{gid}').json()
    assert detail['state'] == 'completed' and detail['closed_by'] == 'maintenance' and detail['auto_closed'] is True
    closure = detail['auto_review']['closure']
    assert closure['decision'] == 'completed' and closure['evidence'] == [done]
    assert closure['evidence_messages'][0]['content'] == '到家了，餐桌上的电热毯收到了，谢谢！'
    assert detail['auto_review']['reopenable'] is True
    listed = client.get('/admin/api/goals?state=completed').json()['items']
    assert [(g['id'], g['auto_closed']) for g in listed] == [(gid, True)]
    actions = client.get('/admin/api/goal-auto-actions').json()
    action, = actions['items']
    assert action['type'] == 'review' and action['goal']['id'] == gid and action['reopenable'] is True
    assert action['evidence_messages'][0]['id'] == done
    stale = client.post(f'/admin/api/goals/{gid}/reopen', json={'expected_revision': detail['revision'] - 1})
    assert stale.status_code == 409
    reopened = client.post(f'/admin/api/goals/{gid}/reopen', json={'expected_revision': detail['revision']})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()['state'] == 'open' and row(store, gid)['closed_by'] is None
    detail = client.get(f'/admin/api/goals/{gid}').json()
    assert detail['auto_closed'] is False and detail['auto_review']['reopenable'] is False
    assert client.get('/admin/api/goal-auto-actions').json()['items'][0]['reopenable'] is False
    again = client.post(f'/admin/api/goals/{gid}/reopen', json={'expected_revision': detail['revision']})
    assert again.status_code == 400


def test_host_projection_is_unchanged(client, store):
    gid, _, _ = basic(store)
    enable(store)
    run_goals(store, Reviewer(store, cite('completed')))
    authorize_host(client)
    item = client.get('/api/v1/goals?state=completed').json()['items'][0]
    assert item['id'] == gid and 'auto_closed' not in item and 'auto_review' not in item


def test_undo_auto_merge_endpoint_lists_state_and_requires_csrf(client, store):
    from goal_auto_helpers import CREATED, goal
    first = goal(store, '周五前整理操作手册', people=(), created='2026-11-02T09:00:00+00:00')
    second = goal(store, '周五前整理操作说明手册', people=(), created='2026-11-02T10:00:00+00:00')
    with store.write() as conn:
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible',?)",
                     (first, second, CREATED))
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})

    class Same(Reviewer):
        def goal_dedup_judge(self, messages, ids, *, budget_seconds):
            return {'decisions': {i: 'same' for i in ids}}
    enable(store, review=False, duplicates=True)
    run_goals(store, Same(store))
    action, = client.get('/admin/api/goal-auto-actions').json()['items']
    assert action['type'] == 'duplicate' and action['outcome'] == 'merged' and action['undoable'] is True
    assert action['source_id'] == second and 'merge_record_json' not in action
    detail = client.get(f'/admin/api/goals/{first}').json()
    assert detail['auto_review']['duplicate_judgments'][0]['undoable'] is True
    path = f"/admin/api/goal-duplicate-judgments/{action['id']}/undo"
    assert client.post(path, json={'expected_revision': action['target_revision']},
                       headers={'X-Iris-CSRF': ''}).status_code == 403
    assert client.post(path, json={'expected_revision': action['target_revision'] - 1}).status_code == 409
    response = client.post(path, json={'expected_revision': action['target_revision']})
    assert response.status_code == 200, response.text
    assert response.json()['id'] == first and row(store, second)['merged_into'] is None
    action, = client.get('/admin/api/goal-auto-actions').json()['items']
    assert action['undoable'] is False and action['undone_at']
    assert client.post(path, json={'expected_revision': row(store, first)['revision']}).status_code == 400
