"""Backup contracts use synthetic private data; no model configuration is loaded."""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import stat
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, msg
from iris import backup, persona
from iris.api import create_app
from iris.cli import main
from iris.configuration import RuntimeConfig
from iris.db import Store
from iris.goals import Goals
from iris.memory_ops import edit_memory, setup_role
from iris.process_lock import StoreLease, LeaseBusyError
from iris.tokens import Tokens


def seed(store):
    setup_role(store, 'Iris', '喜欢阅读')
    msg(store, 1, '备份里的原始消息')
    with store.read() as conn:
        memory = dict(conn.execute('SELECT * FROM memories LIMIT 1').fetchone())
        version = conn.execute('SELECT id FROM persona_versions WHERE is_current=1').fetchone()[0]
    assert edit_memory(store, memory['id'], memory['revision'], content='喜欢读历史书')
    persona.admin_edit(store, '我是 Iris，喜欢读书。', expected_version=version)
    Goals(store).create(content='明天整理书架', entry_id='A', host='backup-test', actor='backup-test')
    Tokens(store).create(host='backup-test', scope={'kind': 'prefix', 'prefix': 'A'})
    media = store.path.parent / 'media' / '图片'
    media.mkdir(parents=True)
    (media / 'one.bin').write_bytes(b'synthetic-media\x00\xff')


def rows(path, table):
    with sqlite3.connect(path) as conn:
        return conn.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()


def manifest(path):
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read('manifest.json'))


def rewrite(path, *, metadata=None, entries=None):
    with zipfile.ZipFile(path) as archive:
        data = {name: archive.read(name) for name in archive.namelist()}
    value = json.loads(data['manifest.json'])
    if metadata:
        metadata(value)
    if entries:
        entries(data, value)
    data['manifest.json'] = json.dumps(value, ensure_ascii=False).encode()
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in data.items():
            archive.writestr(name, content)


def export(store, tmp_path, **options):
    path = tmp_path / 'snapshot.zip'
    backup.export_archive(store, path, **options)
    return path


def test_round_trip_preserves_cognition_credentials_and_media(store, tmp_path):
    seed(store)
    path = export(store, tmp_path)
    description = manifest(path)
    assert description['format_version'] == 1
    assert description['migration_version'] == backup.migration_versions()[-1]
    assert description['includes_model_secrets'] is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {'manifest.json', *description['files']}
        for name, info in description['files'].items():
            data = archive.read(name)
            assert len(data) == info['size']
            assert hashlib.sha256(data).hexdigest() == info['sha256']
    target = tmp_path / 'restored' / 'iris.db'
    result = backup.import_archive(path, target)
    assert result['backup_id'] == description['backup_id']
    for table in ('memories', 'memory_revisions', 'messages', 'sources', 'goals', 'goal_sources',
                  'persona_versions', 'host_tokens', 'schema_migrations'):
        assert rows(store.path, table) == rows(target, table), table
    assert (target.parent / 'media/图片/one.bin').read_bytes() == b'synthetic-media\x00\xff'
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT actor FROM admin_operations WHERE action='backup_import'").fetchone()[0] == 'local_cli'
    assert backup.recent_exports(store)[0]['backup_id'] == description['backup_id']


def test_online_snapshot_pins_transactions_and_does_not_take_writer_lock(store, tmp_path, monkeypatch):
    msg(store, 1, 'before')
    copy = backup._copy_database
    written = threading.Event()
    def concurrent_copy(source, destination):
        def write():
            # Both rows belong to one commit after the read snapshot was pinned.
            with store.write() as conn:
                conn.execute("INSERT INTO runtime_settings VALUES('during_export','true')")
                conn.execute("UPDATE messages SET content='after'")
            written.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(write)
            assert written.wait(3), 'backup held the single writer lock'
            copy(source, destination)
            job.result()
    monkeypatch.setattr(backup, '_copy_database', concurrent_copy)
    path = export(store, tmp_path)
    target = tmp_path / 'restored/iris.db'
    backup.import_archive(path, target)
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT content FROM messages').fetchone()[0] == 'before'
        assert conn.execute("SELECT 1 FROM runtime_settings WHERE key='during_export'").fetchone() is None
    with store.read() as conn:
        assert conn.execute('SELECT content FROM messages').fetchone()[0] == 'after'


