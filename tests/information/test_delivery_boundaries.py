"""Actual ticket commitment precedes disconnect, deletion and mode cutoffs.

The scheduling barrier is synthetic; SQLite writes, loopback resets and the
first-delivery callback are actual. No failed delivery implies memory use.
"""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import struct
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.information.errors import RecallCommitted
from companion_memory.information.management import HostIdentity, ManagementPort
from companion_memory.information.records import record
from companion_memory.persistence import Committed, Found
from companion_memory.retrieval.delivery import current_delivery
from tests.information.host_support import host, learn_one
from tests.information.test_queries import query


class DeliveryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_committed_ticket_does_not_deliver_after_connection_reset_delete_or_mode_cutoff(self):
        for boundary in ('disconnect', 'delete', 'mode'):
            with self.subTest(boundary=boundary), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); h = host(root); reached = asyncio.Event(); resume = asyncio.Event()
                writer: asyncio.StreamWriter | None = None
                try:
                    self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                    oid = await learn_one(h)
                    binding = HostIdentity('delivery', 'principal', 'host', 'entry', frozenset(('search_memory', 'resolve_recall')), (), time.monotonic() + 300)
                    port = await h.bind_business(binding)
                    if h.http is None or h.runtime is None: self.fail('Expected actual HTTP and runtime.')
                    address = await h.http.start(); token = h.http.issue_test_session(port, time.monotonic() + 300)
                    original = h.management.call; delivered: list[bytes] = []
                    async def paused(port: ManagementPort, kind: str, key: str, payload: object, *, confirm_only: bool, trusted_observed_at: int | None = None):
                        result = await original(port, kind, key, payload, confirm_only=confirm_only, trusted_observed_at=trusted_observed_at)
                        if type(result) is RecallCommitted:
                            delivery = current_delivery()
                            if delivery is None: self.fail('Expected the real first-write scope.')
                            start = delivery.start
                            def observe(wire: bytes) -> None:
                                delivered.append(wire); start(wire)
                            delivery.start = observe
                            reached.set(); await resume.wait()
                        return result
                    h.management.call = paused
                    reader, writer = await asyncio.open_connection(*address)
                    body = json.dumps(query('interrupted')).encode()
                    writer.write(('POST /api/host/memory/search HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ' + token
                        + '\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n').encode() + body)
                    await writer.drain(); await asyncio.wait_for(reached.wait(), 2)
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_ticket').fetchone()[0], 1)
                        self.assertEqual(connection.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0], 0)
                    if boundary == 'disconnect':
                        connection = writer.get_extra_info('socket')
                        if connection is None: self.fail('Expected actual isolated loopback socket.')
                        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                        writer.transport.abort()
                        async with asyncio.timeout(0.5):
                            while not all(current.is_closing() for current in h.http.writers): await asyncio.sleep(0.005)
                    elif boundary == 'delete':
                        self.assertIs(type(await h.runtime.maintenance.bind((oid,)).delete_object('delete', oid, 1)), Committed)
                    else: h.runtime.gate.close_ordinary()
                    resume.set()
                    if boundary != 'disconnect':
                        response = await asyncio.wait_for(reader.read(), 2)
                        self.assertNotIn('周末去北京看展'.encode(), response)
                    if h.http.tasks: await asyncio.wait_for(asyncio.gather(*tuple(h.http.tasks)), 2)
                    self.assertFalse(delivered)
                    confirmed = await port.resolve_recall(query('interrupted'))
                    self.assertIs(type(confirmed), Found, confirmed)
                    if type(confirmed) is Found:
                        self.assertEqual(record(confirmed.value)['availability'], 'CONFIRMED_ONLY')
                        self.assertNotIn('sections', record(confirmed.value))
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM memory_usage_receipt').fetchone()[0], 0)
                        self.assertEqual(connection.execute('SELECT count(*) FROM memory_objects').fetchone()[0], int(boundary != 'delete'))
                    self.assertTrue(await h.close()); h = host(root)
                    self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                    port = await h.bind_business(binding)
                    self.assertIs(type(await port.resolve_recall(query('interrupted'))), Found)
                    self.assertEqual(h.adapter.calls, ())
                finally:
                    resume.set()
                    if writer is not None:
                        writer.close()
                        try: await writer.wait_closed()
                        except (ConnectionError, OSError): pass
                    self.assertTrue(await h.close())
