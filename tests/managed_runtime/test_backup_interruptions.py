"""Kill the sole copy process at durable boundaries and recover original facts."""
import asyncio
import json
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
import unittest
from typing import cast

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence import Ready, Committed
from companion_memory.memory.formats import record
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.management.managed_application import ManagedApplication


COPY_PROCESS = '''
import os, signal, sys
from pathlib import Path
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.runtime.managed_resources import ManagedResources
from companion_memory.persistence.managed_backup import ConsistentBackup
root, digest, backup_id, stop = sys.argv[1:]
resources = ManagedResources(resolve_deployment({'deployment.data_root':root}), digest)
def checkpoint(point):
    if point == stop:
        os.kill(os.getpid(), signal.SIGKILL)
ConsistentBackup(resources, lambda: True).create(backup_id, checkpoint=checkpoint)
raise RuntimeError('Requested interruption boundary was not reached.')
'''


class BackupInterruptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_killed_copy_is_classified_without_recopying_new_source(self):
        for point in ('before_copy', 'file_copied', 'before_complete', 'after_complete'):
            with self.subTest(point=point), TemporaryDirectory() as directory:
                root = Path(directory)
                settings = resolve_deployment({'deployment.data_root': directory})
                bootstrap = ManagedBootstrap(settings)
                self.assertIs(type(await bootstrap.open()), Ready)
                owner = bootstrap.assembly.backup
                assert owner is not None
                request = await owner.request('original-backup', 'synthetic-operator')
                assert type(request) is Committed
                backup_id = cast(str, record(request.receipt.result)['backup_id'])
                self.assertTrue(await bootstrap.close())
                process = await asyncio.create_subprocess_exec(sys.executable, '-c', COPY_PROCESS,
                    directory, bootstrap.assembly_digest, backup_id, point,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
                self.assertEqual(process.returncode, -signal.SIGKILL, (stdout, stderr))
                # A new source file after the interruption must never be merged
                # into the old original-key backup during startup confirmation.
                marker = root / 'blobs/new-after-interruption'
                marker.write_bytes(b'synthetic post-interruption input')
                marker.chmod(0o600)
                reopened = ManagedBootstrap(settings)
                self.assertIs(type(await reopened.open()), Ready)
                application = ManagedApplication(reopened)
                try:
                    await application.maintenance.recover()
                    current_owner = reopened.assembly.backup
                    assert current_owner is not None
                    current = await current_owner.rows.read('backups', backup_id)
                    assert current is not None
                    self.assertEqual(current['state'], 'COMPLETE' if point == 'after_complete' else 'FAILED')
                    self.assertEqual(current['revision'], 2)
                    await application.maintenance.recover()
                    self.assertEqual(await current_owner.rows.read('backups', backup_id), current)
                    self.assertFalse(application.business.sends_enabled)
                    if point == 'after_complete':
                        manifest = json.loads((root / 'backups' / backup_id / 'manifest.json').read_text())
                        self.assertNotIn('blobs/new-after-interruption', [item['path'] for item in manifest['files']])
                    else:
                        self.assertFalse((root / 'backups' / backup_id).exists())
                finally:
                    application.identity.close()
                    self.assertTrue(await application.business.close())
                    self.assertTrue(await reopened.close())
