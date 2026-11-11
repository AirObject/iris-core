"""C-B2: auto_v1 is the consolidation default; administrator choices survive the upgrade. No model IO."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from iris import db
from iris.api import create_app
from iris.consolidation import DEFAULTS, consolidation_settings, run_settings
from iris.db import Store
from iris.maintenance import Maintenance
from iris.memory_ops import operation
from iris.settings_api import ConsolidationSettings
from conftest import login_admin
from test_lifecycle import Clock


MIGRATION = '038_consolidation_default_auto.sql'


def pre_default_store(path, monkeypatch):
    original = db.files
    def old_resources(package):
        root = original(package)
        def joinpath(name):
            target = root.joinpath(name)
            if name == 'migrations':
                return SimpleNamespace(iterdir=lambda: [p for p in target.iterdir() if p.name < MIGRATION])
            return target
        return SimpleNamespace(joinpath=joinpath)
    with monkeypatch.context() as patch:
        patch.setattr(db, 'files', old_resources)
        return Store(path)


def stored(store):
    return store.setting('consolidation')


def snapshot_resolution(store, run_id):
    with store.read() as conn:
        raw = json.loads(conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?', (run_id,)).fetchone()[0])
        return raw['resolution'], run_settings(conn, run_id)['resolution']


def test_code_request_and_fallback_defaults_agree():
    assert DEFAULTS['resolution'] == 'auto_v1'
    assert ConsolidationSettings().resolution == 'auto_v1'
    assert ConsolidationSettings.model_fields['resolution'].default == DEFAULTS['resolution']


@pytest.mark.parametrize('precreated', [False, True])
def test_new_database_defaults_to_auto_v1(tmp_path, precreated):
    path = tmp_path / 'new.db'
    if precreated:
        path.touch()
    store = Store(path)
    try:
        assert stored(store)['resolution'] == 'auto_v1'
        with store.read() as conn:
            assert consolidation_settings(conn)['resolution'] == 'auto_v1'
            assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (MIGRATION,)).fetchone()
        rid = Maintenance(store, clock=Clock()).request()
        assert snapshot_resolution(store, rid) == ('auto_v1', 'auto_v1')
    finally:
        store.close()


def test_missing_field_falls_back_to_auto_v1(store):
    store.set_setting('consolidation', {'max_calls': 50})
    with store.read() as conn:
        assert consolidation_settings(conn)['resolution'] == 'auto_v1'


@pytest.mark.parametrize('unknown', ['conservative_v2', 'rewrite_v2', 'original_v1', 'auto_v2', '', None, 1])
def test_unknown_value_still_falls_back_to_report_only(store, unknown):
    store.set_setting('consolidation', {'resolution': unknown})
    with store.read() as conn:
        assert consolidation_settings(conn)['resolution'] == 'report_only_v1'
    rid = Maintenance(store, clock=Clock()).request()
    assert snapshot_resolution(store, rid) == ('report_only_v1', 'report_only_v1')
    with store.write() as conn:
        conn.execute('UPDATE consolidation_runs SET settings_json=? WHERE run_id=?',
                     (json.dumps({'resolution': unknown}), rid))
    with store.read() as conn:
        assert run_settings(conn, rid)['resolution'] == 'report_only_v1'


def test_missing_settings_row_falls_back_to_auto_v1(store):
    with store.write() as conn:
        conn.execute("DELETE FROM runtime_settings WHERE key='consolidation'")
        assert consolidation_settings(conn)['resolution'] == 'auto_v1'


def test_untouched_old_database_upgrades_but_unfinished_run_keeps_its_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'old.db'
    old = pre_default_store(path, monkeypatch)
    before = stored(old)
    assert before['resolution'] == 'report_only_v1'  # Written by migrations 016/019.
    with old.write() as conn:
        # Unrelated administrator activity is not a consolidation choice.
        operation(conn, 'persona_settings_saved', 'settings', 'persona', {'publish_mode': 'all_manual'})
        operation(conn, 'lifecycle_saved', 'settings', 'lifecycle', {'maintenance_time': '04:00'})
    rid = Maintenance(old, clock=Clock()).request()
    assert snapshot_resolution(old, rid) == ('report_only_v1', 'report_only_v1')
    old.close()
    for _ in range(2):
        upgraded = Store(path)
        try:
            assert stored(upgraded) == {**before, 'resolution': 'auto_v1'}
            with upgraded.read() as conn:
                assert consolidation_settings(conn)['resolution'] == 'auto_v1'
            assert snapshot_resolution(upgraded, rid) == ('report_only_v1', 'report_only_v1')
        finally:
            upgraded.close()


@pytest.mark.parametrize('saved_value,changes', [
    ('report_only_v1', {'resolution': 'report_only_v1'}),
    ('report_only_v1', {'merge_enabled': False}),
    ('report_only_v1', {'maintenance_time': '04:15'}),
    ('report_only_v1', {}),
    ('auto_v1', {'resolution': 'auto_v1'}),
])
def test_any_admin_save_preserves_stored_resolution(tmp_path, monkeypatch, saved_value, changes):
    # A real PATCH stores the whole effective object, even when only a switch,
    # a budget or the time changed; every save keeps whatever value it stored.
    path = tmp_path / 'old.db'
    old = pre_default_store(path, monkeypatch)
    old.set_setting('consolidation', {**stored(old), 'resolution': saved_value})
    with old.write() as conn:
        operation(conn, 'consolidation_settings_saved', 'settings', 'consolidation', changes)
    before = stored(old)
    old.close()
    upgraded = Store(path)
    try:
        assert stored(upgraded) == before
        with upgraded.read() as conn:
            assert consolidation_settings(conn)['resolution'] == saved_value
        rid = Maintenance(upgraded, clock=Clock()).request()
        assert snapshot_resolution(upgraded, rid) == (saved_value, saved_value)
    finally:
        upgraded.close()


@pytest.mark.parametrize('value', ['conservative_v2', 'rewrite_v2', None])
def test_unsaved_unknown_or_missing_old_value_is_not_rewritten(tmp_path, monkeypatch, value):
    path = tmp_path / 'old.db'
    old = pre_default_store(path, monkeypatch)
    settings = {key: item for key, item in stored(old).items() if key != 'resolution'}
    if value is not None:
        settings['resolution'] = value
    old.set_setting('consolidation', settings)
    old.close()
    upgraded = Store(path)
    try:
        assert stored(upgraded) == settings
        with upgraded.read() as conn:
            # Unknown stays conservative; a missing field uses the code default.
            assert consolidation_settings(conn)['resolution'] == ('auto_v1' if value is None else 'report_only_v1')
    finally:
        upgraded.close()


def test_settings_api_shows_default_and_can_return_to_report_only(store):
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        login_admin(client)
        client.app.state.scheduler.stop()
        assert client.get('/admin/api/settings').json()['consolidation']['resolution'] == 'auto_v1'
        response = client.patch('/admin/api/settings/consolidation', json={'resolution': 'report_only_v1'})
        assert response.status_code == 200, response.text
        assert response.json()['consolidation']['resolution'] == 'report_only_v1'
        assert stored(store)['resolution'] == 'report_only_v1'
        # A later partial save never resets the choice to the request default.
        response = client.patch('/admin/api/settings/consolidation', json={'max_calls': 12})
        assert response.status_code == 200, response.text
        assert response.json()['consolidation']['resolution'] == 'report_only_v1'
        assert client.get('/admin/api/settings').json()['consolidation']['resolution'] == 'report_only_v1'
        response = client.patch('/admin/api/settings/consolidation', json={'resolution': 'rewrite_v2'})
        assert response.status_code == 400
        assert stored(store)['resolution'] == 'report_only_v1'
        response = client.patch('/admin/api/settings/consolidation', json={'resolution': 'auto_v1'})
        assert response.status_code == 200, response.text
        assert stored(store)['resolution'] == 'auto_v1'
        with store.read() as conn:
            saved = [json.loads(r[0]) for r in conn.execute(
                "SELECT details_json FROM admin_operations WHERE action='consolidation_settings_saved' ORDER BY id")]
        assert saved == [{'resolution': 'report_only_v1'}, {'max_calls': 12}, {'resolution': 'auto_v1'}]
