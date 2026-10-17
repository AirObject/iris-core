"""IM-02–04: independent byte retention, durable cleanup and backup integrity."""
import hashlib
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest

from iris import backup, media
from iris.db import Store
from iris.maintenance import Maintenance, PHASES
from iris.queue import form_batch
from test_backup import rewrite
from test_lifecycle_api import client
from test_media import PNG, STAMP, Vision, file_path, maintain, saved, send
from test_retrieval import put


DUE = STAMP + timedelta(days=8)


@pytest.fixture(autouse=True)
def no_scheduled_backup(store):
    store.set_setting('scheduled_backup', {'enabled': False})


def file_row(store, item):
    with store.read() as conn:
        return dict(conn.execute('SELECT * FROM media_files WHERE sha256=?', (item['sha256'],)).fetchone())


def media_run(store, at=DUE):
    worker = Maintenance(store, clock=lambda: at)
    rid = worker.request()
    with store.write() as conn:
        conn.execute('UPDATE maintenance_runs SET phase=? WHERE id=?', (PHASES.index('media'), rid))
    with store.read() as conn:
        run = dict(conn.execute('SELECT * FROM maintenance_runs WHERE id=?', (rid,)).fetchone())
    return worker, run


@pytest.mark.parametrize('source', ['host', 'system', 'unprocessed'])
def test_expiry_preserves_object_message_and_understanding(store, source):
    item = saved(store, understanding_text='原有理解' if source == 'host' else None)
    mid = send(store, [item['id']])
    if source == 'system':
        media.prepare_media(store, Vision('原有理解'), [mid], clock=lambda: STAMP)
    before = media.get_media(store, item['id'])
    maintain(store, STAMP + timedelta(days=7))
    assert file_path(store, item).exists()
    report = maintain(store, DUE)
    assert not file_path(store, item).exists()
    assert media.get_media(store, item['id']) == before
    assert file_row(store, item)['created_at'] == STAMP.isoformat()
    assert file_row(store, item)['bytes_purged_at'] == DUE.isoformat()
    assert report['summary']['media_purged']['count'] == 1
    with store.read() as conn:
        assert conn.execute('SELECT 1 FROM messages WHERE id=?', (mid,)).fetchone()
        assert conn.execute('SELECT 1 FROM message_media WHERE message_id=?', (mid,)).fetchone()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    with pytest.raises(media.MediaError):
        media.read_media(store, item['id'])


@pytest.mark.parametrize('protection', ['source', 'batch'])
def test_any_shared_object_source_or_live_batch_protects_bytes(store, protection):
    first, second = saved(store), saved(store)
    send(store, [first['id']])
    mid = send(store, [second['id']], key='second', entry='B')
    if protection == 'source':
        put(store, '图片来源', evidence=[mid])
    else:
        assert form_batch(store, 'B', 'test')
    maintain(store, DUE)
    assert file_path(store, first).exists()
    assert file_row(store, first)['bytes_purged_at'] is None


@pytest.mark.parametrize('protection', ['source', 'lease'])
def test_cleanup_rechecks_after_candidate_selection(store, protection):
    item = saved(store)
    mid = send(store, [item['id']])
    worker, run = media_run(store)
    candidate = worker._candidates(run, 'media')[0]
    if protection == 'source':
        put(store, '新引用', evidence=[mid])
    else:
        with store.write() as conn:
            conn.execute('UPDATE media_objects SET attempt_token=?,lease_until=? WHERE id=?',
                         ('lease', (DUE+timedelta(minutes=1)).isoformat(), item['id']))
    worker._process_item(run, 'media', candidate)
    assert file_path(store, item).exists()
    assert file_row(store, item)['bytes_purged_at'] is None


def test_inflight_protection_survives_expired_lease_and_late_worker(store):
    item = saved(store)
    mid = send(store, [item['id']])
    entered, release = Event(), Event()
    def block():
        assert not store._writer.in_transaction
        entered.set()
        assert release.wait(10)
    gateway = Vision(hook=block)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(media.prepare_media, store, gateway, [mid], clock=lambda: STAMP)
        try:
            assert entered.wait(3)
            maintain(store, DUE)  # The durable lease expired days ago, call is still live.
            assert file_path(store, item).exists()
        finally:
            release.set()
        future.result()
    maintain(store, DUE + timedelta(minutes=1))
    assert not file_path(store, item).exists()
    assert media.get_media(store, item['id'])['understanding_source'] == 'system'


