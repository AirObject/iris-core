"""Launch runtime regressions. All model traffic and credentials are synthetic."""
import contextlib
import json
import os
import sqlite3
import stat
import zipfile
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from conftest import FakeGateway, msg
from iris import backup, media
from iris.configuration import RuntimeConfig
from iris.db import Store
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.maintenance import Maintenance
from iris.model_health import ModelHealth
from iris.models import Gateway, ModelConfig, ModelError
from iris.queue import form_batch
from iris.scheduler import Scheduler
from test_lifecycle import Clock, configure
from test_media import PNG, saved, send
from test_models import response
from test_retrieval import put

CONFIG = {kind: ModelConfig('https://example.invalid/v1', '', 'fake-' + kind)
          for kind in ('chat', 'embedding', 'image_understanding')}


@contextlib.contextmanager
def gateway_for(store, handler, clock=None):
    options = {'clock': clock} if clock else {}
    health = ModelHealth(store, CONFIG, **options)
    gateway = Gateway(CONFIG, store, health=health,
        client=httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None, **options)
    try:
        yield gateway, health
    finally:
        gateway.close()


def test_backfill_refusal_skips_one_revision_and_survives_restart(store):
    first = put(store, '被拒绝的正文')
    second = put(store, '可以向量化的正文')
    calls = []
    def handler(request):
        assert not store._writer.in_transaction
        text = json.loads(request.content)['input']
        calls.append(text)
        if text == '被拒绝的正文':
            return httpx.Response(400, json={'error': {'code': 'content_filter'}})
        return httpx.Response(200, json={'data': [{'embedding': [1., 0.]}]})
    with gateway_for(store, handler) as (gateway, health):
        for expected in (1, 0):
            scheduler = Scheduler(store, gateway)
            try:
                assert scheduler.backfill_vectors() == expected
            finally:
                scheduler.stop()
        assert calls == ['被拒绝的正文', '可以向量化的正文']
        assert health.snapshot()['embedding']['state'] == 'normal'
        with store.write() as conn:
            conn.execute('UPDATE memories SET content=?,revision=revision+1 WHERE id=?', ('修订后的正文', first))
        scheduler = Scheduler(store, gateway)
        try:
            assert scheduler.backfill_vectors() == 1
        finally:
            scheduler.stop()
        assert calls[-1] == '修订后的正文'


def test_backfill_failure_backs_off_but_allows_other_memories(store):
    clock = Clock()
    put(store, '坏条目')
    put(store, '好条目')
    gateway = FakeGateway()
    gateway.configs = {'embedding': CONFIG['embedding']}
    calls = []
    def embed(text, purpose):
        calls.append(text)
        if text == '坏条目':
            raise ModelError('item_error', 'HTTP 400')
        return [1., 0.]
    gateway.embedding = embed
    scheduler = Scheduler(store, gateway, clock=clock)
    try:
        assert scheduler.backfill_vectors() == 1
        assert scheduler.backfill_vectors() == 0
        assert calls == ['坏条目', '好条目']
        clock.advance(seconds=59)
        scheduler.backfill_vectors()
        assert len(calls) == 2
        clock.advance(seconds=1)
        scheduler.backfill_vectors()
        assert calls[-1] == '坏条目' and len(calls) == 3
    finally:
        scheduler.stop()


def test_backfill_query_uses_partial_index_and_rejects_stale_refusal(store, monkeypatch):
    mid = put(store, '在途旧正文')
    gateway = FakeGateway()
    gateway.configs = {'embedding': CONFIG['embedding']}
    plans = []
    original = store.read
    @contextlib.contextmanager
    def read():
        with original() as conn:
            conn.set_trace_callback(lambda sql: plans.append(sql) if 'SELECT id,content,revision' in sql else None)
            yield conn
    monkeypatch.setattr(store, 'read', read)
    def embed(text, purpose):
        with store.write() as conn:
            conn.execute('UPDATE memories SET content=?,revision=revision+1 WHERE id=?', ('新版正文', mid))
        raise ModelError('content_rejection', 'refused')
    gateway.embedding = embed
    scheduler = Scheduler(store, gateway)
    try:
        assert scheduler.backfill_vectors() == 0
        with original() as conn:
            detail = ' '.join(row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + plans[0]))
            assert 'memories_vector_candidates' in detail
        gateway.embedding = lambda *args: [1., 0.]
        assert scheduler.backfill_vectors() == 1
    finally:
        scheduler.stop()


