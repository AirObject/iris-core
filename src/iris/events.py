"""Bounded, read-only host reminder streams. No notification is acknowledged here.

Resume bookmarks contain only token IDs and notification watermarks, never prose.
They are process-local: an unknown/expired bookmark explicitly requests pull repair.
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import sqlite3
import threading
import time
import uuid
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import anyio
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import dumps
from .state import local_time, role_zone
from .tokens import Principal, TokenError

log = logging.getLogger('iris.events')
CURSOR_PATTERN = r'^[0-9a-f]{32}:[1-9][0-9]{0,19}$'


class EventQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    last_event_id: str | None = Field(default=None, min_length=34, max_length=53, pattern=CURSOR_PATTERN)


class EventStatus(BaseModel):
    instance_id: str
    connections: int
    reminders_pushed: int
    per_token_limit: int
    global_limit: int
    poll_seconds: float
    heartbeat_seconds: float
    idle_seconds: float
    max_seconds: float
    replay_seconds: float
    cursor_limit: int
    notification_window: int


@dataclass(frozen=True)
class Frame:
    id: str
    event: str
    data: dict

    def encode(self):
        # JSON escapes embedded newlines, preventing reminder prose from injecting
        # SSE fields. IDs and event names are server-generated fixed vocabulary.
        return f'id: {self.id}\nevent: {self.event}\ndata: {dumps(self.data)}\n\n'.encode('utf-8')


@dataclass(eq=False)
class Subscription:
    principal: Principal
    after: int
    opened: float
    last_reminder: float
    next_heartbeat: float
    resync: str | None = None
    closed: bool = False


class EventHub:
    def __init__(self, store, *, clock=time.monotonic, per_token_limit=2, global_limit=32,
                 poll_seconds=1, heartbeat_seconds=15, idle_seconds=300, max_seconds=3600,
                 replay_seconds=900, cursor_limit=10000, notification_window=1000):
        self.store, self.clock = store, clock
        self.per_token_limit, self.global_limit = per_token_limit, global_limit
        self.poll_seconds, self.heartbeat_seconds = poll_seconds, heartbeat_seconds
        self.idle_seconds, self.max_seconds = idle_seconds, max_seconds
        self.replay_seconds, self.cursor_limit = replay_seconds, cursor_limit
        self.notification_window = notification_window
        self.instance_id = uuid.uuid4().hex
        self._sequence = 0
        self._lock = threading.RLock()
        self._streams = set()
        self._tokens = Counter()
        self._cursors = OrderedDict()
        self._pushed = 0
        self._next_log = 0
        self.stopped = asyncio.Event()

    def status(self):
        with self._lock:
            return {'instance_id': self.instance_id, 'connections': len(self._streams),
                    'reminders_pushed': self._pushed, 'per_token_limit': self.per_token_limit,
                    'global_limit': self.global_limit, 'poll_seconds': self.poll_seconds,
                    'heartbeat_seconds': self.heartbeat_seconds, 'idle_seconds': self.idle_seconds,
                    'max_seconds': self.max_seconds, 'replay_seconds': self.replay_seconds,
                    'cursor_limit': self.cursor_limit, 'notification_window': self.notification_window}

    def _log(self, reason):
        # Count successful ASGI writes, not receipt/delivery. Logger failures must
        # not change the service, and neither prose nor credentials reach it.
        try:
            log.info('host_events instance=%s reason=%s connections=%d reminders_pushed=%d',
                     self.instance_id, reason, len(self._streams), self._pushed)
        except Exception:
            pass

    def _prune(self, current):
        while self._cursors:
            _, (_, _, created) = next(iter(self._cursors.items()))
            if len(self._cursors) <= self.cursor_limit and current - created < self.replay_seconds:
                break
            self._cursors.popitem(last=False)

    def open(self, principal, last_event_id=None):
        if last_event_id is not None and (len(last_event_id) > 53 or not re.fullmatch(CURSOR_PATTERN, last_event_id)):
            raise TokenError(400, 'invalid_request', '事件断点格式不合法')
        with self._lock:
            if self.stopped.is_set():
                raise TokenError(503, 'unavailable', '事件流正在关闭', retry_after=1)
            if self._tokens[principal.id] >= self.per_token_limit:
                raise TokenError(429, 'event_token_limit', '已达到该令牌的事件流连接上限', retry_after=5)
            if len(self._streams) >= self.global_limit:
                raise TokenError(429, 'event_global_limit', '已达到服务的事件流连接上限', retry_after=5)
            current = self.clock()
            self._prune(current)
            after, resync = 0, None
            if last_event_id is not None:
                bookmark = self._cursors.get(last_event_id)
                if bookmark is None or bookmark[0] != principal.id:
                    resync = 'cursor_unavailable'
                else:
                    after = bookmark[1]
            stream = Subscription(principal, after, current, current, current, resync)
            self._streams.add(stream)
            self._tokens[principal.id] += 1
            self._log('connected')
            return stream

    def close(self, stream, reason='disconnected'):
        with self._lock:
            if stream.closed:
                return
            stream.closed = True
            self._streams.discard(stream)
            self._tokens[stream.principal.id] -= 1
            if not self._tokens[stream.principal.id]:
                del self._tokens[stream.principal.id]
            self._log(reason)

    def shutdown(self):
        # Called on the ASGI event loop; signal handlers schedule this thread-safely.
        self.stopped.set()

    def _frame(self, stream, event, data):
        with self._lock:
            # Timestamp bookmarks at insertion, preserving age order even when
            # concurrent read snapshots finish in a different order.
            current = self.clock()
            self._sequence += 1
            cursor = f'{self.instance_id}:{self._sequence}'
            self._cursors[cursor] = (stream.principal.id, stream.after, current)
            self._prune(current)
        return Frame(cursor, event, data)

    def poll(self, stream):
        current = self.clock()
        if stream.closed:
            return []
        reason = ('shutdown' if self.stopped.is_set() else
                  'max_duration' if current - stream.opened >= self.max_seconds else
                  'idle_timeout' if current - stream.last_reminder >= self.idle_seconds else None)
        if reason:
            self.close(stream, reason)
            return []
        try:
            with self.store.read() as conn:
                conn.execute('PRAGMA busy_timeout=100')
                # Revocation is rechecked every poll, without rate-bucket writes.
                active = conn.execute('SELECT 1 FROM host_tokens WHERE id=? AND revoked_at IS NULL',
                                      (stream.principal.id,)).fetchone()
                if not active:
                    self.close(stream, 'revoked')
                    return []
                head = conn.execute('SELECT COALESCE(MAX(id),0) FROM notifications').fetchone()[0]
                floor = max(0, head - self.notification_window)
                if stream.after < floor or stream.after > head:
                    stream.resync = 'window_exceeded'
                if stream.resync:
                    stream.after = floor
                clause, args = stream.principal.scope.sql('g.entry_id')
                # PK range is bounded even if most rows belong to other entries.
                rows = conn.execute("SELECT n.* FROM notifications n LEFT JOIN goals g ON g.id=n.goal_id "
                    "WHERE n.id>? AND n.id<=? AND n.kind='goal_reminder' AND n.status!='cancelled' AND "
                    + clause + ' ORDER BY n.id LIMIT 100', (stream.after, head, *args)).fetchall()
                zone = role_zone(conn)
        except sqlite3.Error:
            self.close(stream, 'storage_unavailable')
            return []
        frames = []
        if stream.resync:
            frames.append(self._frame(stream, 'resync', {'reason': stream.resync,
                'pull_url': '/api/v1/notifications'}))
            stream.resync = None
        for row in rows:
            item = dict(row)
            for key in ('deadline_at', 'scheduled_at', 'published_at', 'taken_at', 'cancelled_at'):
                item[key] = local_time(item[key], zone)
            stream.after = row['id']
            frames.append(self._frame(stream, 'reminder', item))
        if rows:
            stream.last_reminder = current
        if len(rows) < 100:
            stream.after = head
        if current >= stream.next_heartbeat:
            frames.append(self._frame(stream, 'heartbeat', {'time': datetime.now(timezone.utc).isoformat()}))
            stream.next_heartbeat = current + self.heartbeat_seconds
        with self._lock:
            if current >= self._next_log:
                self._next_log = current + 60
                self._log('periodic')
        return frames

    def sent(self, frame):
        if frame.event == 'reminder':
            with self._lock:
                self._pushed += 1

    async def frames(self, stream):
        while not stream.closed and not self.stopped.is_set():
            for frame in await asyncio.to_thread(self.poll, stream):
                if self.stopped.is_set():
                    return
                yield frame
            if stream.closed:
                return
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


class EventResponse(StreamingResponse):
    """Bound all waits, including a stalled consumer, and always release the slot."""
    media_type = 'text/event-stream'

    def __init__(self, hub, stream):
        self.hub, self.stream = hub, stream
        super().__init__(hub.frames(stream), media_type=self.media_type,
                         headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    async def __call__(self, scope, receive, send):
        try:
            await send({'type': 'http.response.start', 'status': 200, 'headers': self.raw_headers})
            async with anyio.create_task_group() as tasks:
                async def stop_when_closed():
                    await self.hub.stopped.wait()
                    tasks.cancel_scope.cancel()

                async def disconnected():
                    await self.listen_for_disconnect(receive)
                    tasks.cancel_scope.cancel()

                async def deadline():
                    remaining = max(0, self.hub.max_seconds - (self.hub.clock() - self.stream.opened))
                    await asyncio.sleep(remaining)
                    tasks.cancel_scope.cancel()

                tasks.start_soon(stop_when_closed)
                tasks.start_soon(disconnected)
                tasks.start_soon(deadline)
                async for frame in self.body_iterator:
                    with anyio.move_on_after(5) as timeout:
                        await send({'type': 'http.response.body', 'body': frame.encode(), 'more_body': True})
                    if timeout.cancel_called:
                        break
                    self.hub.sent(frame)
                tasks.cancel_scope.cancel()
            with anyio.move_on_after(1):
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        finally:
            self.hub.close(self.stream)
            await self.body_iterator.aclose()


class EventDrainMiddleware:
    """Cancel at the actual network send, outside BaseHTTPMiddleware buffers.

    This observer stays dormant until an authenticated SSE response starts. All
    Host/readiness/token checks and errors run through the existing boundaries.
    """
    def __init__(self, app, *, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] != '/api/v1/events' or scope['method'] != 'GET':
            return await self.app(scope, receive, send)
        started = asyncio.Event()
        finished = False

        async def bounded_send(message):
            nonlocal finished
            if message['type'] == 'http.response.start' and message['status'] == 200:
                started.set()
            if started.is_set():
                with anyio.fail_after(5):
                    await send(message)
            else:
                await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                finished = True

        async with anyio.create_task_group() as tasks:
            async def drain():
                await started.wait()
                await self.state.events.stopped.wait()
                tasks.cancel_scope.cancel()
            tasks.start_soon(drain)
            try:
                await self.app(scope, receive, bounded_send)
            except TimeoutError:
                if not started.is_set():
                    raise
            finally:
                tasks.cancel_scope.cancel()
        if started.is_set() and not finished:
            with anyio.move_on_after(1):
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})


@contextmanager
def drain_signals(hub):
    """Drain before Uvicorn waits for HTTP connections, not only at lifespan end.

    Preserve and chain the server's installed handlers (including double Ctrl-C).
    Embedded servers outside the main thread can call hub.shutdown() before
    awaiting server shutdown. Lifespan teardown is an additional final safeguard.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()
    installed = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous = signal.getsignal(sig)
            if not callable(previous):
                continue
            def drain(signum, frame, previous=previous):
                loop.call_soon_threadsafe(hub.shutdown)
                previous(signum, frame)
            signal.signal(sig, drain)
            installed[sig] = (previous, drain)
        yield
    finally:
        for sig, (previous, ours) in installed.items():
            if signal.getsignal(sig) is ours:
                signal.signal(sig, previous)
