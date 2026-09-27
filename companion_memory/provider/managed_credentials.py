"""Private immutable Provider keys, prepared before a wizard reference commits.

Each original setup operation owns a separately published directory containing
private key files and its secret-free request/draft. Publication is atomic and
synced; a retry uses exactly that original material. Unreferenced preparations
are bounded and never activate a key or grant permission to send. This directory
is outside the application data volume and is excluded from ordinary backups.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import cast
from uuid import uuid4

from companion_memory.persistence.owned_statements import OwnerFailure

PROVIDERS = ('generation', 'embedding')
FORMAT = 'MANAGED_PROVIDER_PREPARATION_V1'
MAX_PREPARATIONS = 256
MAX_RECORD_BYTES = 524288
REFERENCE = re.compile(r'web_([a-f0-9]{64})_(generation|embedding)\Z')


def key_bytes(value: object, field: str) -> bytes:
    """Accept the Provider's bounded header credential, with a safe failure."""
    if type(value) is not str or not 1 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise OwnerFailure('INVALID_INPUT', field, 'API_KEY_INVALID')
    return value.encode('ascii')


def _directory(path: Path) -> int:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Private credential directory unavailable.')
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or path.resolve(strict=True) != path):
        os.close(descriptor)
        raise ValueError('Private credential directory unavailable.')
    return descriptor


def _read(directory: int, name: str, maximum: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or not 1 <= info.st_size <= maximum):
            raise ValueError('Private credential record unavailable.')
        data = stream.read(maximum + 1)
        if len(data) != info.st_size:
            raise ValueError('Private credential record unavailable.')
        return data