def synthetic_keys(store):
    # Random, test-only bytes are never printed or committed as credentials.
    runtime = RuntimeConfig(store)
    reference = 'test-reference'
    marker = os.urandom(32).hex()
    runtime._write_secrets({reference: marker})
    store.set_setting('models', {'chat': {'base_url': 'http://invalid.test', 'model': 'fake', 'api_key_ref': reference}})
    return runtime, marker


def test_default_export_never_opens_secrets_and_removes_only_snapshot_references(store, tmp_path, monkeypatch):
    runtime, marker = synthetic_keys(store)
    monkeypatch.setattr(RuntimeConfig, '_secrets', lambda self: pytest.fail('default export read credentials'))
    path = export(store, tmp_path)
    with zipfile.ZipFile(path) as archive:
        assert 'secrets.json' not in archive.namelist()
        assert all(marker.encode() not in archive.read(n) for n in archive.namelist())
    assert store.setting('models')['chat']['api_key_ref'] == 'test-reference'
    target = tmp_path / 'restored/iris.db'
    backup.import_archive(path, target)
    restored = Store(target)
    try:
        assert 'api_key_ref' not in restored.setting('models')['chat']
        assert not (target.parent / 'secrets.json').exists()
    finally:
        restored.close()


def test_explicit_secret_export_roundtrips_without_logging(store, tmp_path, caplog, capsys):
    runtime, marker = synthetic_keys(store)
    path = export(store, tmp_path, include_secrets=True)
    assert manifest(path)['includes_model_secrets'] is True
    target = tmp_path / 'restored/iris.db'
    backup.import_archive(path, target)
    restored = Store(target)
    try:
        assert RuntimeConfig(restored).load()['chat'].api_key == marker
        assert stat.S_IMODE((target.parent / 'secrets.json').stat().st_mode) == 0o600
        with restored.read() as conn:
            assert marker not in str(list(conn.execute('SELECT * FROM admin_operations')))
    finally:
        restored.close()
    assert marker not in caplog.text + capsys.readouterr().out
    assert str(path) not in caplog.text


@pytest.mark.parametrize('change', [
    lambda m: m.update(format_version=99),
    lambda m: m.update(migration_version='999_future.sql'),
    lambda m: m.update(migrations=['999_future.sql']),
    lambda m: m.update(includes_model_secrets=True),
    lambda m: m['files']['iris.db'].update(sha256='0'*64),
    lambda m: m['files']['iris.db'].update(size=1),
])
def test_incompatible_or_inconsistent_manifest_rejected_before_target_changes(store, tmp_path, change):
    path = export(store, tmp_path)
    rewrite(path, metadata=change)
    target = tmp_path / 'restored/iris.db'
    with pytest.raises(backup.BackupError):
        backup.import_archive(path, target)
    assert not target.exists()


@pytest.mark.parametrize('kind', ['truncated', 'bad_database', 'unlisted', 'traversal', 'duplicate', 'symlink'])
def test_corrupt_or_unsafe_archive_rejected(store, tmp_path, kind):
    path = export(store, tmp_path)
    if kind == 'truncated':
        path.write_bytes(path.read_bytes()[:-30])
    elif kind == 'bad_database':
        def corrupt(data, metadata):
            data['iris.db'] = b'not a SQLite database'
            metadata['files']['iris.db'] = {'sha256': hashlib.sha256(data['iris.db']).hexdigest(), 'size': len(data['iris.db'])}
        rewrite(path, entries=corrupt)
    else:
        with zipfile.ZipFile(path, 'a') as archive:
            name = {'unlisted': 'unknown', 'traversal': '../outside', 'duplicate': 'manifest.json', 'symlink': 'media/link'}[kind]
            info = zipfile.ZipInfo(name)
            if kind == 'symlink':
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with pytest.warns(UserWarning) if kind == 'duplicate' else contextlib.nullcontext():
                archive.writestr(info, b'invalid')
    target = tmp_path / 'restored/iris.db'
    with pytest.raises(backup.BackupError):
        backup.import_archive(path, target)
    assert not target.exists() and not (tmp_path / 'outside').exists()


