"""Descriptions use a bounded worker, persisted leases, and scheduler shutdown."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

from iris.media import get_media
from iris.scheduler import Scheduler
from test_learning_native_images import native_gateway, run
from test_media import STAMP, saved, send
from thread_helpers import THREAD_WAIT


def test_description_does_not_block_learning_and_stop_drains_accepted_work(store):
    item = saved(store)
    send(store, [item['id']], content='')
    entered, release = Event(), Event()
    gateway = native_gateway(store)
    def blocked_description():
        assert not store._writer.in_transaction
        entered.set()
        assert release.wait(THREAD_WAIT)
    gateway.image_hook = blocked_description
    scheduler = Scheduler(store, gateway, clock=lambda: STAMP)
    with ThreadPoolExecutor(max_workers=1) as closer:
        try:
            scheduler._schedule_media()
            assert entered.wait(THREAD_WAIT)
            first = scheduler._media_job
            for _ in range(3):
                scheduler._schedule_media()
                assert scheduler._media_job is first
            _, result = run(store, gateway)
            assert result['parse_status'] == 'direct'
            assert get_media(store, item['id'])['understanding_source'] == 'unprocessed'
            stopping = closer.submit(scheduler.stop)
            assert not stopping.done()
        finally:
            release.set()
            scheduler.stop()
        stopping.result(timeout=THREAD_WAIT)
    assert len(gateway.images) == 1
    assert get_media(store, item['id'])['understanding_source'] == 'system'


def test_restart_resumes_expired_lease_and_health_pause_does_not_claim(store):
    item = saved(store)
    send(store, [item['id']])
    with store.write() as conn:
        conn.execute('UPDATE media_objects SET attempt_token=?,lease_until=?',
                     ('previous-process', (STAMP + timedelta(seconds=60)).isoformat()))
    gateway = native_gateway(store)
    paused = Scheduler(store, gateway, clock=lambda: STAMP + timedelta(seconds=61))
    paused.health = SimpleNamespace(allowed=lambda kind: False)
    try:
        paused._schedule_media()
        assert paused._media_job is None
        assert not gateway.images
    finally:
        paused.stop()
    restarted = Scheduler(store, gateway, clock=lambda: STAMP + timedelta(seconds=61))
    try:
        restarted._schedule_media()
        restarted._media_job.result(timeout=THREAD_WAIT)
        restarted._schedule_media()
        assert restarted._media_job is None
    finally:
        restarted.stop()
    assert len(gateway.images) == 1
    assert get_media(store, item['id'])['understanding_source'] == 'system'


def test_background_batch_is_bounded_and_disabled_for_legacy_chat(store):
    items = [saved(store) for _ in range(10)]
    send(store, [item['id'] for item in items])
    gateway = native_gateway(store)
    scheduler = Scheduler(store, gateway, clock=lambda: STAMP)
    try:
        scheduler._schedule_media()
        scheduler._media_job.result(timeout=THREAD_WAIT)
        assert len(gateway.images) == 8
        scheduler._schedule_media()
        scheduler._media_job.result(timeout=THREAD_WAIT)
        assert len(gateway.images) == 10
    finally:
        scheduler.stop()
    other = saved(store)
    send(store, [other['id']], key='legacy')
    from dataclasses import replace
    gateway.configs['chat'] = replace(gateway.configs['chat'], vision=False)
    legacy = Scheduler(store, gateway, clock=lambda: STAMP)
    try:
        legacy._schedule_media()
        assert legacy._media_job is None
        assert len(gateway.images) == 10
    finally:
        legacy.stop()


def test_tick_schedules_description_work(store, monkeypatch):
    scheduler = Scheduler(store, native_gateway(store), clock=lambda: STAMP)
    calls = []
    monkeypatch.setattr(scheduler, '_schedule_media', lambda: calls.append(True))
    try:
        scheduler.tick()
        assert calls == [True]
    finally:
        scheduler.stop()


def test_background_follows_runtime_opt_in_without_restart(store):
    gateway = native_gateway(store)
    store.set_setting('native_image_learning', False)
    item = saved(store)
    send(store, [item['id']])
    scheduler = Scheduler(store, gateway, clock=lambda: STAMP)
    try:
        scheduler._schedule_media()
        assert scheduler._media_job is None
        assert not gateway.images
        store.set_setting('native_image_learning', True)
        scheduler._schedule_media()
        scheduler._media_job.result(timeout=THREAD_WAIT)
        assert len(gateway.images) == 1
        other = saved(store)
        send(store, [other['id']], key='after-disable')
        store.set_setting('native_image_learning', False)
        scheduler._schedule_media()
        assert len(gateway.images) == 1
        assert get_media(store, other['id'])['understanding_source'] == 'unprocessed'
    finally:
        scheduler.stop()
