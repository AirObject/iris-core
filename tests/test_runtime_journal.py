"""Metadata-only runtime evidence; no external model configuration is read."""
import json
import logging
import threading
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGateway, authorize_host, msg
from fake_openai import Clock
from iris.api import create_app
from iris.maintenance import Maintenance
from iris.model_health import ModelHealth
from iris.models import MODEL_KINDS, ModelConfig
from iris.queue import form_batch
from iris.runtime_journal import Journal, cleanup, record
from iris.scheduler import Scheduler


def events(store, event=None):
    with store.read() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM runtime_events WHERE (? IS NULL OR event=?) ORDER BY id', (event, event))]


def break_journal(store):
    with store.write() as conn:
        conn.execute("""CREATE TRIGGER reject_journal BEFORE INSERT ON runtime_events
            BEGIN SELECT RAISE(ABORT,'private error text'); END""")


def test_process_identity_heartbeat_backlog_and_normal_stop(store):
    clock = Clock()
    msg(store, 1, 'private message', entry='A')
    msg(store, 2, 'private message', entry='A')
    msg(store, 3, 'private message', entry='B')
    batch = form_batch(store, 'A', 'test', target_count=1, future_count=0)
    with store.write() as conn:
        conn.execute('UPDATE messages SET received_at=?', ((clock()-timedelta(seconds=42)).isoformat(),))
        conn.execute("UPDATE batches SET state='running' WHERE id=?", (batch.id,))
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('empty','private name','test','group')")
        conn.execute("""INSERT INTO memory_gaps(batch_id,entry_id,started_at,ended_at,reason,created_at)
            VALUES(?,'A',?,?,'attempts_exhausted',?)""", (batch.id, *[clock().isoformat()]*3))
    journal = Journal(store, clock=clock)
    journal.start(background=False)
    first = events(store)
    assert [r['event'] for r in first] == ['process_started', 'heartbeat']
    assert len(journal.instance_id) == 32
    assert all(r['instance_id'] == journal.instance_id for r in first)
    with store.read() as conn:
        rows = {r['entry_id']: dict(r) for r in conn.execute('SELECT * FROM runtime_backlog')}
    assert rows['A']['pending_messages'] == 2  # Includes messages already frozen in a batch.
    assert rows['A']['running_batches'] == 1 and rows['A']['waiting_batches'] == 0
    assert rows['A']['oldest_wait_seconds'] == pytest.approx(42, abs=.01)
    assert rows['A']['memory_gaps'] == 1
    assert rows['B']['pending_messages'] == 1
    assert rows['empty']['pending_messages'] == 0 and rows['empty']['oldest_wait_seconds'] is None
    clock.advance(299)
    journal.heartbeat_if_due()
    assert len(events(store, 'heartbeat')) == 1
    clock.advance(1)
    journal.heartbeat_if_due()
    assert len(events(store, 'heartbeat')) == 2
    journal.stop()
    assert events(store)[-1]['event'] == 'process_stopped'
    second = Journal(store, clock=clock)
    second.start(background=False)
    second.stop(normal=False)
    assert second.instance_id != journal.instance_id
    assert len(events(store, 'process_stopped')) == 1
    assert 'private' not in json.dumps(events(store))


def test_heartbeat_uses_one_read_snapshot_without_holding_writer(store, monkeypatch):
    msg(store, 1, 'text')
    journal = Journal(store)
    original = journal._backlog
    def collect(current):
        result = original(current)
        # A writer can commit between collection and the short journal write.
        with store.write() as conn:
            conn.execute("UPDATE messages SET learning_state='learned'")
        return result
    monkeypatch.setattr(journal, '_backlog', collect)
    journal.start(background=False)
    journal.stop()
    with store.read() as conn:
        assert conn.execute('SELECT pending_messages FROM runtime_backlog').fetchone()[0] == 1


def test_journal_failure_is_fixed_log_and_heartbeat_recovers(store, caplog):
    clock = Clock()
    break_journal(store)
    with caplog.at_level(logging.WARNING):
        journal = Journal(store, clock=clock)
        journal.start(background=False)
        assert record(store, 'prepare', result='normal', duration_ms=1) is None
    assert 'runtime journal write failed' in caplog.text
    assert 'private error text' not in caplog.text
    with store.write() as conn:
        conn.execute('DROP TRIGGER reject_journal')
    clock.advance(300)
    journal.heartbeat_if_due()
    journal.stop()
    assert len(events(store, 'heartbeat')) == 2


