"""Lossless bounded leaves for complete persona candidates and model reviews.

Each leaf is canonical JSON with its identity, ordinal and digest included in
the byte budget. Joining requires a contiguous complete digest-bound manifest;
the codec never substitutes a current fact or a newer model result.
"""
from hashlib import sha256
from types import MappingProxyType
from typing import cast

from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.persistence.schema import BoundedTextSchema, InvalidValue, SequenceSchema
from companion_memory.persistence.semantic_records import Record, ID, H, V, integer, isolate, record

LEAF = record(schema_version=V, candidate_id=ID, ordinal=integer(0,16), text=BoundedTextSchema(1024), digest=H)
MANIFEST = record(schema_version=V, candidate_id=ID, byte_count=integer(1,16384),
    leaf_count=integer(1,17), digest=H, leaf_digests=SequenceSchema(H,1,17))


def split_candidate(candidate_id: str, raw: bytes) -> tuple[Record,tuple[Record,...]]:
    """Split an already validated UTF-8 envelope; boundary slack permits 17 leaves."""
    if type(raw) is not bytes or not 1<=len(raw)<=16384:
        raise InvalidValue()
    try:
        text=raw.decode('utf-8',errors='strict')
    except UnicodeError:
        raise InvalidValue() from None
    # Chunks count encoded bytes, not Python characters. Every C0 byte can
    # expand sixfold in JSON and still fits the complete 8192-byte leaf.
    chunks:list[str]=[]; start=0; used=0
    for index,char in enumerate(text):
        size=len(char.encode('utf-8'))
        if used+size>1024:
            chunks.append(text[start:index]);start=index;used=0
        used+=size
    chunks.append(text[start:])
    if len(chunks)>17:raise InvalidValue()
    leaves=tuple(isolate(LEAF,{'schema_version':1,'candidate_id':candidate_id,'ordinal':ordinal,
        'text':chunk,'digest':sha256(chunk.encode()).hexdigest()},8192) for ordinal,chunk in enumerate(chunks))
    manifest=isolate(MANIFEST,{'schema_version':1,'candidate_id':candidate_id,'byte_count':len(raw),
        'leaf_count':len(leaves),'digest':sha256(raw).hexdigest(),'leaf_digests':tuple(leaf['digest'] for leaf in leaves)},8192)
    return manifest,leaves


def join_candidate(manifest: object, leaves: tuple[Record,...]) -> bytes:
    """Require original complete ordering and all digests before returning bytes."""
    root=isolate(MANIFEST,manifest,8192)
    if type(leaves) is not tuple or len(leaves)!=root['leaf_count'] or len(cast(tuple,root['leaf_digests']))!=len(leaves):
        raise InvalidValue()
    chunks=[]
    for ordinal,raw in enumerate(leaves):
        leaf=isolate(LEAF,raw,8192);text=cast(str,leaf['text']);encoded=text.encode()
        if (leaf['candidate_id']!=root['candidate_id'] or leaf['ordinal']!=ordinal
                or sha256(encoded).hexdigest()!=leaf['digest']
                or leaf['digest']!=cast(tuple,root['leaf_digests'])[ordinal]):raise InvalidValue()
        chunks.append(encoded)
    joined=b''.join(chunks)
    if len(joined)!=root['byte_count'] or sha256(joined).hexdigest()!=root['digest']:raise InvalidValue()
    return joined
