"""Administrator media projections, image intake and usage; no real providers."""
import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGateway, authorize_host, login_admin
from iris import admin_data, media
from iris.api import create_app
from iris.learning import PROMPT_VERSION
from iris.memory_ops import set_entry_visibility
from iris.models import ModelError
from iris.queue import form_batch
from test_media import PNG, Vision, learn, saved, send
from test_retrieval import put

UPLOAD = {'content_type': 'image/png', 'data_base64': base64.b64encode(PNG).decode()}


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as value:
        login_admin(value)
        value.app.state.scheduler.stop()
        yield value


def upload(client, data=PNG, content_type='image/png'):
    return client.post('/admin/api/media', json={'content_type': content_type,
        'data_base64': base64.b64encode(data).decode()})


def trial_entry(client):
    return client.post('/admin/api/trial/entries', json={'name': '图片试用'}).json()


def trial_send(client, entry, ids=(), *, text='', key='image-1'):
    return client.post(f"/admin/api/trial/entries/{entry['id']}/messages", json={
        'speaker_id': entry['default_speaker_id'], 'content': text, 'dedupe_key': key,
        'media_ids': list(ids)})


def assert_projection(value, item, label):
    assert value['id'] == item['id']
    for field in ('kind', 'content_type', 'size_bytes', 'understanding_text', 'understanding_source', 'completed_at'):
        assert value[field] == item[field]
    assert value['understanding_source_label'] == label
    assert value['file_url'] == f"/admin/api/media/{item['id']}/file"
    assert not {'file_id', 'path', 'attempt_token', 'lease_until'} & value.keys()


@pytest.mark.parametrize('content_type,data', [
    ('image/png', PNG), ('image/jpeg', b'\xff\xd8\xff\xe0test'),
    ('image/gif', b'GIF89a123456'), ('image/webp', b'RIFF0000WEBPtest'),
])
def test_upload_images_and_read_original_with_admin_session(client, store, content_type, data):
    response = upload(client, data, content_type)
    assert response.status_code == 201
    value = response.json()
    assert value['understanding_source'] == 'unprocessed' and value['completed_at'] is None
    assert_projection(value, media.get_media(store, value['id']), '未理解')
    file = client.get(value['file_url'])
    assert file.status_code == 200 and file.content == data
    assert file.headers['content-type'] == content_type
    assert int(file.headers['content-length']) == len(data)
    assert 'no-store' in file.headers['cache-control']
    assert file.headers['x-content-type-options'] == 'nosniff'
    assert file.headers['cross-origin-resource-policy'] == 'same-origin'
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


@pytest.mark.parametrize('payload', [
    {}, {'content_type': 'image/png', 'data_base64': '%%%invalid'},
    {'content_type': 'image/png', 'data_base64': 'data:image/png;base64,AAAA'},
    {'content_type': 'image/svg+xml', 'data_base64': 'PHN2Zz4='},
    {'content_type': 'image/png', 'data_base64': 'PHN2Zz4='},
    {**UPLOAD, 'understanding_text': '不能冒充宿主理解'}, {**UPLOAD, 'filename': '../outside'},
    {**UPLOAD, 'data_base64': None}, [],
])
def test_upload_validation_has_no_side_effects(client, store, payload):
    assert client.post('/admin/api/media', json=payload).status_code == 400
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM media_objects').fetchone()[0] == 0


def test_upload_rejects_audio_but_admin_can_read_existing_attachments(client, store):
    data = b'ID3audio'
    assert upload(client, data, 'audio/mpeg').status_code == 400
    item = media.save_media(store, data, content_type='audio/mpeg')
    response = client.get(f"/admin/api/media/{item['id']}/file")
    assert response.status_code == 200 and response.content == data
    assert response.headers['content-type'] == 'audio/mpeg'


def test_upload_file_and_streamed_wire_size_limits(client, store):
    exact = PNG + b'\0' * (media.DEFAULT_MAX_BYTES - len(PNG))
    assert upload(client, exact).status_code == 201
    assert upload(client, exact + b'!').status_code == 413
    assert client.post('/admin/api/media', content=b'{}', headers={'Content-Length': str(14 * 1024 * 1024 + 1)}).status_code == 413
    chunks = (b' ' * (1024 * 1024) for _ in range(15))
    assert client.post('/admin/api/media', content=chunks).status_code == 413
    assert client.post('/admin/api/media', content=b'{').status_code == 400
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM media_objects').fetchone()[0] == 1


