"""Manual-by-default publication, conservative upgrades and existing pending counts."""
import json
import sqlite3
from contextlib import closing
from importlib.resources import files

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris.db import Store, dumps
from iris.memory_ops import setup_role
from iris.persona import persona_settings
from test_persona import FakeGateway
from test_persona_admin import current, finished, start


def test_new_install_and_absent_setting_default_to_manual(store):
    from iris.settings_api import PersonaSettings
    assert store.setting('persona_publish_mode') == 'all_manual'
    assert setup_role(store, 'Iris', '我来自云城。') == '我是Iris。初始设定：我来自云城。'
    with store.read() as conn:
        assert persona_settings(conn)['publish_mode'] == 'all_manual'
        assert conn.execute('SELECT COUNT(*) FROM persona_versions WHERE is_current=1').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0
    with store.write() as conn:
        conn.execute("DELETE FROM runtime_settings WHERE key='persona_publish_mode'")
    with store.read() as conn:
        assert persona_settings(conn)['publish_mode'] == 'all_manual'
    assert PersonaSettings(goal='保持稳定。').publish_mode == 'all_manual'


@pytest.fixture
def manual_client(store):
    setup_role(store, 'Iris', '我来自云城。')
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        client.app.state.persona_jobs.engine.gateway = FakeGateway(store, {
            'sentences': [{'text': '初始设定中，我来自云城。', 'basis': ['M1']}]})
        yield client


def pending_count(client):
    response = client.get('/admin/api/persona/versions?status=pending&limit=1')
    assert response.status_code == 200
    return response.json()['total']


@pytest.mark.parametrize('degree', ['small', 'medium', 'large'])
def test_default_keeps_checked_candidate_pending_until_admin_confirmation(manual_client, store, degree):
    client = manual_client
    assert client.get('/admin/api/settings').json()['persona']['publish_mode'] == 'all_manual'
    client.app.state.persona_jobs.engine.gateway.degree = degree
    before = current(client)['current']
    with store.read() as conn:
        notifications = conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]
    assert pending_count(client) == 0
    first = finished(client, start(client))
    assert first['state'] == 'pending'
    assert current(client)['current'] == before and pending_count(client) == 1
    detail = client.get(f"/admin/api/persona/versions/{first['version_id']}").json()
    assert detail['checks']['deterministic']['passed'] and detail['checks']['model']['sentences']
    assert detail['sentences'][0]['basis'][0]['revision'] == 1
    assert detail['settings']['publish_mode'] == 'all_manual'
    second = finished(client, start(client))
    assert second['state'] == 'pending' and pending_count(client) == 1
    assert client.get(f"/admin/api/persona/versions/{first['version_id']}").json()['status'] == 'superseded'
    confirmed = client.post(f"/admin/api/persona/versions/{second['version_id']}/confirm",
                            json={'expected_version': before['id']})
    assert confirmed.status_code == 200 and pending_count(client) == 0
    assert current(client)['current']['id'] == second['version_id']
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == notifications


def test_manual_default_still_rejects_failed_checks(manual_client):
    client = manual_client
    client.app.state.persona_jobs.engine.gateway.supported = False
    assert finished(client, start(client))['state'] == 'rejected'
    assert current(client)['current']['id'] == 1 and pending_count(client) == 0


@pytest.mark.parametrize('mode,degree,status', [('small_medium_auto','small','current'),
    ('small_medium_auto','large','pending'), ('all_auto','large','current')])
def test_admin_can_explicitly_enable_automatic_modes(manual_client, mode, degree, status):
    client = manual_client
    response = client.patch('/admin/api/settings/persona', json={'publish_mode': mode})
    assert response.status_code == 200 and response.json()['persona']['publish_mode'] == mode
    client.app.state.persona_jobs.engine.gateway.degree = degree
    assert finished(client, start(client))['state'] == status


def test_partial_settings_edit_does_not_reset_manual_default(manual_client):
    response = manual_client.patch('/admin/api/settings/persona', json={'rules': '逐句核对依据。'})
    assert response.status_code == 200
    assert response.json()['persona']['publish_mode'] == 'all_manual'


@pytest.mark.parametrize('mode,actor,action,fields,expected', [
    ('small_medium_auto', None, None, [], 'all_manual'),
    ('small_medium_auto', 'admin', 'persona_settings_saved', ['publish_mode'], 'small_medium_auto'),
    ('small_medium_auto', 'admin', 'persona_settings_saved', ['goal'], 'small_medium_auto'),
    ('small_medium_auto', 'admin', 'persona_settings_saved', ['rules'], 'small_medium_auto'),
    ('small_medium_auto', 'persona', 'persona_settings_saved', ['goal'], 'all_manual'),
    ('small_medium_auto', 'admin', 'lifecycle_saved', [], 'all_manual'),
    ('small_medium_auto', 'admin', 'persona_edit', [], 'all_manual'),
    ('all_auto', None, None, [], 'all_auto'),
    ('all_manual', None, None, [], 'all_manual'),
    (None, None, None, [], 'all_manual'),
    (None, 'admin', 'persona_settings_saved', ['rules'], 'small_medium_auto'),
])
def test_upgrade_changes_only_untouched_old_default(tmp_path, mode, actor, action, fields, expected):
    path = tmp_path / 'old.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.create_function('iris_terms', 1, lambda text: text)
        conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        for script in sorted(files('iris').joinpath('migrations').iterdir()):
            if script.name.endswith('.sql') and script.name < '020':
                conn.executescript(script.read_text(encoding='utf-8'))
                conn.execute("INSERT INTO schema_migrations VALUES(?,'2026-10-09')", (script.name,))
                conn.commit()
        if mode is None:
            conn.execute("DELETE FROM runtime_settings WHERE key='persona_publish_mode'")
        else:
            conn.execute("UPDATE runtime_settings SET value_json=? WHERE key='persona_publish_mode'", (dumps(mode),))
        conn.execute("INSERT INTO runtime_settings VALUES('persona_rules',?)", (dumps('保留已保存的监管要求。'),))
        if actor:
            conn.execute("""INSERT INTO admin_operations(actor,action,object_type,object_id,details_json,created_at)
                VALUES(?,?,'settings','persona',?,'2026-10-09')""", (actor, action, dumps({'fields':fields})))
        conn.execute("""INSERT INTO persona_versions(content,created_at,is_current,status)
            VALUES('我是Iris。','2026-10-09',1,'current')""")
        material = dumps({'settings': {'publish_mode':'small_medium_auto'}})
        conn.execute("""INSERT INTO persona_versions(content,created_at,is_current,status,source,material_json)
            VALUES('候选正文。','2026-10-09',0,'pending','regenerate',?)""", (material,))
        conn.execute("""INSERT INTO persona_attempts(source,base_version_id,state,material_json,created_at)
            VALUES('regenerate',1,'queued',?,'2026-10-09')""", (material,))
        conn.commit()
        before = {table: conn.execute('SELECT * FROM '+table).fetchall()
                  for table in ('persona_versions','persona_attempts','admin_operations')}
    with closing(Store(path)) as upgraded:
        assert upgraded.setting('persona_publish_mode') == expected
        assert upgraded.setting('persona_rules') == '保留已保存的监管要求。'
        with upgraded.read() as conn:
            for table, rows in before.items():
                assert [tuple(r) for r in conn.execute('SELECT * FROM '+table)] == rows
    assert len(list(tmp_path.glob('old.db.*.bak'))) == 1
    with closing(Store(path)) as reopened:
        assert reopened.setting('persona_publish_mode') == expected
        with reopened.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version LIKE '020_%'").fetchone()[0] == 1