def test_overwrite_requires_confirmation_and_keeps_verified_preimport_backup(store, tmp_path):
    seed(store)
    path = export(store, tmp_path)
    target = tmp_path / 'existing/iris.db'
    old = Store(target)
    old.set_setting('old-instance', True)
    synthetic_keys(old)
    old.close()
    (target.parent / 'notes.txt').write_text('keep unrelated files', encoding='utf-8')
    with pytest.raises(backup.BackupError, match='confirm-overwrite'):
        backup.import_archive(path, target)
    assert rows(target, 'runtime_settings')
    result = backup.import_archive(path, target, confirm_overwrite=True, backup_include_secrets=True)
    previous = Path(result['previous_backup'])
    assert previous.exists() and previous.parent != target.parent
    assert manifest(previous)['includes_model_secrets'] is True
    assert stat.S_IMODE(previous.stat().st_mode) == 0o600
    assert (target.parent / 'notes.txt').read_text() == 'keep unrelated files'
    assert not (target.parent / 'secrets.json').exists()
    undo = tmp_path / 'undo/iris.db'
    backup.import_archive(previous, undo)
    check = Store(undo)
    try:
        assert check.setting('old-instance') is True
        assert RuntimeConfig(check).load()['chat'].api_key
    finally:
        check.close()


def test_import_failure_before_publication_leaves_all_existing_data_intact(store, tmp_path, monkeypatch):
    path = export(store, tmp_path)
    target = tmp_path / 'existing/iris.db'
    old = Store(target)
    seed(old)
    old.close()
    original_exchange = backup._exchange_directories
    def fail(left, right):
        # Service lock still owns the same inode in the staged replacement.
        for parent in (left, right):
            with pytest.raises(LeaseBusyError):
                with StoreLease(parent / 'iris.db'):
                    pytest.fail('replacement lost the service lease')
        raise OSError('synthetic failure')
    monkeypatch.setattr(backup, '_exchange_directories', fail)
    with pytest.raises(backup.BackupError):
        backup.import_archive(path, target, confirm_overwrite=True)
    # Publication failure preserves every cognition table and the media tree.
    assert rows(target, 'memories') and (target.parent / 'media/图片/one.bin').exists()
    monkeypatch.setattr(backup, '_exchange_directories', original_exchange)


def test_running_service_blocks_import_and_offline_export(store, tmp_path, capsys):
    path = export(store, tmp_path)
    with StoreLease(store.path):
        with pytest.raises(backup.BackupError, match='停止服务'):
            backup.import_archive(path, store.path, confirm_overwrite=True)
        assert main(['--db', str(store.path), 'backup', 'export', '--out', str(tmp_path/'blocked.zip')]) == 1
    assert not (tmp_path/'blocked.zip').exists()
    capsys.readouterr()


