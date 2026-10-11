"""BK-02: offline fixtures exercise the real online exporter, never model secrets."""
import errno
import json
import shutil
import sqlite3
import stat
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, msg
from iris import backup
from iris.api import create_app
from iris.configuration import RuntimeConfig
from iris.db import Store, dumps
from iris.maintenance import Maintenance
from iris.memory_ops import operation
from iris.media import save_media
from test_media import PNG, send


START = datetime.fromisoformat('2026-10-10T04:00:00+08:00')


def run_backup(store, current=START):
    return backup.run_scheduled_backup(store, current=current)


def archives(store, directory=None):
    return sorted((directory or store.path.parent / 'backups').glob('iris-scheduled-*.zip'))


def manifest(path):
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read('manifest.json'))


def test_default_after_maintenance_uses_online_snapshot_private_file_and_no_secrets(store, monkeypatch, tmp_path):
    msg(store, 1, '用于定时快照的消息')
    media = save_media(store, PNG, content_type='image/png', current=START)
    send(store, [media['id']])
    monkeypatch.setattr(RuntimeConfig, '_secrets', lambda self: pytest.fail('scheduled backup must not read keys'))
    clock = lambda: START
    maintenance = Maintenance(store, clock=clock)
    rid = maintenance.request(trigger='scheduled', schedule_key='2026-10-10')
    copy = backup._copy_database
    seen = []
    def check_completed(source, destination):
        assert source.execute('SELECT state FROM maintenance_runs WHERE id=?', (rid,)).fetchone()[0] == 'completed'
        assert not store._writer.in_transaction
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(store.set_setting, 'during_scheduled_copy', True).result(timeout=3)
        seen.append(True)
        copy(source, destination)
    monkeypatch.setattr(backup, '_copy_database', check_completed)
    maintenance.run(rid)
    assert seen == [True]
    [path] = archives(store)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    description = manifest(path)
    assert description['includes_model_secrets'] is False
    assert description['scheduled_backup']['day'] == '2026-10-10'
    with zipfile.ZipFile(path) as archive:
        assert 'secrets.json' not in archive.namelist()
        assert archive.read('media/' + media['sha256']) == PNG
        db = tmp_path / 'snapshot.db'
        db.write_bytes(archive.read('iris.db'))
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT state FROM maintenance_runs WHERE id=?', (rid,)).fetchone()[0] == 'completed'
        assert conn.execute("SELECT 1 FROM runtime_settings WHERE key='during_scheduled_copy'").fetchone() is None
    [record] = backup.recent_exports(store)
    assert record['trigger'] == record['purpose'] == 'scheduled'
    assert record['status'] == 'succeeded' and record['reason'] is None
    assert record['size'] == path.stat().st_size and record['duration_ms'] >= 0
    assert record['started_at'] and record['finished_at'] and record['backup_id'] == description['backup_id']
    assert str(path) not in dumps(record)


def test_daily_claim_survives_recreated_workers_and_catchup_scheduled_manual_runs(store):
    maintenance = Maintenance(store, clock=lambda: START)
    for trigger in ('catchup', 'scheduled', 'manual'):
        rid = maintenance.request(trigger=trigger)
        maintenance.run(rid)
        Maintenance(store, clock=lambda: START).run(rid)
    assert len(archives(store)) == len(backup.recent_exports(store)) == 1
    other = Store(store.path)
    try:
        assert run_backup(other) is None
        run_backup(other, START + timedelta(days=1))
    finally:
        other.close()
    assert len(archives(store)) == 2


def test_local_calendar_day_and_backwards_clock_do_not_duplicate(store):
    run_backup(store, datetime.fromisoformat('2026-10-10T15:59:00+00:00'))
    assert run_backup(store, datetime.fromisoformat('2026-10-10T23:59:59+08:00')) is None
    run_backup(store, datetime.fromisoformat('2026-10-10T16:00:00+00:00'))
    assert run_backup(store, START) is None
    assert {manifest(p)['scheduled_backup']['day'] for p in archives(store)} == {'2026-10-10', '2026-10-11'}


def test_concurrent_workers_only_export_once(store, monkeypatch):
    reached, release = threading.Event(), threading.Event()
    original = backup._copy_database
    def wait(source, destination):
        reached.set()
        assert release.wait(5)
        original(source, destination)
    monkeypatch.setattr(backup, '_copy_database', wait)
    other = Store(store.path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_backup, store)
            assert reached.wait(5)
            assert pool.submit(run_backup, other).result(timeout=3) is None
            release.set()
            assert first.result(timeout=5)['status'] == 'succeeded'
    finally:
        release.set()
        other.close()
    assert len(backup.recent_exports(store)) == 1


