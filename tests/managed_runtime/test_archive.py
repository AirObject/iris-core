"""Actual SQLite archive import, hardlinks, original identity and hostile paths."""
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import tarfile
import unittest

from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.persistence import Ready
from companion_memory.persistence.managed_archive import ManagedBackupArchive, bounded_headers
from companion_memory.persistence.managed_backup import ConsistentBackup, file_digest
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_resources import ManagedResources


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_archive_import_preserves_aliases_and_rejects_escaping_members(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'volume'
            root.mkdir(mode=0o700)
            settings = resolve_deployment({'deployment.data_root':str(root)})
            bootstrap = ManagedBootstrap(settings)
            self.assertIs(type(await bootstrap.open()), Ready)
            self.assertTrue(await bootstrap.close())
            resources = ManagedResources(settings, bootstrap.assembly_digest)
            try:
                sample = root / 'logs/runtime/synthetic.txt'
                sample.write_bytes(b'synthetic archive content')
                sample.chmod(0o600)
                os.link(sample, root / 'logs/runtime/second.txt')
                copier = ConsistentBackup(resources, lambda:True)
                backup = copier.create('archive-test')
                original = copier.verify(backup)
                archive = Path(directory) / 'complete.tar'
                with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as output:
                    for name in ('manifest.json','complete.json','intent.json','files'):
                        output.add(backup / name, arcname=name)
                # Preserve every old byte outside the target backup namespace.
                backup.rename(Path(directory) / 'original-backup')
                importer = ManagedBackupArchive(resources)
                completed = importer.import_archive(archive, 'original-import')
                self.assertEqual(completed['state'],'COMPLETE')
                self.assertEqual(importer.import_archive(archive, 'original-import'), completed)
                self.assertEqual(copier.verify(backup), original)
                self.assertEqual(file_digest(backup / 'files/logs/runtime/synthetic.txt'), (25, file_digest(sample)[1]))
                self.assertEqual((backup / 'files/logs/runtime/synthetic.txt').stat().st_ino,
                                 (backup / 'files/logs/runtime/second.txt').stat().st_ino)
                for kind in ('escape','symlink','early-hardlink'):
                    bad = Path(directory) / (kind + '.tar')
                    with tarfile.open(bad, 'w') as output:
                        entry = tarfile.TarInfo('../outside' if kind=='escape' else 'files/item')
                        if kind=='symlink':entry.type=tarfile.SYMTYPE;entry.linkname='/etc/passwd'
                        elif kind=='early-hardlink':entry.type=tarfile.LNKTYPE;entry.linkname='files/not-present'
                        output.addfile(entry, io.BytesIO())
                    with self.assertRaises(ValueError):
                        importer.import_archive(bad, kind)
                oversized = Path(directory) / 'oversized-extension.tar'
                entry = tarfile.TarInfo('pax')
                entry.type = tarfile.XHDTYPE
                entry.size = 1048576
                oversized.write_bytes(entry.tobuf()+bytes(1024))
                with self.assertRaises(ValueError):
                    bounded_headers(oversized, 2147483648, 100)
                self.assertFalse((Path(directory)/'outside').exists())
            finally:
                resources.close()