def test_cleanup_30_days_and_daily_maintenance(store):
    clock = Clock()
    msg(store, 1, "text")
    old = clock()-timedelta(days=30, seconds=1)
    boundary = clock()-timedelta(days=30)
    journal = Journal(store, clock=lambda: old)
    journal.start(background=False)
    journal.stop()
    record(store, 'prepare', current=boundary, result='normal', duration_ms=2)
    record(store, 'prepare', current=clock(), result='normal', duration_ms=3)
    lifecycle = Maintenance(store, clock=clock)
    lifecycle.run(lifecycle.request())
    assert all(r['occurred_at'] >= boundary.isoformat() for r in events(store))
    assert len(events(store, 'prepare')) == 2
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM runtime_backlog').fetchone()[0] == 0
    # A cleanup failure is observational and cannot break the next maintenance.
    with store.write() as conn:
        conn.execute("CREATE TRIGGER reject_cleanup BEFORE DELETE ON runtime_events BEGIN SELECT RAISE(ABORT,'private'); END")
    cleanup(store, current=clock()+timedelta(days=31))


@pytest.mark.parametrize('kind', MODEL_KINDS)
def test_health_network_pause_and_automatic_recovery_all_purposes(store, kind):
    configs = {k: ModelConfig('offline', '', 'stub') for k in MODEL_KINDS}
    health = ModelHealth(store, configs)
    token = health.check(kind, 'test')
    for _ in range(3):
        health.observe(kind, token, 'retryable', 'private provider error')
    health.observe(kind, token, 'success', probe=True)
    rows = [r for r in events(store, 'model_state') if r['model_kind'] == kind]
    assert [(r['state'], r['reason']) for r in rows][-2:] == [
        ('temporarily_unavailable', 'network'), ('normal', 'probe_success')]
    assert rows[-1]['previous_state'] == 'temporarily_unavailable'
    assert 'private provider error' not in json.dumps(rows)


@pytest.mark.parametrize(('category', 'state', 'reason'), [
    ('authentication', 'invalid_key', 'invalid_key'),
    ('configuration', 'configuration_error', 'configuration'),
    ('account', 'account_problem', 'account'),
])
def test_health_configuration_and_manual_changes(store, category, state, reason):
    config = ModelConfig('offline', '', 'stub')
    health = ModelHealth(store, {'chat': config})
    token = health.check('chat', 'learning')
    health.observe('chat', token, category, 'private')
    assert events(store, 'model_state')[-1]['reason'] == reason
    assert events(store, 'model_state')[-1]['state'] == state
    if category == 'account':
        health.retry_now('chat')
        assert events(store, 'model_state')[-1]['reason'] == 'manual_retry'
    else:
        health.replace_config('chat', replace(config, model='changed'))
        assert any(r['reason'] == 'configuration_change' and r['previous_state'] == state
                   and r['state'] == 'normal' for r in events(store, 'model_state'))


def test_health_rate_limit_expiry_budget_pause_reset_and_limit_change(store):
    clock = Clock()
    configs = {k: ModelConfig('offline', '', 'stub') for k in MODEL_KINDS}
    health = ModelHealth(store, configs, clock=clock)
    token = health.check('recall_judge', 'test')
    health.observe('recall_judge', token, 'retryable', rate_limited=True, retry_after=2)
    assert events(store, 'model_state')[-1]['reason'] == 'rate_limit'
    clock.advance(2)
    health.snapshot()
    assert events(store, 'model_state')[-1]['reason'] == 'cooldown_elapsed'
    with store.write() as conn:
        conn.execute("INSERT INTO model_calls(purpose,model,created_at,prompt_tokens,completion_tokens,duration_ms,result_category) VALUES('learning','stub',?,2,3,0,'success')", (clock().isoformat(),))
    health.set_daily_token_limit(5)
    limited = [r for r in events(store, 'model_state') if r['state'] == 'usage_limit']
    assert {r['model_kind'] for r in limited} == set(MODEL_KINDS)-{'embedding'}
    clock.advance(86400)
    health.snapshot()
    assert any(r['reason'] == 'daily_reset' and r['previous_state'] == 'usage_limit' for r in events(store))
    with store.write() as conn:
        conn.execute('UPDATE model_calls SET created_at=?', (clock().isoformat(),))
    health.snapshot()
    health.set_daily_token_limit(None)
    assert events(store, 'model_state')[-1]['reason'] == 'limit_changed'