@pytest.mark.parametrize('status,body', [(400, {}), (200, {}), (200, ['invalid'])])
def test_single_bad_item_is_not_global_configuration_failure(store, status, body):
    for number in (1, 2, 3):
        msg(store, 1, '独立批次', entry=str(number))
        form_batch(store, str(number), PROMPT_VERSION)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)
    with gateway_for(store, handler) as (gateway, health):
        for _ in range(4):
            with pytest.raises(ModelError) as caught:
                gateway.chat([{'role': 'user', 'content': '同一条'}], 'learning', batch_id=1)
            assert not caught.value.paused
            assert health.snapshot()['chat']['state'] == 'normal'
        assert len(calls) == 4
        for batch in (2, 3):
            with pytest.raises(ModelError) as caught:
                gateway.chat([], 'learning', batch_id=batch)
        assert caught.value.paused
        assert health.snapshot()['chat']['state'] == 'configuration_error'


def test_success_breaks_bad_item_streak_and_connection_check_clears_configuration(store):
    for number in range(5):
        msg(store, 1, '独立批次', entry=str(number))
        form_batch(store, str(number), PROMPT_VERSION)
    results = [httpx.Response(400, json={}), httpx.Response(200, json=response()),
               httpx.Response(400, json={}), httpx.Response(400, json={}), httpx.Response(400, json={}),
               httpx.Response(200, json=response())]
    with gateway_for(store, lambda request: results.pop(0)) as (gateway, health):
        for batch in range(5):
            try:
                gateway.chat([], 'learning', batch_id=batch + 1)
            except ModelError:
                pass
            assert health.snapshot()['chat']['state'] == ('configuration_error' if batch == 4 else 'normal')
        gateway._call('chat', 'connection_check', {'messages': []}, probe=True)
        assert health.snapshot()['chat']['state'] == 'normal'


@pytest.mark.parametrize('status,body', [(400, {}), (200, {})])
def test_probe_bad_response_remains_configuration_error(store, status, body):
    with gateway_for(store, lambda request: httpx.Response(status, json=body)) as (gateway, health):
        with pytest.raises(ModelError):
            gateway._call('chat', 'connection_check', {'messages': []}, probe=True)
        assert health.snapshot()['chat']['state'] == 'configuration_error'


def test_image_invalid_response_is_one_item_failure_without_retry_or_pause(store):
    first, second = saved(store), media.save_media(store, PNG + b'other', content_type='image/png')
    mid = send(store, [first['id'], second['id']])
    results = [httpx.Response(200, json={}), httpx.Response(200, json=response('蓝色方块'))]
    with gateway_for(store, lambda request: results.pop(0)) as (gateway, health):
        media.prepare_media(store, gateway, [mid])
        bad, good = media.get_media(store, first['id']), media.get_media(store, second['id'])
        assert bad['understanding_source'] == 'unprocessed' and bad['completed_at']
        assert good['understanding_text'] == '蓝色方块'
        assert health.snapshot()['image_understanding']['state'] == 'normal'
        assert not results


def test_bad_learning_call_spends_normal_batch_attempt(store):
    msg(store, 1, '测试普通失败计次')
    formed = form_batch(store, 'A', PROMPT_VERSION)
    with gateway_for(store, lambda request: httpx.Response(400, json={})) as (gateway, health):
        LearningEngine(store, gateway).run_batch(formed.id, force=True)
        with store.read() as conn:
            row = conn.execute('SELECT attempt_count,state FROM batches WHERE id=?', (formed.id,)).fetchone()
        assert tuple(row) == (1, 'waiting')
        assert health.snapshot()['chat']['state'] == 'normal'


