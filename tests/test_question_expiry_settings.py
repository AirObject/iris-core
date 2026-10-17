"""Admin exposure of question retention preserves partial saves and audit atomicity."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris.goals import Goals, question_expiry_days


KEY = 'internal_question_expiry_days'
PATH = '/admin/api/settings/goals'


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        yield client


def operations(store):
    with store.read() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM admin_operations WHERE object_type='settings' AND object_id='goals' ORDER BY id")]


@pytest.mark.parametrize('stored', [None, {}, {'default_reminder_minutes': 90, 'overdue_reminders': False}])
def test_get_missing_expiry_uses_default_without_writing(client, store, stored):
    if stored is None:
        with store.write() as conn:
            conn.execute("DELETE FROM runtime_settings WHERE key='goals'")
    else:
        store.set_setting('goals', stored)
    before = operations(store)
    response = client.get('/admin/api/settings')
    assert response.status_code == 200
    assert response.json()['goals'] == {
        'default_reminder_minutes': 60, 'overdue_reminders': True, **(stored or {}), KEY: 7}
    assert store.setting('goals') == stored
    assert operations(store) == before


@pytest.mark.parametrize('days', [1, 7, 365])
def test_get_exposes_existing_internal_setting(client, store, days):
    Goals(store).configure_question_expiry(days)
    before = operations(store)
    response = client.get('/admin/api/settings')
    assert response.status_code == 200
    assert response.json()['goals'][KEY] == days
    assert operations(store) == before


@pytest.mark.parametrize('days', [1, 7, 365])
def test_patch_expiry_alone_preserves_reminder_settings(client, store, days):
    store.set_setting('goals', {'default_reminder_minutes': 90, 'overdue_reminders': False, KEY: 21})
    before = operations(store)
    response = client.patch(PATH, json={KEY: days})
    assert response.status_code == 200
    expected = {'default_reminder_minutes': 90, 'overdue_reminders': False, KEY: days}
    assert response.json()['goals'] == expected
    assert store.setting('goals') == expected
    with store.read() as conn:
        assert question_expiry_days(conn) == days
    after = operations(store)
    assert len(after) == len(before) + 1
    assert after[-1]['actor'] == 'admin'
    assert after[-1]['action'] == 'settings_goals'
    assert json.loads(after[-1]['details_json']) == {KEY: days}


def test_patch_combined_settings_returns_saved_values_and_one_audit(client, store):
    store.set_setting('goals', {'default_reminder_minutes': 90, 'overdue_reminders': False, KEY: 21})
    before = operations(store)
    changes = {'default_reminder_minutes': 30, 'overdue_reminders': True, KEY: 14}
    response = client.patch(PATH, json=changes)
    assert response.status_code == 200
    assert response.json()['goals'] == changes
    assert store.setting('goals') == changes
    assert client.get('/admin/api/settings').json()['goals'] == changes
    after = operations(store)
    assert len(after) == len(before) + 1
    assert after[-1]['actor'] == 'admin'
    assert after[-1]['action'] == 'settings_goals'
    assert json.loads(after[-1]['details_json']) == changes
    assert after[-1]['id'] in [row['id'] for row in response.json()['operations']]


@pytest.mark.parametrize('changes', [
    {'default_reminder_minutes': 30}, {'overdue_reminders': False},
    {'default_reminder_minutes': 30, 'overdue_reminders': False}, {},
])
def test_omitted_expiry_preserves_saved_value_and_other_keys(client, store, changes):
    original = {'default_reminder_minutes': 60, 'overdue_reminders': True, KEY: 21,
                'future_option': {'enabled': True}}
    store.set_setting('goals', original)
    response = client.patch(PATH, json=changes)
    assert response.status_code == 200
    assert response.json()['goals'][KEY] == 21
    assert store.setting('goals') == {**original, **changes}
    assert json.loads(operations(store)[-1]['details_json']) == changes


@pytest.mark.parametrize('value', [0, -1, 366, True, False, 7.0, 7.5, '7', None, [], {}])
def test_invalid_expiry_returns_400_without_partial_save_or_audit(client, store, value):
    original = {'default_reminder_minutes': 60, 'overdue_reminders': True, KEY: 21}
    store.set_setting('goals', original)
    before = operations(store)
    response = client.patch(PATH, json={KEY: value, 'default_reminder_minutes': 30, 'overdue_reminders': False})
    assert response.status_code == 400
    assert any(item['field'] == 'body.' + KEY for item in response.json()['error']['fields'])
    assert store.setting('goals') == original
    assert operations(store) == before


@pytest.mark.parametrize('invalid', [
    {'default_reminder_minutes': True}, {'default_reminder_minutes': -1}, {'overdue_reminders': 'false'},
])
def test_invalid_existing_field_does_not_save_valid_expiry(client, store, invalid):
    original = {'default_reminder_minutes': 60, 'overdue_reminders': True, KEY: 21}
    store.set_setting('goals', original)
    before = operations(store)
    response = client.patch(PATH, json={KEY: 14, **invalid})
    assert response.status_code == 400
    assert store.setting('goals') == original
    assert operations(store) == before


def test_audit_failure_rolls_back_all_settings(client, store):
    original = {'default_reminder_minutes': 60, 'overdue_reminders': True, KEY: 21}
    store.set_setting('goals', original)
    before = operations(store)
    with store.write() as conn:
        conn.execute("""CREATE TRIGGER reject_goal_settings_audit BEFORE INSERT ON admin_operations
            WHEN NEW.action='settings_goals'
            BEGIN SELECT RAISE(ABORT, 'test audit failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='test audit failure'):
        client.patch(PATH, json={KEY: 14, 'default_reminder_minutes': 30, 'overdue_reminders': False})
    assert store.setting('goals') == original
    assert operations(store) == before


@pytest.mark.parametrize('protection', ['session', 'csrf', 'origin', 'content_type', 'host'])
def test_expiry_patch_retains_admin_write_protection(client, store, protection):
    original = store.setting('goals')
    before = operations(store)
    headers = {}
    if protection == 'session':
        client.cookies.clear()
        assert client.get('/admin/api/settings').status_code == 401
    elif protection == 'csrf':
        del client.headers['X-Iris-CSRF']
    elif protection == 'origin':
        headers['Origin'] = 'https://evil.example'
    elif protection == 'content_type':
        headers['Content-Type'] = 'text/plain'
    elif protection == 'host':
        headers['Host'] = 'evil.example'
    response = client.patch(PATH, json={KEY: 14}, headers=headers)
    assert response.status_code in (400, 401, 403)
    assert store.setting('goals') == original
    assert operations(store) == before
