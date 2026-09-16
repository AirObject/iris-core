"""Closed mixed-capability Provider roots in one independent durable format.

A capability, task role and billing mode select a complete bounded variant. All
monetary arithmetic uses integer atoms; allocated usage keeps unknown charges
null. Existing generation and embedding ledger formats remain unchanged.
"""
from dataclasses import replace
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import Field,RecordSchema,SequenceSchema,ScalarSchema,InvalidValue
from companion_memory.persistence.semantic_records import ID,N,P,B,H,Record,enum,record,isolate,number,string
from companion_memory.configuration.daily_schema import ACCOUNT,GENERATION_PROFILE,IMAGE_PROFILE,EMBEDDING_PROFILE
from .embedding_stored_schema import schemas as embedding_schemas
from .embedding_allocated_usage import SCHEMA as ALLOCATED_USAGE,validate as validate_allocated
from .stored_schema import USAGE_FIELDS,TERMINALS,timestamp
from .text_stored_schema import RAW_FIELDS,validate_usage
from .token_costs import rounded_cost

ROLES=('LEARNING','PERSONA','GOAL_DEDUP','MEDIA','EMBEDDING_DOCUMENT','EMBEDDING_QUERY')
OWNERS={'LEARNING':'cognition','PERSONA':'self_model','GOAL_DEDUP':'goals','MEDIA':'media','EMBEDDING_DOCUMENT':'retrieval','EMBEDDING_QUERY':'retrieval'}
VERSION=ScalarSchema('integer',5,5)
ITEM=record(item=enum('input','cached_input','output'),quantity=(N,),price_numerator=N,price_denominator=ScalarSchema('integer',1000000,1000000),cost_atoms=(N,))

def token_usage(deepseek:bool) -> RecordSchema:
    """Attach the complete monetary observation, including original cache fields."""
    raw=RAW_FIELDS.split()+(['prompt_cache_hit_tokens','prompt_cache_miss_tokens'] if deepseek else [])
    fields=record(**{name:(N,) for name in (*USAGE_FIELDS,'total_tokens')})
    return record(format_version=ScalarSchema('integer',4 if deepseek else 2,4 if deepseek else 2),fields=fields,
        raw_usage=record(**{name:(N,) for name in raw}),source=enum('PROVIDER_REPORTED','LOCALLY_ESTIMATED','UNAVAILABLE'),
        coverage=enum('COMPLETE','PARTIAL','UNAVAILABLE'),cost_complete=B,known_cost_atoms=(N,),known_subtotal_atoms=N,held_atoms=N,
        estimated_cost_atoms=(N,),reported_cost_atoms=(N,),price_revision=ID,items=SequenceSchema(ITEM,3,3),valid=B,cost_disagreement=B,
        currency=enum('CNY'),atom_scale=ScalarSchema('integer',1000000,1000000),billing_mode=enum('TOKEN_METERED'),quota_known=(N,),quota_held=N)


def trial_usage(deepseek:bool) -> RecordSchema:
    original=token_usage(deepseek)
    changes={
        'format_version':Field('format_version',ScalarSchema('integer',5,5)),
        'billing_mode':Field('billing_mode',enum('USAGE_ONLY_TRIAL')),
        'price_revision':Field('price_revision',ID,nullable=True),
        'items':Field('items',SequenceSchema(record(item=enum('input','cached_input','output'),quantity=(N,),price_numerator=(N,),price_denominator=(P,),cost_atoms=(N,)),3,3))}
    return RecordSchema(tuple(changes.get(f.name,f) for f in original.fields))


def _replace(schema:RecordSchema,**fields:object) -> RecordSchema:
    result=[]
    for field in schema.fields:
        replacement=fields.pop(field.name,None)
        result.append(replacement if type(replacement) is Field else field)
    result.extend(fields.values())
    if any(type(field) is not Field for field in result):raise InvalidValue()
    return RecordSchema(tuple(cast(list[Field],result)))


