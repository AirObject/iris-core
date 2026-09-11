"""Internal focused model calls bound to a durable run and original request key.

Only a native focus coordinator can use this adapter. Original input is frozen
and committed before independent Provider work; recovery never invokes it.
"""
from companion_memory.provider.values import InvalidData
from types import MappingProxyType
from typing import cast
import time
from companion_memory.provider import WorkGrant,CancellationSource,Found as ProviderFound,Completed,ResultGrant
from companion_memory.configuration import PresentValue,ResolutionOk
from .records import data,json_text,stable_id,DomainFailure
from .results import Committed,WorkDeferred,Rejected


async def generate_focused(control,port,key:object,payload:object):
    runtime=control.runtime;grant=control.ports.get(port)
    if grant is None:return Rejected(runtime._error('claim_work','ACCESS_DENIED','identity','BINDING_MISMATCH'))
    if runtime._lifecycle!='READY':return Rejected(runtime._error('claim_work','MODE_BLOCKED','mode','RECOVERING'))
    provider=runtime._models.provider
    if provider is None or not runtime._models.ready():return WorkDeferred('BLOCKED','NOT_READY')
    try:
        from companion_memory.provider.values import freeze,as_record
        owned=as_record(freeze(payload,8192))
        if set(owned)!= {'messages','input_units_limit','output_units_limit'}:raise ValueError()
        settings=runtime._configuration.candidate.runtime
        if owned['input_units_limit']!=settings.integer('learning.input_units_limit') or owned['output_units_limit']!=settings.integer('learning.output_units_limit'):raise ValueError()
        body=json_text(owned)
    except (InvalidData,ValueError,TypeError):return Rejected(runtime._error('claim_work','INVALID_INPUT','work','INVALID_SHAPE'))
    call_id=stable_id('dream_call',grant.run_id,key)
    prior=await runtime._transactions.rows.load('dream_calls',call_id)
    if prior is None and runtime._mode!='DREAM_FOCUSED':return Rejected(runtime._error('claim_work','MODE_BLOCKED','mode','DREAMING'))
    epoch=cast(int,data(prior)['expected_epoch']) if prior is not None else runtime._epoch
    claimed=await runtime._execute('dream_call',call_id,{'run_id':grant.run_id,'expected_epoch':epoch,'payload':body,'operation_key':key},grant.actor_ref,resolve=prior is not None)
    if type(claimed) is not Committed:return claimed
    roles=runtime._configuration.candidate.foundation.get_entry('provider.role_profiles')
    assert type(roles) is ResolutionOk and type(roles.value.state) is PresentValue
    choices=cast(MappingProxyType,roles.value.state.value)
    if not choices.get('DREAM'):return WorkDeferred('BLOCKED','NOT_READY')
    profile=cast(str,choices['DREAM'][0])
    workgrant=WorkGrant('dream',runtime._instance_id,None,'DREAM',(profile,),('GENERATION',),'synthetic_dream',grant.actor_ref,(grant.run_id,),
        dream_run_ids=(grant.run_id,),internal_dream=True,prompt_revisions=('synthetic_dream:1',))
    work=runtime._models.bind_request(workgrant,stable_id('focus_owner',grant.run_id,'generate',key),epoch)
    import json
    request={'operation_key':call_id,'run_id':grant.run_id,'dream_run_id':grant.run_id,'profile_id':profile,'prompt_revision':'synthetic_dream:1',
        'deadline':time.monotonic()+settings.integer('runtime.operation_timeout_ms')/1000,'cancellation':CancellationSource().token,'payload':json.loads(body)}
    if claimed.source=='EXISTING':
        confirmation=await work.lookup_request('generate',request)
        if type(confirmation) is not ProviderFound:return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        recorded=cast(MappingProxyType,cast(MappingProxyType,confirmation.value)['request'])
        if recorded['phase']!='TERMINAL':return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        owner=provider.bind_result_owner(ResultGrant('synthetic_dream',(cast(str,recorded['object_id']),)))
        found=await owner.recover_result(recorded['object_id'])
        if type(found) is not ProviderFound:return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        return Completed(recorded,cast(MappingProxyType,found.value)['result'],'EXISTING')
    return await work.generate(request)
