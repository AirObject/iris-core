"""Bind frozen work to independent Provider calls and read-only crash recovery.

The gate serializes dispatch with mode closing. Read authority survives the
recovering overlay, but send authority does not. Original request association is
committed before Provider receives any call, including before its first return.
"""
from __future__ import annotations
from collections.abc import Callable
import time
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from weakref import ref,ReferenceType
from companion_memory.buffers import MaterialRecord,build_material
from companion_memory.provider import (
    ProviderService,WorkGrant,WorkPort,ResultGrant,bind_gate,CancellationSource,
    Completed,Pending,Found as ProviderFound,NotFound as ProviderNotFound,Failed as ProviderFailed,
)
from companion_memory.configuration import PresentValue,ResolutionOk
from .records import stored_event,stored_timestamp,DomainFailure,data,digest,stable_id,json_text
from .results import Committed,Found,WorkDeferred
if TYPE_CHECKING:
    from .service import RuntimeService,WorkCapability


class ModelWork:
    """Trusted runtime-owned grants; no caller-submitted role can create authority."""
    def __init__(self,runtime:RuntimeService):
        self.runtime=runtime
        self.provider:ProviderService | None=None
        self.grants:dict[int,tuple[WorkGrant,str,int]]={}
        self._grant_ports:dict[int,ReferenceType[WorkPort]]={}
        self.closing_gate=False
        self.dream_closing:tuple[str,int] | None=None
        self.dream_closers:dict[object,tuple[str,int]]={}
        self.binding=bind_gate(self.check,self.dispatch,self.authorized)
    def attach(self,provider:ProviderService) -> None:
        if self.provider is not None or type(provider) is not ProviderService:raise ValueError('One native Provider is required.')
        self.provider=provider
    def authorized(self,grant:WorkGrant) -> bool:
        with self.runtime._gate_lock:
            binding=self.grants.get(id(grant))
            return binding is not None and binding[0] is grant and self.runtime._owner_issue('claim_work') is None
    def check(self,grant:WorkGrant) -> bool:
        with self.runtime._gate_lock:
            if not self.authorized(grant) or self.runtime._lifecycle!='READY':return False
            if grant.internal_dream:
                round_identity=(grant.dream_run_ids[0],self.runtime._epoch)
                return self.dream_closing!=round_identity and round_identity not in self.dream_closers.values() and self.runtime._mode=='DREAM_FOCUSED' and self.grants[id(grant)][2]==self.runtime._epoch
            return not self.closing_gate and self.runtime._mode in ('NORMAL','DRAINING')
    def dispatch(self,grant:WorkGrant,start:Callable[[],None]) -> bool:
        with self.runtime._gate_lock:
            if not self.check(grant):return False
            start()
            self.runtime._sending.add(self.grants[id(grant)][1])
            return True
    def bind_request(self,grant:WorkGrant,work_id:str,generation:int) -> WorkPort:
        """Authority lives exactly as long as its issued port or Provider owner.

        Provider retains the port during execution and original-key reads. A
        temporary query drops its own port without revoking another work grant.
        """
        if self.provider is None:raise DomainFailure('INVALID_STATE','work','NOT_READY')
        port=self.provider.bind_work(grant);identity=id(grant)
        def released(reference:ReferenceType[WorkPort]):
            with self.runtime._gate_lock:
                if self._grant_ports.get(identity) is reference:
                    self._grant_ports.pop(identity,None);self.grants.pop(identity,None)
        with self.runtime._gate_lock:
            self.grants[identity]=(grant,work_id,generation)
            self._grant_ports[identity]=ref(port,released)
        return port

    def release_grants(self,work_id:str):
        """Collect only ended port owners; live Provider grants cannot be revoked."""
        with self.runtime._gate_lock:
            for key,value in tuple(self.grants.items()):
                if value[1]==work_id and self._grant_ports[key]() is None:
                    self.grants.pop(key,None);self._grant_ports.pop(key,None)

    def ready(self) -> bool:
        return self.provider is not None and self.provider.get_health().lifecycle=='READY'
    async def material(self,work_id:str) -> tuple[tuple[MappingProxyType[str,str],...],dict[str,object],dict[str,object]]:
        tx=self.runtime._transactions
        batch=await tx.buffers.rows.load('batches',work_id)
        work=await tx.rows.load('work',work_id)
        if batch is None or work is None or data(batch)['config_snapshot_id']!=self.runtime._configuration.snapshot_id:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
        if (data(batch)['material_protocol'],data(batch)['template_protocol'],data(batch)['participant_protocol'])!=('synthetic_window_base64:1','synthetic_target_refs:1','synthetic_learning:1'):raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
        entry_id=cast(str,work['entry_id']);entry=await tx.ingress.rows.load('entries',entry_id)
        if entry is None:raise DomainFailure('STORAGE_FAILED','entry','INTEGRITY_FAILURE')
        refs=await tx.buffers.rows.read('members_entry',{'entry_id':entry_id,'state':work_id,'after_sequence':0,'after_id':'','limit':128})
        records=[]
        for ref in refs:
            member=await tx.buffers.rows.load('members',cast(str,ref['object_id']))
            if member is None:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
            value=data(member);mid=cast(str,value['message_id'])
            event=await tx.ingress.rows.load('events',mid);payload=await tx.ingress.rows.load('payloads',mid)
            if event is None or payload is None or event['entry_id']!=entry_id or event['sequence']!=value['entry_seq']:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
            text=cast(str,data(payload)['payload'])
            if digest(text)!=value['payload_digest'] or value['payload_digest']!=data(event)['payload_digest']:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
            micros=cast(int,data(event)['received_at_us'])
            timestamp=stored_timestamp(micros)
            records.append(MaterialRecord(cast(str,value['role']),mid,cast(int,event['sequence']),timestamp,stored_event(text)))
        identities=(self.runtime._instance_id,cast(str,data(entry)['host_id']),cast(str,data(entry)['platform_id']),entry_id,work_id,cast(str,data(work)['run_id']),self.runtime._configuration.snapshot_id)
        messages=build_material(identities,tuple(records))
        if digest(tuple(dict(m) for m in messages))!=data(batch)['material_digest']:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
        return messages,batch,work
    async def request(self,work_id:str) -> tuple[WorkPort,dict[str,object],dict[str,object]]:
        assert self.provider is not None
        messages,batch,work=await self.material(work_id)
        foundation=self.runtime._configuration.candidate.foundation
        profiles=foundation.get_entry('provider.role_profiles')
        assert type(profiles) is ResolutionOk and type(profiles.value.state) is PresentValue
        roles=cast(MappingProxyType,profiles.value.state.value)
        profile=cast(str,roles['LEARNING'][0])
        values=data(work);entry_id=cast(str,work['entry_id'])
        grant=WorkGrant('cognition',self.runtime._instance_id,None,'LEARNING',(profile,),('GENERATION',),'synthetic_learning','scheduler',
            (cast(str,values['run_id']),),(entry_id,),(work_id,),prompt_revisions=('synthetic_target_refs:1',))
        port=self.bind_request(grant,work_id,cast(int,values['generation']))
        settings=self.runtime._configuration.candidate.runtime
        request={'operation_key':values['provider_operation_key'],'run_id':values['run_id'],'profile_id':profile,
            'deadline':time.monotonic()+settings.integer('runtime.operation_timeout_ms')/1000,'cancellation':CancellationSource().token,
            'batch_id':work_id,'entry_ids':[entry_id],'prompt_revision':'synthetic_target_refs:1',
            'payload':{'messages':[dict(m) for m in messages],'input_units_limit':settings.integer('learning.input_units_limit'),'output_units_limit':settings.integer('learning.output_units_limit')}}
        return port,request,work
    async def can_readmit(self,work_id:str) -> bool:
        if not self.ready():return False
        port,original,_=await self.request(work_id)
        confirmed=await port.lookup_request('generate',original)
        if type(confirmed) is not ProviderFound:return False
        request=cast(MappingProxyType,cast(MappingProxyType,confirmed.value)['request'])
        attempts=cast(tuple[MappingProxyType,...],cast(MappingProxyType,confirmed.value)['attempts'])
        return request['phase']=='TERMINAL' and request['outcome'] in ('MODE_BLOCKED','PAUSED_BUDGET','UNSUPPORTED_CAPABILITY') and all(a['state']=='NOT_SENT' for a in attempts)

    async def run(self,capability:WorkCapability):
        r=self.runtime
        if not self.ready():return WorkDeferred('BLOCKED','NOT_READY')
        association_key=stable_id('provider_operation',capability.work_id,capability.owner_generation)
        associated=await r._execute('associate',association_key,{'work_id':capability.work_id,'expected_revision':capability.revision,
            'owner_generation':capability.owner_generation,'operation_key':association_key},'scheduler')
        if type(associated) is not Committed:return associated
        port,request,work=await self.request(capability.work_id)
        if associated.source=='EXISTING':
            if await self.recover(work):
                terminal=await r._operations['finalize'].read_receipt(stable_id('terminal',capability.work_id))
                from companion_memory.persistence import Found as StoredFound
                if type(terminal) is StoredFound:return Committed(terminal.value,'EXISTING')
            return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        result=await port.generate(request)
        if type(result) is Completed:
            outcome=await self.settle(work,result.record,result.result)
            if self.provider is not None and self.provider.get_health().in_flight==0:r._sending.discard(capability.work_id)
            return outcome
        if type(result) is Pending:
            return await self.observe(work,'REMOTE_RESULT_UNKNOWN',cast(str,result.reference['request_id']))
        return await self.observe(work,'SYSTEM_BLOCKED',None)
    async def observe(self,work:dict[str,object],state:str,request_id:str | None):
        values=data(work)
        return await self.runtime._execute('observe_work',stable_id('work_observation',work['object_id'],work['revision'],state,request_id),
            {'work_id':work['object_id'],'expected_revision':work['revision'],'owner_generation':values['generation'],'state':state,'request_id':request_id},'scheduler')
    def check_read_failure(self,result:object):
        """Storage evidence failure is not a malformed participant candidate."""
        if type(result) is ProviderFailed and result.error.code=='PERSISTENCE_FAILED':
            raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE' if result.error.reason=='LEDGER_INCONSISTENT' else 'READ_FAILED',result.error.cleanup_pending)

    async def verify_outcome(self,work_id:str,outcome:dict[str,object]) -> bool:
        if not self.ready() or set(outcome)!={'request_id','outcome','result','candidate'}:return False
        port,original,_=await self.request(work_id)
        confirmed=await port.lookup_request('generate',original)
        self.check_read_failure(confirmed)
        if type(confirmed) is not ProviderFound:return False
        request=cast(MappingProxyType,cast(MappingProxyType,confirmed.value)['request'])
        if request['phase']!='TERMINAL' or request['object_id']!=outcome['request_id'] or request['outcome']!=outcome['outcome']:return False
        assert self.provider is not None
        owner=self.provider.bind_result_owner(ResultGrant('synthetic_learning',(cast(str,request['object_id']),)))
        handoff=await owner.recover_result(request['object_id'])
        self.check_read_failure(handoff)
        if type(handoff) is not ProviderFound or json_text(cast(MappingProxyType,handoff.value)['result'])!=json_text(outcome['result']):return False
        messages,_,_=await self.material(work_id)
        participant=self.runtime._assembly.participant
        if participant is None:return False
        expected=participant.prepare_outcome(MappingProxyType({'batch_id':work_id,'messages':messages,'config_snapshot_id':self.runtime._configuration.snapshot_id}),MappingProxyType({'request_id':request['object_id'],'outcome':request['outcome'],'result':cast(MappingProxyType,handoff.value)['result']}))
        return json_text(expected)==json_text(outcome['candidate'])

    async def settle(self,work:dict[str,object],request:MappingProxyType,result:object):
        from .service import WorkCapability
        values=data(work);work_id=cast(str,work['object_id'])
        if request['attribution']['batch_id']!=work_id or request['attribution']['run_id']!=values['run_id'] or request['capability']!='GENERATION':raise DomainFailure('STORAGE_FAILED','work','INTEGRITY_FAILURE')
        outcome=request['outcome']
        if outcome in ('MODE_BLOCKED','PAUSED_BUDGET','UNSUPPORTED_CAPABILITY'):
            return await self.observe(work,'WAITING_ADMISSION' if await self.can_readmit(work_id) else 'SYSTEM_BLOCKED',cast(str,request['object_id']))
        if outcome not in ('FAILED','SUCCEEDED','SENSITIVE_REFUSAL','OTHER_REFUSAL','TRANSIENT_FAILURE','RATE_LIMITED','AUTHENTICATION_FAILED','INVALID_RESPONSE','TIMED_OUT','CANCELLED'):
            return await self.observe(work,'SYSTEM_BLOCKED',cast(str,request['object_id']))
        if outcome=='SUCCEEDED' and type(result) is not MappingProxyType:raise DomainFailure('PARTICIPANT_FAILED','participant','RESULT_INVALID')
        cap=WorkCapability(work_id,cast(int,work['revision']),cast(int,values['generation']),cast(str,work['entry_id']))
        self.runtime._work[id(cap)]=cap
        # The participant receives an isolated original Provider handoff, never a
        # caller-selected model category or body without durable attribution.
        import json
        participant=self.runtime._assembly.participant
        if participant is None:raise DomainFailure('CAPABILITY_UNAVAILABLE','participant','LEARNING_PARTICIPANT_MISSING')
        messages,_,_=await self.material(work_id)
        candidate=participant.prepare_outcome(MappingProxyType({'batch_id':work_id,'messages':messages,'config_snapshot_id':self.runtime._configuration.snapshot_id}),MappingProxyType({'request_id':request['object_id'],'outcome':outcome,'result':result}))
        staged=await self.runtime.stage_candidate(cap,{'request_id':request['object_id'],'outcome':outcome,'result':json.loads(json_text(result)) if result is not None else None,'candidate':candidate})
        if type(staged) is not Committed:return staged
        return await self.runtime.finalize_batch(cap,stable_id('candidate',work_id))
    async def recover(self,work:dict[str,object]) -> bool:
        """Release native read grants after the bounded original-evidence operation."""
        try:return await self._recover(work)
        finally:self.release_grants(cast(str,work['object_id']))

    async def _recover(self,work:dict[str,object]) -> bool:
        """Only original-key lookup and owner-bound handoff reads; never model work."""
        if not self.ready():return False
        values=data(work);work_id=cast(str,work['object_id'])
        if work['state']=='CANDIDATE_STORED':
            from .service import WorkCapability
            cap=WorkCapability(work_id,cast(int,work['revision'])-1,cast(int,values['generation']),cast(str,work['entry_id']))
            self.runtime._work[id(cap)]=cap
            return type(await self.runtime.finalize_batch(cap,cast(str,values['candidate_id']))) is Committed
        if values['provider_operation_key'] is None:
            # Association must commit before any Provider registration. Under
            # isolated recovery, this absence proves this claim never sent.
            return type(await self.observe(work,'FROZEN',None)) is Committed
        port,original,current=await self.request(work_id)
        lookup=await port.lookup_request('generate',original)
        if type(lookup) is ProviderNotFound:
            # A miss alone grants no resend. The frozen input remains protected.
            return type(await self.observe(current,'SYSTEM_BLOCKED',None)) is Committed
        if type(lookup) is not ProviderFound:return False
        request=cast(MappingProxyType,cast(MappingProxyType,lookup.value)['request'])
        assert self.provider is not None
        if request['phase']!='TERMINAL':
            return type(await self.observe(current,'REMOTE_RESULT_UNKNOWN',cast(str,request['object_id']))) is Committed
        owner=self.provider.bind_result_owner(ResultGrant('synthetic_learning',(cast(str,request['object_id']),)))
        handoff=await owner.recover_result(request['object_id'])
        if type(handoff) is not ProviderFound:return False
        return type(await self.settle(current,request,cast(MappingProxyType,handoff.value)['result'])) is Committed
