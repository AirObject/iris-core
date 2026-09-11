"""Versioned complete configuration entry serialization owned by configuration.

Only known metadata carriers are interpreted. Stored definitions are registered
again and stored values are validated again before any runtime view is issued.
"""
from collections.abc import Callable
from dataclasses import fields
from decimal import Decimal,InvalidOperation
import json
from types import MappingProxyType
from typing import cast

from .definitions import (
    Bound, Declared, LiteralDefault, NoDefault, NotApplicable, ParameterDefinition,
    RangeDescriptor, Unbounded, MetadataValue, ParameterDefinitionInput,
)
from .snapshots import MissingValue, PresentValue, SnapshotEntry

_TAGS = {c.__name__:c for c in (Bound,Declared,LiteralDefault,NoDefault,NotApplicable,RangeDescriptor,Unbounded)}


def _encode(value: object) -> object:
    if value is None or type(value) in (bool,int,str):
        return value
    if type(value) is Decimal:
        return {'type':'decimal','value':str(value)}
    if type(value) is tuple or type(value) is list:
        return {'type':'array','value':[_encode(v) for v in value]}
    if type(value) is MappingProxyType or type(value) is dict:
        return {'type':'record','value':{k:_encode(v) for k,v in value.items()}}
    for name,cls in _TAGS.items():
        if type(value) is cls:
            return {'type':name,'value':{f.name:_encode(getattr(value,f.name)) for f in fields(value)}}
    raise ValueError('Unsupported configuration metadata carrier.')


def _decode(value: object, depth: int = 0) -> object:
    if depth>64:
        raise ValueError('Configuration structure is too deep.')
    if value is None or type(value) in (bool,int,str):
        return value
    if type(value) is not dict or set(value)!= {'type','value'}:
        raise ValueError('Invalid configuration metadata encoding.')
    name,body=value['type'],value['value']
    if type(name) is not str:
        raise ValueError('Invalid configuration metadata tag.')
    if name=='decimal':
        if type(body) is not str:
            raise ValueError('Invalid decimal metadata.')
        try:return Decimal(body)
        except InvalidOperation:raise ValueError('Invalid decimal metadata.') from None
    if name=='array' and type(body) is list:
        return [_decode(v,depth+1) for v in body]
    if type(body) is not dict:
        raise ValueError('Invalid configuration metadata record.')
    values={k:_decode(v,depth+1) for k,v in body.items()}
    if name=='record':
        return values
    cls=_TAGS.get(name)
    if cls is None or set(values)!={f.name for f in fields(cls)}:
        raise ValueError('Unsupported configuration declaration format.')
    # These inert metadata carriers are fully checked by registry registration.
    return cast(Callable[..., object],cls)(**values)


def encode_entry(entry: SnapshotEntry) -> str:
    """Store full metadata, complete effective value and provenance in one bounded row."""
    data={'version':1,'definition':{f.name:_encode(getattr(entry.definition,f.name)) for f in fields(ParameterDefinition)},
          'state':'MISSING' if type(entry.state) is MissingValue else 'PRESENT'}
    if type(entry.state) is PresentValue:
        data.update(value=_encode(entry.state.value),source=entry.state.source)
    encoded=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode())>8192:
        raise ValueError('Configuration entry exceeds its format bound.')
    return encoded


def decode_entry(encoded: str) -> tuple[ParameterDefinitionInput, bool, MetadataValue | None, str | None]:
    """Return a definition for normal registry validation plus its preserved state."""
    if type(encoded) is not str or len(encoded.encode())>8192:
        raise ValueError('Invalid configuration entry size.')
    data=decode_json(encoded)
    if type(data) is not dict or type(data.get('version')) is not int or data['version']!=1 or type(data.get('definition')) is not dict:
        raise ValueError('Unsupported configuration entry format.')
    present=data.get('state')=='PRESENT'
    if set(data)!=({'version','definition','state','value','source'} if present else {'version','definition','state'}) or (not present and data['state']!='MISSING'):
        raise ValueError('Invalid configuration entry state.')
    definition=cast(ParameterDefinitionInput,{k:_decode(v) for k,v in data['definition'].items()})
    source=data.get('source')
    if present and source not in ('EXPLICIT','DEFAULT'):
        raise ValueError('Invalid configuration value provenance.')
    return definition,present,cast(MetadataValue,_decode(data['value']) if present else None),source


def decode_json(encoded:str) -> object:
    """Reject duplicate persistent keys before interpreting any configuration."""
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate configuration metadata key.')
            result[key]=value
        return result
    return json.loads(encoded,object_pairs_hook=pairs)
