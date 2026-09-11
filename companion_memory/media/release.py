"""Media-owned fixed reference observations for source retirement.

The complete observed closure is immutable plan data, never permission to delete.
The writer checks every physical generation, real reference count and exact edge
before any source/object/history mutation. File deletion remains separate GC.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import Field, RecordSchema, SequenceSchema, UnitOfWork, Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import ID, REVISION, record, sequence, isolate
if TYPE_CHECKING:
    from .service import MediaService

BLOB_OBSERVATION = RecordSchema((Field('blob_id', ID), Field('generation', REVISION),
    Field('references_revision', REVISION), Field('reference_count', REVISION)))
MEDIA_RELEASE = RecordSchema((Field('blobs', SequenceSchema(BLOB_OBSERVATION, 0, 8)),
    Field('blob_references', SequenceSchema(ID, 0, 16)), Field('interpretation_references', SequenceSchema(ID, 0, 8))))


def observe_source_release(owner: MediaService, uow: UnitOfWork, source_id: str,
        members: tuple[MappingProxyType[str, Value], ...], deleted_payloads: frozenset[str]) -> MappingProxyType[str, Value]:
    """Return exact media effects; no other owner reads media's tables directly."""
    from .service import identity
    blobs: dict[str, MappingProxyType[str, Value]] = {}
    refs: list[str] = []; versions: list[str] = []
    for member in members:
        mid = cast(str, member['message_id'])
        for item in sequence(member['media']):
            selected = record(item); bid = cast(str, selected['blob_id']); generation = cast(int, selected['generation'])
            occurrence = owner._get('occurrences', uow, 'occurrence_id', selected['occurrence_id'])
            if (occurrence['message_id'], occurrence['blob_id'], occurrence['generation']) != (mid, bid, generation):
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            blob = owner._blob(uow, bid)
            if blob['generation'] != generation or blob['state'] != 'READY':
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')
            blobs[bid] = MappingProxyType({k: blob[k] for k in ('blob_id', 'generation', 'references_revision', 'reference_count')})
            edges = [('SOURCE', source_id)] + ([('EVENT', mid)] if mid in deleted_payloads else [])
            for kind, oid in edges:
                rid = identity('media_ref', kind, oid, bid, generation, selected['occurrence_id'])
                rows = owner.rows.stage('references_get', uow, {'reference_id': rid})
                if len(rows) != 1 or rows[0]['owner_kind'] != kind or rows[0]['owner_id'] != oid:
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                refs.append(rid)
            hid = identity('interpretation_ref', source_id, selected['occurrence_id'])
            rows = owner.rows.stage('interpretation_holders_get', uow, {'holder_id': hid})
            if len(rows) != 1 or rows[0]['interpretation_id'] != selected['interpretation_id'] or rows[0]['owner_id'] != source_id:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            versions.append(hid)
    return isolate(MEDIA_RELEASE, {'blobs': tuple(blobs[k] for k in sorted(blobs)),
        'blob_references': tuple(sorted(refs)), 'interpretation_references': tuple(sorted(versions))}, 8192)


def release_source(owner: MediaService, uow: UnitOfWork, source_id: str,
        members: tuple[MappingProxyType[str, Value], ...], now_us: int) -> tuple[int, int]:
    """Drop only SOURCE edges after the whole coordinator plan was checked."""
    from .service import identity
    blob_refs = version_refs = 0
    for member in members:
        for item in sequence(member['media']):
            selected = record(item)
            owner._reference(uow, cast(str, selected['blob_id']), cast(int, selected['generation']),
                'SOURCE', source_id, cast(str, selected['occurrence_id']), False, now_us)
            blob_refs += 1
            if len(owner.rows.stage('interpretation_holders_delete', uow, {
                    'holder_id': identity('interpretation_ref', source_id, selected['occurrence_id'])})) != 1:
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')
            version_refs += 1
    return blob_refs, version_refs
