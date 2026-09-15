"""Closed embedding extension of the original six Provider ledger roots.

The static format is independent of generation and legacy simulation. Every
original request identity, reservation and terminal remains durable; only result
leaves may retire after the receiving owner's original receipt is confirmed.
"""
from types import MappingProxyType
from dataclasses import replace
from companion_memory.persistence.semantic_records import ID,N,P,B,H,Record,Schema,record,enum,isolate,number
from companion_memory.persistence.schema import RecordSchema,BoundedTextSchema,SequenceSchema,InvalidValue,Field
from companion_memory.configuration.semantic_schema import ACCOUNT,PROFILE,OFFLINE_ACCOUNT,OFFLINE_PROFILE,USAGE_ACCOUNT,USAGE_PROFILE
from .embedding_schema import PAYLOAD,CLEANUP
from .embedding_usage import SCHEMA as USAGE,validate as validate_usage
from .stored_schema import timestamp

TIME=BoundedTextSchema(32)
CAUSE=record(code=ID,field=ID,reason=ID)
ATTRIBUTION=record(run_id=ID,entry_ids=SequenceSchema(ID,0,8),parent_request_id=(ID,),trace_id=(ID,),batch_id=(ID,),dream_run_id=(ID,),prompt_revision=(ID,))
OUTCOME=enum('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','CONFIGURATION_REJECTED','PAUSED_BUDGET','MODE_BLOCKED')


def schemas(simulated: bool, *, usage_only:bool=False) -> dict[str,RecordSchema]:
    """Include complete account and profile schemas in the selected row shape."""
    def root(**fields: Schema|tuple[Schema]) -> RecordSchema:
        return record(object_id=ID,revision=P,**fields)
    if usage_only and simulated:raise InvalidValue()
    from .embedding_allocated_usage import SCHEMA as ALLOCATED_USAGE
    evidence=record(profile=USAGE_PROFILE if usage_only else OFFLINE_PROFILE if simulated else PROFILE,account=USAGE_ACCOUNT if usage_only else OFFLINE_ACCOUNT if simulated else ACCOUNT,
        request_timeout_ms=N,retry_delay_ms=N,request_max_bytes=N,result_max_bytes=N)
    result = {
        'requests':root(caller_module=ID,caller_scope=ID,extension_id=(ID,),operation_key=ID,capability=enum('EMBEDDING'),
            task_role=enum('EMBEDDING_DOCUMENT','EMBEDDING_QUERY'),result_owner=enum('retrieval'),profile_id=ID,account_id=ID,
            created_at=TIME,updated_at=TIME,format_version=N,fingerprint_version=N,attribution=ATTRIBUTION,
            source=enum('SIMULATED' if simulated else 'REMOTE_PROVIDER'),configuration_origin=enum('PERSISTED_CONFIGURATION'),
            config_snapshot_id=ID,profile_revision=ID,price_revision=(ID,),execution_evidence=evidence,fingerprint=H,
            phase=enum('OPEN','TERMINAL','REMOTE_RESULT_UNKNOWN'),outcome=(OUTCOME,),first_error=(CAUSE,),
            attempt_count=N,ever_unknown=B,handoff_id=(ID,)),
        'attempts':root(request_id=ID,ordinal=P,state=enum('PREPARED','COMPLETED','NOT_SENT','REMOTE_RESULT_UNKNOWN'),
            logical_outcome=(OUTCOME,),account_id=ID,profile_id=ID,capability=enum('EMBEDDING'),
            wire_protocol=enum('SIMULATED' if simulated else 'ARK_CODING_DENSE_TEXT_V1'),execution_owner_id=ID,
            created_at=TIME,updated_at=TIME,adapter_duration_ms=(N,),handoff_id=(ID,),confirmed_started=(B,),ever_unknown=B,
            first_error=(CAUSE,),terminal_error=(CAUSE,),usage=ALLOCATED_USAGE if usage_only else USAGE,result_fingerprint=(H,),evidence_revision=N),
        'budget_windows':root(account_id=ID,window_id=ID,policy=USAGE_ACCOUNT if usage_only else OFFLINE_ACCOUNT if simulated else ACCOUNT,attempt_count=N,
            known_subtotal_atoms=N,held_atoms=N,risk_state=enum('CLEAR','RESERVATION_OVERRUN'),format_version=N,
            quota_reserved=N,quota_known=N,quota_held=N),
        'reservations':root(attempt_id=ID,account_id=ID,budget_id=ID,reserved_atoms=N,known_subtotal_atoms=N,held_atoms=N,
            known_cost_atoms=(N,),cost_complete=B,format_version=N,quota_reserved=N,quota_known=N,quota_held=N),
        'cost_items':root(attempt_id=ID,item=enum('input'),cost_atoms=(N,),evidence_revision=N,
            source=enum('LOCALLY_ESTIMATED','UNAVAILABLE'),unit=enum('ITEM' if simulated else 'TOKEN'),known_subtotal_atoms=N,
            cost_complete=B,format_version=N,quantity=(N,),price_numerator=N,price_denominator=P),
        'handoffs':root(request_id=ID,owner_id=ID,checksum=H,source=enum('SIMULATED' if simulated else 'REMOTE_PROVIDER'),
            artifact_id=ID,format_version=N,created_at=TIME,embedding_payload=PAYLOAD,embedding_cleanup=CLEANUP),
    }
    if usage_only:
        for table in ('budget_windows','reservations','cost_items'):
            result[table]=RecordSchema(tuple(replace(f,schema=RecordSchema(()),nullable=True)
                if f.name in ('quota_known','price_numerator','price_denominator') else f for f in result[table].fields)
                +(Field('billing_mode',enum('USAGE_ONLY_TRIAL')),))
    return result


