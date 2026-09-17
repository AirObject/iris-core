"""Retrieval-owned published vector reference verification for complete backups."""
from __future__ import annotations
from pathlib import Path
import sqlite3
import os
from companion_memory.persistence.content_codec import decode_content
from .semantic_schema import validate
from .semantic_binary import VectorHeader, HEADER_BYTES, read_members
from .semantic_files import VectorFiles


def verify_index_backup(connection: sqlite3.Connection, manifest: dict, copied_root: Path) -> None:
    files = {item['path']: item for item in manifest['files']}
    instance = manifest['instance_id']
    cursor = connection.execute('SELECT body FROM retrieval_semantic_generation WHERE scope_id=?', (instance,))
    while rows := cursor.fetchmany(8):
        for (body,) in rows:
            generation = validate('semantic_generation', decode_content(body.encode(), 8192))
            name = VectorFiles._name(str(generation['generation_id']))
            if generation['file_name'] != name:
                raise ValueError('Backup index filename differs from its generation.')
            relative = 'indexes/' + name
            file = files.get(relative)
            if generation['state'] in ('READY', 'PUBLISHED') and file is None:
                raise ValueError('Backup omits a published index generation.')
            if file is None or generation['file_digest'] is None:
                continue
            if (file['bytes'], file['sha256']) != (generation['file_bytes'], generation['file_digest']):
                raise ValueError('Backup index differs from its publication record.')
            with os.fdopen(os.open(copied_root / relative, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                header = VectorHeader.decode(stream.read(HEADER_BYTES), str(generation['space_id']), name)
                if (header.space_id, header.generation_id, header.captured_seq, header.member_count, header.member_digest.hex()) != (
                        generation['space_id'], generation['generation_id'], generation['captured_seq'], generation['member_count'], generation['member_digest']):
                    raise ValueError('Backup index header differs from its publication.')
                for member in read_members(stream, header, lambda: None):
                    if generation['state'] not in ('READY', 'PUBLISHED'):
                        continue
                    reference = connection.execute('SELECT body FROM retrieval_semantic_member WHERE scope_id=? AND generation_id=? AND object_id=?',
                        (instance, name, member.object_id)).fetchone()
                    if reference is None:
                        raise ValueError('Backup vector has no retained artifact reference.')
                    retained = validate('semantic_member', decode_content(reference[0].encode(), 8192))
                    if (retained['object_revision'], retained['ack_revision'], retained['applied_seq'], retained['artifact_id'], retained['vector_digest']) != (
                            member.object_revision, member.ack_revision, member.applied_seq, member.artifact_id, member.vector_digest):
                        raise ValueError('Backup vector differs from its retained artifact binding.')
