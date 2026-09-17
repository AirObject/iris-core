"""Trusted, instance-bound dream control with retained actual coordination work."""
import asyncio
from hashlib import sha256
from types import MappingProxyType
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING,cast
from companion_memory.information.management import HostIdentity
from companion_memory.persistence import Committed,Found,NotCommitted,Rejected
from companion_memory.persistence.schema import InvalidValue,valid_identifier
from companion_memory.persistence.deadlines import DeadlineScope,current_deadline,check_deadline
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.owned_statements import OwnerFailure
if TYPE_CHECKING:
    from companion_memory.runtime.daily_host import DailyCognitionHost

OPERATIONS=frozenset(('start_dream','pause_dream','resume_dream','abort_dream','inspect_dream','confirm_dream_step'))

@dataclass(frozen=True,slots=True,init=False)
class DreamPort:
    owner:'DreamPorts'
    identity:HostIdentity
    def __init__(self):raise TypeError('Trusted native host setup issues dream control.')
    async def start_dream(self,key:str,run_id:str,expected_revision:int,mode_epoch:int,*,mode:str='FOCUSED',trigger:str='MANUAL',local_date:str|None=None):
        return await self.owner.call(self,'start_dream',key,{'run_id':run_id,'expected_revision':expected_revision,'mode_epoch':mode_epoch,
            'mode':mode,'trigger':trigger,'local_date':local_date})
    async def pause_dream(self,key:str,run_id:str,expected_revision:int,mode_epoch:int):
        return await self.owner.call(self,'pause_dream',key,{'run_id':run_id,'expected_revision':expected_revision,'mode_epoch':mode_epoch})
    async def resume_dream(self,key:str,run_id:str,expected_revision:int,mode_epoch:int):
        return await self.owner.call(self,'resume_dream',key,{'run_id':run_id,'expected_revision':expected_revision,'mode_epoch':mode_epoch})
    async def abort_dream(self,key:str,run_id:str,expected_revision:int,mode_epoch:int):
        return await self.owner.call(self,'abort_dream',key,{'run_id':run_id,'expected_revision':expected_revision,'mode_epoch':mode_epoch})
    async def inspect_dream(self,run_id:str):
        deadline=current_deadline(cast(int,self.owner.control.configuration.candidate.text.record('dream.resources')['operation_timeout_ms'])/1000)
        self.owner.check(self,'inspect_dream')
        with DeadlineScope(deadline):
            value=await self.owner.control.inspect(run_id);check_deadline();return value
    async def confirm_dream_step(self,kind:str,key:str):
        deadline=current_deadline(cast(int,self.owner.control.configuration.candidate.text.record('dream.resources')['operation_timeout_ms'])/1000)
        self.owner.check(self,'confirm_dream_step')
        with DeadlineScope(deadline):
            value=await self.owner.control.confirm(kind,key);check_deadline();return value