def test_media_requires_admin_csrf_origin_and_host_before_io(client, store, monkeypatch):
    item = saved(store)
    def forbidden(*args, **kwargs):
        raise AssertionError('Unauthorized request touched a media file')
    monkeypatch.setattr(media, 'read_media', forbidden, raising=False)
    monkeypatch.setattr(media, 'save_media', forbidden)
    assert client.post('/admin/api/media', json=UPLOAD, headers={'X-Iris-CSRF': 'bad'}).status_code == 403
    assert client.post('/admin/api/media', json=UPLOAD, headers={'Origin': 'http://elsewhere.invalid'}).status_code == 403
    assert client.post('/admin/api/media', json=UPLOAD, headers={'Content-Type': 'text/plain'}).status_code == 403
    client.app.state.ready = False
    assert client.get(f"/admin/api/media/{item['id']}/file", headers={'Host': 'elsewhere.invalid'}).status_code == 400
    client.app.state.ready = True
    client.cookies.clear()
    authorize_host(client)
    assert client.get(f"/admin/api/media/{item['id']}/file").status_code == 401
    assert client.post('/admin/api/media', json=UPLOAD).status_code == 401


@pytest.mark.parametrize('damage', ['missing', 'symlink', 'directory_symlink', 'corrupt', 'oversize', 'bad_hash'])
def test_file_read_fails_closed_and_never_recreates_storage(client, store, tmp_path, damage):
    item = saved(store)
    directory = store.path.parent/'media'
    path = directory/item['sha256']
    outside = tmp_path/'outside-attachment.txt'
    outside.write_bytes(b'outside sentinel')
    if damage == 'missing':
        path.unlink()
        directory.rmdir()
    elif damage == 'symlink':
        path.unlink()
        path.symlink_to(outside)
    elif damage == 'directory_symlink':
        directory.rename(tmp_path/'old-media')
        directory.symlink_to(tmp_path/'old-media', target_is_directory=True)
    elif damage == 'corrupt':
        path.write_bytes(b'!' * len(PNG))
    elif damage == 'oversize':
        path.write_bytes(PNG + b'!')
    else:
        with store.write() as conn:
            conn.execute('UPDATE media_files SET sha256=?', ('../outside-attachment.txt'.ljust(64, 'x'),))
    response = client.get(f"/admin/api/media/{item['id']}/file")
    assert response.status_code == 404
    assert 'outside sentinel' not in response.text and str(tmp_path) not in response.text
    if damage == 'missing':
        assert not directory.exists()
    assert client.get('/admin/api/media/missing/file').status_code == 404
    assert client.get('/admin/api/media/%2e%2e%2foutside-attachment.txt/file').status_code == 404


def test_trial_image_only_multiple_order_dedupe_and_shared_file(client, store):
    first, second = upload(client).json(), upload(client).json()
    entry = trial_entry(client)
    response = trial_send(client, entry, [second['id'], first['id']])
    assert response.status_code == 201
    mid = response.json()['message_id']
    assert response.json()['learning_state'] == 'pending'
    retry = trial_send(client, entry, ['missing'], text='重试不能覆盖').json()
    assert retry['message_id'] == mid
    view = client.get(f"/admin/api/trial/entries/{entry['id']}").json()
    message = view['messages'][0]
    assert message['content'] == '' and [m['id'] for m in message['media']] == [second['id'], first['id']]
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM media_files').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0


@pytest.mark.parametrize('ids,status', [([], 400), (['missing'], 404), (['duplicate', 'duplicate'], 400), ([None], 400)])
def test_invalid_trial_media_rolls_back_message(client, store, ids, status):
    entry = trial_entry(client)
    assert trial_send(client, entry, ids).status_code == status
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


def test_trial_only_accepts_images_and_missing_reference_is_atomic(client, store):
    entry = trial_entry(client)
    item = upload(client).json()
    audio = media.save_media(store, b'ID3audio', content_type='audio/mpeg')
    assert trial_send(client, entry, [audio['id']], text='附件').status_code == 400
    assert trial_send(client, entry, [item['id'], 'missing'], text='图片').status_code == 404
    assert trial_send(client, entry, [item['id']], text='中' * 10923).status_code == 413
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM message_media').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


