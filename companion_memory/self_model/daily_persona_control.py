"""Bounded local operator control of the original daily first-persona run.

Generation is explicit. Reentry, reads, import and startup cannot dispatch a new
attempt. Known original Provider results are consumed before network and input
leases are released; human review is never inferred from generated text.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import time
from types import MappingProxyType
from companion_memory.persistence import Committed,Found,NotFound,NotCommitted,ResultBoundCommand
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.schema import InvalidValue,valid_identifier
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.results import NotCommitted as DomainNotCommitted,RuntimeError
from .daily_persona_material import material_id
from .formats import candidate_digest

@dataclass(frozen=True,slots=True,init=False)
class DailyPersonaPort:
    """Trusted local initialization handle, with no free text editing operation."""
    owner:DailyPersonaControl
    def __init__(self):raise TypeError('Trusted daily initialization issues this port.')
    async def prepare(self,key,input_id,self_revision,epoch):return await self.owner.call(self,'prepare_initial_persona',key,{'input_id':input_id,'expected_self_revision':self_revision,'expected_epoch':epoch})
    async def generate(self,key,run_id,generation):return await self.owner.call(self,'generate',key,{'run_id':run_id,'generation':generation})
    async def read_pending(self,run_id):return await self.owner.call(self,'read_pending','read-pending',{'run_id':run_id})
    async def review(self,key,run_id,revision,candidate_id,candidate_revision,checksum,decision):
        return await self.owner.call(self,'review_initial_persona',key,{'run_id':run_id,'expected_revision':revision,'candidate_id':candidate_id,'candidate_revision':candidate_revision,'candidate_digest':checksum,'decision':decision})
    async def publish(self,key,run_id,revision,candidate_id,candidate_revision,checksum,epoch):
        return await self.owner.call(self,'publish_initial_persona',key,{'run_id':run_id,'expected_revision':revision,'candidate_id':candidate_id,'candidate_revision':candidate_revision,'candidate_digest':checksum,'expected_epoch':epoch})
    async def retry(self,key,run_id,revision,generation,candidate_id,epoch):
        return await self.owner.call(self,'retry_initial_persona',key,{'run_id':run_id,'expected_revision':revision,'generation':generation,'candidate_id':candidate_id,'expected_epoch':epoch})

class DailyPersonaControl:
    def __init__(self,owner):
        self.owner=owner;self.closed=False
        port=object.__new__(DailyPersonaPort);object.__setattr__(port,'owner',self);self.port=port

    def command(self,kind,key,values):
        definition=next(d for d in self.owner.commands if d.operation_kind==kind)
        return definition,ResultBoundCommand(1,{'operation_id':key,**values},{r.event_slot:{'actor':self.owner.actor} for r in definition.required_audits})

    async def execute(self,kind,key,values):
        o=self.owner
        if o._task is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        definition,command=self.command(kind,key,values)
        async def write():
            result,cause=await o.causes.execute_original(o.operations[kind],definition,key,command)
            if type(result) is NotCommitted and cause is not None:
                return DomainNotCommitted(RuntimeError(cause.code,kind,cause.field,cause.reason,cause.cleanup_pending or result.error is not None and result.error.cleanup_pending))
            return result
        actual,logical=start_owned(write());o._task=actual
        def ended(task):
            if not task.cancelled():task.exception()
            if o._task is task:o._task=None
        actual.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=5)
        if not done:raise OwnerFailure('TIMEOUT','persona','DEADLINE_EXCEEDED',True)
        return logical.result()

    async def run(self,run_id):
        found=await self.owner.owner.read_original('run',run_id,time.monotonic()+5)
        if found is None:raise OwnerFailure('PRECONDITION_FAILED','persona','NOT_FOUND')
        await self.owner.verify_record_original(found.value['last_operation'],found.value)
        return found.value

    async def pending(self,run_id):
        o=self.owner;run=await self.run(run_id);candidate=None
        if run['resolution_id'] is not None:
            found=await o.owner.read_original('candidate',run['resolution_id'],time.monotonic()+5)
            if found is None:raise InvalidValue()
            candidate=found.value
            await o.verify_record_original(o.candidate_operation(candidate),candidate)
        return Found(MappingProxyType({'run':run,'candidate':candidate,'candidate_digest':None if candidate is None else candidate_digest(candidate),'new_sends':0}))

    async def call(self,port,kind,key,values):
        o=self.owner
        if self.closed or type(port) is not DailyPersonaPort or port is not self.port or not valid_identifier(key):raise InvalidValue()
        if o.runtime.state!='READY':raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        if o._job is not None:raise OwnerFailure('RESOURCE_BUSY','persona','CLEANUP_PENDING',True)
        async def advance():
            if kind=='read_pending':return await self.pending(values['run_id'])
            if kind=='generate':
                run=await self.run(values['run_id'])
                if run['generation']!=values['generation'] or key!=run['provider_operation_key']:raise OwnerFailure('IDEMPOTENCY_CONFLICT','request','CONTENT_MISMATCH')
                return await self.drive(run,allow_first_send=True)
            if kind=='prepare_initial_persona':o.runtime.gate.close_ordinary()
            if kind=='retry_initial_persona':
                definition,command=self.command(kind,key,values)
                prior=await o.operations[kind].resolve_operation(o.operations[kind].recovery_handle(key,command))
                if type(prior) is not NotCommitted or prior.error is not None:return prior
                await self.cleanup(await self.run(values['run_id']))
            result=await self.execute(kind,key,values)
            if o._task is not None:await asyncio.wait((o._task,))
            try:
                if type(result) is Committed and kind in ('review_initial_persona','publish_initial_persona'):
                    await self.cleanup(await self.run(values['run_id']))
                if kind in ('prepare_initial_persona','retry_initial_persona','publish_initial_persona'):
                    mode=await o.mode.synchronize()
                    expected='NORMAL' if kind=='publish_initial_persona' else 'DREAM_FOCUSED'
                    if type(result) is Committed and mode['state']!=expected:
                        # Publication/prepare is already durable, while finite
                        # FIFO transfer or focus drain can still be incomplete.
                        return Found(MappingProxyType({'state':'COMMITTED','commit_id':result.receipt.commit_id,
                            'operation_key':result.receipt.identity.operation_key,'mode_state':mode['state'],
                            'cleanup_state':'PENDING','cleanup_pending':bool(o.runtime.external_work_pending
                                or o.provider.get_health().cleanup_pending or o.storage.get_health().cleanup_pending),
                            'new_sends':0}))
            except (OwnerFailure,InvalidValue) as failure:
                if type(result) is not Committed:raise
                return self.cleanup_failed(result,failure)
            return result
        task,logical=start_owned(advance());o._job=task
        def ended(job):
            if not job.cancelled():job.exception()
            if o._job is job:o._job=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=5)
        return logical.result() if done else Found(MappingProxyType({'state':'PENDING','cleanup_pending':True}))

    async def drive(self,run,*,allow_first_send):
        versions = self.owner.materials.versions
        if versions is not None:
            version = await versions.required(material_id(self.owner.configuration, run['object_id'], run['generation']), run['object_id'])
            with versions.versions.use(version):
                return await self._drive_selected(run, allow_first_send=allow_first_send)
        return await self._drive_selected(run, allow_first_send=allow_first_send)

    async def _drive_selected(self,run,*,allow_first_send):
        o=self.owner;provider=o.provider
        if run['state'] in ('WAITING_REVIEW','APPROVED','KNOWN_FAILED','USER_REJECTED','PUBLISHED'):
            await self.cleanup(run)
            return await self.pending(run['object_id'])
        if run['state']=='PREPARED':
            if not allow_first_send:return Found(MappingProxyType({'state':'PARKED','new_sends':0}))
            mode=await o.mode.content.rows.read('mode_get',{'mode_id':'instance_mode'})
            if len(mode)!=1:raise InvalidValue()
            associated=await self.execute('associate_initial_persona_request',o.key('persona-associate',run['object_id'],run['generation']),
                {'run_id':run['object_id'],'expected_revision':run['revision'],'generation':run['generation'],'expected_epoch':mode[0]['epoch']})
            if type(associated) is not Committed:return associated
            run=await self.run(run['object_id'])
        original=await provider.read_daily_request(run['provider_operation_key'],'PERSONA',run['object_id'],time.monotonic()+5)
        if type(original) is NotFound:
            if run['provider_request_id'] is not None or run['state']=='REMOTE_UNKNOWN':raise InvalidValue()
            root=await o.materials.read_manifest(material_id(o.configuration,run['object_id'],run['generation']),run['object_id'])
            if root is None:raise InvalidValue()
            if time.time_ns()//1000>=root['created_at_us']+60000000:
                ended=await self.execute('record_initial_persona_resolution',o.key('persona-resolution',run['object_id'],run['generation']),{'run_id':run['object_id'],'expected_revision':run['revision'],'generation':run['generation']})
                return ended
            if not allow_first_send:return Found(MappingProxyType({'state':'PARKED','new_sends':0}))
            lease=await o.materials.borrow(root['object_id'],root['payload_digest'],run['object_id'],time.monotonic()+max(0,(root['created_at_us']+60000000-time.time_ns()//1000)/1000000))
            request=None;o._sending=(run,root)
            try:
                if provider.network is None:raise InvalidValue()
                provider.network.resume()
                await provider.network.wait_quiet(lease.deadline)
                if not o.explicit_send(run['provider_operation_key']):raise OwnerFailure('ACCESS_DENIED','request','OPERATION_NOT_GRANTED')
                request=provider.generation_request('PERSONA',lease,run['provider_operation_key'],root['created_at_us']+60000000)
                await provider.send_generation(request)
                await provider.wait_generation_actual(request)
                original=await provider.read_daily_request(run['provider_operation_key'],'PERSONA',run['object_id'],time.monotonic()+5)
            finally:
                if request is not None:
                    await provider.wait_generation_actual(request);provider.release_unused_generation(request)
                o.materials.release_reader(lease);o._sending=None
                await provider.reconcile_daily_network()
        if type(original) is not Found:return original
        request=original.value;o._observed_request=request
        try:
            if run['provider_request_id'] is None:
                confirmed=await self.execute('confirm_initial_persona_request',o.key('persona-confirm',run['object_id'],run['generation']),
                    {'run_id':run['object_id'],'expected_revision':run['revision'],'generation':run['generation'],'request_id':request['object_id'],'request_digest':request['fingerprint']})
                if type(confirmed) is not Committed:return confirmed
                run=await self.run(run['object_id'])
            if request['phase']=='OPEN':return Found(MappingProxyType({'state':'ORIGINAL_REQUEST_UNCONFIRMED','new_sends':0}))
            values={'run_id':run['object_id'],'expected_revision':run['revision'],'generation':run['generation']}
            if request['phase']=='REMOTE_RESULT_UNKNOWN':
                if run['state']=='REMOTE_UNKNOWN':return Found(MappingProxyType({'state':'REMOTE_UNKNOWN','request_id':run['provider_request_id'],'new_sends':0}))
                return await self.execute('mark_initial_persona_unknown',o.key('persona-unknown',run['object_id'],run['generation']),values)
            kind='record_initial_persona_resolution'
            if request['outcome']=='SUCCEEDED':
                o._receiving=await provider.recover_daily_result(request['object_id'],time.monotonic()+5);kind+='_with_result'
            try:ended=await self.execute(kind,o.key('persona-resolution',run['object_id'],run['generation']),values)
            finally:
                if o._task is not None:await asyncio.wait((o._task,))
                if o._receiving is not None:provider.release_daily_result(o._receiving);o._receiving=None
            if type(ended) is Committed:
                try:await self.cleanup(await self.run(run['object_id']))
                except (OwnerFailure,InvalidValue) as failure:return self.cleanup_failed(ended,failure)
            return ended
        finally:o._observed_request=None

    async def cleanup(self,run):
        o=self.owner
        if run['provider_request_id'] is not None:
            original=await o.provider.read_daily_request(run['provider_operation_key'],'PERSONA',run['object_id'],time.monotonic()+5)
            if type(original) is not Found:raise InvalidValue()
            if original.value['outcome']=='SUCCEEDED':
                receipt=await o.operations['record_initial_persona_resolution_with_result'].read_receipt(o.key('persona-resolution',run['object_id'],run['generation']))
                if type(receipt) is not Found:raise InvalidValue()
                cleaned=await o.provider.cleanup_daily(run['provider_request_id'],receipt.value,time.monotonic()+5)
                if type(cleaned) is not Found:raise OwnerFailure('RESULT_UNCONFIRMED','cleanup','COMMIT_UNCONFIRMED',True)
        generation=run['generation']
        for number in range(1,generation+1):
            ids=[material_id(o.configuration,run['object_id'],number)]
            if number<generation or run['state'] in ('PUBLISHED','USER_REJECTED','KNOWN_FAILED'):
                ids.append(o.key('persona-result',run['object_id'],number))
            for context_id in ids:
                root=await o.materials.read_manifest(context_id,run['object_id'])
                if root is None:continue
                o.materials.begin_retirement(context_id)
                try:
                    for offset in range(0,len(root['leaf_refs']),4):
                        result=await self.execute('retire_initial_persona_material',o.key('persona-retire',context_id,offset),
                            {'run_id':run['object_id'],'context_id':context_id,'page_offset':offset})
                        if type(result) is not Committed:raise OwnerFailure('RESULT_UNCONFIRMED','cleanup','COMMIT_UNCONFIRMED',o._task is not None)
                finally:
                    if o._task is not None:await asyncio.wait((o._task,))
                    o.materials.end_retirement(context_id)
        o.cleanup_failure=None

    def cleanup_failed(self,committed,failure):
        """Keep the confirmed business receipt when subsequent local cleanup fails."""
        o=self.owner
        if not isinstance(failure,OwnerFailure):failure=OwnerFailure('STORAGE_FAILED','cleanup','INTEGRITY_FAILURE',o.storage.get_health().cleanup_pending)
        o.cleanup_failure=failure
        if o.provider.network is not None:o.provider.network.pause()
        return Found(MappingProxyType({'state':'COMMITTED','commit_id':committed.receipt.commit_id,'operation_key':committed.receipt.identity.operation_key,
            'cleanup_state':'FAILED','cleanup_pending':failure.cleanup_pending,
            'cleanup_error':MappingProxyType({'code':failure.code,'field':failure.field,'reason':failure.reason}),'new_sends':0}))

    async def wait_actual(self):
        for task in (self.owner._job,self.owner._task):
            if task is not None:await asyncio.wait((task,))

    async def recover(self):
        """Inspect or consume only this retained generation, with first sends disabled."""
        from companion_memory.persistence.text_records import stable_identity
        o=self.owner
        found=await o.owner.read_original('run',stable_identity('persona-run',o.configuration.database_id,o.configuration.scope_id),time.monotonic()+5)
        if found is None:return None
        await o.verify_record_original(found.value['last_operation'],found.value)
        for generation in range(1,found.value['generation']):
            candidate_id=stable_identity('persona-candidate',o.configuration.database_id,o.configuration.scope_id,found.value['object_id'],generation)
            candidate=await o.owner.read_original('candidate',candidate_id,time.monotonic()+5)
            if candidate is None:raise InvalidValue()
            await o.verify_record_original(o.candidate_operation(candidate.value),candidate.value)
        if found.value['resolution_id'] is not None:await self.pending(found.value['object_id'])
        observed=await self.drive(found.value,allow_first_send=False)
        if type(observed) not in (Found,Committed):return observed
        return None
