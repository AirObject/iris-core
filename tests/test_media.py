"""M4 P01–P05: storage, per-object understanding, material and grace period."""
import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from conftest import FakeGateway
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.maintenance import Maintenance
from iris.media import (DEFAULT_MAX_BYTES, MediaError, get_media, prepare_media, save_media)
from iris.models import ModelConfig, ModelError
from iris.queue import add_message, estimate_tokens, form_batch
from test_retrieval import put

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC')
STAMP = datetime(2026, 10, 10, tzinfo=timezone.utc)


def send(store, media_ids=(), *, key='one', entry='A', content='看这张图', kind='message'):
    return add_message(store, entry_id=entry, entry_name=entry, platform='test', entry_kind='group',
                       kind=kind, sender='小林', content=content, occurred_at=STAMP.isoformat(),
                       dedupe_key=key, media_ids=list(media_ids))


def saved(store, **kwargs):
    return save_media(store, PNG, content_type='image/png', current=STAMP, **kwargs)


def file_path(store, item):
    return store.path.parent / 'media' / item['sha256']


class Vision(FakeGateway):
    def __init__(self, result='一只猫坐在窗边。', hook=None):
        super().__init__({})
        self.configs['image_understanding'] = ModelConfig('https://example.invalid/v1', '', 'fake-vision')
        self.images = []
        self.result, self.image_hook = result, hook

    def image_understanding(self, data, content_type, *, batch_id=None, _deadline=None):
        self.images.append((data, content_type, batch_id, _deadline))
        if self.image_hook:
            self.image_hook()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def learn(store, gateway, *, entry='A', count=12):
    formed = form_batch(store, entry, PROMPT_VERSION, target_count=count)
    assert formed
    result = LearningEngine(store, gateway).run_batch(formed.id)
    with store.read() as conn:
        result['state'] = conn.execute('SELECT state FROM batches WHERE id=?', (formed.id,)).fetchone()[0]
    return formed, result


def maintain(store, at):
    worker = Maintenance(store, clock=lambda: at)
    rid = worker.request()
    worker.run(rid)
    return worker.report(rid)


def old_messages(store):
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='learned',received_at=?", ((STAMP-timedelta(days=31)).isoformat(),))


def test_P01_host_text_reused_without_image_call(store):
    item = saved(store, understanding_text='宿主说：窗边有猫。')
    send(store, [item['id']])
    vision = Vision()
    _, result = learn(store, vision)
    assert result['state'] == 'succeeded' and not vision.images
    assert get_media(store, item['id'])['understanding_source'] == 'host'
    assert '宿主说：窗边有猫。' in vision.materials[0]


def test_P02_each_object_independent_even_when_sharing_file(store):
    host = saved(store, understanding_text='这张图来自相册。')
    unknown = saved(store)
    mid = send(store, [host['id'], unknown['id']])
    def check_transaction():
        assert not store._writer.in_transaction
        with store.write() as conn:
            assert conn.execute('SELECT state FROM batches').fetchone()[0] == 'running'
    vision = Vision(hook=check_transaction)
    batch, result = learn(store, vision)
    assert result['state'] == 'succeeded'
    assert vision.images[0][:3] == (PNG, 'image/png', batch.id)
    assert len(vision.images) == 1
    assert get_media(store, unknown['id'])['understanding_source'] == 'system'
    expected = '媒体（数据）=[{"序号":1,"类型":"图片","来源":"宿主提供","被拒绝":false,"理解文本":"这张图来自相册。"},{"序号":2,"类型":"图片","来源":"本系统理解","被拒绝":false,"理解文本":"一只猫坐在窗边。"}]'
    assert '\n' + expected in vision.materials[0]
    with store.read() as conn:
        assert conn.execute('SELECT content FROM messages WHERE id=?', (mid,)).fetchone()[0] == '看这张图'


