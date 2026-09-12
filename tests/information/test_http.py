"""Actual loopback business transport with first-write authorization and bounds."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.persistence import Found
from companion_memory.information.management import HostIdentity
from tests.information.host_support import host, learn_one
from tests.information.test_queries import query


async def exchange(address: tuple[str, int], token: str, path: str, payload: object, *, body_override: bytes | None = None, method: str = 'POST') -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection(*address)
    body = json.dumps(payload, ensure_ascii=False).encode() if body_override is None else body_override
    writer.write((method + ' ' + path + ' HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ' + token + '\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n').encode() + body)
    await writer.drain()
    try:
        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
        status = int(header.split(b' ')[1])
        count = int(next(line.split(b':', 1)[1] for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')))
        response = await asyncio.wait_for(reader.readexactly(count), 5)
        return status, json.loads(response)
    finally:
        writer.close(); await writer.wait_closed()


class InformationHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_headers_and_bodies_and_full_connection_admission_release_actual_slots(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory)); held: list[asyncio.StreamWriter] = []
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')
                port = await h.bind_business(HostIdentity('http', 'principal', 'host', 'entry', frozenset(('search_memory',)), (), time.monotonic() + 300))
                if h.http is None: self.fail('Expected actual HTTP listener.')
                address = await h.http.start(); token = h.http.issue_test_session(port, time.monotonic() + 300)
                for _ in range(4):
                    _, writer = await asyncio.open_connection(*address); held.append(writer)
                async with asyncio.timeout(1):
                    while len(h.http.tasks) != 4: await asyncio.sleep(0.01)
                reader, writer = await asyncio.open_connection(*address)
                try:
                    self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
                finally: writer.close(); await writer.wait_closed()
                self.assertEqual(len(h.http.tasks), 4)
                for writer in held: writer.close(); await writer.wait_closed()
                held.clear()
                async with asyncio.timeout(1):
                    while h.http.tasks: await asyncio.sleep(0.01)
                for incomplete in (b'POST /api/host/memory/search HTTP/1.1\r\n', b'POST /api/host/memory/search HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{'):
                    reader, writer = await asyncio.open_connection(*address)
                    try:
                        writer.write(incomplete); await writer.drain()
                        response = await asyncio.wait_for(reader.read(), 3)
                        self.assertTrue(response.startswith(b'HTTP/1.1 504'), response)
                    finally: writer.close(); await writer.wait_closed()
                status, _ = await exchange(address, token, '/api/host/memory/search', query('after-timeouts'))
                self.assertEqual(status, 200)
                status, _ = await exchange(address, token, '/api/host/memory/deep-recall', query('deep-denied'))
                self.assertEqual(status, 403)
                expired = h.http.issue_test_session(port, time.monotonic() + 0.01)
                await asyncio.sleep(0.02)
                status, _ = await exchange(address, expired, '/api/host/memory/search', query('expired'))
                self.assertEqual(status, 401)
            finally:
                for writer in held: writer.close(); await writer.wait_closed()
                self.assertTrue(await h.close())

    async def test_loopback_search_original_confirmation_and_input_limits(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                port = await h.bind_business(HostIdentity('http', 'principal', 'host', 'entry', frozenset(('search_memory', 'resolve_recall')), (), time.monotonic() + 300))
                if h.http is None: self.fail('Expected actual host HTTP owner.')
                address = await h.http.start(); token = h.http.issue_test_session(port, time.monotonic() + 300)
                status, value = await exchange(address, token, '/api/host/memory/search', query('search'))
                self.assertEqual(status, 200, value); self.assertEqual(value['status'], 'FOUND')
                self.assertIn('周末去北京看展', repr(value))
                status, value = await exchange(address, token, '/api/host/memory/search', query('search'))
                self.assertEqual(status, 200); self.assertIn('CONFIRMED_ONLY', repr(value)); self.assertNotIn('周末去北京看展', repr(value))
                status, _ = await exchange(address, token, '/api/host/memory/search', {}, body_override=b'{"entry_id":"entry","entry_id":"other"}')
                self.assertEqual(status, 400)
                status, _ = await exchange(address, token, '/api/host/memory/search', {}, body_override=b'x' * 16385)
                self.assertEqual(status, 413)
                h.http.revoke_test_session(token)
                status, _ = await exchange(address, token, '/api/host/memory/search', query('new'))
                self.assertEqual(status, 401)
            finally:self.assertTrue(await h.close())

    async def test_revocation_after_query_admission_prevents_first_body_delivery(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory)); resume = asyncio.Event(); reached = asyncio.Event()
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                port = await h.bind_business(HostIdentity('http', 'principal', 'host', 'entry', frozenset(('search_memory',)), (), time.monotonic() + 300))
                if h.http is None or h.queries is None: self.fail('Expected actual HTTP and query owners.')
                address = await h.http.start(); token = h.http.issue_test_session(port, time.monotonic() + 300)
                original = h.queries._sections
                async def paused(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    reached.set(); await resume.wait(); return result
                h.queries._sections = paused
                pending = asyncio.create_task(exchange(address, token, '/api/host/memory/search', query('paused')))
                await asyncio.wait_for(reached.wait(), 2)
                h.http.revoke_test_session(token); resume.set()
                status, body = await pending
                self.assertEqual(status, 401); self.assertNotIn('周末去北京看展', repr(body))
            finally:
                resume.set(); self.assertTrue(await h.close())
