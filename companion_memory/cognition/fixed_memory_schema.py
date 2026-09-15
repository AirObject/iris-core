"""Frozen reviewed memory sets with independently atomic member establishment.

A sealed set may be partially established. Only actual issued reviewer authority
may fill reviewed_by; these record codecs do not issue such authority. Sources
and original domain memory bodies are validated again by their business owners.
"""
from companion_memory.persistence.semantic_records import (ID,P,T,H,CONFIG,OBJECT,RECEIPT,Record,
    enum,integer,row,isolate)
from companion_memory.persistence.semantic_records import number
from companion_memory.persistence.schema import BoundedTextSchema,InvalidValue

FIXED_SET = row(set_id=ID, config=CONFIG, state=enum('OPEN','SEALED','ESTABLISHED'),
    expected_members=integer(12,4096), stored_members=integer(0,4096), established_members=integer(0,4096),
    manifest_digest=H, review_ref=ID, review_digest=H, reviewed_by=BoundedTextSchema(32), created_at=T)
FIXED_MEMBER = row(set_id=ID, ordinal=integer(0,4095), member_id=ID, event_json=BoundedTextSchema(2048),
    memory_json=BoundedTextSchema(4096), content_digest=H, state=enum('STORED','ESTABLISHED'),
    object_ref=(OBJECT,), source_id=(ID,), establishment_ref=(RECEIPT,))


def validate_set(value: object) -> Record:
    result = isolate(FIXED_SET,value)
    if number(result['expected_members']) not in (12,4096) or not 0 <= number(result['established_members']) <= number(result['stored_members']) <= number(result['expected_members']): raise InvalidValue()
    if not result['reviewed_by'] or result['state'] == 'OPEN' and number(result['established_members']) != 0: raise InvalidValue()
    if result['state'] != 'OPEN' and number(result['stored_members']) != number(result['expected_members']): raise InvalidValue()
    if (result['state'] == 'ESTABLISHED') != (number(result['established_members']) == number(result['expected_members'])): raise InvalidValue()
    return result


def validate_member(value: object) -> Record:
    result = isolate(FIXED_MEMBER,value)
    for key in ('object_ref','source_id','establishment_ref'):
        if (result['state'] == 'STORED') != (result[key] is None): raise InvalidValue()
    return result
