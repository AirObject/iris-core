"""Closed first-persona run, resolution, publication and current projection.

Each generation retains its original Provider and input identity. Review changes
only review fields; publication requires the approved immutable candidate. No
format check grants model work or substitutes for the real runtime mode CAS.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import BoundedTextSchema, Field, RecordSchema, ScalarSchema, Value, InvalidValue
from companion_memory.persistence.text_records import BASE, ID, UINT, REVISION, DIGEST, OPERATION, isolate_record, digest

GENERATION=ScalarSchema('integer',1,3)
def enum(*values: str): return ScalarSchema('enum',choices=values)
RUN=RecordSchema(BASE+(
    Field('input_id',ID),Field('input_digest',DIGEST),Field('self_subject_id',ID),Field('self_revision',REVISION),
    Field('generation',GENERATION),Field('state',enum('PREPARED','REQUEST_ASSOCIATED','WAITING_REVIEW','APPROVED','KNOWN_FAILED','USER_REJECTED','REMOTE_UNKNOWN','PUBLISHED')),
    Field('provider_operation_key',ID),Field('provider_request_id',ID,nullable=True),Field('resolution_id',ID,nullable=True),
    Field('publication_id',ID,nullable=True),Field('mode_epoch',REVISION),Field('prompt_ref',ID),Field('schema_ref',ID),
    Field('transform_ref',ID),Field('account_id',ID),Field('window_id',ID),Field('binding_digest',DIGEST),
    Field('original_operation',OPERATION),Field('last_operation',OPERATION),Field('updated_at_us',UINT)))
FAILURE=enum('NONE','OTHER_REFUSAL','OUTPUT_LIMIT','INVALID_RESPONSE','AUTHENTICATION_FAILED','CONFIGURATION_REJECTED',
    'CANCELLED','TIMED_OUT','PAUSED_BUDGET','MODE_BLOCKED')
CANDIDATE=RecordSchema(BASE+(
    Field('run_id',ID),Field('generation',GENERATION),Field('provider_operation_key',ID),Field('provider_request_id',ID,nullable=True),
    Field('handoff_id',ID,nullable=True),Field('terminal_receipt',OPERATION),Field('resolution',enum('SUCCEEDED','KNOWN_FAILED','NOT_SENT')),
    Field('failure_reason',FAILURE),Field('text',BoundedTextSchema(1024),nullable=True),Field('text_digest',DIGEST,nullable=True),
    Field('input_id',ID),Field('input_digest',DIGEST),Field('binding_digest',DIGEST),Field('review',enum('NOT_APPLICABLE','PENDING','APPROVED','REJECTED')),
    Field('reviewed_by',ID,nullable=True),Field('reviewed_at_us',UINT,nullable=True),Field('review_operation',OPERATION,nullable=True)))
PUBLICATION=RecordSchema(BASE+(
    Field('run_id',ID),Field('generation',GENERATION),Field('candidate_id',ID),Field('candidate_revision',REVISION),Field('candidate_digest',DIGEST),
    Field('input_id',ID),Field('input_digest',DIGEST),Field('self_subject_id',ID),Field('self_revision',REVISION),
    Field('provider_request_id',ID),Field('handoff_id',ID),Field('requested_model_id',ID),Field('reported_model_id',ID),Field('resolved_model_id',ID,nullable=True),
    Field('prompt_ref',ID),Field('schema_ref',ID),Field('transform_ref',ID),Field('text',BoundedTextSchema(1024)),
    Field('generated_at_us',UINT),Field('reviewed_by',ID),Field('review_operation',OPERATION),Field('publication_operation',OPERATION)))
PROJECTION=RecordSchema((Field('publication_id',ID),Field('revision',REVISION),Field('text',BoundedTextSchema(1024)),
    Field('generated_at_us',UINT),Field('review',enum('APPROVED')),Field('model_origin',enum('REMOTE_PROVIDER')),Field('stale',ScalarSchema('boolean'))))


def candidate_digest(value: MappingProxyType[str,Value]) -> str:
    """Exclude exactly the review revision and review metadata from identity."""
    return digest(MappingProxyType({key:child for key,child in value.items() if key not in
        ('revision','review','reviewed_by','reviewed_at_us','review_operation')}))


def isolate_run(value: object) -> MappingProxyType[str,Value]:
    result=isolate_record(RUN,value,4096)
    if cast(int,result['updated_at_us'])<cast(int,result['created_at_us']):raise InvalidValue()
    if result['state']=='PREPARED' and any(result[name] is not None for name in ('provider_request_id','resolution_id','publication_id')):
        raise InvalidValue()
    if result['state'] in ('REMOTE_UNKNOWN','REQUEST_ASSOCIATED') and (result['resolution_id'] is not None or result['publication_id'] is not None):raise InvalidValue()
    if result['state'] in ('WAITING_REVIEW','APPROVED','USER_REJECTED','KNOWN_FAILED','PUBLISHED') and result['resolution_id'] is None:raise InvalidValue()
    if (result['state']=='PUBLISHED') != (result['publication_id'] is not None):raise InvalidValue()
    return result


def isolate_candidate(value: object) -> MappingProxyType[str,Value]:
    result=isolate_record(CANDIDATE,value,4096)
    if result['resolution']=='SUCCEEDED':
        if (result['failure_reason']!='NONE' or type(result['text']) is not str or not result['text'].strip()
                or result['text_digest']!=digest(result['text']) or result['handoff_id'] is None or result['provider_request_id'] is None
                or result['review']=='NOT_APPLICABLE'):raise InvalidValue()
    elif ((result['failure_reason']=='NONE' and not (result['resolution']=='NOT_SENT' and result['provider_request_id'] is None))
            or result['text'] is not None or result['text_digest'] is not None or result['review']!='NOT_APPLICABLE'):
        raise InvalidValue()
    if result['resolution']=='NOT_SENT' and result['handoff_id'] is not None:raise InvalidValue()
    if result['provider_request_id'] is None:
        receipt=result['terminal_receipt'];assert type(receipt) is MappingProxyType
        if (result['resolution']!='NOT_SENT' or result['failure_reason']!='NONE'
                or receipt['owner_namespace']!='self_model' or receipt['operation_kind']!='record_initial_persona_resolution'
                or receipt['scope_id']!=result['instance_id']):raise InvalidValue()
    reviewed=result['review'] in ('APPROVED','REJECTED')
    if any((result[name] is not None)!=reviewed for name in ('reviewed_by','reviewed_at_us','review_operation')):raise InvalidValue()
    return result


def isolate_publication(value: object) -> MappingProxyType[str,Value]:
    result=isolate_record(PUBLICATION,value,4096)
    if type(result['text']) is not str or not result['text'].strip():raise InvalidValue()
    return result


def projection(publication: MappingProxyType[str,Value], stale: bool) -> MappingProxyType[str,Value]:
    """Project reviewed text without changing the immutable publication source."""
    value=isolate_publication(publication)
    return isolate_record(PROJECTION,{'publication_id':value['object_id'],'revision':value['revision'],'text':value['text'],
        'generated_at_us':value['generated_at_us'],'review':'APPROVED','model_origin':'REMOTE_PROVIDER','stale':stale},2048)


def query_projection(current: MappingProxyType[str,Value]) -> MappingProxyType[str,Value]:
    """Encode the complete public reply section, including its original metadata."""
    from companion_memory.persistence.content_codec import encode_content
    value=isolate_record(PROJECTION,current,2048)
    section=MappingProxyType({'availability':'AVAILABLE','text':value['text'],'revision':value['revision'],
        'publication_id':value['publication_id'],'generated_at':value['generated_at_us'],
        'review_status':'APPROVED','origin':'REMOTE_PROVIDER','stale':value['stale']})
    encode_content(section,2048)
    return section