def test_budget_cache_is_indexed_unlocked_and_invalidated_by_committed_usage(store, monkeypatch):
    clock = Clock('2026-10-10T08:00:00+08:00')
    health = ModelHealth(store, CONFIG, clock=clock)
    health.set_daily_token_limit(30)
    queries = []
    original = store.read
    @contextlib.contextmanager
    def read():
        with original() as conn:
            def trace(sql):
                if 'SUM(' in sql and 'model_calls' in sql:
                    assert not health._lock._is_owned()
                    queries.append(sql)
            conn.set_trace_callback(trace)
            yield conn
    monkeypatch.setattr(store, 'read', read)
    with store.write() as conn:
        conn.execute("INSERT INTO model_calls(purpose,model,duration_ms,result_category,created_at,prompt_tokens,completion_tokens) VALUES('learning','fake',1,'success','2026-10-09T17:00:00+00:00',10,5)")
    for _ in range(5):
        assert health.budget()['used'] == 15
        health.allowed('chat')
    assert len(queries) == 1
    with original() as conn:
        assert 'model_calls_usage_time' in ' '.join(row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + queries[0]))
    with store.write() as conn:
        conn.execute('UPDATE model_calls SET completion_tokens=20')
    assert health.budget()['exhausted'] and health.budget()['used'] == 30
    health.set_daily_token_limit(31)
    assert not health.budget()['exhausted']
    clock.advance(days=1)
    assert health.budget()['used'] == 0


@pytest.mark.parametrize('change', ['future', 'gap'])
def test_store_rejects_incompatible_schema_without_migrating(tmp_path, change):
    path = tmp_path / 'newer.db'
    store = Store(path)
    with store.write() as conn:
        if change == 'future':
            conn.execute("INSERT INTO schema_migrations VALUES('999_future.sql','2026-10-10')")
        else:
            conn.execute("DELETE FROM schema_migrations WHERE version='003_learning_fixes.sql'")
        versions = conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
    store.close()
    with pytest.raises((ValueError, RuntimeError), match=r'\.bak'):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall() == [tuple(r) for r in versions]


def test_checkpoint_flush_without_per_commit_fullfsync(store):
    assert store._writer.execute('PRAGMA checkpoint_fullfsync').fetchone()[0] == 1
    assert store._writer.execute('PRAGMA fullfsync').fetchone()[0] == 0
    assert store._writer.execute('PRAGMA journal_size_limit').fetchone()[0] == 64 * 1024 * 1024


def test_catchup_after_scheduled_time_occupies_today_slot(store):
    clock = Clock('2026-10-10T12:00:00+08:00')
    maintenance = Maintenance(store, clock=clock)
    clock.advance(minutes=10)
    due = maintenance.due()
    assert due == ('catchup', '2026-10-10')
    rid = maintenance.request(trigger=due[0], schedule_key=due[1])
    maintenance.run(rid)
    assert maintenance.request(trigger='scheduled', schedule_key='2026-10-10') == rid
    assert Maintenance(store, clock=clock).due() is None


def test_cleanup_preserves_old_targets_for_yesterday_retry(store):
    clock = Clock()
    mid = msg(store, 1, '历史积压的失败消息')
    formed = form_batch(store, 'A', PROMPT_VERSION)
    with store.write() as conn:
        conn.execute("UPDATE messages SET learning_state='abandoned',received_at=?", ((clock()-timedelta(days=60)).isoformat(),))
        conn.execute("UPDATE batches SET state='abandoned',attempt_count=4,finished_at=?", ((clock()-timedelta(days=1)).isoformat(),))
    maintenance = Maintenance(store, clock=clock)
    maintenance.run(maintenance.request())
    with store.read() as conn:
        assert conn.execute('SELECT id FROM messages WHERE id=?', (mid,)).fetchone()
        assert conn.execute('SELECT state FROM batches WHERE id=?', (formed.id,)).fetchone()[0] == 'waiting'