def test_disabled_does_not_claim_day_and_custom_relative_directory_keep_count(store):
    store.set_setting('scheduled_backup', {'enabled': False})
    assert run_backup(store) is None
    assert backup.recent_exports(store) == [] and not archives(store)
    store.set_setting('scheduled_backup', {'directory': 'private-snapshots', 'keep': 2})
    for day in range(3):
        run_backup(store, START + timedelta(days=day))
    paths = archives(store, store.path.parent / 'private-snapshots')
    assert len(paths) == 2
    assert {manifest(p)['scheduled_backup']['day'] for p in paths} == {'2026-10-11', '2026-10-12'}
    assert not archives(store)


def test_default_keeps_seven_and_absolute_directory_is_supported(store, tmp_path):
    destination = tmp_path / 'external'
    store.set_setting('scheduled_backup', {'directory': str(destination)})
    for day in range(8):
        run_backup(store, START + timedelta(days=day))
    paths = archives(store, destination)
    assert len(paths) == 7
    assert min(manifest(p)['scheduled_backup']['day'] for p in paths) == '2026-10-11'


@pytest.mark.parametrize('settings', [{'keep': 0}, {'keep': True}, {'keep': '7'}, {'enabled': 'yes'}, {'directory': 7}, []])
def test_bad_settings_are_fixed_failures_and_do_not_export(store, settings):
    store.set_setting('scheduled_backup', settings)
    result = run_backup(store)
    assert result['status'] == 'failed' and result['reason'] == 'invalid_settings'
    assert run_backup(store) is None
    assert not archives(store)
    assert len(backup.recent_exports(store)) == 1


def test_backup_directory_cannot_be_inside_media(store):
    store.set_setting('scheduled_backup', {'directory': 'media/backups'})
    assert run_backup(store)['reason'] == 'invalid_output'
    assert not (store.path.parent / 'media/backups').exists()