def validate(table: str,value: object,*,simulated: bool,usage_only:bool=False) -> Record:
    """Check full roots and recompute liabilities before any use or mutation."""
    layouts=schemas(simulated,usage_only=usage_only)
    if table not in layouts:raise InvalidValue()
    if type(value) is MappingProxyType and 'payload' in value:
        if table!='handoffs' or value['payload']!='':raise InvalidValue()
        value={k:v for k,v in value.items() if k!='payload'}
    result=isolate(layouts[table],value,4096 if table=='handoffs' else 8192)
    for name in ('created_at','updated_at'):
        if name in result:timestamp(result[name])
    if 'format_version' in result and result['format_version']!=(4 if usage_only else 3):raise InvalidValue()
    for name in ('quota_reserved','quota_known','quota_held'):
        if name in result and result[name]!=(None if usage_only and name=='quota_known' else 0):raise InvalidValue()
    if usage_only:
        for name in ('known_subtotal_atoms','held_atoms','reserved_atoms'):
            if name in result and result[name]!=0:raise InvalidValue()
        for name in ('known_cost_atoms','cost_atoms','price_numerator','price_denominator','price_revision'):
            if name in result and result[name] is not None:raise InvalidValue()
        if 'cost_complete' in result and result['cost_complete'] is not False:raise InvalidValue()
    if table=='requests':
        evidence=result['execution_evidence'];assert type(evidence) is MappingProxyType
        profile=evidence['profile'];account=evidence['account'];assert type(profile) is MappingProxyType and type(account) is MappingProxyType
        if (result['fingerprint_version']!=(4 if usage_only else 3) or result['attempt_count'] not in (0,1) or result['extension_id'] is not None
                or result['caller_module']!='retrieval' or result['account_id']!=account['account_id'] or result['profile_id']!=profile['profile_id']
                or profile['account_id']!=account['account_id'] or evidence['request_timeout_ms']!=60000 or evidence['retry_delay_ms']!=0
                or evidence['request_max_bytes']!=65536 or evidence['result_max_bytes']!=40960
                or (result['phase']=='TERMINAL')!=(result['outcome'] is not None)):raise InvalidValue()
        if simulated or usage_only:
            if result['price_revision'] is not None:raise InvalidValue()
            if usage_only and (profile['max_input_units'] is not None or account['price'] is not None or account['cost_limit_atoms'] is not None):raise InvalidValue()
        else:
            price=account['price'];assert type(price) is MappingProxyType
            if result['price_revision']!=price['revision_ref']:raise InvalidValue()
    elif table=='attempts':
        if usage_only:
            from .embedding_allocated_usage import validate as validate_allocated
            usage=validate_allocated(result['usage'])
        else:usage=validate_usage(result['usage'])
        if result['ordinal']!=1 or (usage['billing_mode']=='SIMULATED')!=simulated:raise InvalidValue()
        terminal=result['state'] in ('COMPLETED','NOT_SENT')
        if terminal!=(result['logical_outcome'] is not None):raise InvalidValue()
        if result['logical_outcome']=='SUCCEEDED':
            if result['state']!='COMPLETED' or result['terminal_error'] is not None or result['result_fingerprint'] is None or result['handoff_id'] is None:raise InvalidValue()
        elif terminal and (result['terminal_error'] is None or result['first_error'] is None):raise InvalidValue()
        elif not terminal and result['terminal_error'] is not None:raise InvalidValue()
    elif table=='budget_windows' and usage_only:
        policy=result['policy'];assert type(policy) is MappingProxyType
        if policy['price'] is not None or policy['cost_limit_atoms'] is not None:raise InvalidValue()
    elif table=='reservations':
        if result['cost_complete']:
            if result['held_atoms']!=0 or result['known_cost_atoms']!=result['known_subtotal_atoms']:raise InvalidValue()
        elif result['held_atoms']!=result['reserved_atoms'] or result['known_cost_atoms'] is not None or result['known_subtotal_atoms']!=0:raise InvalidValue()
    elif table=='cost_items':
        if usage_only:
            if result['source']!='UNAVAILABLE':raise InvalidValue()
            return result
        from .token_costs import rounded_cost
        q=result['quantity'];rate=result['price_numerator'];assert type(rate) is int
        cost=None if q is None else number(q) if simulated else rounded_cost(number(q),rate)
        if (result['cost_atoms']!=cost or result['known_subtotal_atoms']!=(cost or 0) or result['cost_complete']!=(cost is not None)
                or result['price_denominator']!=(1 if simulated else 1000000) or simulated and rate!=1
                or result['source']!=('UNAVAILABLE' if cost is None else 'LOCALLY_ESTIMATED')):raise InvalidValue()
    elif table=='handoffs':
        payload=result['embedding_payload'];cleanup=result['embedding_cleanup']
        assert type(payload) is MappingProxyType and type(cleanup) is MappingProxyType
        if result['checksum']!=payload['payload_digest']:raise InvalidValue()
        if cleanup['state']=='HELD':
            if cleanup['received_receipt'] is not None or cleanup['retired_through'] is not None:raise InvalidValue()
        elif cleanup['received_receipt'] is None:raise InvalidValue()
        if cleanup['state']=='RETIRED' and cleanup['retired_through']!=number(payload['leaf_count'])-1:raise InvalidValue()
    return result
