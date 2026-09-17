"""Verified physical bindings for a stopped-source managed restore."""
from __future__ import annotations
from pathlib import Path
from typing import cast, TYPE_CHECKING
from types import MappingProxyType

if TYPE_CHECKING:
    from companion_memory.runtime.managed_resources import ManagedResources

_ISSUER = object()


class MediaRestoreResources:
    """Native file-owner proof; never constructed from HTTP input."""
    def __init__(self) -> None:
        raise TypeError('Obtain verified managed restoration resources.')

    def binding(self, path: Path) -> tuple[int, int, int, int] | None:
        if self._issuer is not _ISSUER or self.resources.fd < 0:
            raise ValueError('Restore file ownership ended.')
        return self.bindings.get(path.relative_to(self.resources.root).as_posix())

    def complete(self) -> None:
        from companion_memory.runtime.managed_resources import atomic_record
        atomic_record(self.resources.root / 'bootstrap/restore-media.json', {
            'switch_id': self.switch_id, 'authority_id': self.resources.identity['authority_id'], 'state': 'COMPLETE'})

    _issuer: object
    resources: ManagedResources
    switch_id: str
    bindings: MappingProxyType[str, tuple[int, int, int, int]]


def restored_media_resources(resources: ManagedResources) -> MediaRestoreResources | None:
    from companion_memory.runtime.managed_resources import read_record
    from companion_memory.runtime.managed_restore import restore_manifest
    from companion_memory.persistence.managed_backup import file_digest
    marker = resources.root / 'bootstrap/restore-switch.json'
    if not marker.exists():
        return None
    intent = read_record(marker)
    if intent.get('state') != 'ACTIVE' or intent.get('target_authority') != resources.identity['authority_id']:
        raise ValueError('Restore is not active on these resources.')
    complete = resources.root / 'bootstrap/restore-media.json'
    if complete.exists():
        if read_record(complete) != {'switch_id': intent['switch_id'], 'authority_id': resources.identity['authority_id'], 'state': 'COMPLETE'}:
            raise ValueError('Media restore completion differs.')
        return None
    manifest = restore_manifest(resources.root, intent)
    bindings: dict[str, tuple[int, int, int, int]] = {}
    for kind in ('directories', 'files'):
        for item in cast(list[dict[str, object]], manifest[kind]):
            name = cast(str, item['path'])
            if name.split('/')[0] not in ('blobs', 'upload_staging'):
                continue
            path = resources.root / name
            if any(parent.is_symlink() for parent in (path, *path.parents)):
                raise ValueError('Restored media alias rejected.')
            if kind == 'files' and file_digest(path) != (item['bytes'], item['sha256']):
                raise ValueError('Restored media content differs.')
            meta = path.lstat()
            bindings[name] = (cast(int, item['device']), cast(int, item['inode']), meta.st_dev, meta.st_ino)
    result = object.__new__(MediaRestoreResources)
    result._issuer, result.resources, result.switch_id, result.bindings = _ISSUER, resources, cast(str, intent['switch_id']), MappingProxyType(bindings)
    return result
