"""Actual stopped-source fencing, target activation and interrupted switch recovery."""
import asyncio
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence import Ready, Committed, Found
from companion_memory.persistence.managed_backup import ConsistentBackup
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_resources import ManagedResources
from companion_memory.runtime.managed_restore import RestoreSwitch


SWITCH_PROCESS = '''
import os, signal, sys
from pathlib import Path
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.runtime.managed_restore import RestoreSwitch
source, digest, target, switch_id, stop = sys.argv[1:]
port = RestoreSwitch(resolve_deployment({'deployment.data_root': source}), digest)
def checkpoint(point):
    if point == stop:
        os.kill(os.getpid(), signal.SIGKILL)
port.activate(Path(target), switch_id, checkpoint=checkpoint)
raise RuntimeError('Requested interruption boundary was not reached.')
'''


class RestoreSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_media_owner_rebinds_after_isolated_restore_and_confirms_original_upload(self):
        from companion_memory.runtime.managed_business import ManagedBusiness
        from companion_memory.memory.formats import record
        from .test_business import setup_draft, synthetic_resources
        with TemporaryDirectory() as directory:
            base = Path(directory)
            source, target = base / 'source', base / 'target'
            source.mkdir(mode=0o700)
            settings = resolve_deployment({'deployment.data_root': str(source)})
            bootstrap = ManagedBootstrap(settings)
            business = None
            try:
                self.assertIs(type(await bootstrap.open()), Ready)
                identity = bootstrap.assembly.identity
                assert identity is not None
                await identity.save_draft('draft', None, setup_draft(source))
                business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                await business.initialize('initialize', 1)
                host = business.host
                assert host is not None
                upload = host.media.bind_upload('entry')
                begun = await upload.begin_upload('original-image-upload', 'IMAGE')
                assert type(begun) is Committed
                uid = record(begun.receipt.result)['upload_id']
                await upload.append_upload(uid, 0, b'synthetic retained image resource')
                published = await upload.finish_upload(uid)
                self.assertIs(type(published), Committed, published)
                assert type(published) is Committed
                blob_id = cast(str, record(published.receipt.result)['blob_id'])
                blob = (await host.media.rows.read('blobs_get', {'blob_id': blob_id}))[0]
                original_inode = blob['inode']
                identity.close()
                self.assertTrue(await business.close())
                resources = bootstrap.resources
                assert resources is not None
                copier = ConsistentBackup(resources, lambda: bootstrap.assembly.storage.get_health().lifecycle == 'CLOSED')
                copier.create('business-restore')
            finally:
                if business is not None:
                    await business.close()
                await bootstrap.close()
            switch = RestoreSwitch(settings, bootstrap.assembly_digest)
            try:
                intent = switch.prepare('business-restore', target)
                switch.activate(target, cast(str, intent['switch_id']))
            finally:
                switch.close()
            os.rename(source, base / 'retired-source');os.rename(target, source)
            for reopen in range(3):
                if reopen == 2:
                    held = ManagedResources(settings, bootstrap.assembly_digest)
                    try:
                        ConsistentBackup(held, lambda: True).create('restored-again')
                    finally:
                        held.close()
                    switch = RestoreSwitch(settings, bootstrap.assembly_digest)
                    try:
                        next_intent = switch.prepare('restored-again', target)
                        switch.activate(target, cast(str, next_intent['switch_id']))
                    finally:
                        switch.close()
                    os.rename(source, base / 'retired-restored-source');os.rename(target, source)
                bootstrap = ManagedBootstrap(settings)
                business = None
                try:
                    self.assertIs(type(await bootstrap.open()), Ready)
                    identity = bootstrap.assembly.identity
                    assert identity is not None
                    business = ManagedBusiness(bootstrap, identity, resource_factory=synthetic_resources)
                    restored = await business.recover()
                    self.assertIs(type(restored), dict, (restored, business.host.phase if business.host else None))
                    self.assertEqual(restored['state'], 'AWAITING_REVIEW', restored)
                    host = business.host
                    assert host is not None
                    current = (await host.media.rows.read('blobs_get', {'blob_id': blob_id}))[0]
                    self.assertNotEqual(current['inode'], original_inode)
                    self.assertEqual(current['inode'], (source / 'blobs/published' / (blob_id + '.1')).stat().st_ino)
                    original = await host.media.bind_upload('entry').resolve_upload('original-image-upload', 'IMAGE')
                    assert type(original) is Found
                    self.assertEqual(original.value.commit_id, published.receipt.commit_id)
                    self.assertFalse(business.sends_enabled)
                finally:
                    if bootstrap.assembly.identity is not None:
                        bootstrap.assembly.identity.close()
                    if business is not None:
                        self.assertTrue(await business.close())
                    self.assertTrue(await bootstrap.close())

    async def test_interrupted_switch_never_leaves_two_startable_copies(self):
        for stop in ('before_source_fence', 'after_source_fence', 'after_target_identity', 'after_target_active'):
            with self.subTest(stop=stop), TemporaryDirectory() as directory:
                base = Path(directory)
                source, target = base / 'source', base / 'target'
                source.mkdir(mode=0o700)
                settings = resolve_deployment({'deployment.data_root': str(source)})
                bootstrap = ManagedBootstrap(settings)
                self.assertIs(type(await bootstrap.open()), Ready)
                resources = bootstrap.resources
                assert resources is not None and bootstrap.assembly.identity is not None and bootstrap.assembly.backup is not None
                database_id = resources.database_id
                with self.assertRaises(BlockingIOError):
                    RestoreSwitch(settings, bootstrap.assembly_digest)
                bootstrap.assembly.identity.close();bootstrap.assembly.backup.close()
                await bootstrap.assembly.storage.close()
                ConsistentBackup(resources, lambda: bootstrap.assembly.storage.get_health().lifecycle == 'CLOSED').create('restore-source')
                self.assertTrue(await bootstrap.close())
                switch = RestoreSwitch(settings, bootstrap.assembly_digest)
                try:
                    intent = switch.prepare('restore-source', target)
                finally:
                    switch.close()
                process = await asyncio.create_subprocess_exec(sys.executable, '-c', SWITCH_PROCESS,
                    str(source), bootstrap.assembly_digest, str(target), cast(str, intent['switch_id']), stop,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                output = await asyncio.wait_for(process.communicate(), 20)
                self.assertEqual(process.returncode, -signal.SIGKILL, output)
                if stop == 'before_source_fence':
                    original = ManagedResources(settings, bootstrap.assembly_digest)
                    original.close()
                else:
                    with self.assertRaises(ValueError):
                        ManagedResources(settings, bootstrap.assembly_digest)
                switch = RestoreSwitch(settings, bootstrap.assembly_digest)
                try:
                    active = switch.activate(target, cast(str, intent['switch_id']))
                    self.assertEqual(active['state'], 'ACTIVE')
                    self.assertEqual(switch.activate(target, cast(str, intent['switch_id'])), active)
                finally:
                    switch.close()
                # This models remounting the isolated volume at the same logical
                # runtime path. The original source bytes remain in an archive.
                os.rename(source, base / 'retired-source')
                os.rename(target, source)
                reopened = ManagedBootstrap(settings)
                try:
                    opened = await reopened.open()
                    self.assertIs(type(opened), Ready, opened)
                    assert reopened.resources is not None
                    self.assertEqual(reopened.resources.database_id, database_id)
                    self.assertEqual(reopened.resources.identity['authority_id'], active['target_authority'])
                finally:
                    self.assertTrue(await reopened.close())