def test_low_space_skips_once_keeps_old_backups_and_does_not_call_export(store, monkeypatch):
    run_backup(store)
    [old] = archives(store)
    monkeypatch.setattr(backup.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(backup, 'export_archive', lambda *a, **kw: pytest.fail('low disk must skip export'))
    result = run_backup(store, START + timedelta(days=1))
    assert result['status'] == 'skipped' and result['reason'] == 'insufficient_space'
    assert result['size'] == 0 and result['duration_ms'] >= 0
    assert old.exists() and len(archives(store)) == 1
    assert run_backup(store, START + timedelta(days=1)) is None


def test_export_error_is_recorded_without_private_exception_text_and_maintenance_completes(store, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError('private payload and path must never be recorded')
    monkeypatch.setattr(backup, 'export_archive', fail)
    maintenance = Maintenance(store, clock=lambda: START)
    rid = maintenance.request()
    maintenance.run(rid)
    assert maintenance.report(rid)['state'] == 'completed'
    [record] = backup.recent_exports(store)
    assert record['status'] == 'failed' and record['reason'] == 'export_failed'
    assert 'private payload' not in dumps(record) + caplog.text
    assert not archives(store)


def test_registered_media_failure_is_terminal_for_today_and_preserves_old_backup(store, monkeypatch):
    run_backup(store)
    store.set_setting('scheduled_backup', {'keep': 1})
    def fail(*args, **kwargs):
        raise backup.BackupError('media_integrity', 'private error body')
    monkeypatch.setattr(backup, 'export_archive', fail)
    result = run_backup(store, START + timedelta(days=1))
    assert result['reason'] == 'media_integrity'
    assert result['status'] == 'failed' and len(archives(store)) == 1


def test_retention_requires_own_filename_manifest_owner_and_regular_file(store, tmp_path):
    store.set_setting('scheduled_backup', {'keep': 1})
    run_backup(store)
    [own] = archives(store)
    folder = own.parent
    manual = folder / 'manual.zip'
    backup.export_archive(store, manual)
    description = manifest(own)
    impostor = own.with_name(own.name.replace(description['backup_id'], 'a'*32))
    shutil.copyfile(manual, impostor)
    corrupt = own.with_name(own.name.replace(description['backup_id'], 'b'*32))
    corrupt.write_bytes(b'not a zip')
    link = own.with_name(own.name.replace(description['backup_id'], 'c'*32))
    link.symlink_to(manual)
    other_store = Store(tmp_path / 'other' / 'iris.db')
    try:
        other_store.set_setting('scheduled_backup', {'directory': str(folder)})
        run_backup(other_store)
        other_path = next(p for p in archives(other_store, folder) if manifest_if_valid(p).get('scheduled_backup', {}).get('owner') not in (None, description['scheduled_backup']['owner']))
    finally:
        other_store.close()
    migration = store.path.with_name('iris.db.20260101T000000Z.bak')
    migration.write_bytes(b'migration backup')
    before = {p: p.read_bytes() for p in (manual, impostor, corrupt, other_path, migration)}
    run_backup(store, START + timedelta(days=1))
    assert not own.exists()
    assert link.is_symlink()
    assert all(p.read_bytes() == raw for p, raw in before.items())


def manifest_if_valid(path):
    try:
        return manifest(path)
    except (zipfile.BadZipFile, ValueError):
        return {}


def test_cleanup_failure_keeps_new_snapshot_and_records_fixed_warning(store, monkeypatch):
    store.set_setting('scheduled_backup', {'keep': 1})
    run_backup(store)
    [old] = archives(store)
    unlink = Path.unlink
    def fail(path, *args, **kwargs):
        if path == old:
            raise PermissionError('private filesystem description')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)
    result = run_backup(store, START + timedelta(days=1))
    assert result['status'] == 'succeeded' and result['retention_reason'] == 'cleanup_failed'
    assert len(archives(store)) == 2
    assert 'private filesystem' not in dumps(backup.recent_exports(store))


def test_interrupted_attempt_is_marked_failed_on_restart_without_second_same_day_export(store, monkeypatch):
    original = backup.export_archive
    def crash(*args, **kwargs):
        raise SystemExit('simulated process exit')
    monkeypatch.setattr(backup, 'export_archive', crash)
    with pytest.raises(SystemExit):
        run_backup(store)
    assert backup.recent_exports(store)[0]['status'] == 'running'
    monkeypatch.setattr(backup, 'export_archive', original)
    assert run_backup(store) is None
    [record] = backup.recent_exports(store)
    assert record['status'] == 'failed' and record['reason'] == 'interrupted'
    run_backup(store, START + timedelta(days=1))
    assert len(archives(store)) == 1


def test_completed_maintenance_reentry_recovers_missing_backup_and_interrupted_run_waits(store, monkeypatch):
    maintenance = Maintenance(store, clock=lambda: START)
    rid = maintenance.request()
    maintenance.run(rid, stop=lambda: True)
    assert not archives(store)
    original = backup.run_scheduled_backup
    monkeypatch.setattr(backup, 'run_scheduled_backup', lambda *a, **kw: None)
    maintenance.run(rid)
    assert maintenance.report(rid)['state'] == 'completed'
    monkeypatch.setattr(backup, 'run_scheduled_backup', original)
    maintenance.run(rid)
    assert len(archives(store)) == 1


def test_history_projects_trigger_for_old_manual_preimport_and_new_scheduled_records(store, tmp_path):
    for purpose in ('manual', 'pre_import'):
        with store.write() as conn:
            operation(conn, 'backup_export', 'backup', purpose, {'backup_id': purpose, 'purpose': purpose,
                'created_at': START.isoformat(), 'size': 1})
    backup.export_archive(store, tmp_path / 'manual.zip')
    run_backup(store)
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        response = client.get('/admin/api/backups')
    assert response.status_code == 200
    records = response.json()['items']
    assert [r['trigger'] for r in records] == ['scheduled', 'manual', 'pre_import', 'manual']
    assert all(r['status'] == 'succeeded' for r in records)
    assert 'directory' not in dumps(records) and str(tmp_path) not in dumps(records)


def test_disk_fills_during_snapshot_leaves_old_backup_and_fixed_skip_reason(store, monkeypatch):
    run_backup(store)
    [old] = archives(store)
    def fail(*args):
        raise OSError(errno.ENOSPC, 'private disk description')
    monkeypatch.setattr(backup, '_copy_database', fail)
    result = run_backup(store, START + timedelta(days=1))
    assert result['status'] == 'skipped' and result['reason'] == 'insufficient_space'
    assert archives(store) == [old]
    assert not list(old.parent.glob('.iris-export-*'))
    assert 'private disk' not in dumps(backup.recent_exports(store))


def test_racing_output_file_is_not_overwritten_or_cleaned_up(store, monkeypatch):
    link = backup.os.link
    raced = []
    def race(source, destination):
        destination.write_bytes(b'concurrent manual file')
        raced.append(destination)
        return link(source, destination)
    monkeypatch.setattr(backup.os, 'link', race)
    result = run_backup(store)
    assert result['status'] == 'failed'
    assert len(raced) == 1 and raced[0].read_bytes() == b'concurrent manual file'
    assert not list(raced[0].parent.glob('.iris-export-*'))