def test_expired_lease_after_restart_is_not_permanent_protection(store):
    item = saved(store)
    send(store, [item['id']])
    with store.write() as conn:
        conn.execute('UPDATE media_objects SET attempt_token=?,lease_until=? WHERE id=?',
                     ('crashed-worker', STAMP.isoformat(), item['id']))
    maintain(store, DUE)
    assert not file_path(store, item).exists()


def test_restart_resumes_unlink_after_durable_intent(store, monkeypatch):
    item = saved(store)
    send(store, [item['id']])
    worker, run = media_run(store)
    real_unlink = type(file_path(store, item)).unlink
    def crash(path, *args, **kwargs):
        if path == file_path(store, item):
            real_unlink(path, *args, **kwargs)
            raise KeyboardInterrupt('synthetic process interruption after unlink')
        return real_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(type(file_path(store, item)), 'unlink', crash)
        with pytest.raises(KeyboardInterrupt):
            worker.run(run['id'])
    reopened = Store(store.path)
    try:
        Maintenance(reopened, clock=lambda: DUE).run(run['id'])
        assert file_row(reopened, item)['bytes_purged_at'] == DUE.isoformat()
        assert not file_path(reopened, item).exists()
        path = store.path.parent / 'resumed.zip'
        backup.export_archive(reopened, path)
        assert path.exists()
    finally:
        reopened.close()


def test_media_candidates_keep_existing_128_item_batches(store):
    for index in range(129):
        item = media.save_media(store, PNG + index.to_bytes(2, 'big'), content_type='image/png', current=STAMP)
        send(store, [item['id']], key=str(index))
    worker, run = media_run(store)
    assert len(worker._candidates(run, 'media')) == 128
    worker.run(run['id'])
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM media_files WHERE bytes_purged_at IS NOT NULL').fetchone()[0] == 129


def test_same_hash_reupload_restores_bytes_and_old_object_can_be_referenced(store):
    item = saved(store, understanding_text='保留理解')
    send(store, [item['id']])
    maintain(store, DUE)
    with pytest.raises(media.MediaError):
        send(store, [item['id']], key='missing')
    before = file_row(store, item)
    restored = media.save_media(store, PNG, content_type='image/png', current=DUE)
    assert restored['sha256'] == item['sha256']
    assert media.read_media(store, restored['id'])[0] == PNG
    after = file_row(store, item)
    assert after['bytes_purged_at'] is None
    assert after['created_at'] == before['created_at']
    assert after['id'] == before['id']
    assert send(store, [item['id'], restored['id']], key='restored')
    assert media.get_media(store, item['id'])['understanding_text'] == '保留理解'


def test_setting_default_patch_preservation_and_audit(client, store):
    assert client.get('/admin/api/settings').json()['media_retention_days'] == 7
    response = client.patch('/admin/api/settings/limits', json={'media_retention_days': 12})
    assert response.status_code == 200
    assert response.json()['media_retention_days'] == 12
    assert client.patch('/admin/api/settings/limits', json={'learning_concurrency': 1}).json()['media_retention_days'] == 12
    with store.read() as conn:
        changes = [json.loads(r[0]) for r in conn.execute("SELECT details_json FROM admin_operations WHERE action='limits_saved' ORDER BY id")]
    assert changes[0]['media_retention_days'] == 12
    assert 'media_retention_days' not in changes[1]
    item = saved(store)
    send(store, [item['id']])
    maintain(store, DUE)
    assert file_path(store, item).exists()
    maintain(store, STAMP+timedelta(days=13))
    assert not file_path(store, item).exists()


@pytest.mark.parametrize('value', [0, 36501, True, False, '7', 7.0, None])
def test_setting_rejects_invalid_without_mutation(client, store, value):
    before = store.setting('media_retention_days')
    response = client.patch('/admin/api/settings/limits', json={'media_retention_days': value})
    assert response.status_code == 400
    assert store.setting('media_retention_days') == before


@pytest.mark.parametrize('value', [1, 36500])
def test_setting_accepts_boundaries(client, value):
    response = client.patch('/admin/api/settings/limits', json={'media_retention_days': value})
    assert response.status_code == 200
    assert response.json()['media_retention_days'] == value


def test_invalid_stored_setting_falls_back_to_default(store):
    store.set_setting('media_retention_days', True)
    assert media.media_retention_days(store) == 7


