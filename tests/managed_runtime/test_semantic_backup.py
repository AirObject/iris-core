"""New managed semantic authority, paid native artifacts and actual index backup.

Only the explicit synthetic adapter sees model requests. Reopening and original
confirmation do not reactivate dispatch, and corrupted index files reject.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
from typing import cast
import unittest

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.information.management import HostIdentity
from companion_memory.memory.formats import record
from companion_memory.persistence import Ready, Committed, Receipt, Found
from companion_memory.persistence.managed_backup import ConsistentBackup
from companion_memory.persistence.semantic_records import identity
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_business import ManagedBusiness
from companion_memory.runtime.managed_restore import RestoreSwitch
from companion_memory.runtime.managed_semantic_authorization import ManagedSemanticAuthorization
from tests.daily_cognition.test_initial_persona_host import persona
from tests.daily_cognition.test_reasoning import responses
from tests.runtime.configuration_support import event
from .memory_support import formal_response
from .semantic_support import semantic_resources, publish_fixture_persona
from .test_business import setup_draft

EMBEDDING = json.dumps({'id': 'synthetic-embedding-response', 'created': 1,
    'model': 'doubao-embedding-vision', 'object': 'list',
    'data': [{'index': 0, 'object': 'embedding', 'embedding': [1.0] + [0.0] * 1023}],
    'usage': {'prompt_tokens': 10, 'total_tokens': 10}}, separators=(',', ':')).encode()


class ManagedSemanticBackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_embedding_published_index_backup_and_zero_send_reopen(self):
        with TemporaryDirectory() as directory, responses((persona, formal_response, EMBEDDING)) as (port, requests, failures):
            base = Path(directory)
            root = base / 'source'
            root.mkdir(mode=0o700)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            bootstrap = ManagedBootstrap(settings)
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                owner = bootstrap.assembly.identity
                assert owner is not None
                await owner.save_draft('draft', None, setup_draft(root))
                business = ManagedBusiness(bootstrap, owner, resource_factory=semantic_resources(port))
                await business.initialize('initialize', 1)
                await publish_fixture_persona(self, business)
                host = business.host
                assert host is not None and host.semantic is not None and host.queries is not None and host.queries.semantic is not None
                self.assertIs(type(host.authorization), ManagedSemanticAuthorization)
                semantic = host.semantic
                assert host.network is not None
                await host.network.wait_quiet(time.monotonic() + 60)
                entry = host.bind_entry('entry')
                for index in range(3):
                    value = event('memory-input-' + str(index), '合成测试的杯子在桌面。')
                    value['event_version'] = 2
                    self.assertIs(type(await entry.accept_event('input-' + str(index), value)), Committed)
                self.assertIs(type(await host.resume_learning('learn-resume')), Committed)
                learned = await entry.run_learning('learn')
                self.assertIn(type(learned), (Committed, Found), learned)
                memory = host.assembly.memory.information
                assert memory is not None
                objects = await memory.current_page('', 1)
                self.assertEqual(len(objects), 1, (learned, len(requests), host.dispatch.last_failure if host.dispatch else None))
                oid = cast(str, objects[0]['object_id'])
                query = await host.bind_query(HostIdentity('native-query', 'principal', 'host', 'entry',
                    frozenset(('search_memory',)), (), time.monotonic() + 300))
                partition = host.queries.semantic.partition(query)
                self.assertIs(type(await semantic.resume('semantic-resume')), Receipt)
                assert host.network is not None
                await host.network.wait_quiet(time.monotonic() + 60)
                wid = await semantic.prepare_document(oid, 'document', partition)
                assert type(wid) is str, wid
                result = await semantic.run_work(wid)
                self.assertIs(type(result), MappingProxyType, result)
                assert type(result) is MappingProxyType
                self.assertEqual(result['state'], 'APPLIED', result)
                self.assertFalse(result['cleanup_pending'])
                publication = await semantic.owner.memory.rows.read('semantic_publication', semantic.owner.memory.root_id)
                assert publication is not None
                gid = identity('semantic-generation', 'managed', 1)
                published = await semantic.publish(gid, cast(int, publication['material_seq']))
                assert type(published) is MappingProxyType, published
                self.assertEqual(published['state'], 'PUBLISHED')
                self.assertEqual(len(requests), 3)
                # A paused consent cannot be reopened by confirming the original
                # successful resume receipt. Paid original work stays readable.
                await business.set_dispatch('pause', 1, False, cast(str, business.dispatch_disclosure()['digest']))
                assert host.authorization is not None
                self.assertFalse(host.authorization.activated)
                self.assertIs(type(await semantic.resume('semantic-resume')), Receipt)
                self.assertFalse(host.authorization.activated)
                repeated = await semantic.run_work(wid)
                assert type(repeated) is MappingProxyType
                self.assertEqual(repeated['state'], 'APPLIED')
                owner.close()
                self.assertTrue(await business.close())
                resources = bootstrap.resources
                assert resources is not None
                copier = ConsistentBackup(resources, lambda: bootstrap.assembly.storage.get_health().lifecycle == 'CLOSED')
                destination = copier.create('semantic-backup')
                manifest = copier.verify(destination)
                self.assertTrue(any(item['path'].startswith('indexes/') for item in cast(list[dict], manifest['files'])))
                # Native reference verification covers the real binary header,
                # members, artifact digest and committed publication watermark.
                index = next(path for path in (destination / 'files/indexes').iterdir() if path.is_file())
                original = index.read_bytes()
                index.write_bytes(original[:-1] + bytes((original[-1] ^ 1,)))
                with self.assertRaises(ValueError):
                    copier.verify(destination)
                index.write_bytes(original)
                copier.verify(destination)
            finally:
                if business is not None:
                    await business.close()
                await bootstrap.close()
            switch = RestoreSwitch(settings, bootstrap.assembly_digest)
            try:
                intent = switch.prepare('semantic-backup', base / 'target')
                switch.activate(base / 'target', cast(str, intent['switch_id']))
            finally:
                switch.close()
            os.rename(root, base / 'retired-source')
            os.rename(base / 'target', root)
            bootstrap = ManagedBootstrap(settings)
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                owner = bootstrap.assembly.identity
                assert owner is not None
                business = ManagedBusiness(bootstrap, owner, resource_factory=semantic_resources(port))
                result = await business.recover()
                self.assertEqual(result['state'], 'READY', result)
                host = business.host
                assert host is not None and host.semantic is not None and host.authorization is not None
                self.assertFalse(host.authorization.activated)
                self.assertEqual((await host.semantic.work(wid))['state'], 'APPLIED')
                self.assertEqual((await host.semantic.control())['current_generation'], gid)
                assert host.combination.generations is not None
                sealed = host.combination.generations.files.verify(host.semantic.owner.space, gid, host.checkpoint)
                self.assertEqual(sealed.header.member_count, 1)
                self.assertIs(type(await host.semantic.resume('semantic-resume')), Receipt)
                self.assertFalse(host.authorization.activated)
                self.assertFalse(business.sends_enabled)
                self.assertEqual(len(requests), 3)
                self.assertFalse(failures)
            finally:
                if bootstrap.assembly.identity is not None:
                    bootstrap.assembly.identity.close()
                if business is not None:
                    self.assertTrue(await business.close())
                self.assertTrue(await bootstrap.close())
