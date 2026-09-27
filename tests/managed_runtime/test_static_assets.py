"""Explicit same-origin browser assets without access to neighboring files."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.management.identity import IdentityAuthority
from companion_memory.management.managed_http import ManagedHTTP


class StaticAssetTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_module_is_served_without_expanding_filesystem_access(self):
        settings = resolve_deployment({'deployment.origin': 'http://127.0.0.1:18193',
            'deployment.bind': '127.0.0.1', 'deployment.port': 18193})
        async def dispatch(*args):
            self.fail('Static asset requests must not enter application dispatch.')
        with TemporaryDirectory() as directory:
            root = Path(directory)
            static = root / 'static'
            static.mkdir()
            module = b'export const syntheticSetup = true;'
            for name in ('index.html', 'app.js', 'style.css', 'external_connections.js',
                         'configuration_operation.js', 'setup_configuration.js', 'onboarding.js'):
                (static / name).write_bytes(module)
            (static / 'unlisted.js').write_bytes(b'synthetic-private-neighbor')
            (root / 'outside.js').write_bytes(b'synthetic-private-parent')
            http = ManagedHTTP(settings, IdentityAuthority(), dispatch,
                lambda: {'state': 'MAINTENANCE'}, static)
            async def exchange(target: str, *, host: str = '127.0.0.1:18193',
                               origin: str = 'http://127.0.0.1:18193') -> tuple[bytes, bytes]:
                reader, writer = await asyncio.open_connection('127.0.0.1', 18193)
                try:
                    writer.write(f'GET {target} HTTP/1.1\r\nHost: {host}\r\nOrigin: {origin}\r\n\r\n'.encode())
                    await writer.drain()
                    wire = await asyncio.wait_for(reader.read(), 3)
                    head, body = wire.split(b'\r\n\r\n', 1)
                    return head, body
                finally:
                    writer.close()
                    await writer.wait_closed()
            try:
                await http.start()
                for name in ('app.js', 'external_connections.js', 'configuration_operation.js',
                             'setup_configuration.js', 'onboarding.js'):
                    with self.subTest(asset=name):
                        head, body = await exchange('/' + name)
                        self.assertEqual(int(head.split(b' ')[1]), 200, head)
                        self.assertIn(b'Content-Type: text/javascript; charset=utf-8', head)
                        self.assertIn(b'X-Content-Type-Options: nosniff', head)
                        self.assertIn(b'Cross-Origin-Resource-Policy: same-origin', head)
                        self.assertEqual(body, module)
                for target in ('/unlisted.js', '/../outside.js', '/%2e%2e/outside.js',
                               '/setup_configuration.js/../unlisted.js', '/run/secrets/bootstrap'):
                    with self.subTest(target=target):
                        head, body = await exchange(target)
                        self.assertNotEqual(int(head.split(b' ')[1]), 200, head)
                        self.assertNotIn(b'synthetic-private', body)
                        self.assertNotIn(target, http.assets)
                for kwargs in ({'host': 'evil.example'}, {'origin': 'https://evil.example'}):
                    with self.subTest(authority=kwargs):
                        head, body = await exchange('/setup_configuration.js', **kwargs)
                        self.assertEqual(int(head.split(b' ')[1]), 403, head)
                        self.assertNotEqual(body, module)
            finally:
                self.assertTrue(await http.close())
