"""Scoped metadata projections with bounded queries and opaque pagination state.

Management receives only this capability. Payloads, audit history, commands and
Provider result bodies never enter the view. A failed refresh preserves the last
successful scoped snapshot with its original observation time and STALE label.
"""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from dataclasses import dataclass,field
from datetime import datetime,timezone
import secrets
import time
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.persistence.schema import valid_identifier
from companion_memory.persistence import RecoveryHandle
from companion_memory.persistence import results as stored
from .records import DomainFailure,data,json_text
from .results import Found,Failed,RuntimeError
if TYPE_CHECKING:
    from .service import RuntimeService


def immutable(value):
    if type(value) is dict:return MappingProxyType({k:immutable(v) for k,v in value.items()})
    if type(value) in (tuple,list):return tuple(immutable(v) for v in value)
    return value


@dataclass(frozen=True,slots=True)
class RuntimeObservationGrant:
    instance_id:str
    entry_ids:tuple[str,...]
    instance_observe:bool=False


class RuntimeObserver:
    """Native observation-only handle with no assembly or write attributes."""
    __slots__=('_binding',)
    def __new__(cls):raise TypeError('Bind a trusted scoped runtime observer.')
    def __setattr__(self,name,value):raise AttributeError('Observation capabilities are immutable.')
    async def read_runtime_view(self,query:object):return await _observe(self,'runtime',query)
    async def read_entry_status(self,query:object):return await _observe(self,'entries',query)
    async def read_batch_status(self,query:object):return await _observe(self,'batches',query)


async def _observe(port:object,kind:str,query:object):
    binding=None
    if type(port) is RuntimeObserver:
        try:binding=object.__getattribute__(port,'_binding')
        except AttributeError:pass
    if type(binding) is not Observations:return Failed(RuntimeError('ACCESS_DENIED',{'runtime':'read_runtime_view','entries':'read_entry_status','batches':'read_batch_status'}[kind],'identity','BINDING_MISMATCH'))
    assert type(port) is RuntimeObserver
    return await binding.query(port,kind,query)