class ManagedCredentials:
    """An optional dedicated writable root; missing roots do not affect old refs."""
    def __init__(self, root: Path, instance_id: str):
        self.root, self.instance_id = root, instance_id

    def operation_id(self, key: str) -> str:
        return hashlib.sha256((self.instance_id + '\0' + key).encode()).hexdigest()

    def reference(self, key: str, provider: str) -> str:
        if provider not in PROVIDERS:
            raise ValueError('Unknown Provider role.')
        return 'web_' + self.operation_id(key) + '_' + provider

    def available(self) -> bool:
        """Observe private writable metadata without reading any key bytes."""
        try:
            descriptor = _directory(self.root)
            try:
                return not bool(os.fstatvfs(descriptor).f_flag & os.ST_RDONLY) and os.access(self.root, os.W_OK | os.X_OK)
            finally:
                os.close(descriptor)
        except (OSError, ValueError):
            return False

    def read(self, reference: str, revision: str) -> bytes:
        """Lend original immutable bytes only to the Provider resolver."""
        match = REFERENCE.fullmatch(reference)
        if match is None or revision != 'v1':
            raise ValueError('Private credential reference unavailable.')
        root = _directory(self.root)
        try:
            directory = _directory(self.root / match[1])
            try:
                raw = _read(directory, match[2], 4096)
                if any(not 33 <= item <= 126 for item in raw):
                    raise ValueError('Private credential record unavailable.')
                return raw
            finally:
                os.close(directory)
        finally:
            os.close(root)

    def configured(self, reference: str, revision: str) -> bool:
        """Observe exact private file metadata; no credential content is returned."""
        match = REFERENCE.fullmatch(reference)
        if match is None or revision != 'v1':
            return False
        try:
            root = _directory(self.root)
            try:
                directory = _directory(self.root / match[1])
                try:
                    info = os.stat(match[2], dir_fd=directory, follow_symlinks=False)
                    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                        and stat.S_IMODE(info.st_mode) == 0o600 and 1 <= info.st_size <= 4096)
                finally:
                    os.close(directory)
            finally:
                os.close(root)
        except (OSError, ValueError):
            return False

    def prepared(self, key: str) -> dict[str, object] | None:
        """Read secret-free original material; absence is not a commit decision."""
        try:
            root = _directory(self.root)
            try:
                try:
                    directory = _directory(self.root / self.operation_id(key))
                except FileNotFoundError:
                    return None
                try:
                    value = json.loads(_read(directory, 'operation.json', MAX_RECORD_BYTES))
                finally:
                    os.close(directory)
            finally:
                os.close(root)
            if (type(value) is not dict or set(value) != {'format', 'request', 'draft', 'updated'}
                    or value['format'] != FORMAT or type(value['request']) is not dict
                    or type(value['draft']) is not dict or type(value['updated']) is not list
                    or any(type(item) is not str or item not in PROVIDERS for item in value['updated'])):
                raise ValueError('Private setup preparation unavailable.')
            return cast(dict[str, object], value)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise OwnerFailure('RESOURCE_UNAVAILABLE', 'provider', 'CREDENTIAL_STORAGE_UNAVAILABLE') from None

    def match(self, key: str, prior: dict[str, object], request: dict[str, object], supplied: dict[str, bytes]) -> None:
        """Reject any changed original request without publishing secret digests."""
        if prior['request'] != request:
            raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'setup', 'CONTENT_MISMATCH')
        for provider, secret in supplied.items():
            if provider not in cast(list[str], prior['updated']):
                raise OwnerFailure('IDEMPOTENCY_CONFLICT', provider + '_api_key', 'CONTENT_MISMATCH')
            try:
                original = self.read(self.reference(key, provider), 'v1')
            except (OSError, ValueError):
                raise OwnerFailure('RESOURCE_UNAVAILABLE', provider + '_api_key', 'CREDENTIAL_STORAGE_UNAVAILABLE') from None
            if not hmac.compare_digest(original, secret):
                raise OwnerFailure('IDEMPOTENCY_CONFLICT', provider + '_api_key', 'CONTENT_MISMATCH')
        # A previous rename may have succeeded before its parent sync failed.
        # Reconfirm that publication's durability before allowing the DB to
        # commit references, including retries that no longer carry key bytes.
        try:
            root = _directory(self.root)
            try:
                directory = _directory(self.root / self.operation_id(key))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                os.fsync(root)
            finally:
                os.close(root)
        except (OSError, ValueError):
            raise OwnerFailure('RESOURCE_UNAVAILABLE', 'provider', 'CREDENTIAL_STORAGE_UNAVAILABLE') from None

    def prepare(self, key: str, request: dict[str, object], draft: dict[str, object], supplied: dict[str, bytes]) -> dict[str, object]:
        """Atomically publish a complete immutable bundle before the DB commit.

        A sync/publication failure leaves at most one bounded private preparation;
        retry with the original key resolves its exact content. No file is removed
        because a caller timed out or a draft commit remains uncertain.
        """
        if not set(supplied) <= set(PROVIDERS) or any(type(raw) is not bytes or not 1 <= len(raw) <= 4096
                or any(not 33 <= item <= 126 for item in raw) for raw in supplied.values()):
            raise OwnerFailure('INVALID_INPUT', 'provider', 'API_KEY_INVALID')
        if not self.available():
            raise OwnerFailure('RESOURCE_UNAVAILABLE', 'provider', 'CREDENTIAL_STORAGE_UNAVAILABLE')
        prior = self.prepared(key)
        if prior is not None:
            self.match(key, prior, request, supplied)
            return prior
        record: dict[str, object] = {'format': FORMAT, 'request': request, 'draft': draft, 'updated': sorted(supplied)}
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(encoded) > MAX_RECORD_BYTES:
            raise OwnerFailure('INVALID_INPUT', 'setup', 'LIMIT_EXCEEDED')
        root = _directory(self.root)
        try:
            if len(os.listdir(root)) >= MAX_PREPARATIONS:
                raise OwnerFailure('RESOURCE_BUSY', 'provider', 'CREDENTIAL_CAPACITY_REACHED')
            temporary = '.pending-' + uuid4().hex
            os.mkdir(temporary, 0o700, dir_fd=root)
            directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                for name, data in {'operation.json': encoded, **supplied}.items():
                    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                os.fsync(directory)
            finally:
                os.close(directory)
            os.rename(temporary, self.operation_id(key), src_dir_fd=root, dst_dir_fd=root)
            os.fsync(root)
            return record
        except OSError:
            raise OwnerFailure('RESOURCE_UNAVAILABLE', 'provider', 'CREDENTIAL_STORAGE_UNAVAILABLE') from None
        finally:
            os.close(root)
