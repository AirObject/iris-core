"""Media-owned reference checks on an isolated, read-only backup database."""
from __future__ import annotations
import sqlite3
from companion_memory.persistence.schema import valid_identifier


def verify_media_backup(connection: sqlite3.Connection, manifest: dict) -> None:
    """Require every published or sealed resource at its recorded size and identity.

    Incomplete uploads and pending retirements retain their original states;
    verification does not finish, delete or repair any resource.
    """
    files = {item['path']: item for item in manifest['files']}
    directories = {item['path']: item for item in manifest['directories']}
    instance = manifest['instance_id']
    roots = connection.execute('SELECT device,inode,staging_device,staging_inode,published_device,published_inode FROM media_roots WHERE scope_id=?', (instance,)).fetchmany(2)
    if len(roots) > 1:
        raise ValueError('Backup contains multiple media root bindings.')
    for row in roots:
        for index, path in enumerate(('blobs', 'upload_staging', 'blobs/published')):
            physical = directories.get(path)
            if physical is None or (physical['device'], physical['inode']) != row[index * 2:index * 2 + 2]:
                raise ValueError('Backup media directory binding differs.')
    cursor = connection.execute('SELECT blob_id,generation,state,byte_count,sha256,device,inode,reference_count FROM media_blobs WHERE scope_id=?', (instance,))
    while rows := cursor.fetchmany(32):
        for blob_id, generation, state, size, digest, device, inode, reference_count in rows:
            if not valid_identifier(blob_id) or type(generation) is not int or generation < 1:
                raise ValueError('Backup media identity is invalid.')
            file = files.get(f'blobs/published/{blob_id}.{generation}')
            if state == 'READY' and file is None:
                raise ValueError('Backup omits a published media resource.')
            if file is not None and (file['bytes'], file['sha256']) != (size, digest):
                raise ValueError('Backup media content differs from its owner record.')
            if file is not None and device is not None and (file['device'], file['inode']) != (device, inode):
                raise ValueError('Backup media physical binding differs.')
            actual = connection.execute('SELECT count(*) FROM media_references WHERE scope_id=? AND blob_id=? AND generation=?', (instance, blob_id, generation)).fetchone()
            if actual != (reference_count,):
                raise ValueError('Backup media reference count differs.')
    if connection.execute('SELECT 1 FROM media_references r LEFT JOIN media_blobs b ON b.scope_id=r.scope_id AND b.blob_id=r.blob_id AND b.generation=r.generation WHERE r.scope_id=? AND (b.blob_id IS NULL OR b.state=\'DELETED\') LIMIT 1', (instance,)).fetchone() is not None:
        raise ValueError('Backup has an unresolved media reference.')
    cursor = connection.execute("SELECT upload_id,byte_count,sha256,staging_device,staging_inode FROM media_uploads WHERE scope_id=? AND state='SEALED'", (instance,))
    while rows := cursor.fetchmany(32):
        for upload, size, digest, device, inode in rows:
            file = files.get('upload_staging/' + upload)
            if file is None or (file['bytes'], file['sha256'], file['device'], file['inode']) != (size, digest, device, inode):
                raise ValueError('Backup omits an original sealed upload.')
