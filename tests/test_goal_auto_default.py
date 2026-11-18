"""G-C2: G1/G2 switches default on; administrator goal-settings choices survive the upgrade. No model IO."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, store_before_migration
from iris.api import create_app
from iris.db import Store
from iris.goals import AUTO_DEFAULTS, AUTO_REVIEW_KEY, DUPLICATE_AUTO_KEY, Goals, auto_review_switches, goal_settings
from iris.maintenance import Maintenance
from iris.memory_ops import operation
from iris.settings_api import GoalsSettings
from goal_auto_helpers import Clock

MIGRATION = '042_goal_auto_default_on.sql'
PATH = '/admin/api/settings/goals'
ON = {AUTO_REVIEW_KEY: True, DUPLICATE_AUTO_KEY: True}
OFF = {AUTO_REVIEW_KEY: False, DUPLICATE_AUTO_KEY: False}


def switches(store):
    with store.read() as conn:
        settings = goal_settings(conn)
        return {key: settings[key] for key in ON}


def run_snapshot(store):
    rid = Maintenance(store, clock=Clock()).request()
    with store.read() as conn:
        return json.loads(conn.execute('SELECT settings_json FROM maintenance_runs WHERE id=?', (rid,)).fetchone()[0])['goal_auto']


def test_code_request_and_fallback_defaults_agree():
    assert AUTO_DEFAULTS == ON
    request = GoalsSettings()
    assert {key: getattr(request, key) for key in ON} == ON
    assert {key: GoalsSettings.model_fields[key].default for key in ON} == AUTO_DEFAULTS


@pytest.mark.parametrize('precreated', [False, True])
def test_new_database_defaults_on(tmp_path, precreated):
    path = tmp_path / 'new.db'
    if precreated:
        path.touch()
    store = Store(path)
    try:
        assert {key: store.setting('goals')[key] for key in ON} == ON
        assert switches(store) == ON
        with store.read() as conn:
            assert auto_review_switches(conn) == {'review': True, 'duplicates': True}
            assert conn.execute('SELECT 1 FROM schema_migrations WHERE version=?', (MIGRATION,)).fetchone()
        assert run_snapshot(store) == {'review': True, 'duplicates': True}
    finally:
        store.close()


def test_missing_row_or_field_falls_back_on(store):
    store.set_setting('goals', {'default_reminder_minutes': 30})
    assert switches(store) == ON
    with store.write() as conn:
        conn.execute("DELETE FROM runtime_settings WHERE key='goals'")
        assert {key: goal_settings(conn)[key] for key in ON} == ON


def test_never_saved_old_database_upgrades(tmp_path, monkeypatch):
    path = tmp_path / 'old.db'
    old = store_before_migration(path, monkeypatch, MIGRATION)
    before = old.setting('goals')
    assert not set(ON) & set(before)  # Written by migration 012 without the switches.
    with old.read() as conn:
        latest_migration = conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        assert latest_migration == '041_persona_auto_default.sql'
    assert old.setting('persona_publish_mode') == 'all_auto'
    with old.write() as conn:
        # Other settings, including goal dedup judge parameters, are not goal settings saves.
        operation(conn, 'goal_dedup_judge_saved', 'setting', 'goal_dedup_judge', {'possible_min_similarity': 0.5})
        operation(conn, 'consolidation_settings_saved', 'settings', 'consolidation', {'resolution': 'auto_v1'})
        operation(conn, 'lifecycle_saved', 'settings', 'lifecycle', {'maintenance_time': '04:00'})
    old.close()
    for _ in range(2):
        upgraded = Store(path)
        try:
            assert upgraded.setting('goals') == {**before, **ON}
            assert upgraded.setting('persona_publish_mode') == 'all_auto'
            assert switches(upgraded) == ON
            assert run_snapshot(upgraded) == {'review': True, 'duplicates': True}
        finally:
            upgraded.close()


# Before this change a save stored a switch only when it differed from the old
# default (off): saving false, or saving only other fields, left the field absent.
@pytest.mark.parametrize('stored,details,expected', [
    ({}, {}, OFF),                                                     # empty save
    ({'default_reminder_minutes': 30}, {'default_reminder_minutes': 30}, OFF),
    ({'internal_question_expiry_days': 14}, {'internal_question_expiry_days': 14}, OFF),  # question expiry only
    ({}, {AUTO_REVIEW_KEY: False}, OFF),                               # explicit false, not stored
    ({AUTO_REVIEW_KEY: False, DUPLICATE_AUTO_KEY: False}, {}, OFF),    # explicit false, stored
    ({AUTO_REVIEW_KEY: True}, {AUTO_REVIEW_KEY: True}, {AUTO_REVIEW_KEY: True, DUPLICATE_AUTO_KEY: False}),
    ({DUPLICATE_AUTO_KEY: True}, {DUPLICATE_AUTO_KEY: True}, {AUTO_REVIEW_KEY: False, DUPLICATE_AUTO_KEY: True}),
    (ON, ON, ON),
])
def test_saved_goal_settings_keep_their_value(tmp_path, monkeypatch, stored, details, expected):
    path = tmp_path / 'old.db'
    old = store_before_migration(path, monkeypatch, MIGRATION)
    old.set_setting('goals', {**old.setting('goals'), **stored})
    with old.write() as conn:
        operation(conn, 'settings_goals', 'settings', 'goals', details)
    before = old.setting('goals')
    old.close()
    for _ in range(2):
        upgraded = Store(path)
        try:
            assert upgraded.setting('goals') == {**before, **expected}
            assert switches(upgraded) == expected
            assert run_snapshot(upgraded) == {'review': expected[AUTO_REVIEW_KEY],
                                              'duplicates': expected[DUPLICATE_AUTO_KEY]}
        finally:
            upgraded.close()


@pytest.mark.parametrize('save', ['configure', 'question_expiry'])
def test_real_saves_on_an_old_database_keep_off(tmp_path, monkeypatch, save):
    path = tmp_path / 'old.db'
    old = store_before_migration(path, monkeypatch, MIGRATION)
    goals = Goals(old, clock=Clock())
    if save == 'configure':
        goals.configure(default_reminder_minutes=45, actor='admin')
    else:
        goals.configure_question_expiry(10)
    assert not set(ON) & set(old.setting('goals'))
    old.close()
    upgraded = Store(path)
    try:
        assert switches(upgraded) == OFF
    finally:
        upgraded.close()


def test_settings_api_shows_default_on_and_can_turn_off(store):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        goals = client.get('/admin/api/settings').json()['goals']
        assert {key: goals[key] for key in ON} == ON
        response = client.patch(PATH, json={AUTO_REVIEW_KEY: False})
        assert response.status_code == 200, response.text
        assert {key: response.json()['goals'][key] for key in ON} == {AUTO_REVIEW_KEY: False, DUPLICATE_AUTO_KEY: True}
        # A later partial save never resets the choice to the request default.
        response = client.patch(PATH, json={'default_reminder_minutes': 30})
        assert response.status_code == 200, response.text
        assert response.json()['goals'][AUTO_REVIEW_KEY] is False
        response = client.patch(PATH, json={DUPLICATE_AUTO_KEY: False})
        assert {key: response.json()['goals'][key] for key in ON} == OFF
        assert {key: store.setting('goals')[key] for key in ON} == OFF
        assert {key: client.get('/admin/api/settings').json()['goals'][key] for key in ON} == OFF
    assert run_snapshot(store) == {'review': False, 'duplicates': False}
