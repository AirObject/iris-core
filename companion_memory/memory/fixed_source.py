"""Complete reviewed text source format, independent of learning manifests.

This pure codec validates original event/world content and retained review
references. A valid source is not a review grant or permission to establish it.
No batch, Provider result, ingress reference or media owner is manufactured.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.ingress.events import isolate_event,plain
from companion_memory.persistence.schema import Value,InvalidValue
from companion_memory.persistence.semantic_records import ID,H,enum,record as schema,isolate,Record
from companion_memory.persistence.content_codec import encode_content,decode_content
from .formats import WORLD,check_world,record

HEADER=schema(format=enum('FIXED_REVIEWED_TEXT_V1'),source_id=ID,entry_id=ID,world_scope=WORLD,
              set_id=ID,member_id=ID,review_ref=ID,review_digest=H)


def isolate_fixed_source(value: object) -> Record:
    """Validate one exact source root and its original no-media event."""
    if type(value) not in (dict,MappingProxyType):raise InvalidValue()
    supplied=cast(dict[str,object],value)
    names={field.name for field in HEADER.fields}
    if set(supplied)!=names|{'event'}:raise InvalidValue()
    header=isolate(HEADER,{name:supplied[name] for name in names})
    submitted_event=supplied['event']
    event=isolate_event(plain(cast(Value,submitted_event)) if type(submitted_event) is MappingProxyType else submitted_event,2048)
    if event['media']:raise InvalidValue()
    check_world(header['world_scope'])
    result=MappingProxyType(dict(header)|{'event':event});encode_content(result,8192)
    return result


def decode_fixed_source(encoded: str) -> Record:
    """Require complete canonical source bytes; never repair a changed root."""
    if type(encoded) is not str:raise InvalidValue()
    result=isolate_fixed_source(decode_content(encoded.encode('utf-8'),8192))
    if encode_content(result,8192).decode('utf-8')!=encoded:raise InvalidValue()
    return result


def is_fixed_source(encoded: str) -> bool:
    """Select a closed source branch only after strict bounded JSON decoding."""
    value=decode_content(encoded.encode('utf-8'),8192)
    return type(value) in (dict,MappingProxyType) and cast(dict[str,object],value).get('format')=='FIXED_REVIEWED_TEXT_V1'


def fixed_digest(source: Record) -> str:
    """Bind all retained source fields, including the delegated review digest."""
    from hashlib import sha256
    return sha256(encode_content(source,8192)).hexdigest()


def check_fixed_anchor(anchor: Record,source: Record) -> None:
    """Verify an actual source event anchor without manufacturing ingress rows."""
    from .sources import SourcePayload,check_anchor
    event=record(source['event'])
    check_anchor(anchor,MappingProxyType({'role':'T','message_id':event['client_event_key']}),
                 SourcePayload(event,encode_content(event,2048),()))


from dataclasses import dataclass
from companion_memory.persistence import UnitOfWork


@dataclass(frozen=True,slots=True,init=False)
class PreparedFixedSource:
    """Memory-issued source proof, usable only in its original establishment UoW."""
    memory: object
    uow: UnitOfWork
    source: Record
    object_value: Record

    def __init__(self):raise TypeError('Fixed sources are issued by the memory owner.')
