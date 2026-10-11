"""Saving model settings waits for reloads throughout one bounded request."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris import configuration, settings_api
from iris.api import create_app
from thread_helpers import THREAD_WAIT

PAYLOAD = {'enabled': True, 'base_url': 'https://models.example/v1', 'model': 'after-reload'}


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 12345)) as client:
        # The test supplies the background reload at an exact point itself.
        client.app.state.scheduler.stop()
        login_admin(client)
        yield client


@pytest.mark.parametrize('stage', ['editable', 'save', 'load', 'public'])
def test_model_save_waits_for_reload_at_every_lock(client, store, monkeypatch, stage):
    runtime = client.app.state.runtime_config
    entered, release, reloaded, saving_lock = (Event() for _ in range(4))
    load, local, method, lock = runtime.load, runtime._load_local, getattr(runtime, stage), runtime._lock
    waits = []
    monkeypatch.setattr(configuration, 'monotonic', lambda: 0.0)
    monkeypatch.setattr(settings_api, 'time', SimpleNamespace(monotonic=lambda: 0.0))

    class ObservedLock:
        def acquire(self, *, timeout):
            waits.append(timeout)
            saving_lock.set()
            if timeout > 0:
                # Observe the requested wait, then let the real reload finish;
                # test scheduling time must not consume its three-second budget.
                assert reloaded.wait(THREAD_WAIT)
            return lock.acquire(timeout=timeout)

        def release(self):
            lock.release()

    def held_load():
        entered.set()
        assert release.wait(THREAD_WAIT), 'test did not release reload'
        return local()

    def reload():
        try:
            return load()
        finally:
            reloaded.set()

    def at_stage(*args, **kwargs):
        # Disarm before invoking the saved method (public may call other reads).
        monkeypatch.setattr(runtime, stage, method)
        monkeypatch.setattr(runtime, '_load_local', held_load)
        reloading.append(executor.submit(reload))
        assert entered.wait(THREAD_WAIT)
        # Reload acquired the real lock; only the saving request is observed.
        monkeypatch.setattr(runtime, '_lock', ObservedLock())
        return method(*args, **kwargs)

    monkeypatch.setattr(runtime, stage, at_stage)
    reloading = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        saving = executor.submit(client.put, '/admin/api/settings/models/recall_judge', json=PAYLOAD)
        try:
            assert saving_lock.wait(THREAD_WAIT)
            assert 0 < waits[0] <= 3, 'saving used a zero-wait lock check'
            assert not saving.done()
        finally:
            release.set()
        response = saving.result(timeout=THREAD_WAIT)
        reloading[0].result(timeout=THREAD_WAIT)
    assert response.status_code == 200, response.text
    assert response.json()['models']['recall_judge']['model'] == PAYLOAD['model']
    assert client.app.state.gateway.configs['recall_judge'].model == PAYLOAD['model']
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='models_saved'").fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0


def test_model_save_shares_one_wait_budget_across_all_reads_and_write(client, monkeypatch):
    runtime = client.app.state.runtime_config
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(configuration, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(settings_api, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    lock, waits = runtime._lock, []

    class DelayedLock:
        def acquire(self, *, timeout):
            waits.append(timeout)
            clock.now += .5
            return lock.acquire(timeout=0)

        def release(self):
            lock.release()

    monkeypatch.setattr(runtime, '_lock', DelayedLock())
    response = client.put('/admin/api/settings/models/recall_judge', json=PAYLOAD)
    assert response.status_code == 200, response.text
    assert waits == pytest.approx([3, 2.5, 2, 1.5])


@pytest.mark.parametrize('acquired_before_timeout', [0, 2, 3])
def test_model_save_lock_timeout_keeps_fixed_error(client, store, monkeypatch, acquired_before_timeout):
    runtime = client.app.state.runtime_config
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(configuration, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(settings_api, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    lock, waits = runtime._lock, []

    class BusyLock:
        def acquire(self, *, timeout):
            waits.append(timeout)
            if len(waits) > acquired_before_timeout:
                clock.now += timeout
                return False
            clock.now += .5
            return lock.acquire(timeout=0)

        def release(self):
            lock.release()

    monkeypatch.setattr(runtime, '_lock', BusyLock())
    response = client.put('/admin/api/settings/models/recall_judge', json=PAYLOAD)
    assert response.status_code == 400
    assert response.json()['error'] == {'code': 'invalid_request', 'message': '配置正在更新，请稍后重试'}
    assert clock.now == pytest.approx(3)
    assert waits[-1] == pytest.approx(3 - acquired_before_timeout * .5)
    with store.read() as conn:
        saved = conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='models_saved'").fetchone()[0]
    assert saved == int(acquired_before_timeout >= 2)
