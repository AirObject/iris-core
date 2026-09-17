"""Bounded consistent backups and isolated verification on a quiescent local volume.

The caller must hold the process resource lease and close every business owner
before entering. SQLite's backup API captures the database; referenced files and
original recovery journals are included. A complete marker is published only
after every file has been verified. Restore never overwrites the source volume.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from typing import cast
from uuid import uuid4

from companion_memory.runtime.managed_resources import ManagedResources, atomic_record, read_record, sync_directory

FORMAT = 'MANAGED_CONSISTENT_BACKUP_V1'


def private_parents(path: Path, root: Path) -> None:
    """Create every intermediate directory privately, without following aliases."""
    for relative in reversed((path.relative_to(root), *path.relative_to(root).parents)):
        directory = root / relative
        if directory == root:
            continue
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('Backup directory is not private.')


def sync_tree(root: Path) -> None:
    for parent, _, _ in os.walk(root, topdown=False, followlinks=False):
        sync_directory(Path(parent))


def file_digest(path: Path) -> tuple[int, str]:
    """Hash a regular single file without unbounded allocation or symlink traversal."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('A regular backup file is required.')
        count, hasher = 0, hashlib.sha256()
        while chunk := stream.read(1048576):
            hasher.update(chunk)
            count += len(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or count != before.st_size:
            raise ValueError('Backup source changed while reading.')
    return count, hasher.hexdigest()


class ConsistentBackup:
    """File owner used only inside the application's exclusive maintenance boundary."""
    def __init__(self, resources: ManagedResources, quiescent: Callable[[], bool]):
        self.resources = resources
        self.quiescent = quiescent

    def create(self, backup_id: str, *, checkpoint: Callable[[str], None] = lambda point: None) -> Path:
        """Copy one frozen resource set; unfinished directories are never selectable."""
        from .schema import valid_identifier
        if not valid_identifier(backup_id) or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in backup_id):
            raise ValueError('Invalid backup identity.')
        r = self.resources
        if r.fd < 0 or self.quiescent() is not True:
            raise ValueError('The volume is not quiescent.')
        destination = r.root / 'backups' / backup_id
        if destination.exists():
            self.verify(destination)
            return destination
        staging = r.root / 'backup_staging' / backup_id
        if staging.exists():
            raise ValueError('An unfinished backup retains its original identity.')
        files: list[Path] = []
        directories_manifest: list[dict[str, object]] = []
        aliases: dict[tuple[int, int], list[Path]] = {}
        total = 0
        for parent, directories, names in os.walk(r.root, followlinks=False):
            directory = Path(parent)
            meta = directory.lstat()
            if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
                raise ValueError('Backup source directory is not private.')
            directories_manifest.append({'path': directory.relative_to(r.root).as_posix(), 'device': meta.st_dev, 'inode': meta.st_ino})
            directories[:] = sorted(name for name in directories if directory / name not in (r.root / 'backups', r.root / 'backup_staging'))
            for name in directories:
                if (directory / name).is_symlink():raise ValueError('Backup directory alias rejected.')
            for name in sorted(names):
                path = directory / name
                if path == r.root / '.process.lock' or path in (r.root / 'db/memory.sqlite3-wal', r.root / 'db/memory.sqlite3-shm'):
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError('Backup source permissions or file kind differ.')
                total += info.st_size
                files.append(path)
                aliases.setdefault((info.st_dev, info.st_ino), []).append(path)
        if any(len(group) != group[0].stat().st_nlink for group in aliases.values()):
            raise ValueError('Backup resource has an undeclared external hardlink.')
        if len(files) > r.settings.integer('management.backup_file_limit') or total > r.settings.integer('management.backup_max_bytes'):
            raise ValueError('Backup exceeds its configured capacity.')
        r.check_space(total + 1048576)
        staging.mkdir(mode=0o700)
        atomic_record(staging / 'intent.json', {'format': FORMAT, 'backup_id': backup_id,
            'database_id': r.database_id, 'source_authority': r.identity['authority_id'], 'state': 'COPYING'})
        checkpoint('before_copy')
        listing: list[dict[str, object]] = []
        copied: dict[tuple[int, int], Path] = {}
        for source in files:
            if self.quiescent() is not True:
                raise ValueError('Maintenance authority was withdrawn.')
            relative = source.relative_to(r.root)
            target = staging / 'files' / relative
            private_parents(target.parent, staging)
            source_meta = source.lstat()
            physical = (source_meta.st_dev, source_meta.st_ino)
            if relative == Path('db/memory.sqlite3'):
                with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as connection:
                    with closing(sqlite3.connect(target)) as output:
                        connection.backup(output, pages=128)
                        if output.execute('PRAGMA journal_mode=WAL').fetchone() != ('wal',):
                            raise ValueError('Backup WAL format is unavailable.')
                        output.execute('PRAGMA synchronous=FULL')
                        if output.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                            raise ValueError('Backup WAL did not reach a consistent checkpoint.')
                        if output.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                            raise ValueError('Backup database integrity failed.')
                os.chmod(target, 0o600)
                fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            elif physical in copied:
                os.link(copied[physical], target, follow_symlinks=False)
            else:
                original = file_digest(source)
                with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as incoming, target.open('xb') as outgoing:
                    os.chmod(target, 0o600)
                    shutil.copyfileobj(incoming, outgoing, 1048576)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if file_digest(target) != original or file_digest(source) != original:
                    raise ValueError('Backup file changed during copying.')
            copied[physical] = target
            size, digest = file_digest(target)
            listing.append({'path': relative.as_posix(), 'bytes': size, 'sha256': digest,
                'device': source_meta.st_dev, 'inode': source_meta.st_ino})
            checkpoint('file_copied')
        manifest = {'format': FORMAT, 'backup_id': backup_id, 'database_id': r.database_id,
            'instance_id': r.instance_id, 'source_authority': r.identity['authority_id'],
            'assembly_digest': r.identity['assembly_digest'], 'files': listing, 'directories': directories_manifest,
            'secrets_included': False, 'startup_sends': 0}
        encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
        if len(encoded) > 4194304:
            raise ValueError('Backup manifest exceeds its carrier.')
        with (staging / 'manifest.json').open('xb') as stream:
            os.chmod(staging / 'manifest.json', 0o600)
            stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        checkpoint('before_complete')
        sync_tree(staging)
        atomic_record(staging / 'complete.json', {'format': FORMAT, 'manifest_sha256': hashlib.sha256(encoded).hexdigest()})
        self.verify(staging)
        sync_directory(staging)
        os.rename(staging, destination)
        sync_directory(destination.parent)
        sync_directory(staging.parent)
        checkpoint('after_complete')
        return destination

    def verify(self, backup: Path) -> dict[str, object]:
        """Verify the whole manifest, closed path set, digests and SQLite identity."""
        if backup.is_symlink() or backup.resolve(strict=True) != backup:
            raise ValueError('Backup alias rejected.')
        complete = read_record(backup / 'complete.json')
        if set(complete) != {'format', 'manifest_sha256'} or complete['format'] != FORMAT:
            raise ValueError('Incomplete or unsupported backup.')
        path = backup / 'manifest.json'
        size, digest = file_digest(path)
        if size > 4194304 or digest != complete['manifest_sha256']:
            raise ValueError('Backup manifest integrity failed.')
        manifest = json.loads(path.read_bytes())
        if (type(manifest) is not dict or set(manifest) != {'format', 'backup_id', 'database_id', 'instance_id',
                'source_authority', 'assembly_digest', 'files', 'directories', 'secrets_included', 'startup_sends'}
                or manifest['format'] != FORMAT or manifest['assembly_digest'] != self.resources.identity['assembly_digest']
                or manifest['database_id'] != self.resources.database_id or manifest['instance_id'] != self.resources.instance_id
                or manifest['source_authority'] != self.resources.identity['authority_id']
                or manifest['secrets_included'] is not False or manifest['startup_sends'] != 0):
            raise ValueError('Backup compatibility differs.')
        files = manifest['files']
        if type(files) is not list or not 1 <= len(files) <= self.resources.settings.integer('management.backup_file_limit'):
            raise ValueError('Backup file count exceeds its bound.')
        seen: set[str] = set()
        groups: dict[tuple[int, int], list[Path]] = {}
        total = 0
        for item in files:
            if type(item) is not dict or set(item) != {'path', 'bytes', 'sha256', 'device', 'inode'} or type(item['path']) is not str:
                raise ValueError('Invalid backup file declaration.')
            name = item['path']; relative = PurePosixPath(name)
            if (relative.is_absolute() or '..' in relative.parts or str(relative) != name or name in seen
                    or not relative.parts or relative.parts[0] in ('backups', 'backup_staging')
                    or name == '.process.lock' or type(item['bytes']) is not int or item['bytes'] < 0
                    or any(type(item[key]) is not int or item[key] < 0 for key in ('device', 'inode'))):
                raise ValueError('Backup path escaped its declared resource set.')
            source = backup / 'files' / name
            if any(p.is_symlink() for p in (source, *source.parents)):
                raise ValueError('Backup file alias rejected.')
            if file_digest(source) != (item['bytes'], item['sha256']):
                raise ValueError('Backup file integrity failed.')
            groups.setdefault((item['device'], item['inode']), []).append(source)
            seen.add(name);total += item['bytes']
        for group in groups.values():
            identities = {(path.stat().st_dev, path.stat().st_ino, path.stat().st_nlink) for path in group}
            if len(identities) != 1 or next(iter(identities))[2] != len(group):
                raise ValueError('Backup physical alias group differs.')
        directories = manifest['directories']
        if type(directories) is not list or len(directories) > self.resources.settings.integer('management.backup_file_limit'):
            raise ValueError('Invalid directory manifest.')
        directory_names: set[str] = set()
        for entry in directories:
            if type(entry) is not dict or set(entry) != {'path', 'device', 'inode'} or type(entry['path']) is not str:
                raise ValueError('Invalid directory declaration.')
            relative = PurePosixPath(entry['path'])
            if (relative.is_absolute() or '..' in relative.parts or str(relative) != entry['path'] or entry['path'] in directory_names
                    or any(type(entry[key]) is not int or entry[key] < 0 for key in ('device', 'inode'))):
                raise ValueError('Invalid directory identity.')
            directory_names.add(entry['path'])
        if '.' not in directory_names or any(str(PurePosixPath(name).parent) not in directory_names for name in seen):
            raise ValueError('Missing source directory identity.')
        if total > self.resources.settings.integer('management.backup_max_bytes'):
            raise ValueError('Backup bytes exceed the declared bound.')
        actual = {p.relative_to(backup / 'files').as_posix() for p in (backup / 'files').rglob('*') if not p.is_dir()}
        if actual != seen or not {'db/memory.sqlite3', 'bootstrap/identity.json'} <= seen:
            raise ValueError('Backup references are incomplete.')
        with closing(sqlite3.connect((backup / 'files/db/memory.sqlite3').as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',) or connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('Backup database integrity failed.')
            if connection.execute('SELECT database_id FROM application_metadata WHERE singleton=1').fetchone() != (manifest['database_id'],):
                raise ValueError('Backup database identity differs.')
            from companion_memory.media.backup_verification import verify_media_backup
            from companion_memory.retrieval.backup_verification import verify_index_backup
            verify_media_backup(connection, manifest)
            verify_index_backup(connection, manifest, backup / 'files')
        return cast(dict[str, object], manifest)

    def restore_isolated(self, backup: Path, target: Path) -> dict[str, object]:
        """Restore into a new resource directory; copied authority remains disabled."""
        manifest = self.verify(backup)
        if target.exists() or not target.is_absolute() or target.parent.resolve(strict=True) != target.parent:
            raise ValueError('Restore requires a new isolated target.')
        info = os.statvfs(target.parent)
        files = cast(list[dict[str, object]], manifest['files'])
        total = sum(cast(int, item['bytes']) for item in files)
        if info.f_bavail * info.f_frsize < total + self.resources.settings.integer('deployment.free_reserve_bytes'):
            raise ValueError('Restore would consume the protected reserve.')
        target.mkdir(mode=0o700)
        preserved: dict[str, str] = {}
        if any(item['path'] == 'restore-intent.json' for item in files):
            preserved['restore-intent.json'] = 'bootstrap/restore-history/' + uuid4().hex + '/restore-intent.json'
        atomic_record(target / 'restore-intent.json', {'format': FORMAT, 'backup_id': manifest['backup_id'],
            'source_authority': manifest['source_authority'], 'state': 'VERIFYING', 'startup_sends': 0, 'preserved_files': preserved})
        for directory in cast(list[dict[str, object]], manifest['directories']):
            private_parents(target / cast(str, directory['path']), target)
        copied: dict[tuple[object, object], Path] = {}
        for item in files:
            name = cast(str, item['path']); destination = target / preserved.get(name, name)
            private_parents(destination.parent, target)
            physical = (item['device'], item['inode'])
            if physical in copied:
                os.link(copied[physical], destination, follow_symlinks=False)
            else:
                with os.fdopen(os.open(backup / 'files' / name, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as incoming, destination.open('xb') as outgoing:
                    os.chmod(destination, 0o600);shutil.copyfileobj(incoming, outgoing, 1048576)
                    outgoing.flush();os.fsync(outgoing.fileno())
                copied[physical] = destination
            if file_digest(destination) != (item['bytes'], item['sha256']):
                raise ValueError('Isolated restore verification failed.')
        # Original physical binding intentionally remains mismatched until a
        # stopped source transfers authority through the controlled switch.
        sync_tree(target)
        atomic_record(target / 'restore-intent.json', {'format': FORMAT, 'backup_id': manifest['backup_id'],
            'source_authority': manifest['source_authority'], 'state': 'VERIFIED', 'startup_sends': 0, 'preserved_files': preserved})
        sync_directory(target)
        return manifest