class DreamPorts:
    def __init__(self,host:'DailyCognitionHost'):
        if host.combination.dream is None or host.combination.dream_mode is None:raise InvalidValue()
        self.host=host;self.control=host.combination.dream;self.mode=host.combination.dream_mode
        self.ports:dict[str,DreamPort]={};self.tasks:set[asyncio.Task]=set();self.closed=False

    def issue(self,identity:HostIdentity):
        if (type(identity) is not HostIdentity or identity.binding_id in self.ports or len(self.ports)>=16
                or not identity.operations or not identity.operations<=OPERATIONS or identity.route_ids
                or not time.monotonic()<identity.expires_at<=time.monotonic()+3600
                or any(not valid_identifier(v) for v in (identity.binding_id,identity.principal_id,identity.host_id,identity.entry_id))):raise InvalidValue()
        port=object.__new__(DreamPort);object.__setattr__(port,'owner',self);object.__setattr__(port,'identity',identity)
        self.ports[identity.binding_id]=port
        return port

    def check(self,port:DreamPort,operation:str):
        if (self.closed or type(port) is not DreamPort or self.ports.get(port.identity.binding_id) is not port
                or operation not in port.identity.operations or time.monotonic()>=port.identity.expires_at
                or not self.control.configuration.candidate.text.record('dream.management')['control_enabled']):
            raise OwnerFailure('ACCESS_DENIED','capability','OPERATION_NOT_GRANTED')
        self.host.checkpoint()

    async def call(self,port:DreamPort,operation:str,key:str,values:dict[str,object]):
        deadline=current_deadline(cast(int,self.control.configuration.candidate.text.record('dream.resources')['operation_timeout_ms'])/1000)
        self.check(port,operation)
        if operation=='resume_dream' and self.host.provider is not None:
            health=self.host.provider.get_health()
            if health.unknown_observations:raise OwnerFailure('INVALID_STATE','request','REMOTE_RESULT_UNKNOWN')
            if health.cleanup_pending:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        if self.host.runtime is None:raise InvalidValue()
        payload=dict(values)
        entry_intent=None
        if operation=='start_dream':
            mode=payload.pop('mode')
            if mode not in ('FOCUSED','BACKGROUND'):raise InvalidValue()
            kind='start_focused_dream' if mode=='FOCUSED' else 'start_background_dream'
        elif operation=='abort_dream':
            entry_intent=self.control.begin_intent('abort_dream',key,payload,port.identity.principal_id)
            kind='abort_dream'
        else:kind=operation
        if operation!='abort_dream':entry_intent=self.control.begin_intent(kind,key,payload,port.identity.principal_id)
        if entry_intent is None:raise InvalidValue()
        # execute() establishes stopping intent synchronously before its first
        # wait. Separate coordinators must not serialize a newer stop behind it.
        async def actual_call():
            with DeadlineScope(deadline):
                check_deadline();self.check(port,operation)
                selected=kind
                if selected=='abort_dream':
                    current=await self.control.inspect(cast(str,values['run_id']));check_deadline()
                    if current is None:raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
                    previous='exit_focused_dream' if current['mode']=='FOCUSED' else 'abort_background_dream'
                    original=await self.control.confirm(previous,key);check_deadline()
                    if original is not None:return original
                    requested=await self.control.execute('request_dream_abort',key,payload,actor=port.identity.principal_id,intent=entry_intent)
                    if type(requested) is not Committed:return requested
                    for _ in range(8):
                        check_deadline()
                        current=await self.control.inspect(cast(str,values['run_id']));check_deadline()
                        if current is None:raise InvalidValue()
                        if current['state']=='ABORTED':
                            from companion_memory.memory.formats import record
                            final_operation=record(current['last_operation'])
                            return await self.control.confirm(cast(str,final_operation['operation_kind']),cast(str,final_operation['operation_key']))
                        if self.host.combination.periodic is None or self.host.combination.periodic.job is not None:
                            return Found(MappingProxyType({'state':'PENDING','cleanup_pending':True,'new_sends':0}))
                        advanced=await self.host.advance_dream();check_deadline()
                        if type(advanced) is not Committed:return advanced
                    return requested
                return await execute_selected(selected)
        async def execute_selected(kind):
            with DeadlineScope(deadline):
                original=await self.control.confirm(kind,key)
                check_deadline()
                if original is None and kind=='start_focused_dream':
                    if self.host.runtime is None:raise InvalidValue()
                    self.host.runtime.gate.close_ordinary()
                value=await self.control.execute(kind,key,payload,actor=port.identity.principal_id,intent=entry_intent)
                if original is not None:return value
                if kind in ('start_focused_dream','exit_focused_dream') and type(value) in (Committed,NotCommitted,Rejected):
                    check_deadline();await self.mode.synchronize()
                if kind=='exit_focused_dream' and type(value) is Committed:
                    current=await self.control.inspect(cast(str,values['run_id']));check_deadline()
                    if current is None:raise InvalidValue()
                    completed=await self.control.execute('complete_dream_exit',self.control.root_id[:12]+':'+sha256(key.encode()).hexdigest(),{
                        'run_id':current['run_id'],'expected_revision':current['revision'],'mode_epoch':current['mode_epoch']},actor=port.identity.principal_id)
                    if type(completed) is not Committed:return completed
                return value
        actual,logical=start_owned(actual_call());self.tasks.add(actual)
        def ended(task):
            if not task.cancelled():task.exception()
            self.tasks.discard(task)
        actual.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        return logical.result()

    def close(self):
        self.closed=True
        return not self.tasks
