"""Frozen three-segment material using a versioned reversible synthetic protocol.

Material construction never fetches undeclared sources, expands quotations or
changes target membership. Profile and byte limits are checked by the caller.
"""
import base64
from dataclasses import dataclass
from types import MappingProxyType

from companion_memory.configuration.material_contracts import SYSTEM_TEXT, HEADER_KEYS, FORMAT_HEADER, FORMAT_TAIL
from companion_memory.ingress.events import canonical_event, decode_event
from companion_memory.persistence import valid_utc
from companion_memory.persistence.schema import InvalidValue, Value, valid_identifier


@dataclass(frozen=True, slots=True)
class MaterialRecord:
    """One complete immutable source and its frozen role/reference association."""
    role: str
    message_id: str
    entry_seq: int
    received_at_utc: str
    event: MappingProxyType[str,Value]


def build_material(identities: tuple[str,...], records: tuple[MaterialRecord,...]) -> tuple[MappingProxyType[str,str],...]:
    """Return exactly two complete Provider messages, without hidden separators."""
    if len(identities) != len(HEADER_KEYS) or not all(valid_identifier(i) for i in identities) or len(records)>128:
        raise InvalidValue()
    text = FORMAT_HEADER+''.join(k+'='+v+'\n' for k,v in zip(HEADER_KEYS,identities))
    previous_role = -1
    previous_seq = 0
    seen: set[str] = set()
    for row in records:
        if type(row) is not MaterialRecord or row.role not in ('H','T','R') or not valid_identifier(row.message_id) or type(row.entry_seq) is not int or not 0<row.entry_seq<2**63 or not valid_utc(row.received_at_utc):
            raise InvalidValue()
        role_index = ('H','T','R').index(row.role)
        if role_index < previous_role or row.entry_seq <= previous_seq or row.message_id in seen:
            raise InvalidValue()
        previous_role,previous_seq = role_index,row.entry_seq
        seen.add(row.message_id)
        encoded = canonical_event(row.event)
        text += row.role+'|'+row.message_id+'|'+str(row.entry_seq)+'|'+row.received_at_utc+'|'+base64.b64encode(encoded).decode('ascii')+'\n'
    return (MappingProxyType({'role':'SYSTEM','text':SYSTEM_TEXT}),MappingProxyType({'role':'USER','text':text+FORMAT_TAIL}))


def decode_material(messages: tuple[MappingProxyType[str,str],...]) -> tuple[tuple[str,...],tuple[MaterialRecord,...]]:
    """Strictly decode the same format, requiring byte-for-byte canonical roundtrip."""
    if len(messages)!=2 or messages[0] != {'role':'SYSTEM','text':SYSTEM_TEXT} or set(messages[1]) != {'role','text'} or messages[1]['role']!='USER':
        raise InvalidValue()
    lines=messages[1]['text'].splitlines(keepends=True)
    if len(lines)<9 or lines[0]!=FORMAT_HEADER or lines[-1]!=FORMAT_TAIL:
        raise InvalidValue()
    identities=[]
    for key,line in zip(HEADER_KEYS,lines[1:8]):
        if not line.startswith(key+'=') or not line.endswith('\n'):
            raise InvalidValue()
        identities.append(line[len(key)+1:-1])
    records=[]
    try:
        for line in lines[8:-1]:
            role,identifier,seq,time,payload=line[:-1].split('|')
            event=decode_event(base64.b64decode(payload,validate=True),8192)
            records.append(MaterialRecord(role,identifier,int(seq),time,event))
        result=tuple(identities),tuple(records)
        if build_material(*result)!=messages:
            raise InvalidValue()
        return result
    except (ValueError,UnicodeError):
        raise InvalidValue() from None
