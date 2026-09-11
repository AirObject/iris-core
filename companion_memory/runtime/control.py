"""Native dream coordination and bounded closure of ordinary dispatch authority."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING,cast
from weakref import WeakKeyDictionary
from companion_memory.persistence.schema import valid_identifier
from .records import stable_id,data,DomainFailure
from .results import Committed,Rejected,WorkDeferred,RuntimeError
if TYPE_CHECKING:
    from .service import RuntimeService


@dataclass(frozen=True,slots=True)
class FocusGrant:
    instance_id:str
    run_id:str
    actor_ref:str


class FocusPort:
    """An issued internal run binding, never a Web or event-provided role."""
    __slots__=('_control','__weakref__')
    def __new__(cls):raise TypeError('Bind an internal focus coordinator.')
    def __setattr__(self,name,value):raise AttributeError('Focus capabilities are immutable.')
    async def enter_focus(self,key:object,expected_epoch:object):return await _controlled(self,'enter',key,expected_epoch)
    async def generate(self,key:object,payload:object):
        return await _controlled(self,'generate',key,payload)
    async def finish_focus(self,key:object,completion_ref:object,expected_epoch:object):return await _controlled(self,'finish',key,completion_ref,expected_epoch)


async def _controlled(port:object,action:str,key:object,*arguments:object):
    control=None
    if type(port) is FocusPort:
        try:control=object.__getattribute__(port,'_control')
        except AttributeError:pass
    if type(control) is not FocusControl:return Rejected(RuntimeError('ACCESS_DENIED','claim_work' if action=='generate' else action+'_focus','identity','BINDING_MISMATCH'))
    assert type(port) is FocusPort
    return await control.owned(port,action,key,*arguments)


class FocusControl:
    def __init__(self,runtime:RuntimeService):
        self.runtime=runtime;self.ports:WeakKeyDictionary[FocusPort,FocusGrant]=WeakKeyDictionary();self.tasks:dict[str,asyncio.Task]={}
    async def owned(self,port:FocusPort,action:str,key:object,*arguments:object):
        """A caller deadline never cancels a started mode or focused-work owner."""
        r=self.runtime
        grant=self.ports.get(port) if type(port) is FocusPort else None
        operation='claim_work' if action=='generate' else action+'_focus'
        if grant is None:return Rejected(r._error(operation,'ACCESS_DENIED','identity','BINDING_MISMATCH'))
        issue=r._owner_issue(operation)
        if issue is not None:return Rejected(issue)
        if not valid_identifier(key):return Rejected(r._error(operation,'INVALID_INPUT','identity','INVALID_SHAPE'))
        identity=stable_id('focus_owner',grant.run_id,action,key)
        if identity in self.tasks:return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        if r._lifecycle!='READY':return Rejected(r._error(operation,'MODE_BLOCKED','mode','RECOVERING'))
        if len(self.tasks)>=r._configuration.candidate.runtime.integer('runtime.max_active_entries'):return WorkDeferred('BLOCKED','ADMISSION_FULL')
        if action=='generate':
            from .dream_work import generate_focused
            coroutine=generate_focused(self,port,key,*arguments)
        else:coroutine=(self.enter if action=='enter' else self.finish)(port,key,*arguments)
        task=asyncio.create_task(coroutine);self.tasks[identity]=task;r._jobs.add(task)
        task.add_done_callback(r._finished)
        task.add_done_callback(lambda done:self.tasks.pop(identity,None))
        if action=='generate':
            r._running[identity]=task;r._owner_started[identity]=time.monotonic()
            task.add_done_callback(lambda done:r._work_finished(identity,done))
        done,_=await asyncio.wait((task,),timeout=r._configuration.candidate.runtime.integer('runtime.operation_timeout_ms')/1000)
        if not done:return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        try:return task.result()
        except DomainFailure as issue:return Rejected(r._error(operation,issue.code,issue.field,issue.reason,issue.cleanup_pending))

    def bind(self,grant:FocusGrant):
        r=self.runtime
        if type(grant) is not FocusGrant or not valid_identifier(grant.instance_id) or grant.instance_id!=r._instance_id or not valid_identifier(grant.run_id) or not valid_identifier(grant.actor_ref):
            return Rejected(r._error('enter_focus','ACCESS_DENIED','identity','BINDING_MISMATCH'))
        issue=r._owner_issue('enter_focus')
        if issue is not None:return Rejected(issue)
        if r._lifecycle=='CLOSING':return Rejected(r._error('enter_focus','INVALID_STATE','state','SERVICE_CLOSED'))
        port=object.__new__(FocusPort);object.__setattr__(port,'_control',self);self.ports[port]=grant
        return port
    async def publish(self,result):
        """Publish only confirmed transaction facts, without a fallible extra read."""
        r=self.runtime
        if type(result) is Committed:
            from types import MappingProxyType
            facts=result.receipt.result
            assert type(facts) is MappingProxyType
            epoch=cast(int,facts['epoch'])
            with r._gate_lock:
                if epoch>=r._epoch:
                    r._mode=cast(str,facts['mode']);r._epoch=epoch
                    r._models.closing_gate=r._mode not in ('NORMAL','DRAINING')
        return result
    async def enter(self,port:FocusPort,key:object,epoch:object):
        r=self.runtime;grant=self.ports.get(port) if type(port) is FocusPort else None
        if grant is None:return Rejected(r._error('enter_focus','ACCESS_DENIED','identity','BINDING_MISMATCH'))
        if r._transactions.publication is None:return Rejected(r._error('enter_focus','CAPABILITY_UNAVAILABLE','participant','DREAM_PARTICIPANT_MISSING'))
        if r._lifecycle!='READY':return Rejected(r._error('enter_focus','MODE_BLOCKED','mode','RECOVERING'))
        with r._gate_lock:r._models.closing_gate=True
        result=await r._execute('enter_focus',key,{'expected_epoch':epoch,'run_id':grant.run_id},grant.actor_ref)
        if type(result) is not Committed:
            # Confirmation uncertainty keeps the dispatch barrier closed.
            from .results import NotCommitted,Rejected as Refused
            if (type(result) is NotCommitted or type(result) is Refused) and (result.error is None or not result.error.cleanup_pending):
                r._models.closing_gate=r._mode not in ('NORMAL','DRAINING')
            return result
        await self.publish(result)
        from companion_memory.persistence import Found,NotFound
        original=await r._operations['focus_ready'].read_receipt(stable_id('focus_ready',grant.run_id))
        if type(original) is Found:return await self.publish(Committed(original.value,'EXISTING'))
        if type(original) is not NotFound:return Rejected(r._error('enter_focus','STORAGE_FAILED','storage','READ_FAILED',getattr(getattr(original,'error',None),'cleanup_pending',False)))
        return await self.drain(grant)
    async def drain(self,grant:FocusGrant):
        r=self.runtime;deadline=time.monotonic()+r._configuration.candidate.runtime.integer('runtime.focus_drain_timeout_ms')/1000
        while r._running or (r._models.provider and (r._models.provider.get_health().in_flight or r._models.provider.get_health().cleanup_pending)) or r._storage.get_health().writes_in_flight:
            if time.monotonic()>=deadline:
                return await self.publish(await r._execute('fault',stable_id('focus_fault',grant.run_id),{'expected_epoch':r._epoch,'run_id':grant.run_id},grant.actor_ref))
            await asyncio.sleep(min(0.01,max(0,deadline-time.monotonic())))
        result=await r._execute('focus_ready',stable_id('focus_ready',grant.run_id),{'expected_epoch':r._epoch,'run_id':grant.run_id},grant.actor_ref)
        if type(result) is not Committed:
            from .results import NotCommitted
            if type(result) is NotCommitted and not (result.error and result.error.cleanup_pending):
                return await self.publish(await r._execute('fault',stable_id('focus_fault',grant.run_id),{'expected_epoch':r._epoch,'run_id':grant.run_id},grant.actor_ref))
        return await self.publish(result)
    async def finish(self,port:FocusPort,key:object,completion:object,epoch:object):
        r=self.runtime;grant=self.ports.get(port) if type(port) is FocusPort else None
        if grant is None:return Rejected(r._error('finish_focus','ACCESS_DENIED','identity','BINDING_MISMATCH'))
        mode=await r._transactions.rows.load('mode','mode')
        if mode is None:raise DomainFailure('STORAGE_FAILED','mode','INTEGRITY_FAILURE')
        current=mode['state']=='DREAM_FOCUSED' and data(mode)['run_id']==grant.run_id and data(mode)['epoch']==epoch
        if not current:
            original=await r._execute('finish_focus',key,{'expected_epoch':epoch,'run_id':grant.run_id,'publication_id':completion},grant.actor_ref,resolve=True)
            from .results import NotCommitted
            if type(original) is NotCommitted:return Rejected(r._error('finish_focus','PRECONDITION_FAILED','mode','REVISION_CHANGED'))
            if type(original) is Committed and mode['state']=='DRAINING' and data(mode)['run_id']==grant.run_id:await r._complete_drain()
            return original
        token=object();round_identity=(grant.run_id,cast(int,epoch))
        with r._gate_lock:r._models.dream_closers[token]=round_identity
        try:
            if r._running or r._models.provider is not None and (r._models.provider.get_health().in_flight or r._models.provider.get_health().cleanup_pending):
                with r._gate_lock:r._models.dream_closing=round_identity
                return WorkDeferred('BLOCKED','OWNER_ACTIVE')
            result=await r._execute('finish_focus',key,{'expected_epoch':epoch,'run_id':grant.run_id,'publication_id':completion},grant.actor_ref)
            from .results import Unconfirmed
            if type(result) is Unconfirmed:
                with r._gate_lock:r._models.dream_closing=round_identity
            await self.publish(result)
            if type(result) is Committed:await r._complete_drain()
            return result
        finally:
            # A rejected request releases only its own close barrier; another
            # concurrent finisher or an uncertain completion remains fenced.
            with r._gate_lock:
                r._models.dream_closers.pop(token,None)