def test_health_startup_config_change_and_journal_failure_do_not_change_behavior(store):
    config = ModelConfig('offline', '', 'stub')
    first = ModelHealth(store, {'chat': config})
    first.observe('chat', first.check('chat', 'test'), 'authentication')
    second = ModelHealth(store, {'chat': replace(config, model='changed')})
    assert any(r['reason'] == 'configuration_change' and r['previous_state'] == 'invalid_key' for r in events(store))
    break_journal(store)
    token = second.check('chat', 'test')
    for _ in range(3):
        second.observe('chat', token, 'retryable')
    assert second.snapshot()['chat']['state'] == 'temporarily_unavailable'
    second.observe('chat', token, 'success', probe=True)
    assert second.learning_allowed()


def test_maintenance_schedules_before_due_and_records_phase_outcomes(store):
    clock = Clock()
    lifecycle = Maintenance(store, clock=clock)
    lifecycle.observe_schedule()
    planned = events(store, 'maintenance_scheduled')
    assert len(planned) == 1 and planned[0]['scheduled_at'] > clock().isoformat()
    lifecycle.observe_schedule()
    assert len(events(store, 'maintenance_scheduled')) == 1
    run_id = lifecycle.request()
    lifecycle.run(run_id)
    rows = [r for r in events(store) if r['run_id'] == run_id]
    assert any(r['event'] == 'maintenance_started' and r['phase'] == 'maintenance' for r in rows)
    assert any(r['event'] == 'maintenance_completed' and r['phase'] == 'maintenance' and r['duration_ms'] >= 0 for r in rows)
    assert any(r['event'] == 'maintenance_skipped' and r['phase'] == 'consolidation' for r in rows)
    assert any(r['phase'] == 'persona' for r in rows)


def test_schedule_changes_and_busy_worker_keep_future_evidence(store):
    clock = Clock()
    scheduler = Scheduler(store, FakeGateway(), clock=clock)
    try:
        scheduler.lifecycle.observe_schedule()
        config = store.setting('lifecycle')
        config['maintenance_time'] = '06:30'
        store.set_setting('lifecycle', config)
        scheduler.lifecycle.observe_schedule()
        assert any(r['reason'] == 'schedule_changed' for r in events(store, 'maintenance_skipped'))
        class Busy:
            def done(self):
                return False
        scheduler._lifecycle_job = Busy()
        clock.advance(86400)
        scheduler.tick()
        assert len(events(store, 'maintenance_scheduled')) == 3
    finally:
        scheduler.stop()


def test_maintenance_error_stop_and_record_failure(store, monkeypatch):
    clock = Clock()
    lifecycle = Maintenance(store, clock=clock)
    run_id = lifecycle.request()
    lifecycle.run(run_id, stop=lambda: True)
    assert events(store, 'maintenance_skipped')[-1]['reason'] == 'shutdown'
    original = lifecycle._candidates
    def fail(*args):
        raise RuntimeError('private error')
    monkeypatch.setattr(lifecycle, '_candidates', fail)
    with pytest.raises(RuntimeError):
        lifecycle.run(run_id)
    assert events(store, 'maintenance_failed')[-1]['reason'] == 'internal_error'
    monkeypatch.setattr(lifecycle, '_candidates', original)
    break_journal(store)
    lifecycle.run(run_id)
    with store.read() as conn:
        assert conn.execute('SELECT state FROM maintenance_runs WHERE id=?', (run_id,)).fetchone()[0] == 'completed'


def test_reminders_reuse_existing_timestamps_record_cancel_and_skip(store):
    from iris.goals import Goals
    clock = Clock()
    goals = Goals(store, clock=clock)
    goal = goals.create(content='private goal', origin='admin', deadline=(clock()+timedelta(hours=2)).isoformat())['goal']
    gid = goal['id']
    goals.generate_notifications()
    clock.advance(7201)
    goals.generate_notifications()
    rows = events(store, 'reminder_resolved')
    assert any(r['state'] == 'skipped' and r['reason'] == 'superseded' for r in rows)
    with store.read() as conn:
        note = dict(conn.execute("SELECT * FROM notifications WHERE kind='goal_reminder'").fetchone())
        assert note['scheduled_at'] and note['published_at'] and note['taken_at'] is None
    assert not any(r['state'] == 'published' for r in rows)
    goals.pull()
    with store.read() as conn:
        assert conn.execute('SELECT taken_at FROM notifications WHERE id=?', (note['id'],)).fetchone()[0]
    goals.update(gid, expected_revision=goals.get(gid)['revision'], state='completed')
    assert events(store, 'reminder_resolved')[-1]['state'] == 'cancelled'
    assert 'private goal' not in json.dumps(events(store))


