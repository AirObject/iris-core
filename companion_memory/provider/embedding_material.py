"""Bounded Provider-owned encoding of complete embedding handoff material.

These transformations supply the original completion transaction's immutable
leaves. They confer neither receipt authority nor permission to send, confirm,
retire or register a result. The Provider execution owner retains those duties.
"""
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
import math
from companion_memory.persistence.semantic_records import Record as StoredRecord,isolate,make_leaf,leaf_bytes,number
from .embedding_schema import HANDOFF_LEAF,PAYLOAD,validate_leaf
from .embedding_protocol import NORMALIZED_LIMIT
from .values import Record,InvalidData,as_record,freeze,dump,load,is_identifier


def normalized_result(value: object, *, space_id: str, model_id: str) -> Record:
    """Check a complete already-normalized result without altering binary64."""
    if not is_identifier(space_id) or not is_identifier(model_id): raise InvalidData()
    result=as_record(freeze(value,NORMALIZED_LIMIT,owned=True))
    if set(result)!={'vectors','dimensions','space_id','model_id','input_items'}: raise InvalidData()
    if (type(result['dimensions']) is not int or result['dimensions']!=1024 or type(result['input_items']) is not int
            or result['input_items']!=1 or result['space_id']!=space_id or result['model_id']!=model_id): raise InvalidData()
    vectors=result['vectors']
    if type(vectors) is not tuple or len(vectors)!=1 or type(vectors[0]) is not tuple: raise InvalidData()
    vector=vectors[0]
    if len(vector)!=1024 or any(type(n) is not float or not math.isfinite(n) for n in vector) or not any(vector): raise InvalidData()
    return result


@dataclass(frozen=True,slots=True)
class HandoffMaterial:
    """Canonical payload metadata and at most ten immutable complete leaves."""
    payload: StoredRecord
    leaves: tuple[StoredRecord,...]


def split_handoff(value: object, *, handoff_id: str,request_id: str,attempt_id: str,
                  space_id: str,model_id: str) -> HandoffMaterial:
    """Shard the original complete normalized result before its atomic write."""
    if not all(is_identifier(x) for x in (handoff_id,request_id,attempt_id)): raise InvalidData()
    raw=dump(normalized_result(value,space_id=space_id,model_id=model_id),NORMALIZED_LIMIT).encode('utf-8')
    leaves=tuple(make_leaf(HANDOFF_LEAF,'embedding-handoff-leaf','handoff_id',handoff_id,ordinal,raw[start:start+4096],
        request_id=request_id,attempt_id=attempt_id) for ordinal,start in enumerate(range(0,len(raw),4096)))
    payload=isolate(PAYLOAD,{'format_version':1,'byte_count':len(raw),'leaf_count':len(leaves),'payload_digest':sha256(raw).hexdigest()})
    return HandoffMaterial(payload,leaves)


def restore_handoff(payload: object,leaves: Iterable[object], *, handoff_id: str,request_id: str,attempt_id: str,
                    space_id: str,model_id: str) -> Record:
    """Reject missing, reordered, substituted or oversized material before return."""
    if not all(is_identifier(x) for x in (handoff_id,request_id,attempt_id)): raise InvalidData()
    root=isolate(PAYLOAD,payload);count=number(root['leaf_count']);length=number(root['byte_count'])
    if count!=(length+4095)//4096: raise InvalidData()
    blocks=[]
    for ordinal,supplied in enumerate(leaves):
        if ordinal>=count: raise InvalidData()
        leaf=validate_leaf(supplied)
        if (leaf['ordinal']!=ordinal or leaf['handoff_id']!=handoff_id or leaf['request_id']!=request_id
                or leaf['attempt_id']!=attempt_id): raise InvalidData()
        block=leaf_bytes(leaf,maximum=4096)
        if len(block)!=(4096 if ordinal<count-1 else length-4096*ordinal): raise InvalidData()
        expected=make_leaf(HANDOFF_LEAF,'embedding-handoff-leaf','handoff_id',handoff_id,ordinal,block,
            request_id=request_id,attempt_id=attempt_id)
        if leaf!=expected: raise InvalidData()
        blocks.append(block)
    if len(blocks)!=count: raise InvalidData()
    raw=b''.join(blocks)
    if len(raw)!=length or sha256(raw).hexdigest()!=root['payload_digest']: raise InvalidData()
    result=normalized_result(load(raw.decode('utf-8'),NORMALIZED_LIMIT),space_id=space_id,model_id=model_id)
    if dump(result,NORMALIZED_LIMIT).encode('utf-8')!=raw: raise InvalidData()
    return result