def test_retention_boundaries_cascades_redaction_and_private_backup_limit(store):
    clock = Clock('2026-10-10T12:00:00+08:00')
    store.set_setting('consolidation', {'enabled': False})
    msg(store, 1, '保留批次行')
    formed = form_batch(store, 'A', PROMPT_VERSION)
    old = (clock()-timedelta(days=91)).isoformat()
    edge = (clock()-timedelta(days=90)).isoformat()
    with store.write() as conn:
        conn.executemany("INSERT INTO model_calls(purpose,model,duration_ms,result_category,created_at) VALUES('learning','fake',1,'success',?)", [(old,)]*260 + [(edge,)])
        for key, stamp in [('old', (clock()-timedelta(days=31)).isoformat()), ('edge', (clock()-timedelta(days=30)).isoformat())]:
            conn.execute('INSERT INTO recalls VALUES(?,?,?,?)', (key, 'A', '{}', stamp))
            conn.execute('INSERT INTO recall_items(recall_id,memory_id,revision) VALUES(?,1,1)', (key,))
        conn.execute("INSERT INTO host_tokens(id,host,scope_json,salt,token_hash,created_at) VALUES('t','fake','{}','fake','fake',?)", (old,))
        conn.execute("INSERT INTO host_recalls VALUES('old','t')")
        conn.execute("INSERT INTO batch_attempts(batch_id,number,started_at,finished_at,raw_output,repair_output,parse_status,error,duration_ms) VALUES(?,1,?,?,'raw','repair','failed','keep error',1)", (formed.id,old,old))
    migration_backups = []
    for day in range(1,6):
        path = store.path.with_name(f'iris.db.2026100{day}T000000Z.bak')
        path.write_bytes(b'old migration snapshot')
        migration_backups.append(path)
    maintenance = Maintenance(store, clock=clock)
    rid = maintenance.request()
    with store.write() as conn:
        work = conn.execute("INSERT INTO consolidation_work(fingerprint,kind,payload_json,importance,changed_at,created_at) VALUES('fake','merge','{}',1,?,?)", (old,old)).lastrowid
        conn.execute("INSERT INTO consolidation_calls(run_id,work_id,purpose,created_at,raw_output,error) VALUES(?,?,'consolidation',?,'raw','keep error')", (rid,work,old))
    maintenance.run(rid)
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 1
        assert [r[0] for r in conn.execute('SELECT id FROM recalls')] == ['edge']
        assert conn.execute('SELECT COUNT(*) FROM recall_items').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM host_recalls').fetchone()[0] == 0
        assert tuple(conn.execute('SELECT raw_output,repair_output,error FROM batch_attempts').fetchone()) == (None,None,'keep error')
        assert tuple(conn.execute('SELECT raw_output,error FROM consolidation_calls').fetchone()) == (None,'keep error')
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        plan = ' '.join(r[3] for r in conn.execute('EXPLAIN QUERY PLAN SELECT * FROM recall_items WHERE memory_id=1'))
        assert 'recall_items_by_memory' in plan
    assert [p.exists() for p in migration_backups] == [False,False,True,True,True]


def test_export_tolerates_upload_temp_and_new_content_files(store, tmp_path, monkeypatch):
    original_media = saved(store)
    directory = store.path.parent/'media'
    temporary = directory/'.upload-inflight'
    temporary.write_bytes(b'partially uploaded')
    copy = backup._copy_database
    def during(source, target):
        copy(source, target)
        temporary.unlink()
        media.save_media(store, PNG+b'new image', content_type='image/png')
    monkeypatch.setattr(backup, '_copy_database', during)
    path = tmp_path/'during-upload.zip'
    backup.export_archive(store, path)
    with zipfile.ZipFile(path) as archive:
        assert 'media/'+original_media['sha256'] in archive.namelist()
        assert not any('.upload-' in name for name in archive.namelist())


def test_export_includes_upload_committed_before_snapshot(store, tmp_path, monkeypatch):
    saved(store)
    versions = backup._versions
    inserted = []
    def upload_before_pin(conn):
        if not inserted:
            inserted.append(media.save_media(store, PNG+b'committed', content_type='image/png'))
        return versions(conn)
    monkeypatch.setattr(backup, '_versions', upload_before_pin)
    path = tmp_path/'committed-before-snapshot.zip'
    backup.export_archive(store, path)
    with zipfile.ZipFile(path) as archive:
        assert 'media/'+inserted[0]['sha256'] in archive.namelist()