def test_reminder_event_failure_cannot_roll_back_goal_update(store):
    from iris.goals import Goals
    clock = Clock()
    goals = Goals(store, clock=clock)
    goal = goals.create(content='goal', origin='admin', deadline=(clock()+timedelta(hours=2)).isoformat())['goal']
    break_journal(store)
    goals.update(goal['id'], expected_revision=goal['revision'], state='completed')
    assert goals.get(goal['id'])['state'] == 'completed'


@pytest.mark.parametrize(('hints', 'judgment', 'result'), [
    ([], 'applied', 'normal'), ([], 'degraded', 'judge_degraded'),
    ([{'code': 'embedding_fallback'}], 'applied', 'fulltext_degraded'),
    ([{'code': 'embedding_unconfigured'}], 'degraded', 'fulltext_degraded'),
])
def test_http_prepare_total_duration_classification(store, monkeypatch, hints, judgment, result):
    import time
    monkeypatch.setattr(Scheduler, 'start', lambda self: None)
    msg(store, 1, 'private body')
    def prepare(self, entry_id, **kwargs):
        time.sleep(.02)
        return {'recall_id': 'test', 'memories': [], 'hints': hints, 'judgment': {'status': judgment}}
    monkeypatch.setattr('iris.api.HostRetrieval.prepare', prepare)
    monkeypatch.setattr('iris.tokens.Tokens.bind_recall', lambda *args: None)
    with TestClient(create_app(store=store, gateway=FakeGateway()), base_url='http://127.0.0.1') as client:
        authorize_host(client)
        response = client.post('/api/v1/entries/A/prepare', json={})
        assert response.status_code == 200
        row = events(store, 'prepare')[-1]
        assert row['result'] == result and row['duration_ms'] >= 20
        assert row['entry_id'] == 'A' and row['instance_id']
    assert events(store)[-1]['event'] == 'process_stopped'


def test_prepare_errors_validation_and_failed_recording_do_not_change_response(store, monkeypatch):
    monkeypatch.setattr(Scheduler, 'start', lambda self: None)
    msg(store, 1, 'private')
    with TestClient(create_app(store=store, gateway=FakeGateway()), base_url='http://127.0.0.1', raise_server_exceptions=False) as client:
        assert client.post('/api/v1/entries/A/prepare', json={}).status_code == 401
        assert not events(store, 'prepare')
        authorize_host(client)
        assert client.post('/api/v1/entries/A/prepare', json={'judge': 42}).status_code == 400
        assert events(store, 'prepare')[-1]['result'] == 'error'
        def fail(*args, **kwargs):
            raise RuntimeError('private')
        monkeypatch.setattr('iris.api.HostRetrieval.prepare', fail)
        assert client.post('/api/v1/entries/A/prepare', json={}).status_code == 500
        assert events(store, 'prepare')[-1]['result'] == 'error'
        break_journal(store)
        assert client.post('/api/v1/entries/A/prepare', json={}).status_code == 500


def test_unknown_free_text_metadata_is_rejected_without_logging_it(store, caplog):
    assert record(store, 'prepare', result='private output', duration_ms=1) is None
    assert record(store, 'model_state', model_kind='private name', state='normal') is None
    assert record(store, 'private event') is None
    assert not events(store)
    assert 'private' not in caplog.text


def test_optional_unconfigured_purpose_is_explicit_metadata(store):
    ModelHealth(store, {'chat': ModelConfig('offline', '', 'stub')})
    states = {r['model_kind']: r for r in events(store, 'model_state')}
    assert states['chat']['configured'] == 1
    assert states['image_understanding']['configured'] == 0
    assert states['embedding']['configured'] == 0


def test_background_heartbeat_survives_wall_clock_rollback(store, monkeypatch):
    import iris.runtime_journal as module
    clock = Clock()
    journal = Journal(store, clock=clock)
    observed = threading.Event()
    original = journal._backlog
    def capture(current):
        result = original(current)
        if current < start:
            observed.set()
        return result
    start = clock()
    monkeypatch.setattr(module, 'HEARTBEAT_SECONDS', .02)
    monkeypatch.setattr(journal, '_backlog', capture)
    journal.start()
    try:
        clock.advance(-3600)
        assert observed.wait(2)
    finally:
        journal.stop()
    assert len(events(store, 'heartbeat')) >= 2


