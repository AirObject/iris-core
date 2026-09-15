"""Token observations for a user-allocated trial without monetary admission.

Incomplete metering does not invalidate a separately verified vector. Null money
and quota mean unobserved charges, while zero local held amounts never release
an UNKNOWN request. Original raw responses belong to the bounded wire evidence.
"""
from dataclasses import replace
from types import MappingProxyType
from companion_memory.persistence.semantic_records import Record,N,enum,integer,isolate,record
from companion_memory.persistence.schema import Field,RecordSchema,SequenceSchema,InvalidValue
from .embedding_usage import SCHEMA as MONEY_SCHEMA
from .embedding_protocol import _parse
from .values import InvalidData

NULL = RecordSchema(())
ITEM = record(item=enum('input'),quantity=(N,),price_numerator=(NULL,),price_denominator=(NULL,),cost_atoms=(NULL,))
_NULL_FIELDS = ('known_cost_atoms','estimated_cost_atoms','reported_cost_atoms','price_revision','quota_known')
SCHEMA = RecordSchema(tuple(
    replace(f,schema=integer(2,2)) if f.name=='format_version' else
    replace(f,schema=enum('USAGE_ONLY_TRIAL')) if f.name=='billing_mode' else
    replace(f,schema=enum('CNY')) if f.name=='currency' else
    replace(f,schema=enum('PROVIDER_REPORTED','UNAVAILABLE')) if f.name=='source' else
    replace(f,schema=enum('COMPLETE','PARTIAL','UNAVAILABLE')) if f.name=='coverage' else
    replace(f,schema=NULL,nullable=True) if f.name in _NULL_FIELDS else
    replace(f,schema=SequenceSchema(ITEM,1,1)) if f.name=='items' else f
    for f in MONEY_SCHEMA.fields) + (Field('observation_reason',enum('OK','MISSING','INVALID','UNSUPPORTED_FIELDS')),))


def observe(raw:bytes|None=None) -> Record:
    """Preserve exact known integer observations, never repair missing values."""
    reported=None
    if raw is not None:
        try:reported=_parse(raw).get('usage')
        except InvalidData:pass
    incoming=total=None;reason='MISSING';coverage='UNAVAILABLE'
    if type(reported) is MappingProxyType:
        def token(name):
            value=reported.get(name)
            return value if type(value) is int and 0<=value<=2**63-1 else None
        incoming=token('prompt_tokens');total=token('total_tokens')
        invalid=any(k in reported and reported[k] is not None and token(k) is None for k in ('prompt_tokens','total_tokens'))
        invalid=invalid or incoming is not None and total is not None and incoming!=total
        unsupported=bool(set(reported)-{'prompt_tokens','total_tokens'})
        if unsupported:reason='UNSUPPORTED_FIELDS';coverage='PARTIAL' if incoming is not None or total is not None else 'UNAVAILABLE'
        elif invalid:reason='INVALID'
        elif incoming is not None and total is not None:reason='OK';coverage='COMPLETE'
        elif incoming is not None or total is not None:reason='MISSING';coverage='PARTIAL'
    elif reported is not None:reason='INVALID'
    return validate({'format_version':2,'billing_mode':'USAGE_ONLY_TRIAL','currency':'CNY',
        'source':'PROVIDER_REPORTED' if incoming is not None or total is not None else 'UNAVAILABLE',
        'fields':{'input_tokens':incoming,'total_tokens':total,'input_items':None,'embedding_dimensions':None,'cache_read_tokens':None},
        'raw_usage':{'prompt_tokens':incoming,'total_tokens':total,'input_units':None},
        'coverage':coverage,'valid':reason=='OK','observation_reason':reason,'cost_complete':False,
        'known_cost_atoms':None,'known_subtotal_atoms':0,'held_atoms':0,'estimated_cost_atoms':None,'reported_cost_atoms':None,'price_revision':None,
        'items':({'item':'input','quantity':incoming,'price_numerator':None,'price_denominator':None,'cost_atoms':None},),
        'quota_known':None,'quota_held':0})


def validate(value:object) -> Record:
    """Reject zero-cost claims and contradictory stored observation relations."""
    result=isolate(SCHEMA,value,4096)
    if (any(result[k] is not None for k in _NULL_FIELDS) or result['cost_complete'] is not False
            or any(result[k]!=0 for k in ('known_subtotal_atoms','held_atoms','quota_held'))):raise InvalidValue()
    fields=result['fields'];raw=result['raw_usage'];items=result['items']
    assert type(fields) is MappingProxyType and type(raw) is MappingProxyType and type(items) is tuple
    item=items[0];assert type(item) is MappingProxyType
    if (fields['input_tokens']!=raw['prompt_tokens'] or fields['total_tokens']!=raw['total_tokens']
            or any(fields[k] is not None for k in ('input_items','embedding_dimensions','cache_read_tokens'))
            or raw['input_units'] is not None or item['quantity']!=fields['input_tokens']
            or any(item[k] is not None for k in ('price_numerator','price_denominator','cost_atoms'))):raise InvalidValue()
    incoming=fields['input_tokens'];total=fields['total_tokens'];reason=result['observation_reason']
    observed=incoming is not None or total is not None
    if result['source']!=('PROVIDER_REPORTED' if observed else 'UNAVAILABLE'):raise InvalidValue()
    if result['valid']!=(reason=='OK') or (result['coverage']=='COMPLETE')!=(reason=='OK'):raise InvalidValue()
    if reason=='OK' and (incoming is None or incoming!=total):raise InvalidValue()
    if reason=='MISSING' and (incoming is not None and total is not None
            or result['coverage']!=('PARTIAL' if observed else 'UNAVAILABLE')):raise InvalidValue()
    if reason=='INVALID' and result['coverage']!='UNAVAILABLE':raise InvalidValue()
    if reason=='UNSUPPORTED_FIELDS' and result['coverage']!=('PARTIAL' if observed else 'UNAVAILABLE'):raise InvalidValue()
    return result
