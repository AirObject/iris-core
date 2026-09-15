"""Retrieval-owned closed work, paid artifacts, generations and query caches.

EMBED and DELETE_LOCAL have disjoint fields and state machines. Durable result
records carry only complete original vectors, never placeholders for paid work.
Cross-owner receipt and revision proofs are verified by transaction participants.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.semantic_records import (ID, N, P, T, B, H, CONFIG, OBJECT,
    REQUEST, RECEIPT, INTENT, ERROR, Record, enum, integer, fields, row, isolate, leaf_bytes)
from companion_memory.persistence.semantic_records import number
from companion_memory.persistence.schema import RecordSchema, BoundedTextSchema, InvalidValue

COMMON = fields(v=integer(1,1), revision=P, row_id=ID, work_id=ID, config=CONFIG, space_id=ID,
    object_ref=(OBJECT,), change_seq=(N,), superseded_by_revision=(P,), error=ERROR,
    cleanup_pending=B, created_at=T, completion_ref=(RECEIPT,))
EMBED_STATES = ('PREPARED','BOUND','REMOTE_UNKNOWN','RESULT_STORED','APPLIED','SUPERSEDED','KNOWN_FAILED','NOT_SENT')
LOCAL_STATES = ('LOCAL_PREPARED','LOCAL_APPLIED','LOCAL_SUPERSEDED','LOCAL_FAILED')
EMBED_WORK = RecordSchema(COMMON + fields(kind=enum('EMBED'), state=enum(*EMBED_STATES), purpose=enum('DOCUMENT','QUERY'),
    partition_id=ID, material_digest=H, input_leaf_count=integer(1,3), input_bytes=integer(1,8192),
    original_request_key=ID, intent=(INTENT,), request_ref=(REQUEST,), artifact_id=(ID,), deadline_at=(T,)))
DELETE_WORK = RecordSchema(COMMON + fields(kind=enum('DELETE_LOCAL'), state=enum(*LOCAL_STATES), deletion_ref=RECEIPT))
INPUT_LEAF = row(work_id=ID, ordinal=integer(0,2), byte_count=integer(1,3072), digest=H, data_base64=BoundedTextSchema(4096))
ARTIFACT = row(artifact_id=ID, config=CONFIG, space_id=ID, purpose=enum('DOCUMENT','QUERY'), partition_id=ID,
    material_digest=H, request_ref=REQUEST, dimension=integer(1024,1024), vector_digest=H,
    vector_leaf_count=integer(2,2), usage_ref=RECEIPT, completion_ref=RECEIPT, received_at=T)
VECTOR_LEAF = row(artifact_id=ID, ordinal=integer(0,1), byte_count=integer(4096,4096), digest=H, data_base64=BoundedTextSchema(5464))
GENERATION = row(generation_id=ID, space_id=ID, config=CONFIG,
    state=enum('BUILDING','READY','PUBLISHED','RETIRING','RETIRED','FAILED'), captured_seq=N,
    member_count=integer(0,4096), page_count=integer(0,512), confirmed_pages=integer(0,512),
    file_name=ID, file_bytes=integer(0,34344960), file_digest=(H,), member_digest=(H,),
    build_cursor=(ID,), retired_cursor=(ID,), error=ERROR, created_at=T, published_at=(T,))
MEMBER = row(generation_id=ID, ordinal=integer(0,4095), object_id=ID, object_revision=P,
    ack_revision=P, applied_seq=P, artifact_id=ID, vector_digest=H)
PAGE = row(generation_id=ID, page_no=integer(0,511), first_ordinal=integer(0,4095), count=integer(1,8),
    members_digest=H, state=enum('STAGED','CONFIRMED'), byte_offset=N, byte_count=integer(1,67072))
CONTROL = row(space_id=ID, config=CONFIG, scheduler=enum('PAUSED','ENABLED'),
    pause_reason=enum('NONE','USER','BUDGET','MODE','UNKNOWN','RESOURCE','INTEGRITY'),
    current_generation=(ID,), building_generation=(ID,), retiring_generation=(ID,),
    authorization_digest=(H,), gc_cursor=(ID,), last_cleanup_at=(T,), operation_count=N)
CACHE = row(cache_key=H, space_id=ID, partition_id=ID, material_digest=H, artifact_id=ID,
    state=enum('ACTIVE','EXPIRED'), expires_at=T, bound_at=T)
SCHEMAS = {'embedding_input_leaf': INPUT_LEAF, 'embedding_artifact': ARTIFACT, 'embedding_vector_leaf': VECTOR_LEAF,
    'semantic_generation': GENERATION, 'semantic_member': MEMBER, 'semantic_page': PAGE,
    'semantic_control': CONTROL, 'query_embedding_cache': CACHE}


def validate_work(value: object) -> Record:
    """Reject mixed local/network records and unsupported terminal observations."""
    if type(value) not in (dict, MappingProxyType): raise InvalidValue()
    result = isolate(DELETE_WORK if cast(dict[str,object],value).get('kind') == 'DELETE_LOCAL' else EMBED_WORK, value)
    if result['kind'] == 'DELETE_LOCAL':
        if result['object_ref'] is None or result['change_seq'] is None or result['change_seq'] == 0: raise InvalidValue()
        if result['state'] != 'LOCAL_PREPARED' and result['completion_ref'] is None: raise InvalidValue()
        if result['state'] == 'LOCAL_PREPARED' and (result['error'] != 'NONE' or result['cleanup_pending'] is not False): raise InvalidValue()
    else:
        document = result['purpose'] == 'DOCUMENT'
        if document and (result['object_ref'] is None or result['change_seq'] is None or result['change_seq'] == 0): raise InvalidValue()
        if not document and (result['object_ref'] is not None or result['change_seq'] is not None): raise InvalidValue()
        if not document and number(result['input_bytes']) > 512: raise InvalidValue()
        if result['input_leaf_count'] != (number(result['input_bytes'])+3071)//3072: raise InvalidValue()
        if (result['intent'] is None) != (result['deadline_at'] is None): raise InvalidValue()
        if result['state'] in ('BOUND','REMOTE_UNKNOWN') and result['request_ref'] is None: raise InvalidValue()
        if result['state'] in ('RESULT_STORED','APPLIED') and (result['artifact_id'] is None or result['completion_ref'] is None): raise InvalidValue()
        if result['state'] in ('SUPERSEDED','KNOWN_FAILED','NOT_SENT') and result['completion_ref'] is None: raise InvalidValue()
    return result


def validate(name: str, value: object) -> Record:
    if name == 'embedding_work': return validate_work(value)
    result = isolate(SCHEMAS[name], value)
    if name == 'embedding_input_leaf': leaf_bytes(result, maximum=3072)
    elif name == 'embedding_vector_leaf': leaf_bytes(result, maximum=4096, exact=4096)
    elif name == 'embedding_artifact':
        reference = result['request_ref']
        if type(reference) is not MappingProxyType or reference['attempt_id'] is None or result['revision'] != 1: raise InvalidValue()
    elif name == 'semantic_page':
        if number(result['first_ordinal']) != 8*number(result['page_no']) or result['byte_offset'] != 4096+8384*number(result['first_ordinal']) or result['byte_count'] != 8384*number(result['count']): raise InvalidValue()
    elif name == 'semantic_generation':
        if number(result['confirmed_pages']) > number(result['page_count']) or number(result['page_count']) != (number(result['member_count'])+7)//8: raise InvalidValue()
        if result['state'] in ('READY','PUBLISHED','RETIRING') and (result['file_digest'] is None or result['member_digest'] is None
                or number(result['confirmed_pages']) != number(result['page_count']) or result['file_bytes'] != 4096+8384*number(result['member_count'])): raise InvalidValue()
        if result['state'] in ('PUBLISHED','RETIRING') and result['published_at'] is None: raise InvalidValue()
    elif name == 'semantic_control':
        if result['building_generation'] is not None and result['retiring_generation'] is not None: raise InvalidValue()
        if (result['scheduler'] == 'ENABLED') != (result['pause_reason'] == 'NONE'): raise InvalidValue()
        if result['scheduler'] == 'ENABLED' and result['authorization_digest'] is None: raise InvalidValue()
        ids = [result[k] for k in ('current_generation','building_generation','retiring_generation') if result[k] is not None]
        if len(set(ids)) != len(ids): raise InvalidValue()
    elif name == 'query_embedding_cache' and result['expires_at'] != number(result['bound_at'])+86400000000: raise InvalidValue()
    return result
