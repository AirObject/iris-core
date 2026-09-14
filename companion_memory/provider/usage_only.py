"""Usage-only trial accounting keeps supplier money and quota explicitly unknown.

This policy has no monetary reservation. It cannot settle an older priced or
subscription account, and token coverage never changes cost_complete to true.
Only complete valid usage permits another explicitly authorized trial request.
"""
from typing import cast
from .values import Record, InvalidData, as_record, freeze
from .stored_schema import exact,integer,enum,boolean,identifier,USAGE_FIELDS
from .token_costs import add,quantity,InvalidAmount

BILLING='USAGE_ONLY_TRIAL'


def continuation_ready(usage: Record) -> bool:
    """Keep the usage-only exception separate from monetary completion."""
    if usage.get('format_version')==3 and usage.get('billing_mode')==BILLING:
        return (usage['valid'] is True and usage['coverage']=='COMPLETE' and usage['cost_complete'] is False
            and usage['held_atoms']==0 and usage['quota_held']==0)
    return usage['cost_complete'] is True


def normalize(observation,account: Record,profile: Record,*,not_sent: bool=False) -> Record:
    """Retain actual token fields; no supplier price or subscription debit exists."""
    from .chat_protocol import observe_usage
    if account['billing_mode']!=BILLING or account['cost_limit_atoms']!=0 or account['quota'] is not None:raise InvalidData()
    if not_sent:observation=observe_usage(None)
    fields=observation.fields
    valid=observation.valid
    if fields['input_tokens'] is not None and cast(int,fields['input_tokens'])>cast(int,profile['max_input_units']):valid=False
    if fields['output_tokens'] is not None and cast(int,fields['output_tokens'])>cast(int,profile['max_output_units']):valid=False
    complete=not_sent or bool(valid and observation.billing_covered)
    items=tuple({'item':name,'quantity':fields[field],'price_numerator':None,'price_denominator':None,'cost_atoms':None}
                for name,field in (('input','input_tokens'),('cached_input','cache_read_tokens'),('output','output_tokens')))
    return as_record(freeze({'format_version':3,'fields':fields,'raw_usage':observation.raw_usage,
        'source':'PROVIDER_REPORTED' if any(v is not None for v in fields.values()) else 'UNAVAILABLE',
        'coverage':'COMPLETE' if complete else 'PARTIAL' if any(v is not None for v in fields.values()) else 'UNAVAILABLE',
        'cost_complete':False,'known_cost_atoms':None,'known_subtotal_atoms':0,'held_atoms':0,
        'estimated_cost_atoms':None,'reported_cost_atoms':None,'price_revision':as_record(account['price'])['revision_ref'],
        'items':items,'valid':bool(valid),'cost_disagreement':False,'currency':account['currency'],'atom_scale':account['atom_scale'],
        'billing_mode':BILLING,'quota_known':None,'quota_held':0},4096,owned=True))


def validate_usage(item: Record,*,not_sent: bool=False) -> Record:
    """Validate UsageV3 without reinterpreting older billable usage records."""
    from .text_stored_schema import RAW_FIELDS
    if item['format_version']!=3 or item['billing_mode']!=BILLING:raise InvalidData()
    fields=exact(item['fields'],' '.join((*USAGE_FIELDS,'total_tokens')));raw=exact(item['raw_usage'],RAW_FIELDS)
    for value in (*fields.values(),*raw.values()):integer(value,True)
    for name in ('cache_write_tokens','input_items','embedding_dimensions','rerank_candidates','media_bytes','media_duration_ms'):
        if fields[name] is not None:raise InvalidData()
    for target,source in (('input_tokens','prompt_tokens'),('output_tokens','completion_tokens'),('total_tokens','total_tokens'),('cache_read_tokens','cached_tokens'),('reasoning_tokens','reasoning_tokens')):
        if fields[target]!=raw[source]:raise InvalidData()
    enum(item['currency'],('CNY','USD'));integer(item['atom_scale'],minimum=1000000,maximum=1000000)
    for name in ('valid','cost_complete','cost_disagreement'):boolean(item[name])
    if item['cost_complete'] is not False or item['cost_disagreement'] is not False:raise InvalidData()
    for name in ('known_cost_atoms','estimated_cost_atoms','reported_cost_atoms','quota_known'):
        if item[name] is not None:raise InvalidData()
    for name in ('known_subtotal_atoms','held_atoms','quota_held'):integer(item[name],minimum=0,maximum=0)
    identifier(item['price_revision']);enum(item['coverage'],('COMPLETE','PARTIAL','UNAVAILABLE'))
    observed=any(v is not None for v in fields.values())
    if item['source']!=('PROVIDER_REPORTED' if observed else 'UNAVAILABLE'):raise InvalidData()
    if item['coverage']=='UNAVAILABLE' and observed:raise InvalidData()
    incoming,outgoing,total=(fields[k] for k in ('input_tokens','output_tokens','total_tokens'))
    if item['valid']:
        try:
            if all(v is not None for v in (incoming,outgoing,total)) and add(quantity(incoming),quantity(outgoing))!=total:raise InvalidData()
            for part,whole in ((fields['cache_read_tokens'],incoming),(fields['reasoning_tokens'],outgoing)):
                if part is not None and whole is not None and quantity(part)>quantity(whole):raise InvalidData()
        except InvalidAmount:raise InvalidData() from None
    if not_sent:
        if any(v is not None for v in raw.values()):raise InvalidData()
    elif item['coverage']=='COMPLETE' and (not item['valid'] or any(v is None for v in (incoming,outgoing,total))):raise InvalidData()
    parts=item['items']
    if type(parts) is not tuple or len(parts)!=3:raise InvalidData()
    for part,(name,field) in zip(parts,(('input','input_tokens'),('cached_input','cache_read_tokens'),('output','output_tokens')),strict=True):
        part=exact(part,'item quantity price_numerator price_denominator cost_atoms')
        if part['item']!=name or part['quantity']!=fields[field] or any(part[k] is not None for k in ('price_numerator','price_denominator','cost_atoms')):raise InvalidData()
    return item


def validate_item(value: Record) -> None:
    """Token observations and an unknown supplier bill occupy distinct rows."""
    for name in ('object_id','attempt_id'):identifier(value[name])
    integer(value['revision']);integer(value['evidence_revision']);integer(value['format_version'],minimum=3,maximum=3)
    enum(value['item'],('input','cached_input','output','reported'));integer(value['quantity'],True)
    if any(value[k] is not None for k in ('price_numerator','price_denominator','cost_atoms')):raise InvalidData()
    if value['cost_complete'] is not False or value['known_subtotal_atoms']!=0:raise InvalidData()
    if value['item']=='reported':
        if value['quantity'] is not None or value['unit']!='CURRENCY_ATOM' or value['source']!='UNAVAILABLE':raise InvalidData()
    elif value['unit']!='TOKEN' or value['source']!=('PROVIDER_REPORTED' if value['quantity'] is not None else 'UNAVAILABLE'):raise InvalidData()