class Observations:
    def __init__(self,runtime:RuntimeService):
        self.runtime=runtime;self.grants:dict[RuntimeObserver,RuntimeObservationGrant]={}
        self.cursors:OrderedDict[str,tuple[RuntimeObserver,str,tuple[str,...],str,float,str,tuple[str | None,str | None]]]=OrderedDict()
        self.cached:dict[tuple[RuntimeObserver,str,tuple[str,...],str,int,tuple[str | None,str | None]],MappingProxyType]={}
        self.jobs:set[asyncio.Task]=set()
        self.operations:OrderedDict[RecoveryHandle,LocalConfirmation]=OrderedDict()

    def started(self,kind:str,handle:RecoveryHandle,values:dict[str,object],task:asyncio.Task):
        """Merge exact command evidence while retaining every admitted task.

        The recovery handle compares the complete operation identity, command
        version, fingerprint version and digest. Conflicting contents occupy a
        separate observation; they never replace the original committed fact.
        Runtime command admission bounds live tasks. Finished attempts, including
        unknown confirmations, enter the same finite retention window. This cache
        does not own recovery responsibilities or underlying storage resources.
        """
        evidence=self.operations.get(handle)
        if evidence is None:
            entry=values.get('entry_id');batch=values.get('work_id',values.get('batch_id'))
            evidence=LocalConfirmation(kind,handle,cast(str | None,entry),cast(str | None,batch))
            self.operations[handle]=evidence
        self.operations.move_to_end(handle)
        evidence.tasks.add(task)
        def completed(done:asyncio.Task):
            evidence.tasks.discard(done)
            if done.cancelled() or done.exception() is not None:
                evidence.unknown()
                evidence.cleanup_pending=True
            else:
                result=done.result()
                if type(result) is stored.Committed:
                    evidence.received(result.receipt)
                    # Storage may return established commit evidence at its wait
                    # deadline while the owning thread still closes a connection.
                    # The successful envelope itself carries no cleanup flag.
                    evidence.cleanup_pending=evidence.cleanup_pending or self.runtime._storage.get_health().cleanup_pending
                elif evidence.status!='COMMITTED':
                    evidence.status={stored.NotCommitted:'NOT_COMMITTED',stored.Rejected:'REJECTED',stored.Unconfirmed:'UNCONFIRMED'}.get(type(result),'UNCONFIRMED')
                error=getattr(result,'error',None)
                # RECOVERY_UNAVAILABLE reports an occupied writer without starting
                # another storage task. It creates no new cleanup ownership.
                if error is not None and error.reason!='RECOVERY_UNAVAILABLE':
                    evidence.cleanup_pending=evidence.cleanup_pending or error.cleanup_pending
            self.trim_operations()
        task.add_done_callback(completed)
        self.trim_operations()
        return evidence

    def trim_operations(self):
        """Retain at most row_limit finished records plus admitted live commands.

        Completed tasks are removed, regardless of their confirmation status.
        Eviction only forgets optional process-local observations; callers retain
        original inputs and the coordinator retains its actual recovery state.
        An absent record has no implication about commit or cleanup completion.
        """
        limit=self.runtime._configuration.candidate.runtime.integer('management.observation_row_limit')
        ended=[key for key,item in self.operations.items() if not item.tasks]
        for key in ended[:-limit]:self.operations.pop(key,None)

    async def local_evidence(self,*,entry_id:str | None=None,batch_id:str | None=None,instance:bool=False):
        """Read only scoped evidence, never run write recovery or infer rollback.

        Each call scans at most row_limit + max_active_entries records and makes
        at most that many point reads. A page makes at most row_limit calls,
        including when new observations arrive between rows. Concurrent view
        tasks have their own configured admission bound.
        """
        projected=[]
        for item in tuple(self.operations.values()):
            if not instance and not (batch_id is not None and item.batch_id==batch_id or entry_id is not None and item.entry_id==entry_id):continue
            if item.status in ('SUBMITTED','UNCONFIRMED'):
                found=await self.runtime._operations[item.kind].read_receipt(item.handle.identity.operation_key)
                if type(found) is stored.Found:
                    item.received(found.value)
                # NotFound proves only the current read snapshot, never rollback.
                # Re-read the shared status after waiting: another task may have
                # established a commit while this point read failed or missed.
            pending=bool(item.tasks) or item.cleanup_pending and self.runtime._storage.get_health().cleanup_pending
            projected.append({'operation':self.runtime._operation_name(item.kind),'status':item.status,'cleanup_pending':pending,'evidence':'OPERATION_RESULT'})
        self.trim_operations()
        return tuple(projected)
    def bind(self,grant:RuntimeObservationGrant):
        if type(grant) is not RuntimeObservationGrant or not valid_identifier(grant.instance_id) or grant.instance_id!=self.runtime._instance_id or type(grant.entry_ids) is not tuple or len(grant.entry_ids)>128 or any(not valid_identifier(i) for i in grant.entry_ids) or type(grant.instance_observe) is not bool:
            raise ValueError('Explicit bounded instance/entry observation scope is required.')
        observer=object.__new__(RuntimeObserver);object.__setattr__(observer,'_binding',self);self.grants[observer]=grant
        return observer
    def revoke(self,observer:RuntimeObserver):
        self.grants.pop(observer,None)
        self.cached={k:v for k,v in self.cached.items() if k[0] is not observer}
    def failure(self,kind,code,field,reason,pending=False):return Failed(self.runtime._error({'runtime':'read_runtime_view','entries':'read_entry_status','batches':'read_batch_status'}[kind],code,field,reason,pending))
    async def query(self,observer:RuntimeObserver,kind:str,query:object):
        grant=self.grants.get(observer) if type(observer) is RuntimeObserver else None
        if grant is None or kind=='runtime' and not grant.instance_observe:return self.failure(kind,'ACCESS_DENIED','identity','BINDING_MISMATCH')
        allowed={'entry_id','cursor','limit'} | ({'batch_id','state'} if kind=='batches' else set())
        if type(query) is not dict or any(type(k) is not str for k in query) or not {'entry_id','cursor','limit'}<=set(query) or set(query)-allowed:return self.failure(kind,'INVALID_INPUT','query','INVALID_SHAPE')
        batch_id=query.get('batch_id');state=query.get('state')
        if batch_id is not None and not valid_identifier(batch_id) or state is not None and (type(state) is not str or state not in ('FROZEN','TERMINAL')):return self.failure(kind,'INVALID_INPUT','query','INVALID_SHAPE')
        filters=(cast(str | None,batch_id),cast(str | None,state))
        entry=query['entry_id'];cursor=query['cursor'];limit=query['limit']
        if entry is not None and (not valid_identifier(entry) or entry not in grant.entry_ids):return self.failure(kind,'ACCESS_DENIED','entry','BINDING_MISMATCH')
        settings=self.runtime._configuration.candidate.runtime
        if type(limit) is not int or not 1<=limit<=settings.integer('management.observation_row_limit') or cursor is not None and (type(cursor) is not str or len(cursor)!=43):return self.failure(kind,'INVALID_INPUT','query','LIMIT_EXCEEDED')
        entries=(entry,) if entry is not None else grant.entry_ids
        after='';waterline=None
        if cursor is not None:
            previous=self.cursors.get(cursor)
            if previous is None or previous[:3]!=(observer,kind,entries) or previous[6]!=filters or previous[4]<time.monotonic():return self.failure(kind,'PRECONDITION_FAILED','query','REVISION_CHANGED')
            after=previous[3];waterline=None if previous[3]>=previous[5] else previous[5]
        cachekey=(observer,kind,entries,after,limit,filters)
        if len(self.jobs)>=settings.integer('management.observation_concurrency'):return self.failure(kind,'RESOURCE_BUSY','query','ADMISSION_FULL')
        task=asyncio.create_task(self.read(observer,kind,entries,after,limit,waterline,filters))
        self.jobs.add(task);self.runtime._jobs.add(task)
        task.add_done_callback(self.jobs.discard);task.add_done_callback(self.runtime._finished)
        done,_=await asyncio.wait((task,),timeout=settings.integer('management.observation_timeout_ms')/1000)
        pending=not done
        if done:
            try:
                result=task.result()
                if observer not in self.grants:return self.failure(kind,'ACCESS_DENIED','identity','BINDING_MISMATCH')
                self.cached[cachekey]=result
                while len(self.cached)>settings.integer('logging.web_window_events'):self.cached.pop(next(iter(self.cached)))
                return Found(result)
            except DomainFailure as failure:
                pending=pending or failure.cleanup_pending
                if failure.reason=='LIMIT_EXCEEDED':return self.failure(kind,failure.code,failure.field,failure.reason)
            except (KeyError,ValueError,TypeError):pass
        old=self.cached.get(cachekey)
        if old is not None:return Found(immutable({**old,'availability':'STALE','cleanup_pending':pending}))
        return Found(immutable({'availability':'UNAVAILABLE','observed_at':None,'cleanup_pending':pending}))
    async def read(self,observer:RuntimeObserver,kind:str,entries:tuple[str,...],after:str,limit:int,waterline:str | None=None,filters:tuple[str | None,str | None]=(None,None)) -> MappingProxyType:
        r=self.runtime;observed=datetime.now(timezone.utc).isoformat();tx=r._transactions
        if kind=='runtime':
            mode=await tx.rows.load('mode','mode')
            if mode is None:raise DomainFailure('STORAGE_FAILED','mode','READ_FAILED')
            value={'mode':mode['state'],'snapshot_revision':mode['revision'],'epoch':data(mode)['epoch'],'overlay':'RECOVERING' if r._lifecycle!='READY' else None,
                'publication_completed':data(mode)['publication_id'] is not None,'dream_run_id':data(mode)['run_id'],'cleanup_pending':bool(r._running) or r._storage.get_health().cleanup_pending,
                'accepting':r._lifecycle=='READY','ordinary_business':r._lifecycle=='READY' and r._mode in ('NORMAL','DRAINING') and not r._models.closing_gate,
                'blocked_reason':'RUNTIME_FAULTED' if r._mode=='FAULTED' else 'RECOVERING' if r._lifecycle!='READY' else 'DREAMING' if r._mode in ('DREAM_PREPARING','DREAM_FOCUSED') else None,'recovery_stage':'RUNTIME' if r._lifecycle=='RECOVERING' else None,'recovery_progress':r._recovery_position[0],'services':'ACTUAL','model':'SIMULATED' if r._models.ready() else 'UNAVAILABLE','results':'SYNTHETIC' if r._assembly.participant else 'UNAVAILABLE','health':dict(r.get_health()),'composition':'COMPOSITE_OBSERVATION'}
            value['local_operations']=await self.local_evidence(instance=True)
            value['local_operation_coverage']='CURRENT_PROCESS_RETAINED'
            rows=(value,);more=False;next_after=after
        else:
            if waterline is None:
                boundary=await tx.buffers.rows.read(kind+'_waterline',{'entries':json_text(entries)})
                waterline=cast(str,boundary[0]['waterline']) if boundary[0]['waterline'] is not None else ''
            projection='entry_observation' if kind=='entries' else 'batch_observation'
            selection={'batch_id':filters[0],'filter_state':filters[1]} if kind=='batches' else {}
            page=await tx.buffers.rows.read(projection,{'entries':json_text(entries),'after':after,'waterline':waterline,'limit':limit,**selection})
            rows=[]
            for item in page:
                decoded=tx.buffers.rows.decode(item,'entry_state' if kind=='entries' else 'batches');values=data(decoded)
                if kind=='entries':
                    platform=r._configuration.candidate.platform(cast(str,values['platform_id']))
                    normal=cast(int,item['normal_count']);recent=platform.count('recent_context_count');target=platform.count('target_count')
                    row={'platform_id':values['platform_id'],'target_candidates':max(0,normal-cast(int,item['active_targets'])-recent),'active_target_count':item['active_targets'],'latest_context_count':min(normal,recent),
                        'earliest_received_at_us':values['earliest_received_at_us'],'latest_received_at_us':values['latest_received_at_us'],'normal_soft_limit_exceeded':normal>platform.count('normal_soft_limit'),
                        'entry_id':item['entry_id'],'snapshot_revision':item['revision'],'normal_pending':item['normal_count'],'focus_pending':item['staged_count'],
                        'pending_total':cast(int,item['normal_count'])+cast(int,item['staged_count']),'active_batches':item['active_batches'],'history_count':len(cast(list,values['history'])),
                        'blocked_reason':'RUNTIME_FAULTED' if r._mode=='FAULTED' else 'RECOVERING' if r._lifecycle!='READY' else 'DREAMING' if r._mode in ('DREAM_PREPARING','DREAM_FOCUSED') else None,'transfer_cursor':values['transfer_cursor'],'transferred_count':values['transferred_count'],'mode':r._mode,'composition':'COMPOSITE_OBSERVATION'}
                else:
                    work=await tx.rows.load('work',cast(str,item['object_id']))
                    if work is None:raise DomainFailure('STORAGE_FAILED','work','INTEGRITY_FAILURE')
                    row={'owner_overdue':r.owner_overdue(cast(str,item['object_id'])),'work_state':work['state'],'remote_unknown':work['state']=='REMOTE_RESULT_UNKNOWN','local_confirmation':'COMMITTED_SNAPSHOT','cleanup_pending':item['object_id'] in r._running,'composition':'COMPOSITE_OBSERVATION',
                        'batch_id':item['object_id'],'entry_id':item['entry_id'],'snapshot_revision':item['revision'],'state':item['state'],'terminal':values['terminal'],'history_count':values['history_count'],
                        'range_start':values['range_start'],'range_end':values['range_end'],'range_start_us':values['range_start_us'],'range_end_us':values['range_end_us'],'learning_gap':values['terminal']=='FAILED_DROPPED','sensitive_history_loss':values['terminal']=='SENSITIVE_DROPPED','target_count':values['target_count'],'recent_count':values['recent_count'],'config_snapshot_id':values['config_snapshot_id'],'result_count':values['result_count'],'synthetic':True}
                row['local_operations']=await self.local_evidence(entry_id=cast(str,item['entry_id'])) if kind=='entries' else await self.local_evidence(batch_id=cast(str,item['object_id']))
                row['local_operation_coverage']='CURRENT_PROCESS_RETAINED'
                rows.append(row)
            next_after=cast(str,page[-1]['object_id']) if page else after
            more=bool(await tx.buffers.rows.read(projection,{'entries':json_text(entries),'after':next_after,'waterline':waterline,'limit':1,**selection})) if page else False
        token=secrets.token_urlsafe(32)
        settings=r._configuration.candidate.runtime
        result={'availability':'AVAILABLE','observed_at':observed,'rows':rows,'next_cursor':token,'has_more':more}
        if len(json_text(result).encode())>settings.integer('management.observation_max_bytes'):raise DomainFailure('INVALID_INPUT','query','LIMIT_EXCEEDED')
        self.cursors[token]=(observer,kind,entries,next_after,time.monotonic()+settings.integer('management.refresh_min_interval_ms')/1000*settings.integer('logging.web_window_events'),waterline or '',filters)
        while len(self.cursors)>settings.integer('logging.web_window_events'):self.cursors.popitem(last=False)
        return cast(MappingProxyType,immutable(result))


@dataclass(slots=True)
class LocalConfirmation:
    """Bounded optional evidence; task ownership remains with the coordinator."""
    kind:str
    handle:RecoveryHandle
    entry_id:str | None
    batch_id:str | None
    tasks:set[asyncio.Task]=field(default_factory=set)
    status:str='SUBMITTED'
    cleanup_pending:bool=False

    def unknown(self):
        if self.status!='COMMITTED':self.status='UNCONFIRMED'

    def received(self,receipt:stored.Receipt):
        """Only matching original evidence establishes a commit for this command."""
        if receipt.identity!=self.handle.identity:
            raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        matches=(receipt.command_version==self.handle.command_version
            and receipt.fingerprint_version==self.handle.fingerprint_version
            and receipt.fingerprint==self.handle.fingerprint)
        if matches:self.status='COMMITTED'
        elif self.status!='COMMITTED':self.status='REJECTED'
