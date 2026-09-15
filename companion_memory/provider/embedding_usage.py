"""Exact embedding liability with independent result and metering evidence.

Reported input tokens are charged at the full applicable input rate. Unknown
usage retains the original reservation; cached tokens and supplier bills are
never inferred. Simulated item accounting has a separate closed representation.
"""
from types import MappingProxyType
from companion_memory.persistence.semantic_records import Record,isolate,record,N,ID,B,enum,integer,number
from companion_memory.persistence.schema import SequenceSchema,InvalidValue
from .embedding_protocol import EmbeddingUsage
from .token_costs import rounded_cost

ITEM=record(item=enum('input'),quantity=(N,),price_numerator=N,price_denominator=integer(1,1000000),cost_atoms=(N,))
SCHEMA=record(format_version=integer(1,1),billing_mode=enum('TOKEN_METERED','SIMULATED'),currency=enum('CNY','TEST'),
    source=enum('PROVIDER_REPORTED','SIMULATED_REPORTED','UNAVAILABLE'),
    fields=record(input_tokens=(N,),total_tokens=(N,),input_items=(N,),embedding_dimensions=(N,),cache_read_tokens=(N,)),
    raw_usage=record(prompt_tokens=(N,),total_tokens=(N,),input_units=(N,)),
    coverage=enum('COMPLETE','UNAVAILABLE'),valid=B,cost_complete=B,known_cost_atoms=(N,),known_subtotal_atoms=N,
    held_atoms=N,estimated_cost_atoms=(N,),reported_cost_atoms=(N,),price_revision=(ID,),
    items=SequenceSchema(ITEM,1,1),quota_known=integer(0,0),quota_held=integer(0,0))


def usage(*,reserved: int,rate: int,price_revision: str|None,reported: EmbeddingUsage|None=None,
          simulated: bool=False,simulated_reported: bool=False,not_sent: bool=False) -> Record:
    """Build bounded metering evidence, without promoting an unknown response."""
    if any(type(n) is not int or n<0 for n in (reserved,rate)) or type(simulated) is not bool or type(not_sent) is not bool:
        raise InvalidValue()
    if (not_sent and reported is not None or simulated and reported is not None or simulated_reported and not simulated
            or type(simulated_reported) is not bool):raise InvalidValue()
    complete=not_sent or simulated_reported or reported is not None
    quantity=0 if not_sent else 1 if simulated_reported else reported.input_tokens if reported is not None else None
    cost=None if quantity is None else quantity if simulated else rounded_cost(quantity,rate)
    if cost is not None and cost>reserved:raise InvalidValue()
    fields={'input_tokens':reported.input_tokens if reported else None,'total_tokens':reported.total_tokens if reported else None,
        'input_items':1 if not not_sent and complete else None,'embedding_dimensions':1024 if not not_sent and complete else None,'cache_read_tokens':None}
    return validate({'format_version':1,'billing_mode':'SIMULATED' if simulated else 'TOKEN_METERED','currency':'TEST' if simulated else 'CNY',
        'source':'UNAVAILABLE' if not complete or not_sent else 'SIMULATED_REPORTED' if simulated else 'PROVIDER_REPORTED',
        'fields':fields,'raw_usage':{'prompt_tokens':fields['input_tokens'],'total_tokens':fields['total_tokens'],
            'input_units':1 if simulated_reported and not not_sent else None},'coverage':'COMPLETE' if complete else 'UNAVAILABLE',
        'valid':complete,'cost_complete':complete,'known_cost_atoms':cost,'known_subtotal_atoms':cost or 0,
        'held_atoms':0 if complete else reserved,'estimated_cost_atoms':cost,'reported_cost_atoms':None,'price_revision':price_revision,
        'items':({'item':'input','quantity':quantity,'price_numerator':1 if simulated else rate,
            'price_denominator':1 if simulated else 1000000,'cost_atoms':cost},),'quota_known':0,'quota_held':0})


def validate(value: object) -> Record:
    """Recompute every persisted monetary amount and its exact source relation."""
    result=isolate(SCHEMA,value,4096)
    fields=result['fields'];raw=result['raw_usage'];items=result['items']
    assert type(fields) is MappingProxyType and type(raw) is MappingProxyType and type(items) is tuple
    item=items[0];assert type(item) is MappingProxyType
    simulated=result['billing_mode']=='SIMULATED'
    if (result['currency']!=('TEST' if simulated else 'CNY') or fields['cache_read_tokens'] is not None
            or result['reported_cost_atoms'] is not None or result['quota_known']!=0 or result['quota_held']!=0):raise InvalidValue()
    q=item['quantity'];rate=item['price_numerator']
    assert type(rate) is int
    if item['price_denominator']!=(1 if simulated else 1000000):raise InvalidValue()
    if simulated and (rate!=1 or fields['input_tokens'] is not None or fields['total_tokens'] is not None or result['price_revision'] is not None):raise InvalidValue()
    if not simulated and (fields['input_tokens']!=raw['prompt_tokens'] or fields['total_tokens']!=raw['total_tokens']
            or raw['input_units'] is not None or fields['input_tokens']!=fields['total_tokens']):raise InvalidValue()
    not_sent=result['coverage']=='COMPLETE' and result['source']=='UNAVAILABLE'
    expected=0 if not_sent else raw['input_units'] if simulated else raw['prompt_tokens']
    if q!=expected:raise InvalidValue()
    cost=None if q is None else number(q) if simulated else rounded_cost(number(q),rate)
    if (item['cost_atoms']!=cost or result['known_cost_atoms']!=cost or result['estimated_cost_atoms']!=cost
            or result['known_subtotal_atoms']!=(cost or 0) or result['cost_complete']!=(cost is not None)
            or result['valid']!=(cost is not None) or result['coverage']!=('COMPLETE' if cost is not None else 'UNAVAILABLE')):raise InvalidValue()
    if cost is not None and result['held_atoms']!=0:raise InvalidValue()
    if not_sent:
        if any(v is not None for v in (*fields.values(),*raw.values())):raise InvalidValue()
    elif cost is not None:
        if fields['input_items']!=1 or fields['embedding_dimensions']!=1024 or result['source']!=('SIMULATED_REPORTED' if simulated else 'PROVIDER_REPORTED'):raise InvalidValue()
    elif result['source']!='UNAVAILABLE' or any(v is not None for v in (*fields.values(),*raw.values())):raise InvalidValue()
    return result
