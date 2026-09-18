"""Real loopback framing and deterministic authority/descendant race checks."""
import asyncio
import gc
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.management.identity import IdentityAuthority, Principal, digest
from companion_memory.management.managed_http import ManagedHTTP
from companion_memory.management.request_admission import RequestAdmission
from companion_memory.persistence.completion import retain_completion
from companion_memory.persistence.owned_statements import OwnerFailure


class CommunicationTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_ipv6_authority_requires_complete_bracket_and_port_syntax(self):
        from companion_memory.configuration.network_origin import authority, parse_origin
        self.assertEqual(authority('[0:0:0:0:0:0:0:1]:443', 'https'), ('::1', 443))
        self.assertEqual(parse_origin('https://[::1]'), parse_origin('HTTPS://[::1]:443'))
        for supplied in ('[::1]evil', '[::1].example', '[::1]:', '[::1]:443:80',
                         '[::1]:+443', '[::1]:0', '[::1]:65536', '[::1%25eth0]'):
            with self.subTest(authority=supplied), self.assertRaises(ValueError):
                authority(supplied, 'https')

    async def test_full_origin_host_comparison_ignores_forwarded_headers(self):
        settings = resolve_deployment({'deployment.origin': 'https://core.example', 'deployment.port': 18192, 'deployment.trusted_proxy_peers': '127.0.0.1'})
        calls = []
        async def dispatch(*args):
            calls.append(args)
            return {}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('index.html', 'app.js', 'style.css'):
                (root / name).write_text('fixture')
            http = ManagedHTTP(settings, IdentityAuthority(), dispatch, lambda: {'state': 'READY'}, root)
            await http.start()
            async def exchange(extra, *, host='core.example', method='GET', target='/health', body=b''):
                reader, writer = await asyncio.open_connection('127.0.0.1', 18192)
                writer.write(f'{method} {target} HTTP/1.1\r\nHost: {host}\r\n'.encode() + extra + b'\r\n' + body)
                await writer.drain()
                wire = await asyncio.wait_for(reader.read(), 3)
                writer.close()
                await writer.wait_closed()
                return int(wire.split(b' ')[1])
            try:
                for origin in ('https://core.example', 'https://CORE.example:443', 'HTTPS://core.example'):
                    self.assertEqual(await exchange(f'Origin: {origin}\r\n'.encode()), 200)
                self.assertEqual(await exchange(b'', host='core.example:443'), 200)
                for origin in ('http://core.example', 'http://core.example:443', 'https://core.example:80',
                               'null', 'https://core.example https://evil.example', 'https://core.example/',
                               'https://user@core.example', 'https://core.example?x'):
                    self.assertIn(await exchange(f'Origin: {origin}\r\n'.encode()), (400, 403))
                self.assertEqual(await exchange(b'Forwarded: host=core.example;proto=https\r\nX-Forwarded-Host: core.example\r\n', host='evil.example'), 403)
                for extra in (b'Host: core.example\r\n', b'Origin: https://core.example\r\nOrigin: https://core.example\r\n',
                              b'Content-Length: 0\r\nContent-Length: 0\r\n', b' folded: value\r\n'):
                    self.assertEqual(await exchange(extra), 400)
                self.assertEqual(await exchange(b'Origin: https://core.example\r\nContent-Type: application/json\r\nContent-Length: 1048577\r\n', method='POST', target='/api/login'), 400)
                self.assertEqual(await exchange(b'X-Oversize: ' + b'x' * 8192 + b'\r\n'), 400)
                duplicate = b'{"key":"a","key":"b"}'
                self.assertEqual(await exchange(b'Origin: https://core.example\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(duplicate)).encode() + b'\r\n', method='POST', target='/api/login', body=duplicate), 400)
                self.assertEqual(calls, [])
            finally:
                self.assertTrue(await http.close())
                self.assertEqual(len(http.connections), 0)

    async def test_equal_principals_revocation_has_no_first_write_gap(self):
        identity = IdentityAuthority()
        token = 'a' * 48
        verifier = digest(token)
        identity.rows = AsyncMock()
        identity.rows.read.return_value = {'object_id': verifier, 'verifier': verifier, 'revision': 1,
            'host_id': 'host', 'entries': ('entry',), 'operations': ('query',), 'revoked': False,
            'expires_at_us': time.time_ns() // 1000 + 60000000}
        first = await identity.authenticate(token, host=True)
        second = await identity.authenticate(token, host=True)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        del first
        gc.collect()
        blocked = asyncio.Event()
        identity._write = AsyncMock(side_effect=lambda *args, **kwargs: None)
        async def change(*args, **kwargs):
            await blocked.wait()
            return None
        identity._write.side_effect = change
        written = []
        identity.start_delivery(second, lambda: written.append('before'))
        revoke = asyncio.create_task(identity.write('revoke_host_token', 'revoke', {'object_id': verifier}, 1, 'digest'))
        await asyncio.sleep(0)
        with self.assertRaises(OwnerFailure):
            identity.start_delivery(second, lambda: written.append('after'))
        blocked.set()
        await revoke
        await asyncio.sleep(0)
        with self.assertRaises(OwnerFailure):
            identity.start_delivery(second, lambda: written.append('finished'))
        self.assertEqual(written, ['before'])
        del second
        gc.collect()
        self.assertEqual(identity._withdrawn, set())

    async def test_request_admission_tracks_actual_descendants_and_exclusive_drain(self):
        admission = RequestAdmission(2, .02)
        release = asyncio.Event()
        async def parent():
            retain_completion(asyncio.create_task(release.wait()))
            return 'committed'
        self.assertEqual(await admission.run(parent, exclusive=False), 'committed')
        self.assertEqual(len(admission.tasks), 1)
        # A different ordinary call proceeds while the first retains its tail.
        self.assertEqual(await admission.run(lambda: asyncio.sleep(0, result='read'), exclusive=False), 'read')
        with self.assertRaises(OwnerFailure) as raised:
            await admission.run(lambda: asyncio.sleep(0), exclusive=True)
        self.assertTrue(raised.exception.cleanup_pending)
        self.assertFalse(admission.exclusive)
        self.assertEqual(len(admission.tasks), 1)
        release.set()
        await asyncio.wait(tuple(admission.tasks))
        self.assertEqual(await admission.run(lambda: asyncio.sleep(0, result='maintenance'), exclusive=True), 'maintenance')
        await asyncio.sleep(0)
        self.assertFalse(admission.tasks)
        self.assertFalse(admission.exclusive)

    async def test_port_cache_does_not_evict_an_actual_descendant(self):
        from typing import cast
        from companion_memory.management.port_cache import PortCache
        from companion_memory.information.business import InformationPort
        cache = PortCache(1)
        released = asyncio.Event()
        revoked = []
        async def bind(expires):
            return cast(InformationPort, object())
        async with cache.lease(('first',), bind, revoked.append) as first:
            retain_completion(asyncio.create_task(released.wait()))
        self.assertEqual(cache.entries[('first',)].users, 1)
        with self.assertRaises(OwnerFailure):
            async with cache.lease(('second',), bind, revoked.append):
                self.fail('An occupied port cannot be replaced.')
        self.assertEqual(revoked, [])
        released.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        async with cache.lease(('second',), bind, revoked.append):
            self.assertEqual(revoked, [first])
        self.assertEqual(len(cache.entries), 1)

    async def test_route_rotation_skips_offline_head_and_preserves_other_routes(self):
        from companion_memory.information.reminders import ReminderDispatcher
        dispatcher = object.__new__(ReminderDispatcher)
        dispatcher.goals = AsyncMock()
        dispatcher.goals.configuration.candidate.information.record = lambda _: {'sink_mode': 'TEST_HTTP'}
        dispatcher.registered_routes = ('offline', 'a', 'b', 'c', 'd')
        from companion_memory.goals.loopback import TestReminderRoute
        from typing import cast
        dispatcher.routes = {name: cast(TestReminderRoute, object()) for name in ('a', 'b', 'c', 'd')}
        dispatcher.route_turn = 0
        dispatcher.goals.route_due_plan.side_effect = lambda route, now: {'route_id': route}
        for expected in ('a', 'b', 'c', 'd', 'a', 'b', 'c', 'd'):
            selected = await dispatcher.select_plan(1)
            assert selected is not None
            self.assertEqual(selected['route_id'], expected)
        self.assertEqual([call.args[0] for call in dispatcher.goals.route_due_plan.call_args_list], ['a', 'b', 'c', 'd'] * 2)

    async def test_wsproto_fragmented_text_and_control_frames(self):
        from wsproto.connection import Connection, ConnectionType
        from wsproto.events import TextMessage, Ping, Pong
        client, server = Connection(ConnectionType.CLIENT), Connection(ConnectionType.SERVER)
        wire = client.send(TextMessage(data='{"version":1,', message_finished=False))
        wire += client.send(Ping(payload=b'bounded'))
        wire += client.send(TextMessage(data='"type":"ack"}', message_finished=True))
        fragments, messages, pings = [], [], []
        for byte in wire:
            server.receive_data(bytes((byte,)))
            for event in server.events():
                if isinstance(event, TextMessage):
                    fragments.append(event.data)
                    if event.message_finished:
                        messages.append(''.join(fragments)); fragments.clear()
                elif isinstance(event, Ping):
                    pings.append(event.payload)
                    client.receive_data(server.send(event.response()))
        self.assertEqual(messages, ['{"version":1,"type":"ack"}'])
        self.assertEqual(pings, [b'bounded'])
        self.assertTrue(any(isinstance(event, Pong) for event in client.events()))

    async def test_ping_flood_without_reads_has_bounded_actual_write_buffer(self):
        import os
        import socket
        from wsproto.connection import Connection, ConnectionType
        from wsproto.events import Ping
        from companion_memory.management.websocket_transport import WebSocketTransport
        completed=asyncio.Event();transports=[];failures=[]
        async def serve(reader,writer):
            transport=WebSocketTransport(reader,writer,lambda _:asyncio.sleep(0))
            transports.append(transport)
            writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,4096)
            headers={'host':'localhost','connection':'Upgrade','upgrade':'websocket','sec-websocket-version':'13',
                'sec-websocket-key':'dGhlIHNhbXBsZSBub25jZQ==','sec-websocket-protocol':'iris.communication.v1'}
            transport.accept(headers,'/api/host/ws')
            try:await transport.run()
            except Exception as error:failures.append(repr(error))
            finally:completed.set()
        server=await asyncio.start_server(serve,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        client=socket.socket();client.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024);client.settimeout(2)
        before=int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
        def flood():
            client.connect(('127.0.0.1',port))
            protocol=Connection(ConnectionType.CLIENT)
            frame=protocol.send(Ping(payload=b'x'*125));count=0
            try:
                for _ in range(10000):client.sendall(frame*64);count+=64
            except OSError:pass
            return count
        try:
            count=await asyncio.to_thread(flood)
            await asyncio.wait_for(completed.wait(),15)
            transport=transports[0]
            after=int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
            self.assertTrue(transport.backpressure_closed)
            self.assertGreater(transport.control_frames_written,16)
            self.assertLessEqual(transport.peak_write_buffer_bytes,transport.byte_limit)
            self.assertLess(after-before,16*1024*1024)
            self.assertTrue(transport.writer.is_closing())
            self.assertEqual(transport.writer.transport.get_write_buffer_size(),0)
            self.assertEqual((len(transport.pending),transport.buffered_bytes),(0,0))
            self.assertEqual(failures,[])
            print({'ping_frames_sent':count,'control_frames_written':transport.control_frames_written,
                'peak_socket_buffer_bytes':transport.peak_write_buffer_bytes,'rss_delta_bytes':after-before,
                'overload_closed':True,'released_buffer_bytes':0})
        finally:
            client.close();server.close();await server.wait_closed()
            for transport in transports:await transport.close()
