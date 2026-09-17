"""Complete daily Chat results use the Provider's existing bounded leaf store.

The result owner can read the full original normalized output only while the
handoff remains held. These pure codecs confer no completion or replay rights.
"""
from collections.abc import Iterable
from hashlib import sha256
from companion_memory.persistence.semantic_records import isolate,make_leaf,leaf_bytes,number
from .embedding_schema import PAYLOAD,HANDOFF_LEAF,validate_leaf
from .embedding_material import HandoffMaterial
from .daily_protocol import DailyChatBinding
from .dream_protocol import DreamChatBinding
from .values import Record,InvalidData,as_record,freeze,dump,load,is_identifier


def normalized(value:object,binding:DailyChatBinding|DreamChatBinding) -> Record:
    result=as_record(freeze(value,40960,owned=True))
    if (set(result)!={'format_version','output','stop_reason','provider_response_ref','requested_model_id','reported_model_id',
            'resolved_model_id','output_schema_ref','raw_output_digest'} or type(result['format_version']) is not int or result['format_version']!=(6 if type(binding) is DreamChatBinding else 5)
            or result['stop_reason']!='STOP' or not is_identifier(result['provider_response_ref'])
            or result['requested_model_id']!=binding.requested_model or result['reported_model_id']!=binding.requested_model
            or result['resolved_model_id'] is not None or result['output_schema_ref']!=binding.schema_ref):raise InvalidData()
    digest=result['raw_output_digest']
    if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise InvalidData()
    output=as_record(result['output']);dump(output,24576)
    if binding.role=='MEDIA':
        from .image_protocol import decode_image_output
        decode_image_output(dump(output,24576).encode())
    return result


def split(value:object,binding:DailyChatBinding|DreamChatBinding,*,handoff_id:str,request_id:str,attempt_id:str) -> HandoffMaterial:
    if not all(is_identifier(value) for value in (handoff_id,request_id,attempt_id)):raise InvalidData()
    raw=dump(normalized(value,binding),40960).encode()
    leaves=tuple(make_leaf(HANDOFF_LEAF,'embedding-handoff-leaf','handoff_id',handoff_id,ordinal,raw[start:start+4096],
        request_id=request_id,attempt_id=attempt_id) for ordinal,start in enumerate(range(0,len(raw),4096)))
    root=isolate(PAYLOAD,{'format_version':1,'byte_count':len(raw),'leaf_count':len(leaves),'payload_digest':sha256(raw).hexdigest()})
    return HandoffMaterial(root,leaves)


def restore(payload:object,leaves:Iterable[object],binding:DailyChatBinding|DreamChatBinding,*,handoff_id:str,request_id:str,attempt_id:str) -> Record:
    root=isolate(PAYLOAD,payload);count=number(root['leaf_count']);length=number(root['byte_count'])
    if not all(is_identifier(value) for value in (handoff_id,request_id,attempt_id)) or count!=(length+4095)//4096:raise InvalidData()
    blocks=[]
    for ordinal,supplied in enumerate(leaves):
        if ordinal>=count:raise InvalidData()
        leaf=validate_leaf(supplied);raw=leaf_bytes(leaf,maximum=4096)
        if len(raw)!=(4096 if ordinal<count-1 else length-4096*ordinal):raise InvalidData()
        expected=make_leaf(HANDOFF_LEAF,'embedding-handoff-leaf','handoff_id',handoff_id,ordinal,raw,request_id=request_id,attempt_id=attempt_id)
        if leaf!=expected:raise InvalidData()
        blocks.append(raw)
    raw=b''.join(blocks)
    if len(blocks)!=count or len(raw)!=length or sha256(raw).hexdigest()!=root['payload_digest']:raise InvalidData()
    result=normalized(load(raw.decode(),40960),binding)
    if dump(result,40960).encode()!=raw:raise InvalidData()
    return result