def schemas() -> dict[str,tuple[RecordSchema,...]]:
    """All variants are part of the static repository declaration and signature."""
    original=embedding_schemas(False,usage_only=True)
    result={}
    variants=[]
    for profile,capability,roles in ((GENERATION_PROFILE,'GENERATION',ROLES[:3]),(IMAGE_PROFILE,'MEDIA_UNDERSTANDING',('MEDIA',)),(EMBEDDING_PROFILE,'EMBEDDING',ROLES[4:])):
        evidence=record(profile=profile,account=ACCOUNT,request_timeout_ms=N,retry_delay_ms=N,request_max_bytes=N,result_max_bytes=N)
        variants.append(_replace(original['requests'],execution_evidence=Field('execution_evidence',evidence),
            capability=Field('capability',enum(capability)),task_role=Field('task_role',enum(*roles)),
            result_owner=Field('result_owner',enum(*(dict.fromkeys(OWNERS[r] for r in roles)))),
            format_version=Field('format_version',VERSION),fingerprint_version=Field('fingerprint_version',VERSION),
            outcome=Field('outcome',enum(*TERMINALS),nullable=True)))
    result['requests']=tuple(variants)
    result['attempts']=tuple(_replace(original['attempts'],usage=Field('usage',usage),capability=Field('capability',enum(*capabilities)),
        wire_protocol=Field('wire_protocol',enum(*protocols)),logical_outcome=Field('logical_outcome',enum(*TERMINALS),nullable=True))
        for usage,capabilities,protocols in ((ALLOCATED_USAGE,('EMBEDDING',),('ARK_CODING_DENSE_TEXT_V1',)),
            (token_usage(True),('GENERATION','MEDIA_UNDERSTANDING'),('DEEPSEEK_CHAT_JSON_V1','DEEPSEEK_IMAGE_JSON_V1')),
            (token_usage(False),('MEDIA_UNDERSTANDING',),('MINIMAX_IMAGE_JSON_V1',)),
            (trial_usage(True),('GENERATION','MEDIA_UNDERSTANDING'),('DEEPSEEK_CHAT_JSON_V1','DEEPSEEK_IMAGE_JSON_V1')),
            (trial_usage(False),('MEDIA_UNDERSTANDING',),('MINIMAX_IMAGE_JSON_V1',))))
    for name in ('budget_windows','reservations','cost_items'):
        allocated=_replace(original[name],format_version=Field('format_version',VERSION))
        if name=='budget_windows':allocated=_replace(allocated,policy=Field('policy',ACCOUNT))
        metered=_replace(allocated,billing_mode=Field('billing_mode',enum('TOKEN_METERED')),quota_known=Field('quota_known',N)) if name!='cost_items' else _replace(allocated,
            billing_mode=Field('billing_mode',enum('TOKEN_METERED')),item=Field('item',enum('input','cached_input','output')),
            price_numerator=Field('price_numerator',N),price_denominator=Field('price_denominator',P))
        trial=(_replace(allocated,item=Field('item',enum('input','cached_input','output'))) if name=='cost_items' else allocated)
        result[name]=(allocated,metered,trial) if name=='cost_items' else (allocated,metered)
    result['handoffs']=(_replace(original['handoffs'],format_version=Field('format_version',VERSION)),)
    return result

LAYOUTS=schemas()

