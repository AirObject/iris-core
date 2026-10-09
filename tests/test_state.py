"""S06–S09: host-owned current state stays separate from cognition."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeGateway, batch, msg
from iris.db import Store
from iris.maintenance import Maintenance
from iris.memory_ops import setup_role
from iris.retrieval import Retrieval
from iris.state import CurrentState
from test_batches import memory


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 9, 4, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


@pytest.fixture
def state(store):
    return CurrentState(store, clock=Clock())


def test_S07_S08_heartbeat_same_activity_and_explicit_start(state):
    initial = state.put(activity='  探索海岛  ', details={'scene': '海岸'}, mood='紧张')
    assert initial['activity'] == '探索海岛'
    assert initial['start_time_basis'] == 'first_report'
    assert initial['started_at'] == '2026-10-09T12:00:00+08:00'
    state.clock.advance(minutes=10)
    heartbeat = state.patch()
    assert heartbeat['started_at'] == initial['started_at']
    assert heartbeat['duration_seconds'] == 600
    assert heartbeat['activity_updated_at'] == initial['activity_updated_at']
    state.clock.advance(minutes=1)
    same = state.put(activity='探索海岛')
    assert same['started_at'] == initial['started_at']
    assert same['details'] == initial['details'] and same['mood_updated_at'] == initial['mood_updated_at']
    corrected = state.put(activity='探索海岛', started_at='2026-10-09T03:00:00Z')
    assert corrected['start_time_basis'] == 'host'
    assert corrected['started_at'] == '2026-10-09T11:00:00+08:00'
    assert corrected['duration_seconds'] == 4260


def test_activity_replacement_resets_start_and_old_details(state):
    first = state.put(activity='Game', details={'scene': '森林'}, mood='紧张')
    state.clock.advance(minutes=3)
    changed = state.put(activity='game')  # Case, punctuation and internal spaces are significant.
    assert changed['started_at'] != first['started_at']
    assert changed['duration_seconds'] == 0 and changed['details'] == {}
    assert changed['mood'] is None and changed['mood_updated_at'] is None
    state.clock.advance(minutes=1)
    changed = state.put(activity='休息', started_at='2026-10-09T04:02:00Z')
    assert changed['duration_seconds'] == 120 and changed['start_time_basis'] == 'host'


def test_details_and_mood_have_independent_report_timestamps(state):
    original = state.put(activity='探索', details={'scene': '海岸', 'progress': 1}, mood='紧张')
    state.clock.advance(minutes=2)
    updated = state.patch(details={'progress': 2, 'ready': True}, mood=None)
    assert updated['details']['scene'] == original['details']['scene']
    assert updated['details']['progress'] == {'value': 2, 'updated_at': updated['updated_at']}
    assert updated['details']['ready']['value'] is True
    assert updated['mood'] is None and updated['mood_updated_at'] == updated['updated_at']
    state.clock.advance(seconds=1)
    reported = state.patch(details={'progress': 2, 'scene': None})
    assert reported['details']['progress']['updated_at'] != updated['details']['progress']['updated_at']
    assert 'scene' not in reported['details']
    assert reported['started_at'] == original['started_at']


def test_stale_is_read_only_uses_clock_and_live_setting(state, store):
    state.put(activity='探索')
    state.clock.advance(minutes=30)
    assert state.get()['possibly_stale'] is False
    state.clock.advance(microseconds=1)
    assert state.get()['possibly_stale'] is True
    store.set_setting('state', {'stale_after_minutes': 31})
    assert state.get()['possibly_stale'] is False
    assert state.get()['stale_after_minutes'] == 31
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM state_reports').fetchone()[0] == 1
    assert state.patch()['possibly_stale'] is False


def test_multiple_hosts_last_report_and_end_history(state, store):
    state.put(activity='游戏', details={'scene': '森林'}, host='游戏宿主', entry_id='game')
    state.clock.advance(seconds=1)
    second = state.patch(details={'scene': '海岸'}, host='直播宿主', entry_id='stream')
    assert second['host'] == '直播宿主' and second['entry_id'] == 'stream'
    assert second['details']['scene']['value'] == '海岸'
    state.patch()  # An anonymous report must not claim the previous host as its source.
    assert state.get()['host'] is None and state.get()['entry_id'] is None
    assert state.delete(host='游戏宿主', entry_id='game') == {}
    assert state.get() == {}
    assert state.delete() == {}
    with store.read() as conn:
        reports = conn.execute('SELECT * FROM state_reports ORDER BY id').fetchall()
    assert len(reports) == 5
    assert [r['action'] for r in reports] == ['start', 'update', 'heartbeat', 'end', 'end']
    assert [r['host'] for r in reports] == ['游戏宿主', '直播宿主', None, '游戏宿主', None]
    changes = json.loads(reports[1]['changes_json'])
    assert changes['details']['scene'] == {'before': '森林', 'after': '海岸'}
    state.clock.advance(minutes=1)
    assert state.put(activity='游戏')['duration_seconds'] == 0


def test_timezone_dst_and_persistence(state, store):
    store.set_setting('timezone', 'America/New_York')
    state.clock.value = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
    result = state.put(activity='直播', started_at='2026-11-01T01:30:00-04:00')
    assert result['started_at'] == '2026-11-01T01:30:00-04:00'
    assert result['updated_at'] == '2026-11-01T01:30:00-05:00'
    assert result['duration_seconds'] == 3600
    reopened = Store(store.path)
    try:
        assert CurrentState(reopened, clock=state.clock).get() == result
    finally:
        reopened.close()


def test_S06_prepare_search_share_state_without_memories_persona_or_goals(state, store):
    setup_role(store, 'Iris', '', 'Asia/Shanghai')
    msg(store, 1, '你好')
    retrieval = Retrieval(store, clock=state.clock)
    before = retrieval.prepare('A', text='', judge=False)
    reported = state.put(activity='探索海岛', details={'scene': '海岸'}, mood='紧张')
    after = retrieval.prepare('A', text='', judge=False)
    assert after['state'] == reported
    assert after['persona'] == before['persona']
    assert after['memories'] == before['memories'] == []
    assert after['goals'] == before['goals'] == []
    assert 'state' not in retrieval.search(text='')
    assert retrieval.search(text='', include_state=True)['state'] == reported
    state.delete()
    assert retrieval.prepare('A', text='', judge=False)['state'] == {}


def test_S09_learning_and_daily_maintenance_cannot_touch_state_tables(state, store):
    state.put(activity='正在战斗', details={'scene': 'STATE_ONLY_MARKER'}, mood='紧张')
    with store.write() as conn:
        before = {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in ('current_state', 'state_reports')}
        # Fail on any write, even one which would restore the same final value.
        for table in before:
            for event in ('INSERT', 'UPDATE', 'DELETE'):
                conn.execute(f"CREATE TRIGGER guard_{table}_{event} BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'state is host owned'); END")
    msg(store, 1, '战斗已经结束，我拿到了钥匙', sender='我', kind='action_result')
    gateway = FakeGateway({'memories': [memory('我结束了战斗并拿到了钥匙', [1], '我', ['我'], '亲历')]})
    _, result = batch(store, gateway, count=1)
    assert result['created']
    assert all('STATE_ONLY_MARKER' not in text for text in gateway.materials)
    maintenance = Maintenance(store, clock=state.clock)
    run_id = maintenance.request(trigger='scheduled', schedule_key='2026-10-09')
    maintenance.run(run_id)
    with store.read() as conn:
        assert conn.execute('SELECT state FROM maintenance_runs WHERE id=?', (run_id,)).fetchone()[0] == 'completed'
        assert before == {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in before}
    assert state.get()['activity'] == '正在战斗'


def test_report_history_and_current_value_commit_atomically(state, store):
    original = state.put(activity='游戏')
    with store.write() as conn:
        conn.execute("CREATE TRIGGER fail_report BEFORE INSERT ON state_reports BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        state.put(activity='休息')
    assert state.get() == original


def test_concurrent_hosts_follow_committed_report_order(state, store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: state.put(activity=f'活动{i}', host=f'host{i}'), range(12)))
    with store.read() as conn:
        last = conn.execute('SELECT * FROM state_reports ORDER BY id DESC LIMIT 1').fetchone()
        assert conn.execute('SELECT COUNT(*) FROM state_reports').fetchone()[0] == 12
    assert state.get()['host'] == last['host']
    assert state.get()['activity'] == json.loads(last['reported_json'])['activity']


def test_prepare_reads_latest_state_after_judgment(state, store, monkeypatch):
    from iris import recall_judge
    msg(store, 1, '你好')
    state.put(activity='游戏')
    original = recall_judge.judge

    def during_judgment(*args, **kwargs):
        state.clock.advance(minutes=1)
        state.delete(host='game')
        return original(*args, **{**kwargs, 'enabled': False})

    monkeypatch.setattr(recall_judge, 'judge', during_judgment)
    assert Retrieval(store, clock=state.clock).prepare('A')['state'] == {}


def test_migration_from_previous_schema_preserves_existing_data(tmp_path):
    from pathlib import Path
    from iris.search_text import segmented
    path = tmp_path / 'v10.db'
    scripts = Path(__file__).resolve().parents[1] / 'src/iris/migrations'
    with sqlite3.connect(path) as conn:
        conn.create_function('iris_terms', 1, segmented, deterministic=True)
        conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        for script in sorted(scripts.glob('*.sql')):
            if script.name.endswith('_current_state.sql'):
                break
            conn.executescript(script.read_text(encoding='utf-8'))
            conn.execute("INSERT INTO schema_migrations VALUES(?,'2026-10-09')", (script.name,))
            conn.commit()
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('self','self','我','2026-10-09')")
        conn.execute("INSERT INTO persona_versions(content,created_at,is_current) VALUES('原 persona','2026-10-09',1)")
    upgraded = Store(path)
    try:
        assert CurrentState(upgraded).get() == {}
        assert upgraded.setting('state') == {'stale_after_minutes': 30}
        with upgraded.read() as conn:
            assert conn.execute('SELECT content FROM persona_versions WHERE is_current=1').fetchone()[0] == '原 persona'
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert len(list(tmp_path.glob('v10.db.*.bak'))) == 1
    finally:
        upgraded.close()