def test_default_preimport_archive_preserves_all_old_synthetic_keys(store, tmp_path):
    incoming = tmp_path/'incoming.zip'
    backup.export_archive(store, incoming)
    target = tmp_path/'destination/iris.db'
    old = Store(target)
    runtime = RuntimeConfig(old)
    marker = os.urandom(32).hex()
    runtime._write_secrets({'unused-old-reference': marker})
    old.close()
    result = backup.import_archive(incoming, target, confirm_overwrite=True, backup_include_secrets=False)
    previous = Path(result['previous_backup'])
    assert stat.S_IMODE(previous.stat().st_mode) == 0o600
    assert result['previous_secrets_archive'] == str(previous)
    with zipfile.ZipFile(previous) as archive:
        data = json.loads(archive.read('secrets.json'))
        assert data['keys']['unused-old-reference'] == marker
    assert marker not in json.dumps(result)


def test_budget_cache_expires_after_one_second_without_usage_change(store, monkeypatch):
    tick = [0.]
    health = ModelHealth(store, CONFIG, clock=Clock(), monotonic=lambda: tick[0])
    original = store.read
    queries = []
    @contextlib.contextmanager
    def read():
        with original() as conn:
            conn.set_trace_callback(lambda sql: queries.append(sql) if 'SUM(' in sql and 'model_calls' in sql else None)
            yield conn
    monkeypatch.setattr(store, 'read', read)
    health.budget()
    assert not queries
    tick[0] = 1.01
    health.budget()
    assert len(queries) == 1


def test_bad_item_streak_persists_restart_without_exposing_input_keys(store):
    clock = Clock()
    health = ModelHealth(store, CONFIG, clock=clock)
    token = health.check('chat', 'learning')
    for identity in ('one', 'two'):
        assert not health.observe('chat', token, 'item_error', 'HTTP 400', item_key=identity)
    health = ModelHealth(store, CONFIG, clock=clock)
    assert health.observe('chat', token, 'invalid_output', 'bad shape', item_key='three')
    assert health.snapshot()['chat']['state'] == 'configuration_error'
    assert 'item_error_keys' not in health.snapshot()['chat']


def test_retention_is_batched_and_resumable(store):
    clock = Clock()
    store.set_setting('consolidation', {'enabled': False})
    old = (clock()-timedelta(days=100)).isoformat()
    with store.write() as conn:
        conn.executemany("INSERT INTO model_calls(purpose,model,duration_ms,result_category,created_at) VALUES('learning','fake',0,'success',?)", [(old,)]*300)
    maintenance = Maintenance(store, clock=clock)
    rid = maintenance.request()
    def count():
        with store.read() as conn:
            return conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0]
    maintenance.run(rid, stop=lambda: count() < 300)
    assert count() == 172
    with store.read() as conn:
        assert conn.execute('SELECT state FROM maintenance_runs WHERE id=?', (rid,)).fetchone()[0] == 'running'
    maintenance.run(rid)
    assert count() == 0
    assert maintenance.report(rid)['state'] == 'completed'


def test_before_schedule_catchup_does_not_claim_later_daily_slot(store):
    clock = Clock('2026-10-10T02:00:00+08:00')
    maintenance = Maintenance(store, clock=clock)
    clock.advance(minutes=10)
    assert maintenance.due() == ('catchup', None)
    maintenance.run(maintenance.request(trigger='catchup'))
    clock.advance(minutes=50)
    assert maintenance.due() == ('scheduled', '2026-10-10')


def test_unregistered_file_change_cannot_abort_registered_snapshot(store, tmp_path, monkeypatch):
    item = saved(store)
    unrelated = store.path.parent/'media/legacy-unregistered.bin'
    unrelated.write_bytes(b'old')
    copy = backup._copy_database
    def during(source, target):
        copy(source, target)
        unrelated.write_bytes(b'changed')
    monkeypatch.setattr(backup, '_copy_database', during)
    path = tmp_path/'snapshot.zip'
    backup.export_archive(store, path)
    with zipfile.ZipFile(path) as archive:
        assert 'media/'+item['sha256'] in archive.namelist()
        assert 'media/legacy-unregistered.bin' not in archive.namelist()
