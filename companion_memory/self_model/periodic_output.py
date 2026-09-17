"""Validate separate persona generation and independent regulatory responses.

Basis references name supplied current facts. A prior persona is a derived
summary and cannot enter the set of independently supported basis references.
Parsing never publishes, resolves a request or grants an approval capability.
"""
from typing import cast

from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.persistence.schema import BoundedTextSchema, InvalidValue, SequenceSchema
from companion_memory.persistence.semantic_records import Record, ID, P, V, enum, isolate, record

BASIS = record(object_id=ID, revision=P)
CANDIDATE = record(schema_version=V, text=BoundedTextSchema(6144),
    basis_refs=SequenceSchema(BASIS,0,16), change_reason=BoundedTextSchema(512))
REVIEW = record(schema_version=V, decision=enum('APPROVE','REJECT','UNCHANGED'), reason=BoundedTextSchema(1024))


def decode_persona_candidate(raw: bytes, supplied_basis: tuple[Record,...]) -> Record:
    """Decode the whole candidate and require exact supplied object revisions."""
    if type(raw) is not bytes or type(supplied_basis) is not tuple or len(supplied_basis)>16:
        raise InvalidValue()
    available = tuple(isolate(BASIS,ref,512) for ref in supplied_basis)
    if len({cast(str,ref['object_id']) for ref in available}) != len(available):
        raise InvalidValue()
    value = isolate(CANDIDATE,decode_content(raw,16384),16384)
    refs = cast(tuple[Record,...],value['basis_refs'])
    if (not cast(str,value['text']).strip() or not cast(str,value['change_reason']).strip()
            or len({cast(str,ref['object_id']) for ref in refs}) != len(refs)
            or any(ref not in available for ref in refs)):
        raise InvalidValue()
    encode_content(value,16384)
    return value


def decode_persona_review(raw: bytes) -> Record:
    """Preserve the independent decision; REJECT and UNCHANGED are not approval."""
    if type(raw) is not bytes:
        raise InvalidValue()
    value = isolate(REVIEW,decode_content(raw,16384),16384)
    if not cast(str,value['reason']).strip():
        raise InvalidValue()
    return value
