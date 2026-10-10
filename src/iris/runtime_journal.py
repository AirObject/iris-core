"""Best-effort, fixed-vocabulary runtime evidence for local read-only review.

This module never accepts arbitrary diagnostic text. Journal transactions are
short and model calls happen in callers, outside them. Missing evidence is not
success: a failed write emits only a fixed warning and is not retried in a
request. Heartbeats provide another observation five minutes later.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import sqlite3
import threading
import time
import uuid
import weakref
from datetime import datetime, timedelta, timezone

log = logging.getLogger('iris.runtime_journal')
HEARTBEAT_SECONDS = 300
RETENTION_DAYS = 30
EVENTS = frozenset(('process_started', 'process_stopped', 'heartbeat', 'model_state',
    'maintenance_scheduled', 'maintenance_started', 'maintenance_completed', 'maintenance_skipped',
    'maintenance_failed', 'reminder_resolved', 'prepare'))
MODEL_KINDS = frozenset(('chat', 'embedding', 'recall_judge', 'goal_dedup_judge', 'image_understanding'))
STATES = frozenset(('normal', 'configuration_error', 'invalid_key', 'account_problem',
    'temporarily_unavailable', 'rate_limited', 'usage_limit', 'cancelled', 'skipped'))
REASONS = frozenset(('startup', 'network', 'rate_limit', 'invalid_key', 'configuration', 'account',
    'daily_limit', 'daily_reset', 'limit_changed', 'manual_retry', 'configuration_change',
    'probe_success', 'response_received', 'cooldown_elapsed', 'schedule_changed', 'shutdown',
    'interrupted', 'internal_error', 'disabled', 'unconfigured', 'model_items_disabled',
    'call_budget', 'usage_limit', 'paused', 'not_due', 'no_changes', 'already_running',
    'invalid_output', 'other', 'goal_changed', 'goal_closed', 'superseded', 'overdue_disabled',
    'already_reminded', 'revision_conflict'))
RESULTS = frozenset(('normal', 'judge_degraded', 'fulltext_degraded', 'error'))
PHASES = frozenset(('maintenance', 'consolidation', 'persona'))
FIELDS = ('entry_id', 'model_kind', 'configured', 'previous_state', 'state', 'reason', 'run_id', 'phase',
          'scheduled_at', 'duration_ms', 'result', 'plan_id', 'notification_id')
_instances = weakref.WeakValueDictionary()


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value):
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return value.astimezone(timezone.utc).isoformat()


@contextlib.contextmanager
def _write(store):
    # Use the existing single writer; observation must not queue behind a busy
    # business transaction for seconds. RLock is also held by Store.write().
    if not store._lock.acquire(timeout=.05):
        raise TimeoutError('journal writer busy')
    try:
        timeout = store._writer.execute('PRAGMA busy_timeout').fetchone()[0]
        try:
            store._writer.execute('PRAGMA busy_timeout=50')
            with store.write() as conn:
                yield conn
        finally:
            store._writer.execute(f'PRAGMA busy_timeout={timeout}')
    finally:
        store._lock.release()


def _insert(conn, event, current, fields):
    if event not in EVENTS or fields.keys()-set(FIELDS):
        raise ValueError('unknown journal field')
    for key, allowed in (('model_kind', MODEL_KINDS), ('previous_state', STATES), ('state', STATES),
                         ('reason', REASONS), ('phase', PHASES), ('result', RESULTS)):
        if fields.get(key) is not None and fields[key] not in allowed:
            raise ValueError('unknown journal category')
    if fields.get('configured') is not None and (type(fields['configured']) is not int or fields['configured'] not in (0, 1)):
        raise ValueError('invalid configuration flag')
    for key in ('run_id', 'plan_id', 'notification_id'):
        if fields.get(key) is not None and (type(fields[key]) is not int or fields[key] < 1):
            raise ValueError('invalid journal id')
    if fields.get('duration_ms') is not None:
        value = fields['duration_ms']
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid duration')
    if fields.get('scheduled_at') is not None:
        fields['scheduled_at'] = stamp(fields['scheduled_at'])
    journal = _instances.get(conn)
    return conn.execute('INSERT INTO runtime_events(instance_id,occurred_at,event,'+','.join(FIELDS)+') '
                        'VALUES('+','.join('?' for _ in range(3+len(FIELDS)))+')',
                        (journal.instance_id if journal else None, stamp(current), event,
                         *(fields.get(key) for key in FIELDS))).lastrowid


def record(target, event, *, current=None, **fields):
    """Accept Store, or an existing short transaction (e.g. reminder changes).

    A savepoint isolates an optional journal insertion from its business write.
    No exception text/traceback is logged, including validation and SQLite errors.
    """
    try:
        current = current or utc_now()
        if isinstance(target, sqlite3.Connection):
            target.execute('SAVEPOINT runtime_journal')
            try:
                result = _insert(target, event, current, fields)
            except Exception:
                target.execute('ROLLBACK TO runtime_journal')
                raise
            finally:
                target.execute('RELEASE runtime_journal')
            return result
        with _write(target) as conn:
            return _insert(conn, event, current, fields)
    except Exception:
        log.warning('runtime journal write failed')
        return None


def cleanup(store, *, current=None):
    """Bound each cleanup transaction; heartbeat children cascade with the event."""
    try:
        cutoff = stamp((current or utc_now())-timedelta(days=RETENTION_DAYS))
        while True:
            with _write(store) as conn:
                count = conn.execute('''DELETE FROM runtime_events WHERE id IN
                    (SELECT id FROM runtime_events WHERE occurred_at<? ORDER BY occurred_at,id LIMIT 1000)''',
                    (cutoff,)).rowcount
            if count < 1000:
                break
    except Exception:
        log.warning('runtime journal cleanup failed')


class Journal:
    def __init__(self, store, *, clock=utc_now):
        self.store, self.clock = store, clock
        self.instance_id = uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread = None
        self._next_heartbeat = None
        self._started = False

    def start(self, *, background=True):
        self._started = True
        _instances[self.store._writer] = self
        record(self.store, 'process_started', current=self.clock())
        self.heartbeat_if_due()
        if background:
            try:
                self._thread = threading.Thread(target=self._loop, name='iris-journal', daemon=True)
                self._thread.start()
            except Exception:
                self._thread = None
                log.warning('runtime journal write failed')

    def _loop(self):
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self.heartbeat_if_due(force=True)

    def _backlog(self, current):
        # All counts describe ONE read snapshot. Only IDs, counts and reception
        # times are selected; no per-entry writer lock or message prose scan.
        with self.store.read() as conn:
            return [tuple(r) for r in conn.execute('''WITH
                pending AS (SELECT entry_id,COUNT(*) AS n,MIN(julianday(received_at)) AS oldest
                    FROM messages WHERE learning_state IN ('pending','batched') GROUP BY entry_id),
                batches_pending AS (SELECT entry_id,SUM(state='waiting') AS waiting,SUM(state='running') AS running
                    FROM batches WHERE state IN ('waiting','running') GROUP BY entry_id),
                gaps AS (SELECT entry_id,COUNT(*) AS n FROM memory_gaps GROUP BY entry_id)
                SELECT e.id,COALESCE(p.n,0),COALESCE(b.waiting,0),COALESCE(b.running,0),
                    CASE WHEN p.oldest IS NOT NULL THEN MAX(0,(julianday(?)-p.oldest)*86400) END,
                    COALESCE(g.n,0)
                FROM entries e LEFT JOIN pending p ON p.entry_id=e.id
                LEFT JOIN batches_pending b ON b.entry_id=e.id LEFT JOIN gaps g ON g.entry_id=e.id
                ORDER BY e.id''', (stamp(current),))]

    def heartbeat_if_due(self, *, force=False):
        try:
            current = self.clock()
            if not force and self._next_heartbeat is not None and current < self._next_heartbeat:
                return
            self._next_heartbeat = current+timedelta(seconds=HEARTBEAT_SECONDS)
            rows = self._backlog(current)
            with _write(self.store) as conn:
                eid = _insert(conn, 'heartbeat', current, {})
                conn.executemany('INSERT INTO runtime_backlog VALUES(?,?,?,?,?,?,?)',
                                 ((eid, *row) for row in rows))
        except Exception:
            log.warning('runtime journal write failed')

    def stop(self, *, normal=True):
        if not self._started:
            return
        self._stop.set()
        if self._thread:
            self._thread.join()
        if normal:
            self.heartbeat_if_due(force=True)
            record(self.store, 'process_stopped', current=self.clock())
        _instances.pop(self.store._writer, None)
        self._started = False


def prepare_result(result):
    # Both degradations can occur. Full-text wins the single result category;
    # existing recalls.request_json.judgment retains the independent judgment.
    if any(h.get('code') in ('embedding_unconfigured', 'embedding_uncalibrated', 'embedding_fallback')
           for h in result.get('hints', ())):
        return 'fulltext_degraded'
    return 'judge_degraded' if result.get('judgment', {}).get('status') == 'degraded' else 'normal'


def phase_outcome(store, run_id, phase):
    """Project only fixed status/reason fields of completed cognitive phases."""
    with store.read() as conn:
        if phase == 'consolidation':
            row = conn.execute('SELECT finished,skip_reason FROM consolidation_runs WHERE run_id=?', (run_id,)).fetchone()
            if row and row['finished']:
                return ('maintenance_skipped', safe_reason(row['skip_reason'])) if row['skip_reason'] else ('maintenance_completed', None)
        elif phase == 'persona':
            row = conn.execute('SELECT status,reason FROM maintenance_persona WHERE run_id=?', (run_id,)).fetchone()
            if row:
                if row['status'] == 'not_due':
                    return 'maintenance_skipped', 'not_due'
                if row['status'] == 'skipped':
                    return 'maintenance_skipped', safe_reason(row['reason'])
                if row['status'] in ('failed', 'conflict', 'rejected'):
                    return 'maintenance_failed', safe_reason(row['reason'])
                if row['status'] != 'running':
                    return 'maintenance_completed', None
        else:
            row = conn.execute('SELECT state FROM maintenance_runs WHERE id=?', (run_id,)).fetchone()
            if row and row['state'] == 'completed':
                return 'maintenance_completed', None
    return 'maintenance_skipped', 'interrupted'


def safe_reason(value):
    # Legacy diagnostic strings may be arbitrary. Never forward them.
    value = {'persona_busy': 'already_running', 'current_version_changed': 'revision_conflict',
             'unexpected_error': 'internal_error', 'temporarily_unavailable': 'paused',
             'temporary_unavailable': 'paused'}.get(value, value)
    return value if value in REASONS else 'other'


@contextlib.contextmanager
def maintenance_span(store, run_id, phase, *, clock=utc_now, stop=None, scheduled_at=None):
    started = time.perf_counter()
    fields = dict(run_id=run_id, phase=phase, scheduled_at=scheduled_at)
    record(store, 'maintenance_started', current=clock(), **fields)
    try:
        yield
    except BaseException:
        record(store, 'maintenance_failed', current=clock(), reason='internal_error',
               duration_ms=(time.perf_counter()-started)*1000, **fields)
        raise
    else:
        try:
            event, reason = phase_outcome(store, run_id, phase)
            if event == 'maintenance_skipped' and reason == 'interrupted' and stop and stop():
                reason = 'shutdown'
            record(store, event, current=clock(), reason=reason,
                   duration_ms=(time.perf_counter()-started)*1000, **fields)
        except Exception:
            log.warning('runtime journal write failed')


class PrepareJournalMiddleware:
    """ASGI timing without adding HTTP middleware tasks to unrelated routes."""
    def __init__(self, app, *, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if not (scope['type'] == 'http' and scope['method'] == 'POST'
                and path.startswith('/api/v1/entries/') and path.endswith('/prepare')
                and len(path.split('/')) == 6):
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        status, finished = 500, False

        async def capture(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)

        try:
            await self.app(scope, receive, capture)
            finished = True
        finally:
            store = getattr(self.state, 'store', None)
            if store is not None:
                result = scope.get('state', {}).get('prepare_result', 'normal') if finished and status < 400 else 'error'
                await asyncio.to_thread(record, store, 'prepare', result=result,
                    entry_id=scope.get('path_params', {}).get('entry_id'),
                    duration_ms=(time.perf_counter()-started)*1000)