def test_journal_writer_wait_is_bounded_and_does_not_change_store_timeout(store):
    import time
    acquired, release = threading.Event(), threading.Event()
    def hold():
        with store.write():
            acquired.set()
            release.wait(3)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert acquired.wait(1)
        started = time.perf_counter()
        assert record(store, 'prepare', result='normal', duration_ms=1) is None
        assert time.perf_counter()-started < .5
    finally:
        release.set()
        thread.join()
    assert record(store, 'prepare', result='normal', duration_ms=1)
    assert store._writer.execute('PRAGMA busy_timeout').fetchone()[0] == 30000


def test_maintenance_models_run_outside_journal_transactions(store, monkeypatch):
    clock = Clock()
    def consolidate(self, run, **kwargs):
        assert not store._writer.in_transaction
        with store.write() as conn:
            conn.execute('UPDATE consolidation_runs SET finished=1 WHERE run_id=?', (run['id'],))
        return True
    def persona(store, gateway, run, **kwargs):
        assert not store._writer.in_transaction
        with store.write() as conn:
            conn.execute("INSERT INTO maintenance_persona(run_id,status) VALUES(?,'pending')", (run['id'],))
    monkeypatch.setattr('iris.consolidation.Consolidation.run', consolidate)
    monkeypatch.setattr('iris.consolidation.update_persona_for_run', persona)
    lifecycle = Maintenance(store, clock=clock)
    lifecycle.run(lifecycle.request())
    assert {r['phase'] for r in events(store, 'maintenance_completed')} == {'maintenance', 'consolidation', 'persona'}


def test_due_run_preserves_scheduled_instant(store):
    from datetime import datetime
    clock = Clock()
    clock.value = datetime.fromisoformat('2026-10-04T18:59:59+00:00')  # 02:59:59 role time
    lifecycle = Maintenance(store, clock=clock)
    lifecycle.observe_schedule()
    clock.advance(2)
    trigger, key = lifecycle.due()
    rid = lifecycle.request(trigger=trigger, schedule_key=key)
    lifecycle.run(rid)
    start = [r for r in events(store, 'maintenance_started') if r['phase'] == 'maintenance'][0]
    assert start['scheduled_at'] == '2026-10-04T19:00:00+00:00'
    assert start['run_id'] == rid


def test_reminder_configuration_cancellation_is_recorded(store):
    from iris.goals import Goals
    clock = Clock()
    goals = Goals(store, clock=clock)
    goals.create(content='goal', origin='admin', deadline=(clock()-timedelta(hours=2)).isoformat())
    clock.advance(61)
    goals.generate_notifications()
    goals.configure(overdue_reminders=False)
    assert any(r['reason'] == 'configuration_change' and r['state'] == 'cancelled'
               for r in events(store, 'reminder_resolved'))
    with store.read() as conn:
        assert conn.execute("SELECT cancelled_at FROM notifications WHERE reminder_kind='overdue'").fetchone()[0]


def test_service_keeps_serving_when_every_event_write_fails(store, monkeypatch, caplog):
    monkeypatch.setattr(Scheduler, 'start', lambda self: None)
    msg(store, 1, 'body')
    break_journal(store)
    with TestClient(create_app(store=store, gateway=FakeGateway()), base_url='http://127.0.0.1') as client:
        authorize_host(client)
        response = client.post('/api/v1/entries/A/prepare', json={'judge': False})
        assert response.status_code == 200
        assert response.json()['recent_messages']
    assert 'private error text' not in caplog.text
    assert not events(store)


def test_duplicate_maintenance_worker_does_not_record_second_execution(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    lifecycle = Maintenance(store)
    rid = lifecycle.request()
    entered, release = threading.Event(), threading.Event()
    original = lifecycle._candidates
    def hold(run, phase):
        entered.set()
        assert release.wait(3)
        return original(run, phase)
    monkeypatch.setattr(lifecycle, '_candidates', hold)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lifecycle.run, rid)
        assert entered.wait(2)
        second = pool.submit(lifecycle.run, rid)
        release.set()
        first.result()
        second.result()
    assert len([r for r in events(store, 'maintenance_started') if r['phase'] == 'maintenance']) == 1
