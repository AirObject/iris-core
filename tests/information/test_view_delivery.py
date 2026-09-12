"""Actual state/goals snapshots pass one encoded native and HTTP delivery barrier."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from companion_memory.information.management import HostIdentity
from companion_memory.information.errors import InformationRejected
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.content_codec import encode_content
from companion_memory.retrieval.delivery import DeliveryScope
from tests.information.host_support import host
from tests.information.test_reminders import ready_goal


class ViewDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_encoded_native_views_check_revocation_mode_and_change_in_both_orders(self):
        for operation in ('get_state_view', 'list_open_goals'):
            for boundary in ('revoke', 'mode', 'change'):
                for first in (False, True):
                    with self.subTest(operation=operation, boundary=boundary, delivered_first=first), TemporaryDirectory() as directory:
                        h = host(Path(directory)); delivered: list[bytes] = []
                        try:
                            self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                            await ready_goal(h)
                            port = await h.bind_business(HostIdentity('views', 'principal', 'host', 'entry', frozenset((operation,)), (), time.monotonic() + 300))
                            if h.business is None or h.runtime is None: self.fail('Expected actual business owners.')
                            business, gate = h.business, h.runtime.gate
                            def cutoff() -> None:
                                if boundary == 'revoke': business.revoke(port)
                                elif boundary == 'mode': gate.close_ordinary()
                                else:
                                    gate.begin_information_change('competing-change'); gate.finish_information_change('competing-change')
                            def prepare(value) -> bytes:
                                wire = encode_content(value, 131072)
                                if not first: cutoff()
                                return wire
                            def start(wire: bytes) -> None:
                                delivered.append(wire)
                                if first: cutoff()
                            with DeliveryScope(prepare, lambda: True, start):
                                result = await (port.get_state_view() if operation == 'get_state_view' else port.list_open_goals())
                            self.assertIs(type(result), Found if first else InformationRejected, result)
                            self.assertEqual(len(delivered), int(first))
                        finally: self.assertTrue(await h.close())

    async def test_native_encoding_itself_precedes_last_permission_check(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await ready_goal(h)
                port = await h.bind_business(HostIdentity('views', 'principal', 'host', 'entry', frozenset(('get_state_view',)), (), time.monotonic() + 300))
                if h.business is None: self.fail('Expected business owner.')
                def encode(value, limit):
                    wire = encode_content(value, limit)
                    port._management.revoke()
                    return wire
                with patch('companion_memory.retrieval.delivery.encode_content', encode):
                    self.assertIs(type(await port.get_state_view()), InformationRejected)
            finally: self.assertTrue(await h.close())

    async def test_actual_state_write_invalidates_a_previously_read_snapshot(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await ready_goal(h)
                read = await h.bind_business(HostIdentity('view', 'principal', 'host', 'entry', frozenset(('get_state_view',)), (), time.monotonic() + 300))
                write = await h.bind_management(HostIdentity('write', 'principal', 'host', 'entry', frozenset(('state_set',)), (), time.monotonic() + 300))
                if h.current_state is None: self.fail('Expected state owner.')
                original = h.current_state.view
                async def inspect(at_us: int):
                    snapshot = await original(at_us)
                    changed = await write.execute('state_set', 'change', {'activity_id': None, 'expected_revision': None, 'replace_activity': False,
                        'patch': {'activity_value': '正文', 'reported_at': at_us, 'reported_offset_minutes': 0}})
                    self.assertIs(type(changed), Committed, changed)
                    return snapshot
                h.current_state.view = inspect
                self.assertIs(type(await read.get_state_view()), InformationRejected)
            finally: self.assertTrue(await h.close())

    async def test_http_session_revoke_after_frame_encoding_never_delivers_view(self):
        from companion_memory.management.information_http import frame
        for path in ('/api/host/state', '/api/host/goals'):
            for first in (False, True):
                with self.subTest(path=path, delivered_first=first), TemporaryDirectory() as directory:
                    h = host(Path(directory)); writer: asyncio.StreamWriter | None = None
                    try:
                        self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                        await ready_goal(h)
                        port = await h.bind_business(HostIdentity('http-view', 'principal', 'host', 'entry', frozenset(('get_state_view', 'list_open_goals')), (), time.monotonic() + 300))
                        if h.http is None: self.fail('Expected HTTP owner.')
                        http = h.http; address = await http.start(); token = http.issue_test_session(port, time.monotonic() + 300)
                        def framed(status, value):
                            wire = frame(status, value)
                            if status == 200:
                                if first: asyncio.get_running_loop().call_soon(http.revoke_test_session, token)
                                else: http.revoke_test_session(token)
                            return wire
                        with patch('companion_memory.management.information_http.frame', framed):
                            reader, writer = await asyncio.open_connection(*address)
                            writer.write(('GET ' + path + ' HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ' + token + '\r\n\r\n').encode())
                            await writer.drain(); response = await asyncio.wait_for(reader.read(), 2)
                        self.assertEqual(response.startswith(b'HTTP/1.1 200'), first, response)
                        if not first: self.assertNotIn(b'"value"', response)
                    finally:
                        if writer is not None: writer.close(); await writer.wait_closed()
                        self.assertTrue(await h.close())