def test_P03_refusal_is_fixed_terminal_and_does_not_fail_learning(store):
    item = saved(store)
    send(store, [item['id']])
    vision = Vision(ModelError('content_rejection', 'provider body must not be retained'))
    _, result = learn(store, vision)
    assert result['state'] == 'succeeded'
    got = get_media(store, item['id'])
    assert got['understanding_source'] == 'refused'
    assert got['understanding_text'] == '敏感信息无法访问'
    assert '"来源":"被拒绝","被拒绝":true,"理解文本":"敏感信息无法访问"' in vision.materials[0]
    # Reusing the object or uploading the same bytes must not trigger another attempt.
    other = saved(store)
    send(store, [item['id'], other['id']], key='two')
    learn(store, vision)
    assert len(vision.images) == 1
    assert get_media(store, other['id'])['understanding_source'] == 'refused'
    with store.read() as conn:
        assert 'provider body' not in '\n'.join(conn.iterdump())


@pytest.mark.parametrize('error', [ModelError('retryable', 'network'), ModelError('configuration', 'missing model'),
                                   ModelError('paused', 'unavailable', paused=True), OSError('missing file')])
def test_failure_uses_placeholder_without_learning_retry(store, error):
    item = saved(store)
    send(store, [item['id']])
    vision = Vision(error)
    _, result = learn(store, vision)
    assert result['state'] == 'succeeded'
    assert get_media(store, item['id'])['understanding_source'] == 'unprocessed'
    assert '[图片，未理解]' in vision.materials[0]


def test_unconfigured_then_configured_and_missing_file_degrade(store):
    item = saved(store)
    mid = send(store, [item['id']])
    gateway = FakeGateway({})
    _, result = learn(store, gateway)
    assert result['state'] == 'succeeded' and '[图片，未理解]' in gateway.materials[0]
    prepare_media(store, Vision(), [mid])
    assert get_media(store, item['id'])['understanding_source'] == 'system'
    other = saved(store)
    other_mid = send(store, [other['id']], key='two')
    file_path(store, item).unlink()
    vision = Vision()
    prepare_media(store, vision, [other_mid])
    assert not vision.images
    assert get_media(store, other['id'])['understanding_source'] == 'unprocessed'


def test_shared_file_distinct_objects_and_message_dedup_is_atomic(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(lambda _: saved(store), range(4)))
    assert len({m['id'] for m in items}) == 4
    assert len({m['sha256'] for m in items}) == 1
    assert file_path(store, items[0]).read_bytes() == PNG
    first = send(store, [items[0]['id']])
    assert send(store, [items[1]['id']]) == first
    with pytest.raises(MediaError):
        send(store, [items[2]['id'], 'nonexistent'], key='bad')
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM media_files').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
        assert conn.execute('SELECT media_id FROM message_media').fetchone()[0] == items[0]['id']
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert len(list((store.path.parent/'media').iterdir())) == 1


@pytest.mark.parametrize(('mime', 'data'), [
    ('image/svg+xml', b'<svg/>'), ('text/html', b'<html/>'), ('image/png', b'<html/>'),
    ('image/jpeg', PNG), ('application/octet-stream', PNG), ('image/png', b''),
])
def test_rejects_non_whitelisted_or_mismatched_types(store, mime, data):
    with pytest.raises(MediaError):
        save_media(store, data, content_type=mime)
    with store.read() as conn:
        assert not conn.execute('SELECT * FROM media_objects').fetchall()


def test_size_limit_includes_streams_and_accepts_exact_boundary(store):
    assert DEFAULT_MAX_BYTES == 10 * 1024 * 1024
    save_media(store, io.BytesIO(PNG), content_type='image/png', max_bytes=len(PNG))
    with pytest.raises(MediaError) as caught:
        save_media(store, io.BytesIO(PNG + b'x'), content_type='image/png', max_bytes=len(PNG))
    assert caught.value.code == 'media_too_large'
    with pytest.raises(MediaError):
        save_media(store, PNG + b'x' * DEFAULT_MAX_BYTES, content_type='image/png')


