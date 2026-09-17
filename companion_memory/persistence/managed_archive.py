"""Stopped-volume import of a complete, bounded, uncompressed backup archive.

No archive path, permission, link target or extraction callback is trusted.
Files are created privately under one retained import identity; interrupted work
can resume only against the same archive digest. Publication follows native
SQLite, media, index and authority verification of the full resource set.
"""
from __future__ import annotations
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile

from .managed_backup import ConsistentBackup, file_digest, private_parents, sync_tree
from .schema import valid_identifier
from companion_memory.runtime.managed_resources import ManagedResources, atomic_record, read_record, sync_directory


def bounded_headers(path: Path, byte_limit: int, count_limit: int) -> None:
    """Bound extension headers before tarfile can allocate their declared bodies."""
    with path.open('rb') as stream:
        count = 0
        end = path.stat().st_size
        while header := stream.read(512):
            if len(header) != 512:
                raise ValueError('Truncated archive header.')
            if header == bytes(512):
                while chunk := stream.read(65536):
                    if any(chunk):
                        raise ValueError('Archive contains data after its end marker.')
                return
            count += 1
            if count > count_limit * 3:
                raise ValueError('Archive header count exceeds capacity.')
            raw = header[124:136].strip(b'\0 ')
            if not raw or any(c not in b'01234567' for c in raw):
                raise ValueError('Unsupported archive length encoding.')
            length = int(raw, 8)
            kind = header[156:157]
            limit = 16384 if kind in (b'x', b'g') else 4096 if kind in (b'L', b'K') else byte_limit
            if kind not in (b'\0', b'0', b'1', b'5', b'x', b'g', b'L', b'K') or length > limit:
                raise ValueError('Archive header type or length is unsupported.')
            next_position = stream.tell() + length + (-length % 512)
            if next_position > end:
                raise ValueError('Archive body exceeds its file.')
            stream.seek(next_position)
        raise ValueError('Archive has no complete end marker.')