def test_old_migration_is_validated_then_upgraded_offline(store, tmp_path):
    path = export(store, tmp_path)
    old_db = tmp_path / 'old.db'
    versions = backup.migration_versions()[:3]
    with sqlite3.connect(old_db) as conn:
        conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        for version in versions:
            conn.executescript(files('iris').joinpath('migrations', version).read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations VALUES(?,?)', (version, '2026-01-01'))
        conn.execute("INSERT INTO runtime_settings VALUES('old-marker','true')")
    def replace_db(data, metadata):
        data['iris.db'] = old_db.read_bytes()
        metadata['migrations'] = versions
        metadata['migration_version'] = versions[-1]
        metadata['files']['iris.db'] = {'sha256': hashlib.sha256(data['iris.db']).hexdigest(), 'size': len(data['iris.db'])}
    rewrite(path, entries=replace_db)
    target = tmp_path / 'restored/iris.db'
    backup.import_archive(path, target)
    restored = Store(target)
    try:
        assert restored.setting('old-marker') is True
        assert [r[0] for r in rows(target, 'schema_migrations')] == backup.migration_versions()
    finally:
        restored.close()


def test_media_mutation_during_snapshot_fails_without_publishing(store, tmp_path, monkeypatch):
    from iris.media import save_media
    from test_media import PNG
    item = save_media(store, PNG, content_type='image/png')
    copy = backup._copy_database
    def change(source, target):
        copy(source, target)
        (store.path.parent / 'media' / item['sha256']).write_bytes(b'changed')
    monkeypatch.setattr(backup, '_copy_database', change)
    with pytest.raises(backup.BackupError, match='媒体'):
        export(store, tmp_path)
    assert not (tmp_path / 'snapshot.zip').exists()
    assert backup.recent_exports(store) == []


def test_export_rejects_media_symlinks_and_never_overwrites_output(store, tmp_path):
    media = store.path.parent / 'media'
    media.mkdir()
    (media / 'link').symlink_to(tmp_path / 'outside')
    with pytest.raises(backup.BackupError):
        export(store, tmp_path)
    (media / 'link').unlink()
    path = export(store, tmp_path)
    before = path.read_bytes()
    with pytest.raises(backup.BackupError):
        export(store, tmp_path)
    assert path.read_bytes() == before


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as value:
        value.app.state.scheduler.stop()
        login_admin(value)
        yield value


def test_admin_download_and_history_are_authenticated_csrf_protected_and_private(client, store):
    seed_marker = 'runtime-only'
    store.set_setting('backup-check', seed_marker)
    assert client.post('/admin/api/backups/export', json={}, headers={'X-Iris-CSRF': ''}).status_code == 403
    response = client.post('/admin/api/backups/export', json={})
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/zip'
    assert response.headers['x-iris-backup-includes-secrets'] == 'false'
    assert 'attachment' in response.headers['content-disposition']
    assert response.headers['cache-control'] == 'no-store'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert 'secrets.json' not in archive.namelist()
    listing = client.get('/admin/api/backups').json()
    assert listing['import_mode'] == 'offline'
    assert len(listing['items']) == 1 and listing['items'][0]['includes_model_secrets'] is False
    assert 'path' not in listing['items'][0]
    assert seed_marker not in json.dumps(listing)
    assert client.post('/admin/api/backups/export', json={'include_secrets': 'yes'}).status_code == 400
    client.cookies.clear()
    assert client.get('/admin/api/backups').status_code == 401
    assert client.post('/admin/api/backups/export', json={}).status_code == 401


def test_admin_secret_download_is_explicit_in_filename_header_and_history(client, store):
    runtime, marker = synthetic_keys(store)
    response = client.post('/admin/api/backups/export', json={'include_secrets': True})
    assert response.status_code == 200
    assert response.headers['x-iris-backup-includes-secrets'] == 'true'
    assert 'with-secrets' in response.headers['content-disposition']
    assert client.get('/admin/api/backups').json()['items'][0]['includes_model_secrets'] is True
    assert marker not in str(response.headers)


def test_cli_export_and_import_do_not_load_model_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('iris.cli.load_test_models', lambda *a, **k: pytest.fail('loaded model config'))
    db = tmp_path / 'source/iris.db'
    store = Store(db)
    store.set_setting('cli', True)
    store.close()
    path = tmp_path / 'cli.zip'
    assert main(['--data-dir', str(db.parent), 'backup', 'export', '--out', str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['includes_model_secrets'] is False
    dest = tmp_path / 'target'
    assert main(['--data-dir', str(dest), 'backup', 'import', str(path)]) == 0
    assert main(['--data-dir', str(dest), 'backup', 'import', str(path)]) == 1
    assert 'confirm-overwrite' in capsys.readouterr().err
    assert main(['--data-dir', str(dest), 'backup', 'import', str(path), '--confirm-overwrite']) == 0
    summary = json.loads(capsys.readouterr().out)
    assert manifest(Path(summary['previous_backup']))['includes_model_secrets'] is False


def test_configuration_can_change_during_copy_without_mismatching_included_keys(store, tmp_path, monkeypatch):
    runtime, marker = synthetic_keys(store)
    copy = backup._copy_database
    def rotate(source, destination):
        from iris.models import ModelConfig
        runtime.save({'chat': ModelConfig('http://new.invalid', os.urandom(32).hex(), 'new-model')})
        copy(source, destination)
    monkeypatch.setattr(backup, '_copy_database', rotate)
    path = export(store, tmp_path, include_secrets=True, runtime=runtime)
    monkeypatch.setattr(backup, '_copy_database', copy)
    target = tmp_path / 'restored/iris.db'
    backup.import_archive(path, target)
    restored = Store(target)
    try:
        config = RuntimeConfig(restored).load()['chat']
        assert config.api_key == marker and config.model == 'fake'
        assert runtime.load()['chat'].model == 'new-model'
    finally:
        restored.close()


def test_service_and_configuration_leases_survive_the_atomic_exchange(store, tmp_path, monkeypatch):
    path = export(store, tmp_path)
    target = tmp_path / 'restored/iris.db'
    exchange = backup._exchange_directories
    def checked(left, right):
        exchange(left, right)
        for parent in (left, right):
            for name in ('iris.db', '.configuration'):
                with pytest.raises(LeaseBusyError):
                    with StoreLease(parent / name):
                        pytest.fail('a running import lost exclusivity')
    monkeypatch.setattr(backup, '_exchange_directories', checked)
    backup.import_archive(path, target)
    with StoreLease(target), StoreLease(target.parent / '.configuration'):
        pass


def test_corrupt_archive_cannot_touch_confirmed_existing_destination(store, tmp_path):
    path = export(store, tmp_path)
    rewrite(path, metadata=lambda m: m['files']['iris.db'].update(sha256='0'*64))
    target = tmp_path / 'existing/iris.db'
    old = Store(target)
    old.set_setting('unchanged', True)
    old.close()
    before = target.read_bytes()
    with pytest.raises(backup.BackupError):
        backup.import_archive(path, target, confirm_overwrite=True)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob('existing.pre-import-*.zip'))


def test_failed_preimport_backup_cannot_replace_existing_data(store, tmp_path, monkeypatch):
    path = export(store, tmp_path)
    target = tmp_path / 'existing/iris.db'
    old = Store(target)
    old.set_setting('unchanged', True)
    old.close()
    before = rows(target, 'runtime_settings')
    def fail(*args, **kwargs):
        raise backup.BackupError('export_failed', 'synthetic export failure')
    monkeypatch.setattr(backup, 'export_archive', fail)
    with pytest.raises(backup.BackupError):
        backup.import_archive(path, target, confirm_overwrite=True)
    assert rows(target, 'runtime_settings') == before


def test_preimport_backup_does_not_upgrade_or_change_the_old_database_on_failure(store, tmp_path, monkeypatch):
    path = export(store, tmp_path)
    target = tmp_path / 'existing/iris.db'
    target.parent.mkdir()
    with sqlite3.connect(target) as conn:
        conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        for version in backup.migration_versions()[:3]:
            conn.executescript(files('iris').joinpath('migrations', version).read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations VALUES(?,?)', (version, '2026-01-01'))
        conn.execute("INSERT INTO runtime_settings VALUES('old-marker','true')")
    before = rows(target, 'schema_migrations')
    def fail(*args):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(backup, '_exchange_directories', fail)
    with pytest.raises(backup.BackupError):
        backup.import_archive(path, target, confirm_overwrite=True)
    assert rows(target, 'schema_migrations') == before
    previous, = tmp_path.glob('existing.pre-import-*.zip')
    assert manifest(previous)['migrations'] == [r[0] for r in before]


def test_model_key_reference_mismatch_rejected_even_with_valid_checksums(store, tmp_path):
    synthetic_keys(store)
    path = export(store, tmp_path, include_secrets=True)
    def mismatch(data, metadata):
        data['secrets.json'] = b'{"format_version":1,"keys":{}}'
        metadata['files']['secrets.json'] = {'sha256': hashlib.sha256(data['secrets.json']).hexdigest(), 'size': len(data['secrets.json'])}
    rewrite(path, entries=mismatch)
    target = tmp_path / 'restored/iris.db'
    with pytest.raises(backup.BackupError):
        backup.import_archive(path, target)
    assert not target.exists()


def test_admin_online_export_allows_http_ingest_and_fake_learning(client, store, tmp_path, monkeypatch):
    from conftest import FakeGateway, authorize_host, batch
    authorize_host(client)
    copy = backup._copy_database
    copying = threading.Event()
    proceed = threading.Event()
    def wait_for_writes(source, target):
        copying.set()
        assert proceed.wait(5)
        copy(source, target)
    monkeypatch.setattr(backup, '_copy_database', wait_for_writes)
    with ThreadPoolExecutor(max_workers=1) as pool:
        downloading = pool.submit(client.post, '/admin/api/backups/export', json={})
        try:
            assert copying.wait(5)
            for number in (1, 2, 3, 4):
                response = client.post('/api/v1/entries/online/messages', json={
                    'sender': '读者', 'content': '在导出期间发送', 'dedupe_key': str(number),
                    'occurred_at': f'2026-10-10T10:00:0{number}+08:00'})
                assert response.status_code == 200
            formed, result = batch(store, FakeGateway(), entry='online')
            with store.read() as conn:
                assert conn.execute('SELECT state FROM batches WHERE id=?', (formed.id,)).fetchone()[0] == 'succeeded'
        finally:
            proceed.set()
        response = downloading.result(timeout=5)
    assert response.status_code == 200
    path = tmp_path / 'online.zip'
    path.write_bytes(response.content)
    target = tmp_path / 'restored/iris.db'
    backup.import_archive(path, target)
    assert not rows(target, 'messages')


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_process_death_at_publication_has_only_complete_old_or_new_directory(store, tmp_path, phase):
    import subprocess
    import sys
    seed(store)
    path = export(store, tmp_path)
    target = tmp_path / 'existing/iris.db'
    old = Store(target)
    old.set_setting('old-instance', True)
    old.close()
    (target.parent / 'media').mkdir()
    (target.parent / 'media/old.bin').write_bytes(b'old')
    script = '''
import os, sys
from pathlib import Path
from iris import backup
exchange = backup._exchange_directories
def crash(left, right):
    if sys.argv[3] == 'after':
        exchange(left, right)
    os._exit(73)
backup._exchange_directories = crash
backup.import_archive(Path(sys.argv[1]), Path(sys.argv[2]), confirm_overwrite=True)
'''
    completed = subprocess.run([sys.executable, '-c', script, str(path), str(target), phase], capture_output=True)
    assert completed.returncode == 73
    if phase == 'before':
        assert not rows(target, 'memories')
        assert (target.parent / 'media/old.bin').read_bytes() == b'old'
        assert not (target.parent / 'media/图片/one.bin').exists()
    else:
        assert rows(target, 'memories') == rows(store.path, 'memories')
        assert not (target.parent / 'media/old.bin').exists()
        assert (target.parent / 'media/图片/one.bin').read_bytes() == b'synthetic-media\x00\xff'
    with StoreLease(target):
        pass


def registered_media(store):
    import base64
    from iris.media import save_media
    from iris.queue import add_message
    content = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC')
    first = save_media(store, content, content_type='image/png', understanding_text='窗边有猫。')
    second = save_media(store, content, content_type='image/png', understanding_text='共享图片的另一段说明。')
    add_message(store, entry_id='A', entry_name='A', platform='test', entry_kind='group',
                kind='message', sender='读者', content='请看图片', occurred_at='2026-10-10T10:00:00+08:00',
                dedupe_key='registered-image', media_ids=[first['id'], second['id']])
    return first, content


def test_registered_media_objects_references_and_shared_bytes_round_trip(store, tmp_path):
    item, content = registered_media(store)
    path = export(store, tmp_path)
    target = tmp_path / 'restored/iris.db'
    backup.import_archive(path, target)
    for table in ('media_files', 'media_objects', 'message_media', 'messages'):
        assert rows(target, table) == rows(store.path, table)
    assert len(rows(target, 'media_files')) == 1 and len(rows(target, 'media_objects')) == 2
    assert (target.parent / 'media' / item['sha256']).read_bytes() == content


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_export_rejects_missing_or_corrupt_registered_media(store, tmp_path, damage):
    item, _ = registered_media(store)
    media = store.path.parent / 'media' / item['sha256']
    if damage == 'missing':
        media.unlink()
    else:
        media.write_bytes(b'corrupt registered media')
    with pytest.raises(backup.BackupError, match='媒体'):
        export(store, tmp_path)
    assert not (tmp_path / 'snapshot.zip').exists()
    assert backup.recent_exports(store) == []


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_import_checks_registered_media_beyond_archive_checksums(store, tmp_path, damage):
    item, _ = registered_media(store)
    path = export(store, tmp_path)
    name = 'media/' + item['sha256']
    def corrupt(data, metadata):
        if damage == 'missing':
            del data[name]
            del metadata['files'][name]
        else:
            data[name] = b'corrupt registered media'
            metadata['files'][name] = {'size': len(data[name]), 'sha256': hashlib.sha256(data[name]).hexdigest()}
    rewrite(path, entries=corrupt)
    target = tmp_path / 'restored/iris.db'
    with pytest.raises(backup.BackupError, match='媒体'):
        backup.import_archive(path, target)
    assert not target.exists()
