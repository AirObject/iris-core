"""Versioned exact embedding material and authorization-partition cache identity.

Only a current authorized MEMORY is renderable as a document. Query text is
preserved byte-for-byte. Identity hashes contain no credentials or billing data.
"""
from hashlib import sha256
import json
from companion_memory.persistence.semantic_records import string
from companion_memory.persistence.schema import Value, InvalidValue, valid_identifier
from companion_memory.persistence.semantic_records import Record, identity
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence.schema import _json_value

RENDER_ID = 'TEXT_MEMORY_RENDER_V1'
RENDER_RESOURCE = b'TEXT_MEMORY_RENDER_V1\nDOCUMENT:body,world_scope,subject_ids(sorted ASCII),category,stance,occurred_range,applicable_range;JSON UTF-8 compact ordered keys\nQUERY:exact UTF-8\n'
RENDER_DIGEST = sha256(RENDER_RESOURCE).hexdigest()


def render_document(current: Record) -> bytes:
    """Render only defined memory fields, without provenance or model prefixes."""
    if current['kind'] != 'MEMORY': raise InvalidValue()
    content = record(current['content'])
    values: dict[str,object] = {}
    for key in ('body','world_scope','subject_ids','category','stance','occurred_range','applicable_range'):
        values[key] = sorted(string(v) for v in sequence(content[key])) if key == 'subject_ids' else _json_value(content[key])
    raw = json.dumps(values,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
    if not 1 <= len(raw) <= 8192: raise InvalidValue()
    return raw


def render_query(query_text: str) -> bytes:
    if type(query_text) is not str: raise InvalidValue()
    try: raw = query_text.encode('utf-8')
    except UnicodeError: raise InvalidValue() from None
    if not 1 <= len(raw) <= 512: raise InvalidValue()
    return raw


def cache_key(instance_id: str, partition_id: str, space_id: str, query_text: str) -> str:
    """Bind the original text and purpose to this instance's data authorization."""
    if not all(valid_identifier(v) for v in (instance_id,partition_id,space_id)): raise InvalidValue()
    raw = render_query(query_text)
    return identity('query-embedding',instance_id,partition_id,space_id,'QUERY',RENDER_ID,sha256(raw).hexdigest()).split(':',1)[1]


def space_identity(protocol: str, endpoint_deployment: str, model: str, deployment_epoch: str) -> str:
    """Use an explicit deployment epoch when a stable backend version is absent."""
    if protocol not in ('SIMULATED','ARK_CODING_DENSE_TEXT_V1') or not all(valid_identifier(v) for v in (model,deployment_epoch)):
        raise InvalidValue()
    return identity('embedding-space',protocol,endpoint_deployment,model,deployment_epoch,1024,
        RENDER_ID,RENDER_DIGEST,'DOCUMENT_QUERY_COMPATIBLE','EXACT_COSINE_F64_V1')