def test_purged_media_backup_round_trip_and_scheduled_manifest(store, tmp_path):
    old = saved(store, understanding_text='清理后保留')
    send(store, [old['id']])
    maintain(store, DUE)
    fresh = media.save_media(store, PNG+b'fresh', content_type='image/png', current=DUE)
    send(store, [fresh['id']], key='fresh')
    path = tmp_path/'roundtrip.zip'
    backup.export_archive(store, path)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['purged_media'] == {old['sha256']: DUE.isoformat()}
        assert 'media/'+old['sha256'] not in manifest['files']
        assert archive.read('media/'+fresh['sha256']) == PNG+b'fresh'
    target = tmp_path/'restored'/'iris.db'
    backup.import_archive(path, target)
    restored = Store(target)
    try:
        assert media.get_media(restored, old['id']) == media.get_media(store, old['id'])
        assert file_row(restored, old)['bytes_purged_at'] == DUE.isoformat()
        assert not file_path(restored, old).exists()
        assert media.read_media(restored, fresh['id'])[0] == PNG+b'fresh'
    finally:
        restored.close()
    store.set_setting('scheduled_backup', {'enabled': True})
    result = backup.run_scheduled_backup(store, current=DUE)
    assert result['status'] == 'succeeded'
    [scheduled] = list((store.path.parent/'backups').glob('*.zip'))
    with zipfile.ZipFile(scheduled) as archive:
        assert json.loads(archive.read('manifest.json'))['purged_media'] == manifest['purged_media']


@pytest.mark.parametrize('when', ['snapshot', 'copy', 'publication'])
def test_cleanup_racing_export_is_rejected(store, tmp_path, monkeypatch, when):
    item = saved(store)
    send(store, [item['id']])
    worker, run = media_run(store)
    candidate = worker._candidates(run, 'media')[0]
    target_function = {'snapshot': '_copy_database', 'copy': '_copy_media', 'publication': '_sync_file'}[when]
    real = getattr(backup, target_function)
    def race(*args, **kwargs):
        result = real(*args, **kwargs)
        worker._process_item(run, 'media', candidate)
        return result
    monkeypatch.setattr(backup, target_function, race)
    output = tmp_path/'race.zip'
    with pytest.raises(backup.BackupError):
        backup.export_archive(store, output)
    assert not output.exists()


@pytest.mark.parametrize('damage', ['missing', 'changed', 'purge_metadata'])
def test_unmarked_loss_corruption_and_false_purge_manifest_rejected(store, tmp_path, damage):
    item = saved(store)
    send(store, [item['id']])
    output = tmp_path/'invalid.zip'
    if damage == 'purge_metadata':
        backup.export_archive(store, output)
        rewrite(output, metadata=lambda m: m.update(purged_media={item['sha256']: DUE.isoformat()}))
        with pytest.raises(backup.BackupError):
            backup.import_archive(output, tmp_path/'bad-import'/'iris.db')
    else:
        if damage == 'missing':
            file_path(store, item).unlink()
        else:
            file_path(store, item).write_bytes(b'broken')
        with pytest.raises(backup.BackupError):
            backup.export_archive(store, output)


def test_old_backup_without_cleanup_columns_or_manifest_imports(store, tmp_path):
    item = saved(store)
    send(store, [item['id']])
    output = tmp_path/'old.zip'
    backup.export_archive(store, output)
    def old_format(data, manifest):
        database = tmp_path/'old.db'
        database.write_bytes(data['iris.db'])
        with sqlite3.connect(database) as conn:
            columns = {r[1] for r in conn.execute('PRAGMA table_info(media_files)')}
            if 'bytes_purged_at' in columns:
                conn.execute('DROP INDEX IF EXISTS media_files_retention')
                conn.execute('ALTER TABLE media_files DROP COLUMN bytes_purged_at')
                conn.execute('ALTER TABLE media_files DROP COLUMN bytes_purge_pending')
                conn.execute('DELETE FROM schema_migrations WHERE version=?', (manifest['migrations'].pop(),))
                manifest['migration_version'] = manifest['migrations'][-1]
        data['iris.db'] = database.read_bytes()
        manifest.pop('purged_media', None)
        manifest['files']['iris.db'] = {'sha256': hashlib.sha256(data['iris.db']).hexdigest(), 'size': len(data['iris.db'])}
    rewrite(output, entries=old_format)
    target = tmp_path/'upgraded'/'iris.db'
    backup.import_archive(output, target)
    restored = Store(target)
    try:
        assert file_row(restored, item)['bytes_purged_at'] is None
        assert media.read_media(restored, item['id'])[0] == PNG
    finally:
        restored.close()


