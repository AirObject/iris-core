"""Strict host clients accept a locally published role without model dispatch."""
import asyncio
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest

from clients.iris_client import IrisClient
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.managed_http import ManagedHTTP
from companion_memory.persistence import Committed, Ready
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from .test_business import setup_draft
from .test_protocol_resources import ROOT, validate_http


class LocalPersonaProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_role_query_and_prepare_match_strict_http_schema(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            port = 18190
            settings = resolve_deployment({'deployment.data_root': str(root),
                'deployment.port': port, 'deployment.origin': f'http://127.0.0.1:{port}'})
            bootstrap = ManagedBootstrap(settings)
            self.assertIs(type(await bootstrap.open()), Ready)
            application = ManagedApplication(bootstrap)
            business, identity = application.business, application.identity
            http = None
            try:
                draft = setup_draft(root)
                for name in ('entry_id', 'host_id', 'conversation_id'):
                    del draft[name]
                draft['setup_mode'] = 'SIMPLE'
                self.assertIs(type(await identity.save_draft('local-http-draft', None, draft)), Committed)
                initialized = await business.initialize('local-http-initialize', 1)
                self.assertEqual(initialized, {'state': 'READY', 'startup_sends': 0, 'business_ready': True})
                host = business.host
                assert host is not None
                bindings = await host.assembly.ingress.registered_entries()
                self.assertEqual(len(bindings), 1)
                binding = bindings[0]
                entry_id = str(binding['entry_id'])
                _, token = await identity.create_token('local-schema-client', str(binding['host_id']),
                    (entry_id,), ('query', 'prepare'), time.time_ns() // 1000 + 120000000)
                assert token is not None
                static = root / 'static'
                static.mkdir()
                for name in ('index.html', 'app.js', 'style.css'):
                    (static / name).write_text('local protocol fixture')
                http = ManagedHTTP(settings, identity, application.dispatch, application.health, static,
                    communication_source=lambda: business.communication)
                await http.start()
                client = IrisClient(f'http://127.0.0.1:{port}', token)
                query = json.loads((ROOT / 'clients/examples/query.json').read_text())
                query['entry_id'] = entry_id
                for path, body in (('memory/search', query), ('prepare', query | {
                        'request_key': 'local-schema-prepare', 'participant_ids': [], 'situation': '本地角色协议验证'})):
                    response = await asyncio.to_thread(client.request, '/api/host/' + path,
                        {'entry_id': entry_id, 'input': body})
                    validate_http('/api/host/' + path, response)
                    self.assertEqual(response['outcome'], 'OBSERVED', response)
                    value = response['data']['value']
                    if path == 'memory/search':
                        self.assertEqual(value['capabilities']['persona_origin'], 'UNAVAILABLE')
                        continue
                    self.assertEqual(value['capabilities']['persona_origin'], 'LOCAL_CONFIGURATION')
                    self.assertFalse(value['capabilities']['real_persona'])
                    persona = value['sections']['persona']
                    self.assertEqual(persona['origin'], 'LOCAL_CONFIGURATION')
                    self.assertEqual(persona['publication_origin'], 'LOCAL_DEFAULT')
                    self.assertEqual(persona['review_status'], 'USER_CONFIRMED')
                    self.assertEqual(persona['availability'], 'AVAILABLE')
                    self.assertTrue(persona['text'])
                    await asyncio.sleep(.05)
                self.assertFalse(business.sends_enabled)
                with sqlite3.connect((root / 'db/memory.sqlite3').as_uri() + '?mode=ro', uri=True) as database:
                    for table in ('provider_requests', 'provider_attempts'):
                        self.assertEqual(database.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
                print({'local_persona': 'LOCAL_CONFIGURATION', 'http_query_and_prepare': 'SCHEMA_VALID',
                    'provider_requests': 0, 'provider_attempts': 0})
            finally:
                if http is not None:
                    for _ in range(100):
                        if await http.close():
                            break
                        await asyncio.sleep(.05)
                    else:
                        self.fail('HTTP cleanup did not finish.')
                for _ in range(100):
                    if await business.close():
                        break
                    await asyncio.sleep(.05)
                else:
                    self.fail('Business cleanup did not finish.')
                self.assertTrue(await bootstrap.close())
