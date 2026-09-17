"""Authenticated, bounded archive streaming of a verified complete backup.

The archive preserves hardlink groups and includes the completion manifest. No
secret directory or temporary archive is created. Each response chunk rechecks
current administrator authority; revocation ends the stream before later bytes.
"""
from __future__ import annotations
import asyncio
from collections.abc import Callable, Generator, Awaitable
import hashlib
import os
from pathlib import Path
import stat
import tarfile
from typing import cast

from companion_memory.persistence.managed_backup import ConsistentBackup, file_digest
from companion_memory.persistence.owned_statements import OwnerFailure
from .identity import Principal


class BackupDownload:
    def __init__(self, application, principal: Principal, backup: Path, manifest: dict[str, object]):
        self.application, self.principal = application, principal
        self.backup = backup
        self.filename = str(manifest['backup_id']) + '.tar'
        self.parts: list[tuple[bytes, Path | None, int, str | None]] = []
        self.length = 1024
        directories = ['files'] + ['files/' + str(row['path']) for row in cast(list[dict], manifest['directories']) if row['path'] != '.']
        for name in sorted(directories):
            self._add(name, None, 0, None, directory=True)
        for name in ('manifest.json', 'complete.json', 'intent.json'):
            size, digest = file_digest(backup / name)
            self._add(name, backup / name, size, digest)
        physical: dict[tuple[int, int], str] = {}
        for row in cast(list[dict], manifest['files']):
            name = 'files/' + str(row['path'])
            group = (int(row['device']), int(row['inode']))
            link = physical.get(group)
            self._add(name, None if link else backup / name, 0 if link else int(row['bytes']),
                None if link else str(row['sha256']), link=link)
            physical[group] = name

    def _add(self, name: str, path: Path | None, size: int, digest: str | None, *, directory: bool = False, link: str | None = None):
        header = tarfile.TarInfo(name)
        header.mode = 0o700 if directory else 0o600
        header.size = size
        header.type = tarfile.DIRTYPE if directory else tarfile.LNKTYPE if link else tarfile.REGTYPE
        if link:
            header.linkname = link
        encoded = header.tobuf(format=tarfile.PAX_FORMAT)
        self.parts.append((encoded, path, size, digest))
        self.length += len(encoded) + size + (-size % 512)

    def chunks(self) -> Generator[bytes, None, None]:
        for header, path, size, digest in self.parts:
            yield header
            if path is None:
                continue
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                    raise ValueError('Verified backup changed before download.')
                count = 0
                hasher = hashlib.sha256()
                while chunk := stream.read(65536):
                    count += len(chunk)
                    if count > size:
                        raise ValueError('Verified backup grew during download.')
                    hasher.update(chunk)
                    yield chunk
                if count != size or hasher.hexdigest() != digest:
                    raise ValueError('Verified backup changed during download.')
            if size % 512:
                yield bytes(-size % 512)
        yield bytes(1024)

    async def stream(self, write: Callable[[bytes], None], drain: Callable[[], Awaitable[None]]) -> None:
        async def send(chunk: bytes):
            authority = self.application.identity
            async with authority.delivery_lock:
                await authority.recheck(self.principal)
                write(chunk)
            await drain()
        await send((f'HTTP/1.1 200 OK\r\nContent-Type: application/x-tar\r\nContent-Length: {self.length}\r\n'
            f'Content-Disposition: attachment; filename="{self.filename}"\r\n'
            'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nCross-Origin-Resource-Policy: same-origin\r\nConnection: close\r\n\r\n').encode())
        chunks = self.chunks()
        try:
            while (chunk := await asyncio.to_thread(next, chunks, None)) is not None:
                await send(chunk)
        finally:
            chunks.close()


async def prepare_download(application, principal: Principal, backup_id: str) -> BackupDownload:
    if principal.kind != 'administrator' or not backup_id or len(backup_id) > 128 or any(
            char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for char in backup_id):
        raise OwnerFailure('ACCESS_DENIED', 'backup', 'OPERATION_NOT_GRANTED')
    owner = application.bootstrap.assembly.backup
    if owner is None:
        raise OwnerFailure('INVALID_STATE', 'backup', 'NOT_READY')
    row = await owner.rows.read('backups', backup_id)
    if row is None or row['state'] != 'COMPLETE':
        raise OwnerFailure('PRECONDITION_FAILED', 'backup', 'BACKUP_NOT_COMPLETE')
    backup = application.resources.root / 'backups' / backup_id
    copier = ConsistentBackup(application.resources, lambda: False)
    manifest = await asyncio.to_thread(copier.verify, backup)
    if (await asyncio.to_thread(file_digest, backup / 'manifest.json'))[1] != row['manifest_digest']:
        raise OwnerFailure('STORAGE_FAILED', 'backup', 'CONTENT_MISMATCH')
    await application.identity.recheck(principal)
    return BackupDownload(application, principal, backup, manifest)
