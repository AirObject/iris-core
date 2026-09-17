"""Actual private directories, file leases, SQLite copies and isolated restores."""
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from typing import cast, Any

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.runtime.managed_resources import ManagedResources
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.persistence import Ready
from companion_memory.persistence.managed_backup import ConsistentBackup


class ResourceTests(unittest.TestCase):
    def test_readonly_and_space_failures_preserve_reserve_without_large_allocations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            root.chmod(0o500)
            try:
                with self.assertRaisesRegex(ValueError, 'not writable'):
                    ManagedResources(settings, 'a' * 64)
                self.assertFalse((root / '.process.lock').exists())
            finally:
                root.chmod(0o700)
            resource = ManagedResources(settings, 'a' * 64)
            try:
                free = os.statvfs(root)
                available = free.f_bavail * free.f_frsize
                with self.assertRaisesRegex(ValueError, 'reserve'):
                    resource.check_space(available)
                self.assertGreaterEqual(available, 2147483648)
                resource.check_space(0)
            finally:
                resource.close()

    def test_exclusive_identity_is_retained_and_never_adopts_an_unbound_database(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            first = ManagedResources(settings, 'a' * 64)
            identity = first.database_id
            try:
                with self.assertRaises(BlockingIOError):
                    ManagedResources(settings, 'a' * 64)
                self.assertFalse((root / 'db/memory.sqlite3').exists())
            finally:
                first.close()
            reopened = ManagedResources(settings, 'a' * 64)
            self.assertEqual(reopened.database_id, identity)
            reopened.close()
            with self.assertRaises(ValueError):
                ManagedResources(settings, 'b' * 64)

    def test_group_access_and_directory_alias_reject_before_database(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = resolve_deployment({'deployment.data_root': str(root)})
            root.chmod(0o750)
            with self.assertRaises(ValueError):
                ManagedResources(settings, 'a' * 64)
            root.chmod(0o700)
            (root / 'db').symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                ManagedResources(settings, 'a' * 64)
            self.assertFalse((root / 'memory.sqlite3').exists())


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_waits_for_actual_upload_and_gc_owners_before_copy(self):
        import asyncio
        from unittest.mock import patch
        from companion_memory.management.managed_application import ManagedApplication
        from companion_memory.memory.formats import record
        from companion_memory.persistence import Committed
        from .test_business import setup_draft, synthetic_resources
        for operation in ('upload', 'gc'):
            with self.subTest(operation=operation), TemporaryDirectory() as directory:
                root = Path(directory)
                bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': directory,
                    'management.maintenance_timeout_seconds': 1}))
                self.assertIs(type(await bootstrap.open()), Ready)
                application = ManagedApplication(bootstrap)
                application.business.resource_factory = synthetic_resources
                release, entered = asyncio.Event(), asyncio.Event()
                pending = None
                try:
                    secret = application.resources.read_secret('bootstrap').decode()
                    await application.identity.establish('administrator', secret, 'synthetic-backup-race-password')
                    _, session = await application.identity.login('login', 'synthetic-backup-race-password')
                    assert session is not None
                    principal = await application.identity.authenticate(session['session'], host=False)
                    await application.identity.save_draft('draft', None, setup_draft(root))
                    await application.business.initialize('initialize', 1)
                    assert application.business.task is not None
                    initialized = await application.business.task
                    self.assertEqual(initialized['state'], 'AWAITING_REVIEW')
                    host = application.business.host
                    assert host is not None
                    media = host.media
                    port = media.bind_upload('entry')
                    result = await port.begin_upload('race-upload', 'IMAGE')
                    assert type(result) is Committed
                    uid = record(result.receipt.result)['upload_id']
                    await port.append_upload(uid, 0, b'original')
                    name = '_upload' if operation == 'upload' else '_collect_unreferenced'
                    original = getattr(media, name)
                    async def blocked(*args):
                        entered.set()
                        await release.wait()
                        return await original(*args)
                    with patch.object(media, name, side_effect=blocked):
                        pending = asyncio.create_task(port.append_upload(uid, 8, b' tail') if operation == 'upload'
                            else media.collect_unreferenced())
                        await asyncio.wait_for(entered.wait(), 5)
                        requested = await application.maintenance.create_backup('race-backup', principal)
                        assert type(requested) is dict, requested
                        self.assertEqual(requested['state'], 'UNCONFIRMED', requested)
                        self.assertTrue(requested['cleanup_pending'])
                        self.assertEqual(application.health()['state'], 'MAINTENANCE')
                        self.assertEqual(tuple((root / 'backups').iterdir()), ())
                        with self.assertRaises(BlockingIOError):
                            ManagedResources(bootstrap.settings, bootstrap.assembly_digest)
                        self.assertFalse(pending.done())
                        release.set()
                        await asyncio.wait_for(pending, 10)
                        assert application.maintenance.task is not None
                        completed = await asyncio.wait_for(application.maintenance.task, 20)
                    self.assertEqual(completed['state'], 'COMPLETE', completed)
                    self.assertFalse(application.business.sends_enabled)
                    self.assertEqual(application.health()['state'], 'AWAITING_REVIEW')
                finally:
                    release.set()
                    if pending is not None:
                        await pending
                    if application.maintenance.task is not None:
                        await application.maintenance.task
                    if application.business.task is not None:
                        await application.business.task
                    application.identity.close()
                    self.assertTrue(await application.business.close())
                    self.assertTrue(await application.bootstrap.close())

    async def test_backup_reopens_all_business_owners_without_new_initialization(self):
        from companion_memory.management.managed_application import ManagedApplication
        from companion_memory.persistence import Committed
        from .test_business import setup_draft, synthetic_resources
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': directory}))
            self.assertIs(type(await bootstrap.open()), Ready)
            application = ManagedApplication(bootstrap)
            application.business.resource_factory = synthetic_resources
            try:
                secret = application.resources.read_secret('bootstrap').decode()
                await application.identity.establish('administrator', secret, 'synthetic-backup-password')
                _, session = await application.identity.login('login', 'synthetic-backup-password')
                assert session is not None
                await application.identity.save_draft('draft', None, setup_draft(root))
                await application.business.initialize('initialize', 1)
                previous = application.business.host
                assert previous is not None and previous.stored is not None
                principal = await application.identity.authenticate(session['session'], host=False)
                result = cast(dict[str, Any], await application.dispatch(principal, 'POST', '/api/backups/create', {'key': 'full-backup'}))
                self.assertEqual(result['state'], 'COMPLETE', result)
                current = application.business.host
                assert current is not None and current.stored is not None
                self.assertIsNot(previous, current)
                self.assertEqual(current.stored.snapshot_id, previous.stored.snapshot_id)
                self.assertEqual(application.health()['state'], 'AWAITING_REVIEW')
                self.assertFalse(application.business.sends_enabled)
                # Original root initialization remains the original receipt;
                # reopening must not create another SELF or snapshot.
                self.assertEqual((await application.identity.read_draft())['revision'], 3)
                owner = application.bootstrap.assembly.backup
                assert owner is not None
                self.assertIs(type(await owner.request('full-backup', 'administrator')), Committed)
            finally:
                if application.maintenance.task is not None:
                    await application.maintenance.task
                application.identity.close()
                self.assertTrue(await application.business.close())
                self.assertTrue(await application.bootstrap.close())

    async def test_verified_archive_preserves_files_and_revocation_stops_later_chunks(self):
        import io
        import tarfile
        from companion_memory.management.managed_application import ManagedApplication
        from companion_memory.management.backup_download import BackupDownload
        from companion_memory.persistence.owned_statements import OwnerFailure
        with TemporaryDirectory() as directory:
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': directory}))
            self.assertIs(type(await bootstrap.open()), Ready)
            application = ManagedApplication(bootstrap)
            try:
                secret = application.resources.read_secret('bootstrap').decode()
                await application.identity.establish('administrator', secret, 'synthetic-download-password')
                _, session = await application.identity.login('login', 'synthetic-download-password')
                assert session is not None
                principal = await application.identity.authenticate(session['session'], host=False)
                payload = Path(directory) / 'blobs/synthetic-export'
                payload.write_bytes(b'synthetic-archive-payload' * 8192)
                payload.chmod(0o600)
                os.link(payload, Path(directory) / 'upload_staging/synthetic-link')
                result = cast(dict, await application.dispatch(principal, 'POST', '/api/backups/create', {'key': 'download-backup'}))
                self.assertEqual(result['state'], 'COMPLETE', result)
                downloaded = await application.dispatch(principal, 'GET', '/api/backups/download/' + result['backup_id'], {})
                assert type(downloaded) is BackupDownload
                chunks = []
                async def drain():
                    pass
                await downloaded.stream(chunks.append, drain)
                header, archive = b''.join(chunks).split(b'\r\n\r\n', 1)
                self.assertEqual(len(archive), downloaded.length)
                self.assertIn(b'application/x-tar', header)
                with tarfile.open(fileobj=io.BytesIO(archive)) as saved:
                    names = saved.getnames()
                    self.assertIn('complete.json', names)
                    self.assertFalse(any('secret' in name for name in names))
                    file = saved.extractfile('files/blobs/synthetic-export')
                    assert file is not None
                    self.assertEqual(file.read(), payload.read_bytes())
                    self.assertTrue(saved.getmember('files/upload_staging/synthetic-link').islnk())
                partial = []
                async def revoke_after_header():
                    if len(partial) == 1:
                        await application.identity.revoke('logout-download', principal.identity, principal.revision, host=False)
                with self.assertRaises(OwnerFailure):
                    await downloaded.stream(partial.append, revoke_after_header)
                self.assertEqual(len(partial), 1)
                with self.assertRaises(OwnerFailure):
                    await application.dispatch(principal, 'GET', '/api/backups/download/' + result['backup_id'], {})
            finally:
                if application.maintenance.task is not None:
                    await application.maintenance.task
                application.identity.close()
                self.assertTrue(await application.business.close())
                self.assertTrue(await application.bootstrap.close())

    async def test_managed_request_closes_reopens_and_keeps_original_login(self):
        from companion_memory.management.managed_application import ManagedApplication
        from companion_memory.persistence import Committed
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            self.assertIs(type(await bootstrap.open()), Ready)
            application = ManagedApplication(bootstrap)
            from companion_memory.runtime.managed_logging import ManagedLogging
            application.logging = ManagedLogging()
            application.logging.open(bootstrap)
            try:
                secret = application.resources.read_secret('bootstrap').decode()
                self.assertIs(type(await application.identity.establish('administrator', secret, 'synthetic-backup-password')), Committed)
                _, session = await application.identity.login('login', 'synthetic-backup-password')
                assert session is not None
                principal = await application.identity.authenticate(session['session'], host=False)
                original_id = application.resources.database_id
                lease = application.resources.fd
                result = cast(dict[str, Any], await application.dispatch(principal, 'POST', '/api/backups/create', {'key': 'backup'}))
                self.assertEqual(result['state'], 'COMPLETE', result)
                self.assertEqual(application.resources.fd, lease)
                self.assertEqual(application.resources.database_id, original_id)
                self.assertFalse(application.business.sends_enabled)
                self.assertEqual(application.health()['state'], 'BOOTSTRAP')
                principal = await application.identity.authenticate(session['session'], host=False)
                confirmed = cast(dict[str, Any], await application.dispatch(principal, 'POST', '/api/backups/create', {'key': 'backup'}))
                self.assertEqual(confirmed['state'], 'COMPLETE', confirmed)
                self.assertEqual(confirmed['backup_id'], result['backup_id'])
                listing = cast(dict[str, Any], await application.dispatch(principal, 'POST', '/api/backups/list', {'after': ''}))
                self.assertEqual(len(listing['items']), 1)
                self.assertEqual(listing['items'][0]['state'], 'COMPLETE')
                with self.assertRaises(BlockingIOError):
                    ManagedResources(bootstrap.settings, bootstrap.assembly_digest)
            finally:
                if application.maintenance.task is not None:
                    await application.maintenance.task
                application.identity.close()
                self.assertTrue(await application.business.close())
                self.assertTrue(await application.logging.close())
                self.assertTrue(await application.bootstrap.close())

    async def test_consistent_copy_verification_isolated_restore_and_corruption_rejection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            root.mkdir(mode=0o700)
            bootstrap = ManagedBootstrap(resolve_deployment({'deployment.data_root': str(root)}))
            self.assertIs(type(await bootstrap.open()), Ready)
            resources = bootstrap.resources
            identity = bootstrap.assembly.identity
            assert resources is not None and identity is not None
            # Synthetic file resources are explicitly test material, not owner
            # acceptance evidence for a production media object.
            for name in ('blobs/test-material', 'indexes/test-index', 'upload_staging/test-upload'):
                path = root / name
                path.write_bytes(b'synthetic-test-file\x00' * 256)
                path.chmod(0o600)
            os.link(root / 'blobs/test-material', root / 'upload_staging/shared-material')
            identity.close()
            assert bootstrap.assembly.backup is not None
            bootstrap.assembly.backup.close()
            await bootstrap.assembly.storage.close()
            backup = ConsistentBackup(resources, lambda: bootstrap.assembly.storage.get_health().lifecycle == 'CLOSED')
            try:
                completed = backup.create('backup-one')
                manifest = backup.verify(completed)
                self.assertFalse(manifest['secrets_included'])
                self.assertEqual(backup.create('backup-one'), completed)
                restored = Path(directory) / 'isolated'
                backup.restore_isolated(completed, restored)
                with sqlite3.connect(restored / 'db/memory.sqlite3') as connection:
                    self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                self.assertEqual(json.loads((restored / 'restore-intent.json').read_text())['state'], 'VERIFIED')
                self.assertEqual((restored / 'blobs/test-material').stat().st_ino,
                    (restored / 'upload_staging/shared-material').stat().st_ino)
                with self.assertRaises(ValueError):
                    ManagedResources(resolve_deployment({'deployment.data_root': str(restored)}), bootstrap.assembly_digest)
                (completed / 'files/blobs/test-material').write_bytes(b'corrupt')
                with self.assertRaises(ValueError):
                    backup.verify(completed)
                self.assertEqual((root / 'blobs/test-material').read_bytes(), b'synthetic-test-file\x00' * 256)
            finally:
                self.assertTrue(await bootstrap.close())


if __name__ == '__main__':
    unittest.main()
