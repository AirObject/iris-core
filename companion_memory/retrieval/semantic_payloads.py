"""Retrieval-owned immutable input and original binary64 artifact leaves.

The work owner must establish its native authorization and original Provider
completion before using these pure encoders inside a result transaction.
"""
from collections.abc import Iterable
from hashlib import sha256
from typing import cast
from companion_memory.persistence.semantic_records import Record,make_leaf,leaf_bytes
from companion_memory.persistence.schema import InvalidValue,valid_identifier
from companion_memory.provider.embedding_material import normalized_result
from .semantic_schema import INPUT_LEAF,VECTOR_LEAF,validate
from .semantic_binary import vector_bytes,validate_vector


def input_leaves(work_id: str,text: str,purpose: str) -> tuple[Record,...]:
    """Freeze exact UTF-8; leaves may split code points but never change text."""
    if not valid_identifier(work_id) or type(text) is not str or purpose not in ('DOCUMENT','QUERY'): raise InvalidValue()
    raw=text.encode('utf-8')
    if not 1<=len(raw)<=(8192 if purpose=='DOCUMENT' else 512): raise InvalidValue()
    return tuple(make_leaf(INPUT_LEAF,'embedding-input-leaf','work_id',work_id,ordinal,raw[start:start+3072])
        for ordinal,start in enumerate(range(0,len(raw),3072)))


def restore_input(work_id: str,purpose: str,byte_count: int,digest: str,leaves: Iterable[object]) -> str:
    """Bound the complete original input and reject substitutions before use."""
    if not valid_identifier(work_id) or purpose not in ('DOCUMENT','QUERY') or type(byte_count) is not int or not 1<=byte_count<=(8192 if purpose=='DOCUMENT' else 512): raise InvalidValue()
    count=(byte_count+3071)//3072;blocks=[]
    for ordinal,value in enumerate(leaves):
        if ordinal>=count: raise InvalidValue()
        leaf=validate('embedding_input_leaf',value);raw=leaf_bytes(leaf,maximum=3072)
        if len(raw)!=(3072 if ordinal<count-1 else byte_count-3072*ordinal): raise InvalidValue()
        if leaf!=make_leaf(INPUT_LEAF,'embedding-input-leaf','work_id',work_id,ordinal,raw): raise InvalidValue()
        blocks.append(raw)
    raw=b''.join(blocks)
    if len(blocks)!=count or len(raw)!=byte_count or sha256(raw).hexdigest()!=digest: raise InvalidValue()
    text=raw.decode('utf-8')
    if tuple(input_leaves(work_id,text,purpose))!=tuple(make_leaf(INPUT_LEAF,'embedding-input-leaf','work_id',work_id,n,block) for n,block in enumerate(blocks)):
        raise InvalidValue()
    return text


def artifact_leaves(artifact_id: str,value: object,*,space_id: str,model_id: str) -> tuple[Record,Record]:
    """Keep all original coordinates and signed zero in exactly two leaves."""
    if not valid_identifier(artifact_id): raise InvalidValue()
    result=normalized_result(value,space_id=space_id,model_id=model_id)
    vectors=result['vectors'];assert type(vectors) is tuple
    vector=cast(tuple[float,...],vectors[0]);raw=vector_bytes(vector)
    return (make_leaf(VECTOR_LEAF,'embedding-vector-leaf','artifact_id',artifact_id,0,raw[:4096]),
            make_leaf(VECTOR_LEAF,'embedding-vector-leaf','artifact_id',artifact_id,1,raw[4096:]))


def restore_vector(artifact_id: str,digest: str,leaves: Iterable[object]) -> bytes:
    """Return only a complete digest-checked nonzero finite original vector."""
    if not valid_identifier(artifact_id): raise InvalidValue()
    blocks=[]
    for ordinal,value in enumerate(leaves):
        if ordinal>1: raise InvalidValue()
        leaf=validate('embedding_vector_leaf',value);raw=leaf_bytes(leaf,maximum=4096,exact=4096)
        if leaf!=make_leaf(VECTOR_LEAF,'embedding-vector-leaf','artifact_id',artifact_id,ordinal,raw): raise InvalidValue()
        blocks.append(raw)
    raw=b''.join(blocks)
    if len(blocks)!=2 or sha256(raw).hexdigest()!=digest: raise InvalidValue()
    validate_vector(raw)
    return raw