@pytest.mark.parametrize('result,source,text,label', [
    ('一只猫坐在窗边。', 'system', '一只猫坐在窗边。', '本系统理解'),
    (ModelError('content_rejection', 'refused'), 'refused', '敏感信息无法访问', '被拒绝'),
    (ModelError('retryable', 'temporary failure'), 'unprocessed', '[图片，未理解]', '未理解'),
])
def test_trial_image_enters_normal_learning_outside_transaction(client, store, result, source, text, label):
    entry, item = trial_entry(client), upload(client).json()
    mid = trial_send(client, entry, [item['id']], text='看图').json()['message_id']
    def outside():
        assert not store._writer.in_transaction
    gateway = Vision(result, hook=outside)
    formed, learned = learn(store, gateway, entry=entry['id'])
    assert learned['state'] == 'succeeded' and len(gateway.images) == 1
    assert text in gateway.materials[0]
    response = client.get(f"/admin/api/batches/{formed.id}").json()['segments']['target'][0]
    assert response['id'] == mid
    current = media.get_media(store, item['id'])
    assert current['understanding_source'] == source and current['completed_at']
    assert_projection(response['media'][0], current, label)


def test_media_projections_all_segments_sources_context_and_admin_visibility(client, store):
    host = saved(store, understanding_text='宿主提供的说明')
    unknown = saved(store)
    before = send(store, [host['id']], key='before', content='前文')
    learn(store, FakeGateway())
    target = send(store, [unknown['id'], host['id']], key='target', content='来源')
    after = send(store, [host['id']], key='after', content='后文')
    formed = form_batch(store, 'A', PROMPT_VERSION, target_count=1, history_count=1, future_count=1)
    memory = put(store, '私有图片的记忆', entry='A', evidence=[target])
    set_entry_visibility(store, 'A', visibility='entry_only', visible_in=[])
    with store.read() as conn:
        previous = list(conn.iterdump())
    batch = client.get(f'/admin/api/batches/{formed.id}').json()
    assert [batch['segments'][s][0]['id'] for s in ('history', 'target', 'future')] == [before, target, after]
    for segment in batch['segments'].values():
        assert segment[0]['media']
    detail = client.get(f'/admin/api/memories/{memory}').json()
    source = detail['sources'][0]
    assert [m['id'] for m in source['message']['media']] == [unknown['id'], host['id']]
    assert [m['id'] for m in source['context']] == [before, target, after]
    assert all(m['media'] for m in source['context'])
    assert_projection(source['message']['media'][1], host, '宿主提供')
    assert_projection(source['message']['media'][0], unknown, '未理解')
    assert client.get(source['message']['media'][1]['file_url']).content == PNG
    with store.read() as conn:
        assert list(conn.iterdump()) == previous


def test_no_media_empty_arrays_and_cleared_batch_message(client, store):
    entry = trial_entry(client)
    mid = trial_send(client, entry, text='仅文字').json()['message_id']
    snapshot = client.get(f"/admin/api/trial/entries/{entry['id']}").json()
    assert snapshot['messages'][0]['media'] == []
    formed = form_batch(store, entry['id'], PROMPT_VERSION)
    with store.write() as conn:
        conn.execute('DELETE FROM messages WHERE id=?', (mid,))
    missing = client.get(f'/admin/api/batches/{formed.id}').json()['segments']['target'][0]
    assert missing == {'id': mid, 'missing': True, 'content': '已清理'}


def call(store, stamp, *, purpose='image_understanding', kind='image_understanding', outcome='success',
         duration=100, prompt=10, completion=5, reasoning=2, timed_out=0):
    with store.write() as conn:
        conn.execute('''INSERT INTO model_calls(purpose,model,model_kind,duration_ms,prompt_tokens,completion_tokens,
            reasoning_tokens,result_category,timed_out,created_at) VALUES(?,'fake',?,?,?,?,?,?,?,?)''',
            (purpose, kind, duration, prompt, completion, reasoning, outcome, timed_out, stamp.isoformat()))


