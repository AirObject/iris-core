"""One complete public projection for imported, initial and periodic persona.

The public value contains no candidate, historical body or full basis list.
Canonical encoding is shared by material construction and public reads so an
accepted summary cannot silently shrink at a downstream boundary.
"""
from types import MappingProxyType
from typing import cast

from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import BoundedTextSchema, InvalidValue
from companion_memory.persistence.semantic_records import Record, ID, N, P, B, H, enum, isolate, record

PROJECTION = record(projection_version=enum('PERIODIC_PERSONA_V1'), availability=enum('AVAILABLE'),
    text=BoundedTextSchema(6144), revision=P, publication_id=ID, generated_at=N,
    review_status=enum('APPROVED','MODEL_REVIEWED'), origin=enum('REMOTE_PROVIDER'),
    publication_origin=enum('IMPORTED_APPROVED','INITIAL_APPROVED','PERIODIC_REVIEWED'), stale=B,
    original_database_id=(ID,), original_publication_id=(ID,), original_candidate_id=(ID,),
    review_evidence_digest=(H,))
MAX_PROJECTION_BYTES = 8192


def encode_projection(value: object) -> bytes:
    """Validate full provenance/status combinations and encode without truncation."""
    current = isolate(PROJECTION,value,MAX_PROJECTION_BYTES)
    imported = current['publication_origin']=='IMPORTED_APPROVED'
    if ((current['publication_origin']=='PERIODIC_REVIEWED') != (current['review_status']=='MODEL_REVIEWED')
            or any((current[key] is not None)!=imported for key in
                ('original_database_id','original_publication_id','original_candidate_id','review_evidence_digest'))
            or not cast(str,current['text']).strip()):
        raise InvalidValue()
    return encode_content(current,MAX_PROJECTION_BYTES)


def project_current(current: Record) -> Record:
    """Normalize an owner-verified current value, retaining its real publication origin.

    This helper cannot verify a database pointer. Only the self-model reader may
    supply that verified value; the result is a bounded projection, not a grant.
    """
    origin = current.get('publication_origin','INITIAL_APPROVED')
    result = MappingProxyType({'projection_version':'PERIODIC_PERSONA_V1','availability':'AVAILABLE',
        'text':current['text'],'revision':current['revision'],'publication_id':current['publication_id'],
        'generated_at':current['generated_at_us'],'review_status':current['review'],
        'origin':current['model_origin'],'publication_origin':origin,'stale':current['stale'],
        **{key:current.get(key) for key in ('original_database_id','original_publication_id',
            'original_candidate_id','review_evidence_digest')}})
    encode_projection(result)
    return result
