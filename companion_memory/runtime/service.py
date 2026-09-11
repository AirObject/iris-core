"""Bound local runtime coordination with durable evidence and finite task ownership.

This service never invokes a model during initialization or local recovery. Each
write uses its original operation identity; timed-out tasks keep the exclusive
owner until they actually finish. Unsupported external participants fail closed.
"""
from __future__ import annotations
import asyncio
from datetime import datetime,timezone
from dataclasses import dataclass,replace
import threading
import time
from companion_memory.provider.values import InvalidData
from types import MappingProxyType
from typing import cast,Callable,Coroutine
from weakref import WeakKeyDictionary,WeakValueDictionary

from companion_memory.configuration.persistence import StoredRuntimeConfiguration,stored_configuration_issue
from companion_memory.persistence import PersistenceService,ResultBoundCommand,RecoveryHandle
from companion_memory import persistence as storage_results
from companion_memory.persistence.schema import InvalidValue,valid_identifier,freeze_value
from companion_memory.ingress.events import canonical_event,isolate_event,event_identity,event_scope_prefix
from companion_memory.buffers import MaterialRecord,build_material
from .operation_wait import CallBudget,budget,bounded,expired
from .assembly import RuntimeAssembly
from .records import stored_event,stored_timestamp,DomainFailure,data,stable_id,digest,json_text
from .results import (
    Committed,NotCommitted,Rejected,Unconfirmed,RuntimeError,Found,NotFound,Failed,
    WorkDeferred,RuntimeReady,RecoveryPending,CloseReport,
)


@dataclass(frozen=True,slots=True)
class IngressGrant:
    """Trusted instance/host/entry identity; sender data cannot alter these bindings."""
    instance_id:str
    host_id:str
    platform_id:str
    entry_id:str
    actor_ref:str


class IngressPort:
    """Native bound original-event acceptance and original-key confirmation."""
    __slots__=('_service','__weakref__')
    def __new__(cls):raise TypeError('Bind ingress through trusted runtime assembly.')
    def __setattr__(self,name,value):raise AttributeError('Ingress capabilities are immutable.')
    async def accept_event(self,event:object):
        service=_ingress_service(self)
        return await service._accept(self,event,False) if service else Rejected(RuntimeError('ACCESS_DENIED','accept_event','identity','BINDING_MISMATCH'))
    async def resolve_acceptance(self,event_identity:object,original_event:object):
        service=_ingress_service(self)
        return await service._accept(self,original_event,True,expected_identity=event_identity) if service else Rejected(RuntimeError('ACCESS_DENIED','resolve_acceptance','identity','BINDING_MISMATCH'))
    async def lookup_acceptance(self,event_identity:object):
        service=_ingress_service(self)
        return await service._lookup(self,event_identity) if service else Failed(RuntimeError('ACCESS_DENIED','lookup_acceptance','identity','BINDING_MISMATCH'))


def _ingress_service(port:object):
    if type(port) is not IngressPort:return None
    try:service=object.__getattribute__(port,'_service')
    except AttributeError:return None
    return service if type(service) is RuntimeService else None


@dataclass(frozen=True,slots=True,weakref_slot=True)
class WorkCapability:
    """Issued work/owner binding; service identity registry prevents reconstructed grants."""
    work_id:str
    revision:int
    owner_generation:int
    entry_id:str