def validate(table:str,value:object) -> Record:
    """Reject incomplete variants and recompute each stored liability on both ends."""
    if table not in LAYOUTS:raise InvalidValue()
    if type(value) is MappingProxyType and 'payload' in value:
        if table!='handoffs' or value['payload']!='':raise InvalidValue()
        value={k:v for k,v in value.items() if k!='payload'}
    result=None
    for schema in LAYOUTS[table]:
        try:result=isolate(schema,value,4096 if table=='handoffs' else 8192)
        except InvalidValue:continue
        break
    if result is None:raise InvalidValue()
    for name in ('created_at','updated_at'):
        if name in result:timestamp(result[name])
    if 'updated_at' in result and string(result['updated_at'])<string(result['created_at']):raise InvalidValue()
    if table=='requests':
        evidence=cast(Record,result['execution_evidence']);profile=cast(Record,evidence['profile']);account=cast(Record,evidence['account'])
        role=string(result['task_role']);embedding=result['capability']=='EMBEDDING'
        if (role!=profile['material_role'] or result['result_owner']!=OWNERS[role] or result['caller_module']!=OWNERS[role]
                or result['profile_id']!=profile['profile_id'] or result['account_id']!=account['account_id']
                or profile['account_id']!=account['account_id'] or profile['capability']!=result['capability']
                or profile['billing_mode']!=account['billing_mode'] or result['extension_id'] is not None
                or result['attempt_count'] not in (0,1) or evidence['retry_delay_ms']!=0
                or evidence['request_timeout_ms']!=60000
                or evidence['request_max_bytes']!=(65536 if embedding else 2097152 if role=='MEDIA' else 1048576)
                or evidence['result_max_bytes']!=40960
                or (result['phase']=='TERMINAL')!=(result['outcome'] is not None)):
            raise InvalidValue()
        if account['billing_mode']=='USAGE_ONLY_TRIAL':
            if account['price'] is not None or account['cost_limit_atoms'] is not None or result['price_revision'] is not None:raise InvalidValue()
        elif type(account['price']) is not MappingProxyType or result['price_revision']!=account['price']['revision_ref']:raise InvalidValue()
    elif table=='attempts':
        usage=cast(Record,result['usage'])
        # The original no-send observation is committed before the separate
        # terminal transaction. Its PREPARED row already carries exact zero
        # liability, while a still-unobserved registration remains conservative.
        not_sent=result['state']=='NOT_SENT' or (result['state']=='PREPARED' and result['confirmed_started'] is False and result['evidence_revision']==1)
        if result['capability']=='EMBEDDING':validate_allocated(usage)
        elif usage['billing_mode']=='USAGE_ONLY_TRIAL':
            from .daily_usage import validate as validate_daily
            validate_daily(usage,deepseek=string(result['wire_protocol']).startswith('DEEPSEEK_'),not_sent=not_sent)
        else:validate_usage(usage,not_sent=not_sent)
        terminal=result['state'] in ('COMPLETED','NOT_SENT')
        if result['ordinal']!=1 or terminal!=(result['logical_outcome'] is not None):raise InvalidValue()
        if result['logical_outcome']=='SUCCEEDED':
            if result['state']!='COMPLETED' or result['terminal_error'] is not None or result['result_fingerprint'] is None or result['handoff_id'] is None:raise InvalidValue()
        elif terminal and (result['terminal_error'] is None or result['first_error'] is None):raise InvalidValue()
        elif not terminal and result['terminal_error'] is not None:raise InvalidValue()
    elif table in ('budget_windows','reservations','cost_items'):
        trial=result['billing_mode']=='USAGE_ONLY_TRIAL'
        if table=='budget_windows':
            policy=cast(Record,result['policy'])
            if policy['billing_mode']!=result['billing_mode'] or policy['account_id']!=result['account_id'] or policy['window_id']!=result['window_id'] or number(result['attempt_count'])>number(policy['attempt_limit']):raise InvalidValue()
        for name in ('quota_reserved','quota_known','quota_held'):
            if name in result and result[name]!=(None if trial and name=='quota_known' else 0):raise InvalidValue()
        if trial:
            for name in ('known_subtotal_atoms','held_atoms','reserved_atoms'):
                if name in result and result[name]!=0:raise InvalidValue()
            no_send=result.get('cost_complete') is True
            for name in ('known_cost_atoms','cost_atoms'):
                if name in result and result[name]!=(0 if no_send else None):raise InvalidValue()
            for name in ('price_numerator','price_denominator'):
                if name in result and result[name] is not None:raise InvalidValue()
            if table=='cost_items' and result['source']!=('LOCALLY_ESTIMATED' if no_send else 'UNAVAILABLE'):raise InvalidValue()
        elif table=='reservations':
            if result['cost_complete']:
                if result['held_atoms']!=0 or result['known_cost_atoms']!=result['known_subtotal_atoms']:raise InvalidValue()
            elif result['known_cost_atoms'] is not None or result['known_subtotal_atoms']!=0 or result['held_atoms']!=result['reserved_atoms']:raise InvalidValue()
        elif table=='cost_items':
            cost=None if result['cost_atoms'] is None else rounded_cost(number(result['quantity']),number(result['price_numerator']))
            if (result['cost_atoms']!=cost or result['known_subtotal_atoms']!=(cost or 0) or result['cost_complete']!=(cost is not None)
                    or result['price_denominator']!=1000000 or result['source']!=('UNAVAILABLE' if cost is None else 'LOCALLY_ESTIMATED')):raise InvalidValue()
    elif table=='handoffs':
        payload=cast(Record,result['embedding_payload']);cleanup=cast(Record,result['embedding_cleanup'])
        count=number(payload['leaf_count']);size=number(payload['byte_count'])
        if count!=(size+4095)//4096 or (cleanup['state']=='HELD')!=(cleanup['received_receipt'] is None):raise InvalidValue()
        if cleanup['retired_through'] is not None and not 0<=number(cleanup['retired_through'])<count:raise InvalidValue()
        if cleanup['state']=='RETIRED' and cleanup['retired_through']!=count-1:raise InvalidValue()
    return result