class ManagedBackupArchive:
    def __init__(self, resources: ManagedResources):
        if resources.fd < 0:
            raise ValueError('The stopped volume lease is required.')
        self.resources = resources

    def import_archive(self, archive: Path, operation_key: str) -> dict[str, object]:
        r = self.resources
        if not valid_identifier(operation_key):
            raise ValueError('Invalid original import identity.')
        if archive.resolve(strict=True) != archive or not archive.is_file():
            raise ValueError('An absolute regular archive path without aliases is required.')
        byte_limit = r.settings.integer('management.backup_max_bytes')
        count_limit = r.settings.integer('management.backup_file_limit') * 2 + 4
        size = archive.stat().st_size
        if size > byte_limit + count_limit * 4096 + 8388608:
            raise ValueError('Archive exceeds its bounded carrier.')
        bounded_headers(archive, byte_limit, count_limit)
        digest = file_digest(archive)[1]
        imported = 'import-' + sha256(operation_key.encode()).hexdigest()
        staging = r.root / 'backup_staging' / imported
        intent: dict[str, object] = {'format': 'MANAGED_ARCHIVE_IMPORT_V1', 'archive_sha256': digest, 'operation_key': operation_key}
        if staging.exists():
            if read_record(staging / 'import.json') != intent:
                raise ValueError('Original import content differs.')
        else:
            r.check_space(size + 1048576)
            staging.mkdir(mode=0o700)
            atomic_record(staging / 'import.json', intent)
        published = staging / 'published.json'
        if published.exists():
            prior = read_record(published)
            if (prior.get('archive_sha256') != digest or type(prior.get('backup_id')) is not str
                    or not prior['backup_id'] or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in str(prior['backup_id']))):
                raise ValueError('Published import identity differs.')
            destination = r.root / 'backups' / str(prior['backup_id'])
            ConsistentBackup(r, lambda: True).verify(destination)
            return {'state': 'COMPLETE', **prior, 'startup_sends': 0}
        content = staging / 'content'
        private_parents(content, staging)
        seen: set[str] = set()
        total = 0
        with tarfile.open(archive, mode='r:') as source:
            for member in source:
                name = member.name.rstrip('/') if member.isdir() else member.name
                relative = PurePosixPath(name)
                if (len(seen) >= count_limit or len(name.encode()) > 4096 or name in seen
                        or relative.is_absolute() or '..' in relative.parts or str(relative) != name
                        or any(part.startswith('.import-') for part in relative.parts)
                        or name not in ('manifest.json', 'complete.json', 'intent.json', 'files') and not name.startswith('files/')
                        or member.sparse is not None or not (member.isdir() or member.isreg() or member.islnk())
                        or member.size < 0 or member.size > byte_limit):
                    raise ValueError('Archive member type, path or capacity is not supported.')
                total += member.size
                if total > byte_limit + 8388608:
                    raise ValueError('Archive contents exceed their declared limit.')
                target = content / relative
                private_parents(target.parent, content)
                if member.isdir():
                    private_parents(target, content)
                elif member.islnk():
                    link = PurePosixPath(member.linkname)
                    if (member.size != 0 or member.linkname not in seen or not member.linkname.startswith('files/')
                            or str(link) != member.linkname or link.is_absolute() or '..' in link.parts):
                        raise ValueError('Archive hardlink target is not an earlier owned file.')
                    original = content / link
                    if not stat.S_ISREG(original.lstat().st_mode):
                        raise ValueError('Archive hardlink target is not a regular file.')
                    if target.exists():
                        if target.stat().st_ino != original.stat().st_ino or target.stat().st_dev != original.stat().st_dev:
                            raise ValueError('Retained import hardlink differs.')
                    else:
                        os.link(original, target, follow_symlinks=False)
                else:
                    # One deterministic partial file belongs to this original
                    # import. Its incomplete bytes are never exposed as final.
                    partial = target.parent / ('.import-' + sha256(name.encode()).hexdigest())
                    incoming = source.extractfile(member)
                    if incoming is None:
                        raise ValueError('Archive file body is missing.')
                    r.check_space(member.size)
                    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    with incoming, os.fdopen(descriptor, 'wb') as outgoing:
                        info = os.fstat(outgoing.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                            raise ValueError('Retained partial import is not owned.')
                        os.ftruncate(outgoing.fileno(), 0)
                        left = member.size
                        while left:
                            chunk = incoming.read(min(1048576, left))
                            if not chunk:
                                raise ValueError('Archive body ended early.')
                            outgoing.write(chunk)
                            left -= len(chunk)
                        outgoing.flush(); os.fsync(outgoing.fileno())
                    if target.exists():
                        if file_digest(target) != file_digest(partial):
                            raise ValueError('Retained import file differs.')
                        partial.unlink()
                    else:
                        os.rename(partial, target)
                    sync_directory(target.parent)
                seen.add(name)
        if file_digest(archive) != (size, digest):
            raise ValueError('Archive changed during import.')
        manifest = ConsistentBackup(r, lambda: True).verify(content)
        backup_id = manifest['backup_id']
        if type(backup_id) is not str or not backup_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in backup_id):
            raise ValueError('Backup identity cannot be published.')
        destination = r.root / 'backups' / backup_id
        sync_tree(content)
        if destination.exists():
            ConsistentBackup(r, lambda: True).verify(destination)
            if file_digest(destination / 'manifest.json') != file_digest(content / 'manifest.json'):
                raise ValueError('A different original backup already exists.')
        else:
            os.rename(content, destination)
            sync_directory(destination.parent)
            sync_directory(staging)
        atomic_record(staging / 'published.json', {'backup_id': backup_id, 'archive_sha256': digest})
        return {'state': 'COMPLETE', 'backup_id': backup_id, 'archive_sha256': digest, 'startup_sends': 0}
