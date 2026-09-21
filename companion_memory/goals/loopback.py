"""Explicit isolated TEST_HTTP receiver and its unforgeable local route handle.

This receiver is a validation resource. It acknowledges receipt of a bounded
intent, never execution of a goal. No arbitrary URL, hostname, redirect or remote
service can be selected through this capability.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
import secrets
import time
from companion_memory.persistence.deadlines import bounded_deadline
from types import MappingProxyType
from companion_memory.persistence import Field, RecordSchema
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.schema import InvalidValue, valid_identifier
from companion_memory.persistence.record_primitives import Record, ID, REVISION, TIME, checked, text, choice

INTENT = RecordSchema((Field('delivery_id', ID), Field('canonical_goal_id', ID), Field('revision', REVISION),
    Field('kind', choice('UPCOMING', 'DUE')), Field('deadline', TIME), Field('observed_at', TIME),
    Field('suggestion', choice('UPCOMING', 'CONSIDER_ABANDON_OR_CHANGE_DEADLINE'))))


@dataclass(frozen=True, slots=True, init=False)
class TestReminderRoute:
    route_id: str
    port: int
    _token: str = field(repr=False)
    _owner: LoopbackReminderReceiver = field(repr=False)

    def __init__(self): raise TypeError('A test route requires a live isolated loopback receiver.')
    def valid(self) -> bool: return self._owner.route is self and not self._owner.closed


class LoopbackReminderReceiver:
    """Finite actual listener owned solely by an explicit validation caller."""
    def __init__(self, *, acknowledge: bool = True, response_gate: asyncio.Event | None = None):
        self.acknowledge, self.response_gate = acknowledge, response_gate
        self.server: asyncio.Server | None = None
        self.route: TestReminderRoute | None = None
        self.received: list[Record] = []
        self.tasks: set[asyncio.Task[object]] = set()
        self.closed = False
        self.arrived = asyncio.Event()

    async def start(self, route_id: str) -> TestReminderRoute:
        if self.server is not None or self.closed or not valid_identifier(route_id): raise ValueError('A fresh explicit test receiver is required.')
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, limit=2049)
        route = object.__new__(TestReminderRoute)
        for name, value in (('route_id', route_id), ('port', int(self.server.sockets[0].getsockname()[1])), ('_token', secrets.token_urlsafe(32)), ('_owner', self)):
            object.__setattr__(route, name, value)
        self.route = route
        return route

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None or len(self.tasks) >= 4 or self.closed: writer.transport.abort(); return
        self.tasks.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
            if len(header) > 2048 or not header.startswith(b'POST /intent HTTP/1.1\r\n'): return
            pairs = [line.decode('ascii').partition(':') for line in header.split(b'\r\n')[1:-2]]
            headers = {name.lower(): value.strip() for name, separator, value in pairs if separator == ':'}
            if len(headers) != len(pairs) or self.route is None or headers.get('authorization') != 'Bearer ' + self.route._token: return
            if 'transfer-encoding' in headers: return
            count = int(headers['content-length'])
            if not 0 < count <= 2048: return
            value = checked(INTENT, decode_content(await asyncio.wait_for(reader.readexactly(count), 2), 2048), 2048)
            if len(self.received) >= 16: return
            self.received.append(value); self.arrived.set()
            if self.response_gate is not None: await self.response_gate.wait()
            if not self.acknowledge: return
            body = encode_content(MappingProxyType({'delivery_id': value['delivery_id'], 'status': 'ACKNOWLEDGED'}), 1024)
            writer.write(('HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)) + '\r\nConnection: close\r\n\r\n').encode() + body)
            await asyncio.wait_for(writer.drain(), 1)
        except (InvalidValue, ValueError, KeyError, UnicodeError, TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError): pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except (ConnectionError, OSError): pass
            self.tasks.discard(task)

    async def close(self) -> bool:
        self.closed = True
        if self.server is not None: self.server.close(); await self.server.wait_closed()
        if self.tasks: await asyncio.wait(tuple(self.tasks), timeout=2)
        return not self.tasks


async def send_test_intent(route: TestReminderRoute, intent: Record) -> tuple[str, str]:
    """One bounded attempt; any possibly sent unacknowledged result is UNKNOWN."""
    if type(route) is not TestReminderRoute or not route.valid(): return 'NOT_SENT', 'REMINDER_SINK_UNAVAILABLE'
    body = encode_content(checked(INTENT, intent, 2048), 2048)
    writer: asyncio.StreamWriter | None = None
    sent = False
    try:
        async with asyncio.timeout_at(bounded_deadline(time.monotonic(), 1)):
            reader, writer = await asyncio.open_connection('127.0.0.1', route.port, limit=2049)
            writer.write(('POST /intent HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ' + route._token + '\r\nContent-Length: '
                + str(len(body)) + '\r\nConnection: close\r\n\r\n').encode() + body)
            sent = True
            await writer.drain()
            header = await reader.readuntil(b'\r\n\r\n')
            if len(header) > 2048 or not header.startswith(b'HTTP/1.1 200 '): return 'UNKNOWN', 'INTEGRITY_FAILURE'
            pairs = [line.decode('ascii').partition(':') for line in header.split(b'\r\n')[1:-2]]
            headers = {name.lower(): value.strip() for name, separator, value in pairs if separator == ':'}
            if len(headers) != len(pairs) or 'transfer-encoding' in headers: return 'UNKNOWN', 'INTEGRITY_FAILURE'
            count = int(headers['content-length'])
            if not 0 < count <= 1024: return 'UNKNOWN', 'INTEGRITY_FAILURE'
            result = decode_content(await reader.readexactly(count), 1024)
            if type(result) is not dict or result != {'delivery_id': intent['delivery_id'], 'status': 'ACKNOWLEDGED'}: return 'UNKNOWN', 'INTEGRITY_FAILURE'
            return 'ACKNOWLEDGED', 'NO_CHANGE'
    except (TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return ('UNKNOWN' if sent else 'NOT_SENT'), 'DEADLINE_EXCEEDED'
    except (InvalidValue, UnicodeError, KeyError, ValueError): return 'UNKNOWN', 'INTEGRITY_FAILURE'
    finally:
        if writer is not None:
            writer.close()
            try: await writer.wait_closed()
            except (ConnectionError, OSError): pass
