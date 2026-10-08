"""Configuration writers wait for reloads; background reads never queue."""
import errno
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from iris import configuration, process_lock
from iris.cli import main
from iris.configuration import RuntimeConfig
from iris.process_lock import StoreLease


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0
        waits = []

        def monotonic(self):
            return self.now

        def sleep(self, duration):
            self.waits.append(duration)
            self.now += duration

    clock = Clock()
    monkeypatch.setattr(configuration, 'monotonic', clock.monotonic, raising=False)
    monkeypatch.setattr(process_lock, 'monotonic', clock.monotonic, raising=False)
    monkeypatch.setattr(process_lock, 'sleep', clock.sleep, raising=False)
    return clock


def test_import_waits_for_reload_then_succeeds(store, tmp_path, monkeypatch):
    runtime = RuntimeConfig(store)
    reload_held, release_reload, reload_done, import_waiting = (Event() for _ in range(4))
    original_load = runtime._load_local

    def held_load():
        reload_held.set()
        assert release_reload.wait(5), 'test did not release the reload lock'
        return original_load()

    def reload():
        try:
            return runtime.load()
        finally:
            reload_done.set()

    def wait_for_release(_duration):
        import_waiting.set()
        assert reload_done.wait(5), 'reload did not release its lock'

    monkeypatch.setattr(runtime, '_load_local', held_load)
    monkeypatch.setattr(process_lock, 'sleep', wait_for_release, raising=False)
    fixture = tmp_path / 'models-fixture.toml'
    fixture.write_text('[chat]\nbase_url="https://models.example/v1"\nmodel="import-after-reload"\n', encoding='utf-8')
    with ThreadPoolExecutor(max_workers=2) as executor:
        reloading = executor.submit(reload)
        try:
            assert reload_held.wait(2)
            importing = executor.submit(main, ['--data-dir', str(store.path.parent), 'models', 'import', '--from', str(fixture)])
            assert import_waiting.wait(2), 'import failed instead of waiting for the reload'
            assert not importing.done()
        finally:
            release_reload.set()
        assert reloading.result(timeout=2) == {}
        assert importing.result(timeout=2) == 0
    assert runtime.load()['chat'].model == 'import-after-reload'
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='models_saved'").fetchone()[0] == 1


def test_reload_does_not_wait_for_another_process_lock(store, monkeypatch):
    def unexpected_wait(_duration):
        pytest.fail('background reload must not retry a busy lock')

    monkeypatch.setattr(process_lock, 'sleep', unexpected_wait, raising=False)
    with RuntimeConfig(store).locked():
        with pytest.raises(ValueError, match='配置正在更新'):
            RuntimeConfig(store).load()


def test_reload_does_not_wait_for_same_instance_writer(store):
    runtime = RuntimeConfig(store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with runtime._lock:
            reading = executor.submit(runtime.load)
            with pytest.raises(ValueError, match='配置正在更新'):
                reading.result(timeout=1)


def test_save_lock_timeout_is_bounded_and_does_not_write(store, clock, caplog):
    writer = RuntimeConfig(store)
    with RuntimeConfig(store).locked():
        with pytest.raises(ValueError, match='配置锁等待超时（3 秒）'):
            writer.save({})
    assert clock.now == pytest.approx(3)
    assert 0 < max(clock.waits) <= .05
    assert '配置锁等待超时（3 秒）' in caplog.text
    assert store.setting('models', None) is None
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='models_saved'").fetchone()[0] == 0
    # Both locks and the failed file handle were released; a later save succeeds.
    writer.save({})
    assert store.setting('models', None) == {}


def test_thread_and_process_locks_share_the_save_wait_budget(store, clock):
    class DelayedThreadLock:
        released = False

        def acquire(self, *, timeout):
            assert timeout == 3
            clock.now += 2
            return True

        def release(self):
            self.released = True

    writer = RuntimeConfig(store)
    writer._lock = DelayedThreadLock()
    with RuntimeConfig(store).locked():
        with pytest.raises(ValueError, match='配置锁等待超时（3 秒）'):
            writer.save({})
    assert clock.now == pytest.approx(3)
    assert sum(clock.waits) == pytest.approx(1)
    assert writer._lock.released


def test_cli_reports_configuration_lock_timeout(store, tmp_path, clock, caplog, capsys):
    fixture = tmp_path / 'models-fixture.toml'
    fixture.write_text('[chat]\nbase_url="https://models.example/v1"\nmodel="timeout-model"\n', encoding='utf-8')
    with RuntimeConfig(store).locked():
        assert main(['--data-dir', str(store.path.parent), 'models', 'import', '--from', str(fixture)]) == 1
    assert clock.now == pytest.approx(3)
    assert '配置锁等待超时（3 秒）' in caplog.text
    assert '导入失败' in capsys.readouterr().err
    assert store.setting('models', None) is None


@pytest.mark.parametrize('timeout', [0, .12])
def test_lease_releases_handle_on_timeout_and_can_be_reused(tmp_path, clock, timeout):
    database = tmp_path / 'lease.db'
    contender = StoreLease(database, timeout=timeout)
    with StoreLease(database):
        with pytest.raises(ValueError, match='数据库正由服务或离线命令使用'):
            contender.__enter__()
        assert contender.file is None
    assert clock.now == pytest.approx(timeout)
    with contender:
        assert contender.file is not None
    assert contender.file is None


def test_lease_does_not_retry_non_contention_errors(tmp_path, monkeypatch, clock):
    import fcntl

    def denied(_descriptor, _flags):
        raise OSError(errno.EPERM, 'fixture lock permission failure')

    monkeypatch.setattr(fcntl, 'flock', denied)
    lease = StoreLease(tmp_path / 'lease.db', timeout=3)
    with pytest.raises(ValueError):
        lease.__enter__()
    assert not clock.waits
    assert lease.file is None
