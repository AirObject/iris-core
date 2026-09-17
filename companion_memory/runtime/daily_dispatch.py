"""Finite event-driven daily scheduling through the native runtime entry ports.

An accepted trigger names the original entry coverage. One actual drive chooses
entries in a persistent round robin and joins the native preparation, image and
learning owners. Reopening observes intent while its volatile switch stays shut.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Found,Committed,NotFound
from companion_memory.persistence.schema import InvalidValue,valid_identifier
from companion_memory.persistence.owned_statements import OwnerFailure
from .content_assembly import stable

@dataclass(frozen=True,slots=True,init=False)
class DailyEntryPort:
    """An entry can request FOCUS learning; it cannot sign internal reasons."""
    owner:DailyDispatch
    entry_id:str
    def __init__(self):raise TypeError('The daily host issues entry capabilities.')
    async def accept_event(self,key:object,event:object):
        native=self.owner.entry(self,receiving=True)
        result=await native.accept_event(key,event)
        if type(result) is Committed and result.source=='NEW':
            await self.owner.threshold(self.entry_id,cast(str,key))
        return result
    async def request_learning(self,key:object,*,target_through_seq:int|None=None):
        self.owner.entry(self)
        return await self.owner.request(self.entry_id,key,'FOCUS',target_through_seq)
    async def run_learning(self,key:object):
        """Join the same durable queue used by explicit and threshold triggers."""
        self.owner.entry(self)
        accepted=await self.owner.request(self.entry_id,key,'FOCUS',None)
        if type(accepted) is not Committed:return accepted
        await self.owner.wait_actual(time.monotonic()+1200)
        return await self.owner.confirm(self.entry_id,cast(str,key))

class DailyDispatch:
    """A single real learning slot; there is no periodic idle write or send."""
    def __init__(self,runtime,schedule,normal,admitted,receiving):
        self.runtime=runtime;self.schedule=schedule;self.normal=normal;self.admitted=admitted;self.receiving=receiving
        self.closed=False;self._task:asyncio.Task|None=None;self._changed=False
        self._ports:dict[str,DailyEntryPort]={};self._native={};self.last_failure:OwnerFailure|None=None
        schedule.authorize=lambda entry:entry in self._ports and not self.closed

    def bind(self,entry_id:str):
        self.receiving()
        return self._bind(entry_id)

    def restore_entries(self,entry_ids:tuple[str,...]):
        """Restore trusted scopes while scheduling stays paused, including focus."""
        if self.closed or self._ports or self.schedule.enabled or self.runtime.state!='READY':raise InvalidValue()
        for entry_id in entry_ids:self._bind(entry_id)

    def _bind(self,entry_id:str):
        if entry_id in self._ports:return self._ports[entry_id]
        native=self.runtime.bind_entry(entry_id)
        port=object.__new__(DailyEntryPort);object.__setattr__(port,'owner',self);object.__setattr__(port,'entry_id',entry_id)
        self._native[entry_id]=native;self._ports[entry_id]=port;return port

    def entry(self,port:DailyEntryPort,*,receiving:bool=False):
        (self.receiving if receiving else self.normal)()
        if self.closed or type(port) is not DailyEntryPort or self._ports.get(port.entry_id) is not port:raise OwnerFailure('ACCESS_DENIED','entry','BINDING_MISMATCH')
        return self._native[port.entry_id]

    def key(self,entry_id:str,key:str):return stable('daily-trigger-request',entry_id,key)

    async def request(self,entry_id:str,key:object,reason:str,upper:int|None):
        self.normal()
        if self.closed or entry_id not in self._ports or not valid_identifier(key) or reason not in ('THRESHOLD','FOCUS','ACTIVE'):raise InvalidValue()
        if upper is not None and (type(upper) is not int or not 0<=upper<2**63):raise InvalidValue()
        schedule=self.schedule;operation=self.key(entry_id,cast(str,key))
        trigger_id=self.trigger_id(operation)
        old=await schedule.rows.read('daily_learning_triggers',trigger_id)
        if old is None:
            proof=await schedule.operations['enqueue_learning'].read_receipt(operation)
            if type(proof) is Found:
                from companion_memory.memory.formats import record
                original=record(record(proof.value.result)['trigger'])
                if original['object_id']!=trigger_id or original['entry_id']!=entry_id or original['reason']!=reason or upper is not None and original['target_through_seq']!=upper:
                    raise OwnerFailure('IDEMPOTENCY_CONFLICT','input','CONTENT_MISMATCH')
                ended=await schedule.operations['complete_trigger'].read_receipt(stable('daily-trigger-end',trigger_id))
                if type(ended) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
                return Committed(proof.value,'EXISTING')
            if type(proof) is not NotFound:return proof
        if old is not None:
            if old['entry_id']!=entry_id or old['reason']!=reason or upper is not None and old['target_through_seq']!=upper:raise OwnerFailure('IDEMPOTENCY_CONFLICT','input','CONTENT_MISMATCH')
            proof=await schedule.operations['enqueue_learning'].read_receipt(operation)
            if type(proof) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
            self.wake();return Committed(proof.value,'EXISTING')
        coverage=await self.runtime.daily_coverage(entry_id,upper)
        if coverage is None:return Found(MappingProxyType({'state':'NO_TARGET','entry_id':entry_id,'new_sends':0}))
        outcome=await schedule.execute('enqueue_learning',operation,{'entry_id':entry_id,'reason':reason,'target_through_seq':coverage if upper is None else upper},'daily_scheduler')
        if type(outcome) is Committed:self.wake()
        return outcome

    def trigger_id(self,key):
        from companion_memory.persistence.daily_records import identity
        return identity('learning-trigger',self.schedule.configuration.database_id,self.schedule.configuration.scope_id,key)

    async def threshold(self,entry_id:str,key:str):
        """Only actual successful acceptance can issue this internal trigger."""
        try:await self.request(entry_id,stable('threshold',key),'THRESHOLD',None)
        except OwnerFailure as failure:
            # Acceptance is an independent durable fact. A full scheduling queue
            # remains observable and can be advanced after explicit admission.
            self.last_failure=failure

    def wake(self):
        if self.closed or not self.admitted():return
        self._changed=True
        if self._task is not None:return
        task=asyncio.create_task(self._drive());self._task=task;self.runtime.retain_external_work(task)
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
            if self._changed and not self.closed and self.admitted():self.wake()
        task.add_done_callback(ended)

    async def _drive(self):
        self._changed=False
        try:
            for _ in range(128):
                if self.closed or not self.admitted():return
                claimed=await self.schedule.claimed()
                if claimed:trigger=claimed[0]
                else:
                    current=await self.schedule.current()
                    if current is None:raise InvalidValue()
                    page=await self.schedule.queued(cast(str,current['last_entry_id'] or ''))
                    if not page:page=await self.schedule.queued()
                    if not page:return
                    trigger=page[0]
                    claimed_result=await self.schedule.execute('claim_learning',stable('daily-claim',trigger['object_id']),
                        {'trigger_id':trigger['object_id'],'expected_revision':trigger['revision'],'schedule_revision':current['revision'],'batch_id':trigger['batch_id']},'daily_scheduler')
                    if type(claimed_result) is not Committed:return
                    trigger=await self.schedule.rows.read('daily_learning_triggers',cast(str,trigger['object_id']))
                    if trigger is None:raise InvalidValue()
                entry=cast(str,trigger['entry_id'])
                if entry not in self._native:return
                canonical=await self.canonical(trigger)
                outcome=await self.runtime.drive_daily_trigger(entry,cast(str,canonical['object_id']),cast(int,trigger['target_through_seq']))
                rows=await self.runtime.assembly.rows.read('batches_get',{'batch_id':trigger['batch_id']})
                if rows and rows[0]['terminal']=='FROZEN':return
                if not rows and (type(outcome) is not Found or outcome.value.get('state') not in ('NO_TARGET','EXPIRED','INVALIDATED')):return
                ended=await self.schedule.execute('complete_trigger',stable('daily-trigger-end',trigger['object_id']),
                    {'trigger_id':trigger['object_id'],'expected_revision':trigger['revision'],'batch_id':trigger['batch_id']},'daily_scheduler')
                if type(ended) is not Committed:return
                retired=await self.schedule.retire_page()
                if type(retired) not in (Committed,Found):return
        except OwnerFailure as failure:self.last_failure=failure

    async def canonical(self,trigger):
        rows=await self.schedule.rows.rows.read('daily_trigger_batch',{'batch_id':trigger['batch_id']})
        if not rows:raise OwnerFailure('STORAGE_FAILED','trigger','INTEGRITY_FAILURE')
        return self.schedule.rows.decode('daily_learning_triggers',rows[0])

    async def recover(self):
        """Finish only triggers whose original actual batch already ended, with no dispatch."""
        after=''
        for _ in range(3):
            page=await self.schedule.rows.rows.read('daily_unfinished_page',{'after':after})
            if not page:
                for _ in range(32):
                    retired=await self.schedule.retire_page()
                    if type(retired) is Found:return
                    if type(retired) is not Committed:raise OwnerFailure('RESULT_UNCONFIRMED','trigger','COMMIT_UNCONFIRMED',bool(getattr(retired,'cleanup_pending',False)))
                return
            for raw in page:
                trigger=self.schedule.rows.decode('daily_learning_triggers',raw);after=cast(str,trigger['object_id'])
                rows=await self.runtime.assembly.rows.read('batches_get',{'batch_id':trigger['batch_id']})
                if not rows or rows[0]['terminal']=='FROZEN':continue
                result=await self.schedule.execute('complete_trigger',stable('daily-trigger-end',trigger['object_id']),
                    {'trigger_id':trigger['object_id'],'expected_revision':trigger['revision'],'batch_id':trigger['batch_id']},'daily_scheduler')
                if type(result) is not Committed:raise OwnerFailure('RESULT_UNCONFIRMED','trigger','COMMIT_UNCONFIRMED',bool(getattr(result,'cleanup_pending',False)))
        raise OwnerFailure('STORAGE_FAILED','trigger','INTEGRITY_FAILURE')

    async def confirm(self,entry_id:str,key:str):
        trigger=await self.schedule.rows.read('daily_learning_triggers',self.trigger_id(self.key(entry_id,key)))
        if trigger is None:
            proof=await self.schedule.operations['complete_trigger'].read_receipt(stable('daily-trigger-end',self.trigger_id(self.key(entry_id,key))))
            if type(proof) is not Found:raise InvalidValue()
            from companion_memory.memory.formats import record
            trigger=record(record(proof.value.result)['trigger'])
        batch=await self.runtime.assembly.rows.read('batches_get',{'batch_id':trigger['batch_id']})
        if trigger['phase']!='TERMINAL':
            if not batch or batch[0]['terminal']=='FROZEN':return Found(MappingProxyType({'state':trigger['phase'],'trigger_id':trigger['object_id'],'cleanup_pending':self._task is not None}))
            completed=await self.schedule.execute('complete_trigger',stable('daily-trigger-end',trigger['object_id']),
                {'trigger_id':trigger['object_id'],'expected_revision':trigger['revision'],'batch_id':trigger['batch_id']},'daily_scheduler')
            if type(completed) is not Committed:return completed
        if not batch:return Found(MappingProxyType({'state':'NO_TARGET','trigger_id':trigger['object_id'],'new_sends':0}))
        if self.runtime.daily_learning is None:raise InvalidValue()
        original=await self.runtime.assembly.read_daily_batch(cast(str,trigger['batch_id']))
        from companion_memory.memory.formats import record
        return await self.runtime.daily_learning.learn_batch(record(original['source']),fresh=False)

    @property
    def pending(self):
        return self._task is not None

    async def wait_actual(self,deadline:float|None=None):
        for _ in range(128):
            task=self._task
            if task is None:return
            done,_=await asyncio.wait((task,),timeout=None if deadline is None else max(0,deadline-time.monotonic()))
            if not done:return
            # The exact task's completion callback may install a queued wake.
            # Join that owned successor before reporting the queue has ended.
            await asyncio.sleep(0)

    def close(self):
        self.closed=True;self._changed=False
        return self._task is None