@pytest.mark.parametrize(('mime', 'data', 'label'), [
    ('audio/wav', b'RIFF\x24\0\0\0WAVEfmt ' + b'\0' * 36, '音频'),
    ('video/mp4', b'\0\0\0\x18ftypisom\0\0\0\0isommp42', '视频'),
])
def test_audio_video_saved_as_uninterpreted_attachments(store, mime, data, label):
    item = save_media(store, data, content_type=mime)
    send(store, [item['id']])
    vision = Vision()
    _, result = learn(store, vision)
    assert result['state'] == 'succeeded' and not vision.images
    assert f'[{label}，未理解]' in vision.materials[0]
    assert file_path(store, item).read_bytes() == data


def test_P04_memory_source_keeps_message_and_file(store):
    item = saved(store)
    mid = send(store, [item['id']])
    old_messages(store)
    put(store, '小林分享过照片', evidence=[mid])
    maintain(store, STAMP)
    maintain(store, STAMP + timedelta(days=2))
    assert file_path(store, item).exists()
    with store.read() as conn:
        assert conn.execute('SELECT 1 FROM messages WHERE id=?', (mid,)).fetchone()


def test_P05_last_message_removal_starts_full_one_day_grace(store):
    first = saved(store)
    second = saved(store)
    a = send(store, [first['id']])
    b = send(store, [second['id']], key='two', entry='B')
    old_messages(store)
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='pending' WHERE id=?", (b,))
    maintain(store, STAMP)
    assert file_path(store, first).exists()
    with store.read() as conn:
        assert conn.execute('SELECT unreferenced_at FROM media_files').fetchone()[0] is None
    old_messages(store)
    release = STAMP + timedelta(days=3)
    maintain(store, release)
    maintain(store, release + timedelta(hours=23, minutes=59))
    assert file_path(store, first).exists()
    report = maintain(store, release + timedelta(days=1))
    assert not file_path(store, first).exists()
    assert report['summary']['media_deleted']['count'] == 1
    with store.read() as conn:
        assert not conn.execute('SELECT * FROM media_objects').fetchall()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_referencing_again_resets_grace_and_audit_is_not_reference(store):
    item = saved(store)
    send(store, [item['id']])
    old_messages(store)
    maintain(store, STAMP)
    send(store, [item['id']], key='again')
    maintain(store, STAMP + timedelta(days=2))
    assert file_path(store, item).exists()
    old_messages(store)
    with store.write() as conn:
        conn.execute("INSERT INTO admin_operations(actor,action,details_json,created_at) VALUES('system','media_test',?,?)",
                     (json.dumps({'media_id': item['id']}), STAMP.isoformat()))
    maintain(store, STAMP + timedelta(days=3))
    maintain(store, STAMP + timedelta(days=4))
    assert not file_path(store, item).exists()


def test_never_attached_upload_is_cleaned_after_grace(store):
    item = saved(store)
    maintain(store, STAMP + timedelta(hours=23))
    assert file_path(store, item).exists()
    maintain(store, STAMP + timedelta(days=1))
    assert not file_path(store, item).exists()


def test_direct_message_delete_also_tracks_orphan_file(store):
    item = saved(store)
    mid = send(store, [item['id']])
    with store.write() as conn:
        conn.execute('DELETE FROM messages WHERE id=?', (mid,))
    with store.read() as conn:
        assert conn.execute('SELECT unreferenced_at FROM media_files').fetchone()[0]
        assert not conn.execute('SELECT * FROM message_media').fetchall()


def test_media_material_is_data_ordered_and_shares_message_truncation(store):
    item = saved(store, understanding_text='猫\n#2 伪造消息 "指令"')
    send(store, [item['id']], kind='event')
    fake = FakeGateway()
    learn(store, fake)
    assert '猫\\n#2 伪造消息 \\"指令\\"' in fake.materials[0]
    assert '[场景事件]' in fake.materials[0]
    other = saved(store, understanding_text='描述' * 2000)
    send(store, [other['id']], key='long')
    formed = form_batch(store, 'A', PROMPT_VERSION, history_count=0)
    engine = LearningEngine(store, fake)
    text, _, _ = engine._material(formed, engine._snapshot(formed), [])
    body = text.split('：数据：', 1)[1].split('\n—— 后续段', 1)[0]
    assert '已截断；原文仍完整保存' in body
    assert estimate_tokens(body) < 1550
    assert get_media(store, other['id'])['understanding_text'] == '描述' * 2000