class RuntimeService:
    """Application-local transaction owner and runtime gate after upstream readiness."""
    def __init__(self,assembly:RuntimeAssembly,storage:PersistenceService,configuration:StoredRuntimeConfiguration,instance_id:str):
        if stored_configuration_issue(configuration) or not valid_identifier(instance_id):raise TypeError('Native stored configuration and instance identity are required.')
        self._assembly,self._storage,self._configuration,self._instance_id=assembly,storage,configuration,instance_id
        self._transactions=assembly.bind(storage,configuration,instance_id,lambda:datetime.now(timezone.utc))
        self._operations={d.operation_kind:storage.bind_operation(d,instance_id) for d in assembly.commands}
        self._definition={d.operation_kind:d for d in assembly.commands}
        self._logger=None
        self._lifecycle='NEW';self._mode='RECOVERING';self._epoch=0
        self._closing_owners:set[asyncio.Task]=set()
        self._jobs:set[asyncio.Task]=set();self._operation_jobs:set[asyncio.Task]=set();self._command_jobs:set[asyncio.Task]=set();self._ports:WeakKeyDictionary[IngressPort,IngressGrant]=WeakKeyDictionary();self._work:WeakValueDictionary[int,WorkCapability]=WeakValueDictionary()
        self._running:dict[str,asyncio.Task]={};self._owner_started:dict[str,float]={};self._uncertain:dict[str,tuple[str,RecoveryHandle]]={}
        self._gate_lock=threading.RLock();self._sending:set[str]=set();self._last_entry='';self._scheduler_active=False
        self._recovery_task:asyncio.Task | None=None
        self._initialization_task:asyncio.Task | None=None
        self._recovery_generation=time.time_ns()
        self._recovery_position:tuple[int,str]=(0,'')
        self._pending_checkpoint:tuple[str,dict[str,object],tuple[int,str]] | None=None
        from .model_work import ModelWork
        self._models=ModelWork(self)
        self.provider_gate=self._models.binding
        from .control import FocusControl
        self._focus=FocusControl(self)
        from .observation import Observations
        self._observations=Observations(self)

    def _operation_name(self,operation:str) -> str:
        if operation=='drain_complete':operation='transfer_dream_page'
        operation={'schedule':'request_learning','dream_call':'claim_work','initialize':'initialize_runtime','register':'register_entry','accept':'accept_event','freeze':'request_learning','claim':'claim_work','associate':'claim_work','observe_work':'recover_runtime','stage':'stage_candidate','finalize':'finalize_batch','focus_ready':'enter_focus','fault':'recover_runtime','transfer':'transfer_dream_page','recover_page':'recover_runtime'}.get(operation,operation)
        return operation

    def _error(self,operation:str,code:str,field:str,reason:str,pending:bool=False):
        return RuntimeError(code,self._operation_name(operation),field,reason,pending)

    def _map(self,kind:str,result:object,issue:DomainFailure | None=None):
        if type(result) is storage_results.Committed:return Committed(result.receipt,result.source)
        if type(result) is storage_results.NotCommitted and result.error is None:return NotCommitted(None)
        lower=getattr(result,'error',None)
        pending=getattr(lower,'cleanup_pending',False)
        reason=getattr(lower,'reason',None)
        if reason=='CONTENT_MISMATCH':error=self._error(kind,'IDEMPOTENCY_CONFLICT','identity','CONTENT_MISMATCH',pending)
        elif reason in ('ADMISSION_BUSY','LOCK_DEADLINE'):error=self._error(kind,'RESOURCE_BUSY','storage','LOCK_BUSY' if reason=='LOCK_DEADLINE' else 'ADMISSION_FULL',pending)
        elif reason=='OPERATION_DEADLINE':error=self._error(kind,'TIMEOUT','state','DEADLINE_EXCEEDED',pending)
        elif reason in ('INVALID_SHAPE','UNSUPPORTED_COMMAND','LIMIT_EXCEEDED'):error=self._error(kind,'INVALID_INPUT','event' if kind=='accept' else 'state','LIMIT_EXCEEDED' if reason=='LIMIT_EXCEEDED' else 'INVALID_SHAPE',pending)
        elif reason=='CAPABILITY_MISMATCH':error=self._error(kind,'ACCESS_DENIED','identity','BINDING_MISMATCH',pending)
        elif reason in ('NOT_INITIALIZED','SERVICE_FAULTED','SERVICE_CLOSED'):error=self._error(kind,'INVALID_STATE','state','NOT_READY' if reason=='NOT_INITIALIZED' else reason,pending)
        elif reason in ('SCHEMA_VERSION_UNSUPPORTED','SCHEMA_MISMATCH','FOREIGN_DATABASE','DATABASE_ID_MISMATCH'):error=self._error(kind,'STORAGE_FAILED','storage','FORMAT_UNSUPPORTED',pending)
        elif reason=='DATA_INCONSISTENT':error=self._error(kind,'STORAGE_FAILED','storage','INTEGRITY_FAILURE',pending)
        elif reason=='PARTICIPANT_REJECTED' and issue is not None:
            error=self._error(kind,issue.code,issue.field,issue.reason,pending)
        else:error=self._error(kind,'STORAGE_FAILED','storage','COMMIT_UNCONFIRMED' if type(result) is storage_results.Unconfirmed else 'WRITE_NOT_COMMITTED',pending)
        if type(result) is storage_results.Unconfirmed:return Unconfirmed(result.recovery_handle,error)
        if type(result) is storage_results.NotCommitted:return NotCommitted(error)
        return Rejected(error)

    async def _bounded_call[R](self,operation:str,call:Callable[[],Coroutine[object,object,R]],read_only:bool) -> R | Failed | Rejected | Unconfirmed:
        """A whole operation has one waiting deadline across all preparation reads."""
        if budget.get() is not None:return await call()
        if len(self._operation_jobs)>=self._configuration.candidate.runtime.integer('runtime.max_active_entries'):
            error=self._error(operation,'RESOURCE_BUSY','state','ADMISSION_FULL')
            return Failed(error) if read_only else Rejected(error)
        current=CallBudget(time.monotonic()+self._configuration.candidate.runtime.integer('runtime.operation_timeout_ms')/1000,owner=asyncio.current_task())
        async def owned():
            token=budget.set(current)
            try:return await call()
            except DomainFailure as failure:
                error=self._error(operation,failure.code,failure.field,failure.reason,failure.cleanup_pending)
                return Failed(error) if read_only else Rejected(error)
            finally:budget.reset(token)
        task=asyncio.create_task(owned());self._jobs.add(task);self._operation_jobs.add(task)
        task.add_done_callback(self._finished);task.add_done_callback(self._operation_jobs.discard)
        try:done,_=await asyncio.wait((task,),timeout=max(0,current.deadline-time.monotonic()))
        except asyncio.CancelledError:
            current.stopped=True
            raise
        if done:return task.result()
        current.stopped=True
        error=self._error(operation,'TIMEOUT','state','DEADLINE_EXCEEDED',True)
        if current.reference is not None and not read_only:return Unconfirmed(current.reference,error)
        return Failed(error) if read_only else Rejected(error)

    def _owner_issue(self,operation:str) -> RuntimeError | None:
        """Released instances and all their capabilities lose resource authority."""
        lease=self._transactions.lease
        if self._lifecycle=='CLOSED' or lease is None or not lease.is_active():
            return self._error(operation,'INVALID_STATE','state','SERVICE_CLOSED')
        return None

    def _can_write(self,kind:str) -> bool:
        if self._owner_issue(kind) is not None:return False
        if self._lifecycle=='READY':return True
        if self._lifecycle=='RECOVERING':
            current=budget.get()
            owner=current.owner if current is not None else asyncio.current_task()
            return owner in (self._initialization_task,self._recovery_task) and kind in ('initialize','recover_page','observe_work','stage','finalize','focus_ready','finish_focus','fault','drain_complete')
        if self._lifecycle=='CLOSING' and kind in ('observe_work','stage','finalize'):
            current=budget.get()
            return asyncio.current_task() in self._closing_owners or current is not None and current.owner in self._closing_owners
        return False

    async def _execute(self,kind:str,key:object,values:dict[str,object],actor:str,*,resolve:bool=False):
        issue=self._owner_issue(kind)
        if issue is not None:return Rejected(issue)
        if expired():return Rejected(self._error(kind,'TIMEOUT','state','DEADLINE_EXCEEDED'))
        confirmation_only=not resolve and not self._can_write(kind)
        resolve=resolve or confirmation_only
        if kind in ('enter_focus','focus_ready','finish_focus','fault'):values={**values,'actor_ref':actor}
        values={**values,'coordination_key':key}
        try:owned=freeze_value(self._definition[kind].input_schema,values)
        except InvalidValue:return Rejected(self._error(kind,'INVALID_INPUT','state','INVALID_SHAPE'))
        assert type(owned) is MappingProxyType
        import json
        command=ResultBoundCommand(1,json.loads(json_text(owned)),{r.event_slot:{'actor':actor} for r in self._definition[kind].required_audits})
        port=self._operations[kind]
        handle=port.recovery_handle(key,command)
        if type(handle) is not RecoveryHandle:return self._map(kind,handle)
        if len(self._command_jobs)>=self._configuration.candidate.runtime.integer('runtime.max_active_entries'):return Rejected(self._error(kind,'RESOURCE_BUSY','state','ADMISSION_FULL'))
        current=budget.get()
        if current is not None and not resolve:current.reference=handle
        cause_key,cause_slot=self._transactions.causes.watch(kind,owned)
        deadline=time.monotonic()+self._configuration.candidate.runtime.integer('runtime.operation_timeout_ms')/1000
        if current is not None:deadline=min(deadline,current.deadline)
        task=asyncio.create_task(port.resolve_operation(handle) if resolve else self._local_command(port,key,command,deadline))
        self._jobs.add(task);self._command_jobs.add(task)
        observation=self._observations.started(kind,handle,values,task)
        task.add_done_callback(self._finished);task.add_done_callback(self._command_jobs.discard)
        task.add_done_callback(lambda finished:self._transactions.causes.release(cause_key))
        done,_=await asyncio.wait((task,),timeout=self._configuration.candidate.runtime.integer('runtime.operation_timeout_ms')/1000)
        if not done:
            observation.unknown()
            work_id=values.get('work_id',values.get('batch_id'))
            if type(work_id) is str:self._uncertain[work_id]=(kind,handle)
            return Unconfirmed(handle,self._error(kind,'TIMEOUT','storage','DEADLINE_EXCEEDED',True))
        result=self._map(kind,task.result(),cause_slot.issue)
        if confirmation_only and type(result) is NotCommitted:
            return Rejected(self._error(kind,'INVALID_STATE','state','SERVICE_CLOSED' if self._lifecycle in ('CLOSING','CLOSED') else 'NOT_READY'))
        if kind=='accept' and resolve and (type(result) is Rejected or type(result) is NotCommitted or type(result) is Unconfirmed) and result.error is not None:
            result=replace(result,error=replace(result.error,operation='resolve_acceptance'))
        work_id=values.get('work_id',values.get('batch_id'))
        if type(work_id) is str and type(result) is Unconfirmed:self._uncertain[work_id]=(kind,handle)
        if type(work_id) is str and type(result) is Committed:self._uncertain.pop(work_id,None)
        if type(result) is Committed and self._logger is not None:
            try:
                self._logger.emit({'level':'INFO','event_code':'OPERATION_COMPLETED','context':{},'attributes':{'count':1,'outcome':'SUCCESS'}})
            except MemoryError:raise
            except Exception:pass
        return result

    async def _local_command(self,port,key,command,deadline:float):
        """Retry only a proven rejected/rolled-back local contention attempt."""
        result=await port.execute(key,command)
        remaining=self._configuration.candidate.runtime.integer('runtime.local_retry_limit')
        while remaining and time.monotonic()<deadline:
            if type(result) is not storage_results.Rejected and type(result) is not storage_results.NotCommitted:break
            cause=result.error
            if cause is None or cause.cleanup_pending or cause.reason not in ('ADMISSION_BUSY','LOCK_DEADLINE'):break
            remaining-=1
            await asyncio.sleep(0)
            result=await port.execute(key,command)
        return result

    def _finished(self,task:asyncio.Task) -> None:
        if not task.cancelled():task.exception()
        self._jobs.discard(task)

    async def initialize_runtime(self) -> RuntimeReady | RecoveryPending | Rejected:
        """Initialize only after storage/configuration and the trusted Provider layer.

        Standalone local binding leaves model capability absent. Recovery never
        issues model calls, and an incomplete call association blocks readiness.
        """
        upstream=self._upstream_pending()
        if upstream is not None:return upstream
        if self._lifecycle=='READY':return RuntimeReady(self._configuration.snapshot_id)
        if self._lifecycle not in ('NEW','RECOVERING'):return Rejected(self._error('initialize_runtime','INVALID_STATE','state','SERVICE_CLOSED'))
        if self._initialization_task is not None and not self._initialization_task.done():return RecoveryPending('RUNTIME','OWNER_ACTIVE',True)
        task=asyncio.create_task(self._initialize_runtime())
        self._initialization_task=task;self._jobs.add(task);task.add_done_callback(self._finished)
        done,_=await asyncio.wait((task,),timeout=self._configuration.candidate.runtime.integer('runtime.recovery_timeout_ms')/1000)
        return task.result() if done else RecoveryPending('RUNTIME','DEADLINE_EXCEEDED',True)

    def _upstream_pending(self) -> RecoveryPending | None:
        """An upstream failure blocks every downstream read and keeps its own stage."""
        storage=self._storage.get_health()
        if storage.lifecycle!='READY':return RecoveryPending('STORAGE','NOT_READY',storage.cleanup_pending)
        if not self._models.ready():
            provider=self._models.provider
            return RecoveryPending('PROVIDER','NOT_READY',bool(provider and provider.get_health().cleanup_pending))
        return None

    def _recovery_failure(self,reason:str) -> RecoveryPending:
        upstream=self._upstream_pending()
        if upstream is not None:return upstream
        health=self._storage.get_health()
        return RecoveryPending('RUNTIME',reason,health.cleanup_pending or bool(health.reads_in_flight or health.writes_in_flight))

    def _recovery_commit_failure(self,result:object) -> RecoveryPending:
        error=getattr(result,'error',None)
        reason=getattr(error,'reason',None)
        return self._recovery_failure(reason if reason in ('INTEGRITY_FAILURE','FORMAT_UNSUPPORTED','SERVICE_CLOSED') else 'COMMIT_UNCONFIRMED')

    async def _initialize_runtime(self) -> RuntimeReady | RecoveryPending:
        if self._lifecycle in ('CLOSING','CLOSED'):return RecoveryPending('RUNTIME','SERVICE_CLOSED',False)
        deadline=time.monotonic()+self._configuration.candidate.runtime.integer('runtime.recovery_timeout_ms')/1000
        self._lifecycle='RECOVERING'
        try:
            mode=await self._transactions.rows.load('mode','mode')
            if mode is None:
                result=await self._execute('initialize','initialize:'+self._configuration.snapshot_id,{'config_snapshot_id':self._configuration.snapshot_id},'bootstrap')
                if type(result) is not Committed:return RecoveryPending('RUNTIME','COMMIT_UNCONFIRMED',bool(self._jobs))
            result=await self._begin_recovery(deadline)
            return result
        except DomainFailure as failure:
            return self._recovery_failure(failure.reason)

    async def recover_runtime(self) -> RuntimeReady | RecoveryPending:
        """Validate local rows under a finite total deadline; no remote work is replayed."""
        return await self._begin_recovery(time.monotonic()+self._configuration.candidate.runtime.integer('runtime.recovery_timeout_ms')/1000)

    async def _begin_recovery(self,deadline:float) -> RuntimeReady | RecoveryPending:
        if self._lifecycle in ('CLOSING','CLOSED'):return RecoveryPending('RUNTIME','SERVICE_CLOSED',bool(self._jobs))
        upstream=self._upstream_pending()
        if upstream is not None:return upstream
        if self._recovery_task is not None and not self._recovery_task.done():return RecoveryPending('RUNTIME','OWNER_ACTIVE',True)
        if self._running:return RecoveryPending('RUNTIME','OWNER_ACTIVE',True)
        if self._lifecycle=='READY':
            self._recovery_generation=time.time_ns();self._recovery_position=(0,'');self._pending_checkpoint=None
        self._recovery_task=asyncio.create_task(self._recover(deadline))
        task=self._recovery_task;self._jobs.add(task);task.add_done_callback(self._finished)
        done,_=await asyncio.wait((task,),timeout=max(0,deadline-time.monotonic()))
        return task.result() if done else RecoveryPending('RUNTIME','DEADLINE_EXCEEDED',True)

    async def _recover(self,deadline:float) -> RuntimeReady | RecoveryPending:
        if self._lifecycle in ('CLOSING','CLOSED'):return RecoveryPending('RUNTIME','SERVICE_CLOSED',False)
        self._lifecycle='RECOVERING';self._mode='RECOVERING'
        try:
            for work_id,(kind,handle) in tuple(self._uncertain.items()):
                confirmed=await self._operations[kind].resolve_operation(handle)
                if type(confirmed) is not storage_results.Committed and type(confirmed) is not storage_results.NotCommitted:return RecoveryPending('RUNTIME','COMMIT_UNCONFIRMED',self._storage.get_health().cleanup_pending)
                self._uncertain.pop(work_id,None)
            mode=await self._transactions.rows.load('mode','mode')
            if mode is None or data(mode)['configuration_id']!=self._configuration.snapshot_id:return RecoveryPending('RUNTIME','CONFIGURATION_MISMATCH',False)
            if self._pending_checkpoint is not None:
                pending_key,pending_values,position=self._pending_checkpoint
                confirmation=await self._execute('recover_page',pending_key,pending_values,'recovery',resolve=True)
                if type(confirmation) is NotCommitted:confirmation=await self._execute('recover_page',pending_key,pending_values,'recovery')
                if type(confirmation) is not Committed:return self._recovery_commit_failure(confirmation)
                self._recovery_position=position;self._pending_checkpoint=None
            # Enumerate bounded metadata pages, then verify every referenced body.
            limit=self._configuration.candidate.runtime.integer('runtime.read_page_size')
            layouts=[(owned,table) for owned,tables in ((self._transactions.ingress.rows,('entries','events','payloads')),(self._transactions.buffers.rows,('positions','references','batches','members','entry_state')),(self._transactions.rows,('work','triggers','transfers','scheduler','dream_calls','recovery'))) for table in tables]
            for index,(owner,table) in enumerate(layouts):
                if index>=self._recovery_position[0]:
                    after=self._recovery_position[1] if index==self._recovery_position[0] else ''
                    while True:
                        if self._lifecycle in ('CLOSING','CLOSED'):return RecoveryPending('RUNTIME','SERVICE_CLOSED',False)
                        if time.monotonic()>=deadline:return RecoveryPending('RUNTIME','DEADLINE_EXCEEDED',False)
                        upstream=self._upstream_pending()
                        if upstream is not None:return upstream
                        page=await owner.read(table+'_history_page' if table=='recovery' else table+'_page',{'after':after,'limit':limit,**({'generation':self._recovery_generation} if table=='recovery' else {})})
                        if not page:break
                        for meta in page:
                            row=await owner.load(table,cast(str,meta['object_id']))
                            if row is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
                            if table=='work' and row['state'] in ('EXECUTING','REMOTE_RESULT_UNKNOWN','CANDIDATE_STORED'):
                                if not await self._models.recover(row):return RecoveryPending('RUNTIME','COMMIT_UNCONFIRMED',False)
                        page_end=cast(str,page[-1]['object_id'])
                        if table=='recovery':
                            for meta in page:
                                checkpoint=await owner.load(table,cast(str,meta['object_id']))
                                if checkpoint is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
                                self._transactions.verify_checkpoint(checkpoint)
                            after=page_end;self._recovery_position=(index,after)
                            continue
                        current=[]
                        for meta in page:
                            checked=await owner.load(table,cast(str,meta['object_id']))
                            if checked is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
                            current.append({'object_id':checked['object_id'],'revision':checked['revision']})
                        key=stable_id('recovery_page',self._recovery_generation,owner.owner,table,page_end)
                        checkpoint_values:dict[str,object]={'generation':self._recovery_generation,'owner':owner.owner,'table':table,'after':page_end,'rows':current}
                        self._pending_checkpoint=(key,checkpoint_values,(index,page_end))
                        committed=await self._execute('recover_page',key,checkpoint_values,'recovery')
                        if type(committed) is not Committed:return self._recovery_commit_failure(committed)
                        after=page_end;self._recovery_position=(index,after);self._pending_checkpoint=None
                    self._recovery_position=(index+1,'')
            if mode['state'] in ('DREAM_PREPARING','DREAM_FOCUSED'):
                run=cast(str,data(mode)['run_id']);epoch=cast(int,data(mode)['epoch'])
                publication=self._transactions.publication
                recovery=await publication.recover_publication(run,self._configuration.snapshot_id) if publication else None
                if mode['state']=='DREAM_FOCUSED' and recovery is not None:
                    from .assembly import PublicationRecovery
                    if type(recovery) is not PublicationRecovery or recovery.run_id!=run or recovery.configuration_id!=self._configuration.snapshot_id or recovery.expected_epoch!=epoch:return RecoveryPending('RUNTIME','INTEGRITY_FAILURE',False)
                    result=await self._execute('finish_focus',recovery.operation_key,{'expected_epoch':epoch,'run_id':run,'publication_id':recovery.publication_id},recovery.actor_ref)
                elif mode['state']=='DREAM_PREPARING':
                    result=await self._execute('focus_ready',stable_id('focus_ready',run),{'expected_epoch':epoch,'run_id':run},cast(str,data(mode)['actor_ref']))
                    if type(result) is not Committed:
                        result=await self._execute('fault',stable_id('focus_fault',run),{'expected_epoch':epoch,'run_id':run},cast(str,data(mode)['actor_ref']))
                else:
                    result=await self._execute('fault',stable_id('focus_fault',run),{'expected_epoch':epoch,'run_id':run},cast(str,data(mode)['actor_ref']))
                if type(result) is not Committed:return RecoveryPending('RUNTIME','COMMIT_UNCONFIRMED',bool(self._jobs))
                mode=await self._transactions.rows.load('mode','mode')
                if mode is None:return RecoveryPending('RUNTIME','INTEGRITY_FAILURE',False)
            if self._lifecycle in ('CLOSING','CLOSED'):return RecoveryPending('RUNTIME','SERVICE_CLOSED',False)
            upstream=self._upstream_pending()
            if upstream is not None:return upstream
            self._epoch=cast(int,data(mode)['epoch']);self._mode=cast(str,mode['state'])
            completed=await self._complete_drain()
            if completed is not None and type(completed) is not Committed:return self._recovery_commit_failure(completed)
            if self._lifecycle in ('CLOSING','CLOSED'):return RecoveryPending('RUNTIME','SERVICE_CLOSED',False)
            self._lifecycle='READY'
            self._models.closing_gate=self._mode not in ('NORMAL','DRAINING')
            return RuntimeReady(self._configuration.snapshot_id)
        except DomainFailure as failure:
            return self._recovery_failure(failure.reason)
        except (KeyError,ValueError,TypeError):
            return self._recovery_failure('INTEGRITY_FAILURE')

    def attach_logger(self,logger) -> None:
        """Trusted unified diagnostic capability; it receives no audit or receipt body."""
        from companion_memory.logging_service import Logger
        if type(logger) is not Logger:raise TypeError('A native unified logger is required.')
        self._logger=logger

    def attach_provider(self,provider) -> None:
        """Trusted upstream resource binding before runtime initialization."""
        self._models.attach(provider)

    def bind_runtime_observer(self,grant):
        """Trusted issuance of metadata-only instance or entry scope."""
        return self._observations.bind(grant)

    @bounded('get_batch_snapshot',read_only=True)
    async def get_batch_snapshot(self,work:WorkCapability):
        """Only the issued work owner can read original frozen material."""
        if type(work) is not WorkCapability or self._work.get(id(work)) is not work:return Failed(self._error('get_batch_snapshot','ACCESS_DENIED','work','BINDING_MISMATCH'))
        issue=self._owner_issue('get_batch_snapshot')
        if issue is not None:return Failed(issue)
        try:
            messages,batch,current=await self._models.material(work.work_id)
            if data(current)['generation']!=work.owner_generation:return Failed(self._error('get_batch_snapshot','PRECONDITION_FAILED','work','WORK_FENCED'))
            return Found(MappingProxyType({'messages':messages,'batch_id':work.work_id,'configuration_id':self._configuration.snapshot_id}))
        except DomainFailure as failure:return Failed(self._error('get_batch_snapshot',failure.code,failure.field,failure.reason,failure.cleanup_pending))

    @bounded('transfer_dream_page',read_only=False)
    async def transfer_dream_page(self,entry_work:IngressPort,expected_cursor:object):
        """Move one scoped FIFO page without dropping its pending references."""
        grant=self._ports.get(entry_work) if type(entry_work) is IngressPort else None
        if grant is None:return Rejected(self._error('transfer_dream_page','ACCESS_DENIED','entry','BINDING_MISMATCH'))
        issue=self._owner_issue('transfer_dream_page')
        if issue is not None:return Rejected(issue)
        if type(expected_cursor) is not int or not 0<=expected_cursor<2**63:return Rejected(self._error('transfer_dream_page','INVALID_INPUT','entry','INVALID_SHAPE'))
        key=stable_id('transfer',grant.entry_id,expected_cursor)
        original=await self._operations['transfer'].read_receipt(key)
        if type(original) is storage_results.NotFound and self._can_write('transfer'):
            if self._mode not in ('NORMAL','DRAINING'):return Rejected(self._error('transfer_dream_page','MODE_BLOCKED','mode','DREAMING'))
            state=await self._transactions.buffers.rows.load('entry_state',grant.entry_id)
            if state is None:raise DomainFailure('STORAGE_FAILED','entry','INTEGRITY_FAILURE')
            if data(state)['transfer_cursor']!=expected_cursor:return Rejected(self._error('transfer_dream_page','PRECONDITION_FAILED','entry','TRANSFER_CURSOR_CHANGED'))
            page=await self._transactions.buffers.rows.read('positions_entry',{'entry_id':grant.entry_id,'state':'STAGED','after_sequence':0,'after_id':'','limit':1})
            if not page:
                completed=await self._complete_drain()
                if completed is not None and type(completed) is not Committed:return completed
                return Rejected(self._error('transfer_dream_page','INVALID_STATE','state','NOT_READY'))
        elif type(original) is not storage_results.Found and type(original) is not storage_results.NotFound:
            return Rejected(self._error('transfer_dream_page','STORAGE_FAILED','storage','READ_FAILED'))
        result=await self._execute('transfer',stable_id('transfer',grant.entry_id,expected_cursor),{'entry_id':grant.entry_id,'expected_cursor':expected_cursor},'scheduler')
        return await self._focus.publish(result)

    async def _complete_drain(self):
        """Close an empty instance backlog, including an instance with no entries."""
        if self._mode!='DRAINING':return None
        pending=await self._transactions.buffers.rows.read('entry_pending_count',{'state':'PENDING'})
        if pending[0]['count']:return None
        result=await self._execute('drain_complete',stable_id('drain_complete',self._epoch),{'expected_epoch':self._epoch},'scheduler')
        return await self._focus.publish(result)

    def bind_focus_coordinator(self,grant):
        """Trusted internal run authority; no event/Web control surface."""
        return self._focus.bind(grant)

    async def run_work(self,work:WorkCapability):
        """Run one claimed frozen batch through the independent Provider."""
        if type(work) is not WorkCapability or self._work.get(id(work)) is not work:return Rejected(self._error('claim_work','ACCESS_DENIED','work','BINDING_MISMATCH'))
        issue=self._owner_issue('claim_work')
        if issue is not None:return Rejected(issue)
        original=await self._operations['finalize'].read_receipt(stable_id('terminal',work.work_id))
        if type(original) is storage_results.Found:return Committed(original.value,'EXISTING')
        if type(original) is not storage_results.NotFound:return Rejected(self._error('claim_work','STORAGE_FAILED','storage','READ_FAILED'))
        if self._lifecycle!='READY':return Rejected(self._error('claim_work','INVALID_STATE','state','SERVICE_CLOSED' if self._lifecycle=='CLOSING' else 'NOT_READY'))
        if work.work_id in self._running:return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        if len(self._running)>=self._configuration.candidate.runtime.integer('runtime.max_active_entries'):return WorkDeferred('BLOCKED','ADMISSION_FULL')
        task=asyncio.create_task(self._models.run(work));self._jobs.add(task);self._running[work.work_id]=task;self._owner_started[work.work_id]=time.monotonic()
        task.add_done_callback(self._finished)
        task.add_done_callback(lambda completed:self._work_finished(work.work_id,completed))
        done,_=await asyncio.wait((task,),timeout=self._configuration.candidate.runtime.integer('runtime.operation_timeout_ms')/1000)
        if not done:return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        try:return task.result()
        except DomainFailure as failure:return Rejected(self._error('stage_candidate',failure.code,failure.field,failure.reason,failure.cleanup_pending))

    def _work_finished(self,work_id:str,completed:asyncio.Task):
        provider=self._models.provider
        if self._storage.get_health().writes_in_flight or provider is not None and (provider.get_health().in_flight or provider.get_health().cleanup_pending):
            task=asyncio.create_task(self._wait_provider_cleanup(work_id))
            self._running[work_id]=task;self._jobs.add(task);task.add_done_callback(self._finished)
        else:
            self._running.pop(work_id,None);self._owner_started.pop(work_id,None)
            self._models.release_grants(work_id)

    async def _wait_provider_cleanup(self,work_id:str):
        provider=self._models.provider
        while self._storage.get_health().writes_in_flight or provider is not None and (provider.get_health().in_flight or provider.get_health().cleanup_pending):await asyncio.sleep(0.01)
        self._running.pop(work_id,None);self._owner_started.pop(work_id,None);self._sending.discard(work_id);self._models.release_grants(work_id)

    @bounded('register_entry',read_only=False)
    async def register_entry(self,operation_key:object,registration:object):
        if type(registration) is not dict or any(type(k) is not str for k in registration) or set(registration)!= {'instance_id','host_id','platform_id','external_entry_id'}:return Rejected(self._error('register_entry','INVALID_INPUT','entry','INVALID_SHAPE'))
        if any(type(v) is not str for v in registration.values()):return Rejected(self._error('register_entry','INVALID_INPUT','entry','INVALID_SHAPE'))
        if registration['instance_id']!=self._instance_id:return Rejected(self._error('register_entry','ACCESS_DENIED','identity','BINDING_MISMATCH'))
        try:self._configuration.candidate.platform(registration['platform_id'])
        except (ValueError,TypeError):return Rejected(self._error('register_entry','CONFIGURATION_UNSUPPORTED','configuration','CONFIGURATION_REQUIRED'))
        return await self._execute('register',operation_key,registration,'bootstrap')

    @bounded('bind_ingress',read_only=False)
    async def bind_ingress(self,principal:IngressGrant) -> IngressPort | Rejected:
        """Trusted authorization boundary verifies registration before issuing scope."""
        if type(principal) is not IngressGrant or not valid_identifier(principal.instance_id) or principal.instance_id!=self._instance_id or not all(valid_identifier(v) for v in (principal.host_id,principal.platform_id,principal.entry_id,principal.actor_ref)):
            return Rejected(self._error('bind_ingress','ACCESS_DENIED','identity','BINDING_MISMATCH'))
        issue=self._owner_issue('bind_ingress')
        if issue is not None:return Rejected(issue)
        if self._lifecycle=='CLOSING':return Rejected(self._error('bind_ingress','INVALID_STATE','state','SERVICE_CLOSED'))
        try:entry=await self._transactions.ingress.rows.load('entries',principal.entry_id)
        except DomainFailure as failure:return Rejected(self._error('bind_ingress',failure.code,failure.field,failure.reason,failure.cleanup_pending))
        if entry is None or any(data(entry)[k]!=getattr(principal,k) for k in ('instance_id','host_id','platform_id')):return Rejected(self._error('bind_ingress','ACCESS_DENIED','entry','BINDING_MISMATCH'))
        port=object.__new__(IngressPort);object.__setattr__(port,'_service',self);self._ports[port]=principal
        return port

    def _event_key(self,grant:IngressGrant,event:MappingProxyType) -> str:
        return event_identity((grant.instance_id,grant.host_id,grant.entry_id),event)[0]

    @bounded('accept_event',read_only=False)
    async def _accept(self,port:IngressPort,source:object,resolve:bool,expected_identity:object=None):
        grant=self._ports.get(port) if type(port) is IngressPort else None
        if grant is None:return Rejected(self._error('accept_event','ACCESS_DENIED','identity','BINDING_MISMATCH'))
        issue=self._owner_issue('accept_event')
        if issue is not None:return Rejected(issue)
        try:event=isolate_event(source,8192)
        except InvalidValue:return Rejected(self._error('accept_event','INVALID_INPUT','event','INVALID_SHAPE'))
        key=self._event_key(grant,event)
        if resolve and (type(expected_identity) is not str or expected_identity!=key):return Rejected(self._error('resolve_acceptance','ACCESS_DENIED','identity','BINDING_MISMATCH'))
        values:dict[str,object]={'entry_id':grant.entry_id,'event':canonical_event(event).decode()}
        if resolve:return await self._execute('accept',key,values,grant.actor_ref,resolve=True)
        existing=await self._operations['accept'].read_receipt(key)
        if type(existing) is storage_results.Found:
            return await self._execute('accept',key,values,grant.actor_ref,resolve=True)
        if type(existing) is not storage_results.NotFound:return Rejected(self._error('accept_event','STORAGE_FAILED','storage','READ_FAILED'))
        if self._lifecycle!='READY':return Rejected(self._error('accept_event','MODE_BLOCKED','mode','RECOVERING'))
        if event['media'] and self._assembly.media is None:return Rejected(self._error('accept_event','CAPABILITY_UNAVAILABLE','event','MEDIA_NOT_SUPPORTED'))
        if len(canonical_event(event))>self._configuration.candidate.runtime.integer('ingress.event_max_bytes'):return Rejected(self._error('accept_event','INVALID_INPUT','event','LIMIT_EXCEEDED'))
        return await self._execute('accept',key,values,grant.actor_ref)

    @bounded('lookup_acceptance',read_only=True)
    async def _lookup(self,port:IngressPort,key:object):
        grant=self._ports.get(port) if type(port) is IngressPort else None
        if grant is None:return Failed(self._error('lookup_acceptance','ACCESS_DENIED','identity','BINDING_MISMATCH'))
        issue=self._owner_issue('lookup_acceptance')
        if issue is not None:return Failed(issue)
        if not valid_identifier(key) or not cast(str,key).startswith(event_scope_prefix((grant.instance_id,grant.host_id,grant.entry_id))):return Failed(self._error('lookup_acceptance','ACCESS_DENIED','identity','BINDING_MISMATCH'))
        result=await self._operations['accept'].read_receipt(key)
        if type(result) is storage_results.Found:
            value=result.value.result
            if type(value) is not MappingProxyType or value['entry_id']!=grant.entry_id:return Failed(self._error('lookup_acceptance','ACCESS_DENIED','entry','BINDING_MISMATCH'))
            return Found(value)
        if type(result) is storage_results.NotFound:return NotFound()
        return Failed(self._error('lookup_acceptance','STORAGE_FAILED','storage','READ_FAILED'))

    @bounded('request_learning',read_only=False)
    async def request_learning(self,trigger:object):
        if type(trigger) is not dict or any(type(k) is not str for k in trigger) or set(trigger)!= {'entry_id','trigger_key','type'} or type(trigger['type']) is not str or trigger['type']!='THRESHOLD':return Rejected(self._error('request_learning','INVALID_INPUT','trigger','INVALID_SHAPE'))
        if not valid_identifier(trigger['entry_id']) or not valid_identifier(trigger['trigger_key']):return Rejected(self._error('request_learning','INVALID_INPUT','trigger','INVALID_IDENTIFIER'))
        issue=self._owner_issue('request_learning')
        if issue is not None:return Rejected(issue)
        if self._assembly.participant is None:return Rejected(self._error('request_learning','CAPABILITY_UNAVAILABLE','participant','LEARNING_PARTICIPANT_MISSING'))
        entry_id=trigger['entry_id'];tx=self._transactions
        trigger_identity=stable_id('trigger',entry_id,trigger['type'],trigger['trigger_key'])
        original=await self._operations['freeze'].read_receipt(trigger_identity)
        if type(original) is storage_results.Found:return Committed(original.value,'EXISTING')
        if type(original) is not storage_results.NotFound:return Rejected(self._error('request_learning','STORAGE_FAILED','storage','READ_FAILED'))
        if self._lifecycle!='READY':return WorkDeferred('NOT_READY','RECOVERING')
        if self._mode not in ('NORMAL','DRAINING'):return WorkDeferred('BLOCKED','DREAMING')
        try:
            entry=await tx.ingress.rows.load('entries',entry_id)
            if entry is None:return WorkDeferred('NOT_READY','BINDING_MISMATCH')
            platform=self._configuration.candidate.platform(cast(str,data(entry)['platform_id']))
            t,r=platform.count('target_count'),platform.count('recent_context_count')
            positions=await tx.buffers.rows.read('positions_entry',{'entry_id':entry_id,'state':'NORMAL','after_sequence':0,'after_id':'','limit':t+r})
            if len(positions)<t+r:return WorkDeferred('NO_TARGET','NOT_READY')
            state=await tx.buffers.rows.load('entry_state',entry_id)
            if state is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
            history=cast(list[str],data(state)['history'])
            ids=history+[cast(str,p['object_id']) for p in positions]
            roles=['H']*len(history)+['T']*t+['R']*r
            batch_id=stable_id('batch',entry_id,trigger['trigger_key']);run_id=stable_id('learning',batch_id)
            material=[];members=[]
            for mid,role in zip(ids,roles):
                event=await tx.ingress.rows.load('events',mid);payload=await tx.ingress.rows.load('payloads',mid)
                if event is None or payload is None:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
                text=cast(str,data(payload)['payload'])
                if digest(text)!=data(event)['payload_digest']:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
                time_us=cast(int,data(event)['received_at_us'])
                timestamp=stored_timestamp(time_us)
                material.append(MaterialRecord(role,mid,cast(int,event['sequence']),timestamp,stored_event(text)))
                members.append({'message_id':mid,'entry_seq':event['sequence'],'role':role,'payload_digest':data(event)['payload_digest']})
            identities=(self._instance_id,cast(str,data(entry)['host_id']),platform.platform_id,entry_id,batch_id,run_id,self._configuration.snapshot_id)
            messages=build_material(identities,tuple(material))
            if sum(len(m['text'].encode()) for m in messages)>self._configuration.candidate.runtime.integer('learning.material_max_bytes'):return WorkDeferred('BLOCKED','CONTEXT_LIMIT')
            return await self._execute('freeze',trigger_identity,{'entry_id':entry_id,'run_id':run_id,'batch_id':batch_id,'material_digest':digest(tuple(dict(m) for m in messages)),'members':members},'scheduler')
        except DomainFailure as failure:return Rejected(self._error('request_learning',failure.code,failure.field,failure.reason,failure.cleanup_pending))
        except InvalidValue:return Rejected(self._error('request_learning','STORAGE_FAILED','storage','INTEGRITY_FAILURE'))

    async def run_ready_cycle(self):
        """Persist a bounded round-robin cursor; consider each entry at most once."""
        if self._scheduler_active:return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        if self._lifecycle!='READY' or self._mode not in ('NORMAL','DRAINING'):return WorkDeferred('BLOCKED','DREAMING')
        if len(self._running)>=self._configuration.candidate.runtime.integer('runtime.max_active_entries'):return WorkDeferred('BLOCKED','ADMISSION_FULL')
        self._scheduler_active=True
        try:
            cursor=await self._transactions.rows.load('scheduler','scheduler')
            if cursor is None:raise DomainFailure('STORAGE_FAILED','state','INTEGRITY_FAILURE')
            limit=self._configuration.candidate.runtime.integer('runtime.read_page_size')
            page=await self._transactions.ingress.rows.read('entries_page',{'after':data(cursor)['after'],'limit':limit})
            if not page:page=await self._transactions.ingress.rows.read('entries_page',{'after':'','limit':limit})
            for entry in page:
                eid=cast(str,entry['object_id'])
                advanced=await self._execute('schedule',stable_id('schedule',cursor['revision'],eid),{'entry_id':eid,'expected_revision':cursor['revision']},'scheduler')
                if type(advanced) is not Committed:return advanced
                cursor=await self._transactions.rows.load('scheduler','scheduler')
                if cursor is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
                positions=await self._transactions.buffers.rows.read('positions_entry',{'entry_id':eid,'state':'NORMAL','after_sequence':0,'after_id':'','limit':1})
                if not positions:continue
                active=await self._transactions.buffers.rows.read('batches_entry',{'entry_id':eid,'state':'FROZEN','after_sequence':0,'after_id':'','limit':1})
                if active:bid=cast(str,active[0]['object_id'])
                else:
                    trigger={'entry_id':eid,'trigger_key':stable_id('threshold',eid,positions[0]['sequence']),'type':'THRESHOLD'}
                    frozen=await self.request_learning(trigger)
                    if type(frozen) is not Committed:continue
                    bid=cast(str,cast(MappingProxyType,frozen.receipt.result)['object_id'])
                work=await self._transactions.rows.load('work',bid)
                if work is None:raise DomainFailure('STORAGE_FAILED','work','INTEGRITY_FAILURE')
                if work['state']!='FROZEN':continue
                owned=await self.claim_work(bid,cast(int,work['revision']))
                if type(owned) is Found:return await self.run_work(cast(WorkCapability,owned.value))
            return WorkDeferred('NO_TARGET','NOT_READY')
        except DomainFailure as failure:return Rejected(self._error('request_learning',failure.code,failure.field,failure.reason,failure.cleanup_pending))
        finally:self._scheduler_active=False

    @bounded('claim_work',read_only=False)
    async def claim_work(self,work_id:str,expected_revision:int):
        if not valid_identifier(work_id) or type(expected_revision) is not int or expected_revision<1:return Rejected(self._error('claim_work','INVALID_INPUT','work','INVALID_SHAPE'))
        issue=self._owner_issue('claim_work')
        if issue is not None:return Rejected(issue)
        if self._lifecycle!='READY':return Rejected(self._error('claim_work','INVALID_STATE','state','SERVICE_CLOSED' if self._lifecycle=='CLOSING' else 'NOT_READY'))
        current=await self._transactions.rows.load('work',work_id)
        if current is not None and current['state']=='WAITING_ADMISSION':
            if work_id in self._running:return WorkDeferred('BLOCKED','OWNER_ACTIVE')
            if not await self._models.can_readmit(work_id):return WorkDeferred('BLOCKED','OWNER_ACTIVE')
        generation=expected_revision
        result=await self._execute('claim',stable_id('claim',work_id,expected_revision),{'work_id':work_id,'expected_revision':expected_revision,'owner_generation':generation},'scheduler')
        if type(result) is Committed:
            value=result.receipt.result;assert type(value) is MappingProxyType
            for issued in self._work.values():
                if issued.work_id==work_id and issued.owner_generation==generation:return Found(issued)
            capability=WorkCapability(work_id,cast(int,value['revision']),generation,cast(str,value['entry_id']))
            self._work[id(capability)]=capability
            return Found(capability)
        return result

    @bounded('stage_candidate',read_only=False)
    async def stage_candidate(self,work:WorkCapability,outcome:dict[str,object]):
        if type(work) is not WorkCapability or self._work.get(id(work)) is not work:return Rejected(self._error('stage_candidate','ACCESS_DENIED','work','BINDING_MISMATCH'))
        issue=self._owner_issue('stage_candidate')
        if issue is not None:return Rejected(issue)
        candidate_id=stable_id('candidate',work.work_id)
        try:
            from companion_memory.provider.values import freeze,as_record
            import json
            isolated=as_record(freeze(outcome,8192))
            outcome=json.loads(json_text(isolated))
            original=await self._operations['stage'].read_receipt(candidate_id)
            if type(original) is storage_results.NotFound:
                if not await self._models.verify_outcome(work.work_id,outcome):return Rejected(self._error('stage_candidate','PARTICIPANT_FAILED','participant','RESULT_INVALID'))
            elif type(original) is not storage_results.Found:return Rejected(self._error('stage_candidate','STORAGE_FAILED','storage','READ_FAILED'))
        except DomainFailure as failure:return Rejected(self._error('stage_candidate',failure.code,failure.field,failure.reason,failure.cleanup_pending))
        except (InvalidData,ValueError,TypeError):return Rejected(self._error('stage_candidate','PARTICIPANT_FAILED','participant','RESULT_INVALID'))
        return await self._execute('stage',candidate_id,{'work_id':work.work_id,'expected_revision':work.revision,'owner_generation':work.owner_generation,'candidate_id':candidate_id,'outcome':json_text(outcome)},'scheduler')

    @bounded('finalize_batch',read_only=False)
    async def finalize_batch(self,work:WorkCapability,candidate_ref:str):
        if type(work) is not WorkCapability or self._work.get(id(work)) is not work:return Rejected(self._error('finalize_batch','ACCESS_DENIED','work','BINDING_MISMATCH'))
        issue=self._owner_issue('finalize_batch')
        if issue is not None:return Rejected(issue)
        return await self._execute('finalize',stable_id('terminal',work.work_id),{'work_id':work.work_id,'expected_revision':work.revision+1,'owner_generation':work.owner_generation,'candidate_id':candidate_ref},'scheduler')

    def owner_overdue(self,work_id:str) -> bool:
        """Lease expiry is an observation only, never a replacement-owner permit."""
        started=self._owner_started.get(work_id)
        return started is not None and time.monotonic()-started>=self._configuration.candidate.runtime.integer('runtime.claim_lease_ms')/1000

    def get_health(self) -> MappingProxyType[str,object]:
        storage=self._storage.get_health()
        provider=self._models.provider.get_health() if self._models.provider else None
        return MappingProxyType({'lifecycle':self._lifecycle,'overdue_owners':sum(self.owner_overdue(key) for key in self._owner_started),'mode':self._mode,'epoch':self._epoch,'cleanup_pending':bool(self._jobs) or storage.cleanup_pending or bool(provider and provider.cleanup_pending),'storage_lifecycle':storage.lifecycle,'storage_reads':storage.reads_in_flight,'storage_writes':storage.writes_in_flight,'provider_lifecycle':provider.lifecycle if provider else 'UNAVAILABLE','provider_in_flight':provider.in_flight if provider else None,'provider_unknown_observations':provider.unknown_observations if provider else None,'provider_cleanup_pending':provider.cleanup_pending if provider else None,'configuration':'LOADED','model_capability':'SIMULATED' if self._models.ready() else 'UNAVAILABLE','learning_participant':'SYNTHETIC' if self._assembly.participant else 'UNAVAILABLE'})

    async def close(self) -> CloseReport:
        if self._lifecycle=='CLOSED':return CloseReport('CLOSED',False)
        if self._lifecycle!='CLOSING':
            with self._gate_lock:
                self._closing_owners=set(self._jobs)
                self._lifecycle='CLOSING'
        if self._jobs:await asyncio.wait(tuple(self._jobs),timeout=self._configuration.candidate.runtime.integer('runtime.close_timeout_ms')/1000)
        if self._jobs or self._scheduler_active or self._storage.get_health().writes_in_flight or self._storage.get_health().reads_in_flight:return CloseReport('INCOMPLETE',True)
        if self._models.provider is not None and (self._models.provider.get_health().in_flight or self._models.provider.get_health().cleanup_pending):return CloseReport('INCOMPLETE',True)
        if self._transactions.lease is not None and not self._transactions.lease.release():return CloseReport('INCOMPLETE',True)
        self._lifecycle='CLOSED';self._closing_owners.clear();return CloseReport('CLOSED',False)