def test_image_usage_windows_include_retries_refusals_probes_and_unknown_usage(client, store, monkeypatch):
    current = datetime(2026, 10, 14, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(admin_data, 'utc_now', lambda: current)
    store.set_setting('timezone', 'Asia/Shanghai')
    call(store, current, prompt=100, completion=20)
    call(store, current-timedelta(minutes=10), duration=200, outcome='retryable', timed_out=1)
    call(store, current-timedelta(minutes=20), duration=300, outcome='content_rejection', prompt=None, completion=None, reasoning=None)
    call(store, current-timedelta(days=1), duration=400, purpose='probe', prompt=20, completion=10)
    call(store, current-timedelta(days=4))
    call(store, current+timedelta(minutes=1))
    call(store, current, kind='chat', purpose='learning', prompt=999)
    value = client.get('/admin/api/status').json()
    image = value['usage']['by_purpose']['image_understanding']
    day, week = image['today'], image['week']
    assert day['calls'] == 3 and day['tokens'] == 135
    assert day['prompt_tokens'] == 110 and day['completion_tokens'] == 25 and day['reasoning_tokens'] == 4
    assert day['failures'] == 2 and day['refusals'] == 1 and day['failure_rate'] == pytest.approx(2/3)
    assert day['calls_without_usage'] == 1
    assert day['duration_ms'] == 600 and day['p50_ms'] == 200 and day['p95_ms'] == 290 and day['max_ms'] == 300
    assert day['timeouts'] == 1
    assert week['calls'] == 4 and week['tokens'] == 165 and week['duration_ms'] == 1000
    assert value['timeouts_seconds']['image_understanding'] == 120


def test_image_usage_empty_and_read_only(client, store):
    with store.read() as conn:
        before = list(conn.iterdump())
    value = client.get('/admin/api/status').json()['usage']['by_purpose']['image_understanding']
    for bucket in value.values():
        assert bucket['calls'] == bucket['tokens'] == bucket['failures'] == bucket['refusals'] == bucket['duration_ms'] == 0
        assert bucket['p95_ms'] is None and bucket['failure_rate'] is None
    with store.read() as conn:
        assert list(conn.iterdump()) == before


def test_file_read_pins_directory_across_replacement(client, store, tmp_path, monkeypatch):
    item = saved(store)
    directory = store.path.parent/'media'
    replacement = tmp_path/'replacement'
    replacement.mkdir()
    (replacement/item['sha256']).write_bytes(b'outside sentinel')
    real_open = media.os.open
    replaced = []
    def replace_before_file_open(path, flags, *args, **kwargs):
        if path == item['sha256'] and kwargs.get('dir_fd') is not None and not replaced:
            directory.rename(tmp_path/'pinned-media')
            directory.symlink_to(replacement, target_is_directory=True)
            replaced.append(True)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(media.os, 'open', replace_before_file_open)
    response = client.get(f"/admin/api/media/{item['id']}/file")
    assert replaced and response.status_code == 200 and response.content == PNG
    assert client.get(f"/admin/api/media/{item['id']}/file").status_code == 404


def test_admin_upload_does_not_grant_host_reference_permission(client, store):
    item = upload(client).json()
    authorize_host(client)
    response = client.post('/api/v1/entries/A/messages', json={'sender': '小林', 'content': '',
        'occurred_at': '2026-10-10T00:00:00+00:00', 'dedupe_key': 'host-1', 'media_ids': [item['id']]})
    assert response.status_code == 404
    assert trial_send(client, trial_entry(client), [item['id']]).status_code == 201


def test_unattached_admin_upload_keeps_cleanup_grace(client, store):
    item = upload(client).json()
    with store.write() as conn:
        row = conn.execute('SELECT * FROM media_files').fetchone()
        uploaded = datetime.fromisoformat(row['unreferenced_at'])
        assert not media.delete_unreferenced_file(store, conn, row['id'], uploaded+timedelta(hours=23))
    assert client.get(item['file_url']).status_code == 200
    with store.write() as conn:
        assert media.delete_unreferenced_file(store, conn, row['id'], uploaded+timedelta(days=1))
    assert client.get(item['file_url']).status_code == 404


def test_image_usage_legacy_purpose_and_partial_usage(client, store):
    current = datetime.now(timezone.utc)
    call(store, current, kind=None, prompt=12, completion=None, reasoning=None)
    call(store, current, kind='chat', purpose='health_probe', prompt=999)
    value = client.get('/admin/api/status').json()['usage']['by_purpose']['image_understanding']['today']
    assert value['calls'] == 1 and value['tokens'] == 12 and value['calls_without_usage'] == 0