def test_pending_cleanup_rechecks_new_source_and_rejects_backup(store, tmp_path):
    item = saved(store)
    mid = send(store, [item['id']])
    worker, run = media_run(store)
    candidate = worker._candidates(run, 'media')[0]
    with store.write() as conn:
        media.mark_expired_file(store, conn, candidate['id'], as_of=DUE, current=DUE, days=7)
    with pytest.raises(backup.BackupError) as caught:
        backup.export_archive(store, tmp_path/'pending.zip')
    assert caught.value.code == 'media_changed'
    put(store, '清理两阶段之间的新来源', evidence=[mid])
    worker._process_item(run, 'media', candidate)
    assert media.read_media(store, item['id'])[0] == PNG
    assert file_row(store, item)['bytes_purged_at'] is None


def test_reupload_between_cleanup_transactions_cancels_pending_delete(store):
    item = saved(store)
    send(store, [item['id']])
    worker, run = media_run(store)
    candidate = worker._candidates(run, 'media')[0]
    with store.write() as conn:
        media.mark_expired_file(store, conn, candidate['id'], as_of=DUE, current=DUE, days=7)
    media.save_media(store, PNG, content_type='image/png', current=DUE)
    with store.write() as conn:
        worker._apply(conn, run, 'media', candidate)
    assert media.read_media(store, item['id'])[0] == PNG
    assert file_row(store, item)['bytes_purged_at'] is None


def test_maintenance_does_not_label_accidental_loss_as_purged(store, tmp_path):
    item = saved(store)
    send(store, [item['id']])
    file_path(store, item).unlink()
    maintain(store, DUE)
    assert file_row(store, item)['bytes_purged_at'] is None
    with pytest.raises(backup.BackupError):
        backup.export_archive(store, tmp_path/'missing.zip')


def test_purged_media_uses_existing_admin_and_learning_fallbacks(client, store):
    from test_learning_native_images import native_gateway, run, text_parts
    known = saved(store, understanding_text='保留图片描述')
    unknown = saved(store)
    send(store, [known['id'], unknown['id']])
    maintain(store, DUE)
    assert client.get(f"/admin/api/media/{known['id']}/file").status_code == 404
    gateway = native_gateway(store)
    _, result = run(store, gateway)
    content = gateway.materials[0]
    text = content if isinstance(content, str) else text_parts(content)
    assert '保留图片描述' in text and '[图片，未理解]' in text
    assert not gateway.images
    assert [image['path'] for image in result['media_attempts'][0]['images']] == ['description', 'placeholder']


@pytest.mark.parametrize('attack', ['session', 'csrf', 'origin'])
def test_retention_setting_keeps_admin_guards(client, store, attack):
    headers = {}
    if attack == 'session':
        client.cookies.clear()
    elif attack == 'csrf':
        client.headers.pop('X-Iris-CSRF')
    else:
        headers['Origin'] = 'http://untrusted.example'
    response = client.patch('/admin/api/settings/limits', json={'media_retention_days': 2}, headers=headers)
    assert response.status_code == (401 if attack == 'session' else 403)
    assert store.setting('media_retention_days') is None
    with store.read() as conn:
        assert not conn.execute("SELECT 1 FROM admin_operations WHERE action='limits_saved'").fetchone()


def test_timed_out_gateway_keeps_bytes_until_its_http_worker_exits(store, monkeypatch):
    import httpx
    from fake_openai import Clock
    from iris.models import Gateway, ModelConfig
    from test_models import response
    from thread_helpers import expire_after_entry

    item = saved(store)
    mid = send(store, [item['id']])
    entered, release = Event(), Event()
    clock = Clock()
    def handler(request):
        entered.set()
        assert release.wait(10)
        return httpx.Response(200, json=response('迟到的理解'))
    configs = {'image_understanding': ModelConfig('https://example.invalid/v1', '', 'synthetic-vision')}
    gateway = Gateway(configs, store, client=httpx.Client(transport=httpx.MockTransport(handler)),
                      monotonic=clock.monotonic, sleeper=clock.advance)
    requests = expire_after_entry(monkeypatch, gateway._image_pool, entered, clock)
    try:
        media.prepare_media(store, gateway, [mid], budget_seconds=1, clock=lambda: STAMP)
        assert entered.is_set() and not requests[0].done()
        with store.read() as conn:
            assert conn.execute('SELECT attempt_token FROM media_objects WHERE id=?', (item['id'],)).fetchone()[0] is None
        maintain(store, DUE)
        assert file_path(store, item).exists()
        release.set()
        requests[0].result(timeout=10)
        with gateway._image_admission.condition:
            assert gateway._image_admission.condition.wait_for(lambda: gateway._image_admission.active == 0, timeout=10)
        maintain(store, DUE+timedelta(minutes=1))
        assert not file_path(store, item).exists()
    finally:
        release.set()
        gateway.close()