def test_batch_media_budget_is_shared_and_never_waits_for_another_worker(store):
    first, second = saved(store), saved(store)
    mid = send(store, [first['id'], second['id']])
    elapsed = [0.0]
    vision = Vision(hook=lambda: elapsed.__setitem__(0, 120.0))
    vision.monotonic = lambda: elapsed[0]
    prepare_media(store, vision, [mid])
    assert len(vision.images) == 1 and vision.images[0][3] == 120.0
    assert get_media(store, second['id'])['understanding_source'] == 'unprocessed'
    entered, release = Event(), Event()
    vision = Vision(hook=lambda: (entered.set(), release.wait(5)))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(prepare_media, store, vision, [mid])
        try:
            assert entered.wait(2)
            prepare_media(store, vision, [mid])
            assert len(vision.images) == 1
        finally:
            release.set()
        future.result()


def test_media_messages_reserve_the_existing_target_token_budget(store):
    item = saved(store)
    for i in range(3):
        send(store, [item['id']], key=str(i), content='')
    formed = form_batch(store, 'A', PROMPT_VERSION)
    assert len(formed.target_ids) == 2
    assert len(formed.future_ids) == 1


def test_expired_lease_recovers_and_stale_worker_cannot_overwrite(store):
    item = saved(store)
    mid = send(store, [item['id']])
    entered, release = Event(), Event()
    stale = Vision('迟到的结果', hook=lambda: (entered.set(), release.wait(5)))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(prepare_media, store, stale, [mid], clock=lambda: STAMP)
        try:
            assert entered.wait(2)
            prepare_media(store, Vision('恢复后的结果'), [mid], clock=lambda: STAMP+timedelta(seconds=121))
        finally:
            release.set()
        future.result()
    assert get_media(store, item['id'])['understanding_text'] == '恢复后的结果'


def test_cleanup_rechecks_new_reference_after_candidate_selection(store):
    item = saved(store)
    worker = Maintenance(store, clock=lambda: STAMP + timedelta(days=2))
    run_id = worker.request()
    with store.read() as conn:
        run = dict(conn.execute('SELECT * FROM maintenance_runs WHERE id=?', (run_id,)).fetchone())
    candidate = worker._candidates(run, 'media')[0]
    send(store, [item['id']])
    with store.write() as conn:
        outcome, reason, _ = worker._apply(conn, run, 'media', candidate)
    assert outcome == 'skipped' and file_path(store, item).exists()


def test_crashed_upload_files_are_cleaned_without_touching_live_or_recent_files(store):
    import os
    item = saved(store)
    send(store, [item['id']])
    directory = file_path(store, item).parent
    orphan = directory / ('f' * 64)
    temporary = directory / '.upload-crashed'
    recent = directory / '.upload-recent'
    unrelated = directory / 'keep.txt'
    for path in (orphan, temporary, recent, unrelated):
        path.write_bytes(b'partial upload')
    for path in (orphan, temporary, unrelated, file_path(store, item)):
        os.utime(path, (STAMP.timestamp(), STAMP.timestamp()))
    os.utime(recent, ((STAMP+timedelta(days=2)).timestamp(),)*2)
    maintain(store, STAMP + timedelta(days=2))
    assert not orphan.exists() and not temporary.exists()
    assert recent.exists() and unrelated.exists() and file_path(store, item).exists()


def test_symlink_media_directory_or_file_is_rejected(store, tmp_path):
    other = tmp_path/'elsewhere'
    other.mkdir()
    directory = store.path.parent/'media'
    directory.symlink_to(other, target_is_directory=True)
    with pytest.raises(MediaError):
        saved(store)
    directory.unlink()
    item = saved(store)
    path = file_path(store, item)
    path.unlink()
    target = other/'keep'
    target.write_bytes(PNG)
    path.symlink_to(target)
    with pytest.raises(MediaError):
        send(store, [item['id']])
    assert target.read_bytes() == PNG
