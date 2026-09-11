"""Trusted application transaction assembly across ingress, buffers and runtime.

The coordinator alone commits. Domain owners receive finite scoped statements;
synthetic result/source participants must be explicitly supplied by test assembly.
Model execution is outside these synchronous short handlers.
"""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime,timezone
from types import MappingProxyType
from typing import Protocol,cast,Never

from companion_memory.configuration.persistence import StoredRuntimeConfiguration
from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence import (
    AuditFieldBinding,AuditResultBinding,BoundedTextSchema,Field,PersistenceService,RecordSchema,ModuleOwnerLease,
    RepositoryDefinition,ResultBoundCommandDefinition,ScalarSchema,SequenceSchema,UnitOfWork,Value,
)
from companion_memory.persistence.runtime_repositories import create_runtime_repositories
from companion_memory.ingress.transactions import IngressTransactions
from companion_memory.ingress.media import MediaParticipant
from companion_memory.ingress.events import decode_event,event_identity
from companion_memory.buffers.transactions import BufferTransactions
from .records import stored_event,stored_timestamp,OwnedRows,DomainFailure,data,stable_id,digest
from .transaction_schema import ID,INT,CHANGE,RESULT,ACCEPTANCE,facts


class LearningParticipant(Protocol):
    """Explicit local result owner; no model authority or default success behavior."""
    repositories: tuple[RepositoryDefinition,...]
    owner_module: str
    def bind(self,storage:PersistenceService,instance_id:str) -> None: ...
    def prepare_outcome(self,batch_snapshot:MappingProxyType,provider_handoff:MappingProxyType) -> dict[str,object]: ...
    def stage(self,uow:UnitOfWork,batch_id:str,candidate_id:str,outcome:dict[str,object]) -> None: ...
    def finalize(self,uow:UnitOfWork,batch_id:str,candidate_id:str,members:tuple[dict[str,object],...],protect:Callable[[str,str,int,str],None]) -> tuple[str,int]: ...
    async def recover(self,candidate_id:str) -> dict[str,object] | None: ...


@dataclass(frozen=True,slots=True)
class PublicationRecovery:
    """Retained completion and original local exit request, owned by the run owner."""
    run_id:str
    configuration_id:str
    publication_id:str
    operation_key:str
    expected_epoch:int
    actor_ref:str


class PublicationParticipant(Protocol):
    """Explicit durable publication evidence owner, independent of a mode flag."""
    def verify(self,uow:UnitOfWork,run_id:str,publication_id:str,configuration_id:str) -> bool: ...
    async def recover_publication(self,run_id:str,configuration_id:str) -> PublicationRecovery | None: ...


class RuntimeAssembly:
    """Complete fixed statements and result-bound command declarations before CREATE_NEW."""
    def __init__(self,participant:LearningParticipant | None = None,publication:PublicationParticipant | None=None,media:MediaParticipant | None=None):
        self._repositories=create_runtime_repositories()
        self.participant=participant
        self.publication=publication
        self.media=media
        self._binding:RuntimeTransactions | None=None
        repository={r.definition.owner_module:r.definition for r in self._repositories}
        extra=participant.repositories if participant else ()
        self.repositories=tuple(repository.values())+extra+(media.repositories if media else ())
        input_fields={
            'drain_complete':(Field('expected_epoch',INT),),
            'dream_call':(Field('run_id',ID),Field('expected_epoch',INT),Field('operation_key',ID),Field('payload',BoundedTextSchema(8192))),
            'schedule':(Field('entry_id',ID),Field('expected_revision',INT)),
            'initialize':(Field('config_snapshot_id',ID),),
            'register':tuple(Field(k,BoundedTextSchema(512) if k=='external_entry_id' else ID) for k in ('instance_id','host_id','platform_id','external_entry_id')),
            'accept':(Field('entry_id',ID),Field('event',BoundedTextSchema(8192))),
            'freeze':(Field('entry_id',ID),Field('run_id',ID),Field('batch_id',ID),Field('material_digest',ID),Field('members',SequenceSchema(RecordSchema((Field('message_id',ID),Field('entry_seq',INT),Field('role',ScalarSchema('enum',choices=('H','T','R'))),Field('payload_digest',ID))),1,128))),
            'associate':(Field('work_id',ID),Field('expected_revision',INT),Field('owner_generation',INT),Field('operation_key',ID)),
            'observe_work':(Field('work_id',ID),Field('expected_revision',INT),Field('owner_generation',INT),Field('state',ScalarSchema('enum',choices=('FROZEN','WAITING_ADMISSION','REMOTE_RESULT_UNKNOWN','SYSTEM_BLOCKED'))),Field('request_id',ID,nullable=True)),
            'claim':(Field('work_id',ID),Field('expected_revision',INT),Field('owner_generation',INT)),
            'stage':(Field('work_id',ID),Field('expected_revision',INT),Field('owner_generation',INT),Field('candidate_id',ID),Field('outcome',BoundedTextSchema(8192))),
            'finalize':(Field('work_id',ID),Field('expected_revision',INT),Field('owner_generation',INT),Field('candidate_id',ID)),
            'enter_focus':(Field('actor_ref',ID),Field('expected_epoch',INT),Field('run_id',ID)),
            'focus_ready':(Field('actor_ref',ID),Field('expected_epoch',INT),Field('run_id',ID)),
            'finish_focus':(Field('actor_ref',ID),Field('expected_epoch',INT),Field('run_id',ID),Field('publication_id',ID)),
            'fault':(Field('actor_ref',ID),Field('expected_epoch',INT),Field('run_id',ID)),
            'transfer':(Field('entry_id',ID),Field('expected_cursor',INT)),
            'recover_page':(Field('after',BoundedTextSchema(128)),Field('generation',INT),Field('owner',ScalarSchema('enum',choices=('ingress','buffers','runtime'))),Field('table',ID),Field('rows',SequenceSchema(RecordSchema((Field('object_id',ID),Field('revision',INT))),0,128))),
        }
        writes={
            'drain_complete':('runtime',),
            'schedule':('runtime',),'dream_call':('runtime',),'initialize':('runtime',),'register':('ingress','buffers'),'accept':('ingress','buffers'),
            'freeze':('buffers','runtime'),'associate':('runtime',),'observe_work':('runtime',),'claim':('runtime',),'stage':('runtime',),'finalize':('ingress','buffers','runtime'),
            'enter_focus':('runtime',),'focus_ready':('runtime',),'finish_focus':('runtime',),'fault':('runtime',),
            'transfer':('buffers','runtime'),'recover_page':('runtime',),
        }
        commands=[]
        for kind,fields in input_fields.items():
            owners=writes[kind]+((participant.owner_module,) if participant and kind in ('stage','finalize') else ())+((media.owner_module,) if media and kind in ('accept','finalize') else ())
            requirements=tuple(AuditRequirement(owner,owner+'_changed',kind.upper(),1,(kind.upper(),),CHANGE) for owner in owners)
            bindings=tuple(AuditResultBinding(r.event_slot,1,(
                AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),
                AuditFieldBinding('reason_code','CONSTANT',constant=kind.upper()),AuditFieldBinding('target_refs','RESULT',('targets',)),AuditFieldBinding('change','RESULT',('change',)),
            )) for r in requirements)
            def handler(uow,values,action=kind):
                if self._binding is None:raise DomainFailure('INVALID_STATE','state','NOT_READY')
                try:return self._binding.handle(action,uow,MappingProxyType({k:v for k,v in values.items() if k!='coordination_key'}))
                except DomainFailure as issue:
                    self._binding.causes.record(action,values,issue)
                    raise
            commands.append(ResultBoundCommandDefinition('runtime',kind,1,RecordSchema(fields+(Field('coordination_key',ID),)),1,ACCEPTANCE if kind=='accept' else RESULT,self.repositories,requirements,handler,RecordSchema((Field('actor',ID),)),bindings))
        self.commands=tuple(commands)

    def bind(self,storage:PersistenceService,configuration:StoredRuntimeConfiguration,instance_id:str,utc_now:Callable[[],datetime]) -> RuntimeTransactions:
        if self._binding is not None:raise ValueError('Runtime assembly is already bound.')
        rows={r.definition.owner_module:OwnedRows(r,storage,instance_id) for r in self._repositories}
        lease=storage.claim_module_owner(self._repositories[2].definition)
        if lease is None:raise ValueError('Runtime owner is unavailable.')
        if configuration.database_id!=lease.database_id:
            lease.release()
            raise ValueError('Configuration belongs to another database.')
        if self.participant:self.participant.bind(storage,instance_id)
        if self.media:self.media.bind(storage,instance_id)
        binding=RuntimeTransactions(rows,configuration,instance_id,utc_now,self.participant)
        binding.lease=lease
        binding.publication=self.publication
        binding.media=self.media
        self._binding=binding
        return binding


class RuntimeTransactions:
    def __init__(self,rows:dict[str,OwnedRows],configuration:StoredRuntimeConfiguration,instance_id:str,utc_now:Callable[[],datetime],participant:LearningParticipant | None):
        self.ingress=IngressTransactions(rows['ingress']);self.buffers=BufferTransactions(rows['buffers']);self.rows=rows['runtime']
        self.configuration,self.instance_id,self.utc_now,self.participant=configuration,instance_id,utc_now,participant
        self.publication:PublicationParticipant | None=None
        self.media:MediaParticipant | None=None
        from .causes import CommandCauses
        self.causes=CommandCauses()
        self.lease:ModuleOwnerLease | None=None

    def mode(self,uow:UnitOfWork) -> dict[str,object]:
        mode=self.rows.get('mode','mode',uow)
        if mode is None:raise DomainFailure('INVALID_STATE','state','NOT_READY')
        return mode

    def handle(self,kind:str,uow:UnitOfWork,v:MappingProxyType[str,Value]) -> object:
        if self.lease is None or not self.lease.is_active():raise DomainFailure('INVALID_STATE','state','SERVICE_CLOSED')
        import json
        config=self.configuration
        sid=config.snapshot_id
        batch_facts:dict[str,object]={}
        def report(key,entry,state,**kw):return facts(key,entry,state,config_snapshot_id=sid,run_id=cast(str,kw.pop('run_id','runtime')),**{**batch_facts,**kw})
        if kind=='initialize':
            if self.rows.get('mode','mode',uow) is not None:raise DomainFailure('PRECONDITION_FAILED','state','REVISION_CHANGED')
            self.rows.insert('mode',uow,'mode',self.instance_id,0,'NORMAL',{'epoch':1,'run_id':None,'publication_id':None,'configuration_id':sid,'actor_ref':'bootstrap'})
            self.rows.insert('scheduler',uow,'scheduler',self.instance_id,0,'READY',{'after':''})
            return report('mode',self.instance_id,'NORMAL',epoch=1,revision=1)
        if kind=='register':
            entry=self.ingress.register(uow,v)
            self.buffers.register(uow,entry,cast(str,v['platform_id']))
            return report(entry,entry,'REGISTERED',revision=1,host_id=v['host_id'],platform_id=v['platform_id'])
        mode=self.mode(uow);epoch=cast(int,data(mode)['epoch']);mode_state=cast(str,mode['state'])
        if kind=='schedule':
            if mode_state not in ('NORMAL','DRAINING'):raise DomainFailure('MODE_BLOCKED','mode','DREAMING')
            cursor=self.rows.get('scheduler','scheduler',uow)
            if cursor is None or cursor['revision']!=v['expected_revision']:raise DomainFailure('PRECONDITION_FAILED','state','REVISION_CHANGED')
            if self.ingress.rows.get('entries',cast(str,v['entry_id']),uow) is None:raise DomainFailure('PRECONDITION_FAILED','entry','REVISION_CHANGED')
            self.rows.update('scheduler',uow,cursor,'READY',{'after':v['entry_id']})
            return report('scheduler',cast(str,v['entry_id']),'READY',revision=cast(int,cursor['revision'])+1,epoch=epoch)
        if kind=='dream_call':
            if mode_state!='DREAM_FOCUSED' or epoch!=v['expected_epoch'] or data(mode)['run_id']!=v['run_id']:raise DomainFailure('MODE_BLOCKED','mode','DREAMING')
            key=stable_id('dream_call',v['run_id'],v['operation_key'])
            self.rows.insert('dream_calls',uow,key,self.instance_id,epoch,'CLAIMED',dict(v))
            return report(key,self.instance_id,'EXECUTING',run_id=v['run_id'],revision=1,epoch=epoch,mode=mode_state)
        if kind=='accept':
            entry_id=cast(str,v['entry_id']);entry=self.ingress.rows.get('entries',entry_id,uow)
            if entry is None:raise DomainFailure('ACCESS_DENIED','entry','BINDING_MISMATCH')
            event=decode_event(cast(str,v['event']).encode(),config.candidate.runtime.integer('ingress.event_max_bytes'))
            state=self.buffers.rows.get('entry_state',entry_id,uow)
            if state is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
            staged=state['state']!='NONE'
            placement='FOCUS_STAGED' if mode_state in ('DREAM_PREPARING','DREAM_FOCUSED','FAULTED') else 'DRAIN_STAGED' if staged else 'NORMAL_PENDING'
            sequence,_=self.buffers.allocate(uow,entry_id)
            now=self.utc_now()
            if type(now) is not datetime or now.tzinfo is not timezone.utc:raise DomainFailure('INVALID_INPUT','state','INVALID_TIME')
            time_us=int(now.timestamp()*1000000)
            message_id=self.ingress.accept(uow,entry,event,sequence,time_us,placement,mode_state,epoch)
            if event['media'] and self.media is None:
                raise DomainFailure('CAPABILITY_UNAVAILABLE','event','MEDIA_NOT_SUPPORTED')
            if self.media is not None:
                self.media.retain_event(uow,entry_id,message_id,cast(tuple[Value,...],event['media']))
            self.buffers.append(uow,entry_id,message_id,sequence,placement,time_us)
            result=report(message_id,entry_id,placement,sequence=sequence,time_us=time_us,epoch=epoch,mode=mode_state,revision=1,host_id=data(entry)['host_id'],platform_id=data(entry)['platform_id'])
            return {**result,'receipt_version':1,'acceptance_id':stable_id('acceptance',message_id),'message_id':message_id,'entry_seq':sequence,'received_at_utc':now.isoformat(timespec='microseconds').replace('+00:00','Z'),'accepted_placement':placement,'mode_at_accept':mode_state,'mode_epoch_at_accept':epoch,'learning_state':'NOT_LEARNED'}
        if kind=='freeze':
            if mode_state not in ('NORMAL','DRAINING'):raise DomainFailure('MODE_BLOCKED','mode','DREAMING')
            entry_id=cast(str,v['entry_id']);entry=self.ingress.rows.get('entries',entry_id,uow)
            if entry is None:raise DomainFailure('ACCESS_DENIED','entry','BINDING_MISMATCH')
            p=config.candidate.platform(cast(str,data(entry)['platform_id']))
            member_values=tuple(cast(dict[str,object],dict(cast(MappingProxyType[str,Value],m))) for m in cast(tuple[Value,...],v['members']))
            batch_id=cast(str,v['batch_id']);run_id=cast(str,v['run_id'])
            from companion_memory.buffers import build_material,MaterialRecord
            material=[];target_times=[]
            for member in member_values:
                event=self.ingress.rows.get('events',cast(str,member['message_id']),uow)
                payload=self.ingress.rows.get('payloads',cast(str,member['message_id']),uow)
                if event is None or payload is None or event['entry_id']!=entry_id or event['sequence']!=member['entry_seq'] or data(event)['payload_digest']!=member['payload_digest'] or digest(data(payload)['payload'])!=member['payload_digest']:
                    raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
                micros=cast(int,data(event)['received_at_us'])
                if member['role']=='T':target_times.append(micros)
                timestamp=stored_timestamp(micros)
                material.append(MaterialRecord(cast(str,member['role']),cast(str,member['message_id']),cast(int,member['entry_seq']),timestamp,stored_event(data(payload)['payload'])))
            messages=build_material((self.instance_id,cast(str,data(entry)['host_id']),p.platform_id,entry_id,batch_id,run_id,sid),tuple(material))
            if digest(tuple(dict(m) for m in messages))!=v['material_digest']:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
            created_at_us=int(self.utc_now().timestamp()*1000000)
            batch=self.buffers.freeze(uow,entry_id,batch_id,run_id,sid,p.count('target_count'),p.count('recent_context_count'),p.count('history_context_count'),cast(str,v['material_digest']),member_values,created_at_us,(min(target_times),max(target_times)))
            self.rows.insert('work',uow,batch_id,entry_id,cast(int,batch['sequence']),'FROZEN',{'run_id':run_id,'generation':0,'candidate_id':None,'provider_request_id':None,'admission_generation':0,'provider_operation_key':None})
            self.rows.insert('triggers',uow,stable_id('trigger',batch_id),entry_id,cast(int,batch['sequence']),'FROZEN',{'batch_id':batch_id})
            batch_facts={k:data(batch)[k] for k in ('range_start','range_end','range_start_us','range_end_us')}
            return report(batch_id,entry_id,'FROZEN',run_id=run_id,revision=1,epoch=epoch,target_count=data(batch)['target_count'],history_count=data(batch)['history_count'],recent_count=data(batch)['recent_count'])
        if kind in ('claim','associate','observe_work','stage','finalize'):
            work=self.rows.get('work',cast(str,v['work_id']),uow)
            if work is None or work['revision']!=v['expected_revision']:raise DomainFailure('PRECONDITION_FAILED','work','REVISION_CHANGED')
            w=data(work);entry_id=cast(str,work['entry_id']);batch_id=cast(str,work['object_id']);generation=cast(int,v['owner_generation'])
            batch=self.buffers.rows.get('batches',batch_id,uow)
            if batch is None:raise DomainFailure('STORAGE_FAILED','batch','INTEGRITY_FAILURE')
            batch_facts={k:data(batch)[k] for k in ('target_count','history_count','recent_count','range_start','range_end','range_start_us','range_end_us')}
            if kind=='claim':
                if mode_state not in ('NORMAL','DRAINING'):raise DomainFailure('MODE_BLOCKED','mode','DREAMING')
                if work['state'] not in ('FROZEN','WAITING_ADMISSION') or generation<=cast(int,w['generation']):raise DomainFailure('PRECONDITION_FAILED','work','WORK_FENCED')
                if work['state']=='WAITING_ADMISSION':
                    if cast(int,w['admission_generation'])>=2**63-1:raise DomainFailure('INVALID_INPUT','work','LIMIT_EXCEEDED')
                    w={**w,'admission_generation':cast(int,w['admission_generation'])+1,'provider_operation_key':None,'provider_request_id':None}
                self.rows.update('work',uow,work,'EXECUTING',{**w,'generation':generation})
                return report(batch_id,entry_id,'EXECUTING',run_id=w['run_id'],owner_generation=generation,revision=cast(int,work['revision'])+1,epoch=epoch)
            if w['generation']!=generation:raise DomainFailure('PRECONDITION_FAILED','work','WORK_FENCED')
            if kind=='associate':
                if work['state']!='EXECUTING' or w['provider_operation_key'] is not None:raise DomainFailure('PRECONDITION_FAILED','work','WORK_FENCED')
                self.rows.update('work',uow,work,'EXECUTING',{**w,'provider_operation_key':v['operation_key']})
                return report(batch_id,entry_id,'EXECUTING',run_id=w['run_id'],owner_generation=generation,revision=cast(int,work['revision'])+1,epoch=epoch)
            if kind=='observe_work':
                if work['state'] not in ('EXECUTING','REMOTE_RESULT_UNKNOWN','SYSTEM_BLOCKED'):raise DomainFailure('PRECONDITION_FAILED','work','WORK_FENCED')
                if v['state']=='FROZEN' and (work['state']!='EXECUTING' or w['provider_operation_key'] is not None or w['provider_request_id'] is not None or v['request_id'] is not None):raise DomainFailure('PRECONDITION_FAILED','work','WORK_FENCED')
                self.rows.update('work',uow,work,cast(str,v['state']),{**w,'provider_request_id':v['request_id']})
                return report(batch_id,entry_id,cast(str,v['state']),run_id=w['run_id'],owner_generation=generation,revision=cast(int,work['revision'])+1,epoch=epoch)
            if self.participant is None:raise DomainFailure('CAPABILITY_UNAVAILABLE','participant','LEARNING_PARTICIPANT_MISSING')
            candidate_id=cast(str,v['candidate_id'])
            if kind=='stage':
                if work['state'] not in ('EXECUTING','REMOTE_RESULT_UNKNOWN'):raise DomainFailure('PRECONDITION_FAILED','work','WORK_FENCED')
                outcome=json.loads(cast(str,v['outcome']))
                self.participant.stage(uow,batch_id,candidate_id,outcome)
                self.rows.update('work',uow,work,'CANDIDATE_STORED',{**w,'candidate_id':candidate_id,'provider_request_id':outcome['request_id']})
                return report(batch_id,entry_id,'CANDIDATE_STORED',run_id=w['run_id'],candidate_id=candidate_id,owner_generation=generation,revision=cast(int,work['revision'])+1,epoch=epoch)
            if work['state']!='CANDIDATE_STORED' or w['candidate_id']!=candidate_id:raise DomainFailure('PRECONDITION_FAILED','work','WORK_FENCED')
            batch=self.buffers.rows.get('batches',batch_id,uow)
            if batch is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
            members=self.buffers.members(uow,entry_id,batch_id)
            for member in members:
                if member['role']=='T':
                    self.ingress.settle_target(uow,cast(str,member['message_id']),batch_id)
                    if self.media:self.media.settle_event(uow,cast(str,member['message_id']))
            def protect(e:str,m:str,s:int,o:str):
                from companion_memory.persistence.schema import valid_identifier
                if e not in ('',entry_id) or not valid_identifier(o) or not any(member['message_id']==m and member['entry_seq']==s for member in members):raise DomainFailure('ACCESS_DENIED','batch','BINDING_MISMATCH')
                self.buffers.reference(uow,entry_id,m,s,o)
            terminal,count=self.participant.finalize(uow,batch_id,candidate_id,members,protect)
            if terminal not in ('SUCCEEDED','FAILED_DROPPED','SENSITIVE_DROPPED') or type(count) is not int or count<0:raise DomainFailure('PARTICIPANT_FAILED','participant','RESULT_INVALID')
            entry=self.ingress.rows.get('entries',entry_id,uow)
            if entry is None:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
            p=config.candidate.platform(cast(str,data(entry)['platform_id']))
            released,hcount=self.buffers.terminate(uow,batch,terminal,count,p.count('history_context_count'))
            deleted=0
            for mid in released:
                if not self.buffers.referenced(uow,entry_id,mid):
                    if self.media:self.media.release_event(uow,mid)
                    deleted+=int(self.ingress.release_payload(uow,mid))
            # Every target records consumption even when a shared source keeps its
            # payload. The ingress audit covers both consumption and release facts.
            self.rows.update('work',uow,work,'TERMINAL',{**w,'terminal':terminal})
            targets=[m for m in members if m['role']=='T']
            return report(batch_id,entry_id,terminal,run_id=w['run_id'],candidate_id=candidate_id,revision=cast(int,work['revision'])+1,epoch=epoch,owner_generation=generation,
                result_count=count,target_count=len(targets),history_count=hcount,recent_count=data(batch)['recent_count'],released_count=deleted,range_start=targets[0]['entry_seq'],range_end=targets[-1]['entry_seq'])
        if kind in ('enter_focus','focus_ready','finish_focus','fault'):
            if epoch!=v['expected_epoch']:raise DomainFailure('PRECONDITION_FAILED','mode','REVISION_CHANGED')
            target={'enter_focus':'DREAM_PREPARING','focus_ready':'DREAM_FOCUSED','finish_focus':'DRAINING','fault':'FAULTED'}[kind]
            required={'enter_focus':'NORMAL','focus_ready':'DREAM_PREPARING','finish_focus':'DREAM_FOCUSED'}
            if kind in required and mode_state!=required[kind]:raise DomainFailure('PRECONDITION_FAILED','mode','REVISION_CHANGED')
            if kind!='enter_focus' and data(mode)['run_id']!=v['run_id']:raise DomainFailure('ACCESS_DENIED','mode','BINDING_MISMATCH')
            if kind=='focus_ready':
                for state in ('EXECUTING','REMOTE_RESULT_UNKNOWN','LOCAL_COMMIT_UNCONFIRMED','CANDIDATE_STORED','SYSTEM_BLOCKED'):
                    page=self.rows.participate('work_state_count',uow,{'state':state})
                    if page[0]['count']:raise DomainFailure('RESOURCE_BUSY','work','OWNER_ACTIVE')
            if kind=='finish_focus' and (self.publication is None or not self.publication.verify(uow,cast(str,v['run_id']),cast(str,v['publication_id']),sid)):
                raise DomainFailure('PRECONDITION_FAILED','mode','PUBLICATION_MISSING')
            self.rows.update('mode',uow,mode,target,{**data(mode),'epoch':epoch+1,'run_id':v['run_id'],'actor_ref':v['actor_ref'],'publication_id':v.get('publication_id',data(mode)['publication_id'])})
            return report('mode',self.instance_id,target,run_id=v['run_id'],mode=target,previous_mode=mode_state,publication_id=v.get('publication_id') or 'NONE',epoch=epoch+1,previous_epoch=epoch,revision=cast(int,mode['revision'])+1)
        if kind=='drain_complete':
            if mode_state!='DRAINING' or epoch!=v['expected_epoch']:raise DomainFailure('PRECONDITION_FAILED','mode','REVISION_CHANGED')
            if self.buffers.rows.participate('entry_pending_count',uow,{'state':'PENDING'})[0]['count']:raise DomainFailure('PRECONDITION_FAILED','entry','TRANSFER_CURSOR_CHANGED')
            self.rows.update('mode',uow,mode,'NORMAL',{**data(mode),'epoch':epoch+1})
            return report('mode',self.instance_id,'NORMAL',mode='NORMAL',previous_mode=mode_state,epoch=epoch+1,previous_epoch=epoch,revision=cast(int,mode['revision'])+1)
        if kind=='transfer':
            if mode_state not in ('DRAINING','NORMAL'):raise DomainFailure('MODE_BLOCKED','mode','DREAMING')
            entry_id=cast(str,v['entry_id']);cursor,count,remaining=self.buffers.transfer(uow,entry_id,cast(int,v['expected_cursor']),config.candidate.runtime.integer('runtime.transfer_page_size'))
            key=stable_id('transfer',entry_id,v['expected_cursor'])
            self.rows.insert('transfers',uow,key,entry_id,cursor,'TRANSFERRED',{'cursor':cursor,'count':count,'remaining':remaining})
            if mode_state=='DRAINING' and self.buffers.rows.participate('entry_pending_count',uow,{'state':'PENDING'})[0]['count']==0:
                self.rows.update('mode',uow,mode,'NORMAL',{**data(mode),'epoch':epoch+1})
                mode_state='NORMAL';epoch+=1
            return report(key,entry_id,'TRANSFERRED',sequence=cursor,previous_cursor=v['expected_cursor'],range_start=v['expected_cursor'],range_end=cursor,remaining_count=remaining,time_us=int(self.utc_now().timestamp()*1000000),transferred_count=count,epoch=epoch,mode=mode_state,revision=1)
        if kind=='recover_page':
            owner=cast(str,v['owner']);table=cast(str,v['table'])
            owned={'ingress':self.ingress.rows,'buffers':self.buffers.rows,'runtime':self.rows}[owner]
            if table not in {'ingress':('entries','events','payloads'),'buffers':('positions','references','batches','members','entry_state'),'runtime':('work','triggers','transfers','scheduler','dream_calls','recovery')}[owner]:raise DomainFailure('INVALID_INPUT','state','INVALID_SHAPE')
            for meta in cast(tuple[MappingProxyType[str,Value],...],v['rows']):
                row=owned.get(table,cast(str,meta['object_id']),uow)
                if row is None or row['revision']!=meta['revision']:raise DomainFailure('PRECONDITION_FAILED','state','REVISION_CHANGED')
                self.verify_row(owner,table,row,uow)
            key=stable_id('recovery',v['generation'],owner,table,v['after'])
            self.rows.insert('recovery',uow,key,self.instance_id,cast(int,v['generation']),'RECOVERING',{'after':v['after'],'generation':v['generation'],'configuration_id':sid,'database_id':config.database_id,'protocol':1,'scan_owner':owner,'scan_table':table,'verified_rows':v['rows']})
            return report(key,self.instance_id,'RECOVERING',revision=1,epoch=epoch,mode=mode_state)
        raise DomainFailure('INVALID_INPUT','state','INVALID_SHAPE')

    def verify_row(self,owner:str,table:str,row:dict[str,object],uow:UnitOfWork) -> None:
        """Revalidate current body and cross-owner references inside the checkpoint UoW."""
        def corrupt() -> Never:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        entry_id=cast(str,row['entry_id']);object_id=cast(str,row['object_id']);values=data(row)
        if owner=='ingress' and table=='entries':
            if values['instance_id']!=self.instance_id or stable_id('entry',values['instance_id'],values['host_id'],values['platform_id'],values['external_entry_id'])!=object_id:corrupt()
            self.configuration.candidate.platform(cast(str,values['platform_id']))
            if self.buffers.rows.get('entry_state',object_id,uow) is None:corrupt()
        if owner=='ingress' and table in ('events','payloads') or owner=='buffers':
            entry=self.ingress.rows.get('entries',entry_id,uow)
            if entry is None:corrupt()
        if owner=='ingress' and table=='payloads':
            event=self.ingress.rows.get('events',object_id,uow)
            if event is None or event['entry_id']!=entry_id or event['sequence']!=row['sequence'] or digest(values['payload'])!=data(event)['payload_digest']:corrupt()
            stored_event(values['payload'])
            if not self.buffers.referenced(uow,entry_id,object_id):corrupt()
        if owner=='ingress' and table=='events':
            entry=self.ingress.rows.get('entries',entry_id,uow)
            state=self.buffers.rows.get('entry_state',entry_id,uow)
            if entry is None or state is None or cast(int,row['sequence'])>cast(int,data(state)['next_sequence']):corrupt()
            if values['binding']!=[self.instance_id,data(entry)['host_id'],entry_id]:corrupt()
            position=self.buffers.rows.get('positions',object_id,uow)
            if row['state']=='ACCEPTED':
                if 'terminal_batch_id' in values or position is None or position['entry_id']!=entry_id or position['sequence']!=row['sequence']:corrupt()
                if self.buffers.rows.get('references',stable_id('reference','pending',object_id),uow) is None:corrupt()
                payload=self.ingress.rows.get('payloads',object_id,uow)
                if payload is None or digest(data(payload)['payload'])!=values['payload_digest']:corrupt()
            else:
                if position is not None or 'terminal_batch_id' not in values:corrupt()
                batch_id=cast(str,values['terminal_batch_id'])
                batch=self.buffers.rows.get('batches',batch_id,uow)
                member=self.buffers.rows.get('members',stable_id('member',batch_id,object_id),uow)
                if batch is None or batch['state']!='TERMINAL' or batch['entry_id']!=entry_id or member is None or data(member)['role']!='T':corrupt()
        if owner=='buffers' and table in ('positions','references','members'):
            mid=cast(str,values['message_id']) if table!='positions' else object_id
            event=self.ingress.rows.get('events',mid,uow)
            if event is None or event['entry_id']!=entry_id or event['sequence']!=row['sequence']:corrupt()
            if table=='members':
                batch=self.buffers.rows.get('batches',cast(str,row['state']),uow)
                if batch is None or values['payload_digest']!=data(event)['payload_digest']:corrupt()
                needed=batch['state']!='TERMINAL'
            else:needed=True
            if needed:
                payload=self.ingress.rows.get('payloads',mid,uow)
                if payload is None or digest(data(payload)['payload'])!=data(event)['payload_digest']:corrupt()
            if table=='positions':
                if event['state']!='ACCEPTED' or self.buffers.rows.get('references',stable_id('reference','pending',mid),uow) is None:corrupt()
            if table=='references':
                reference_owner=cast(str,values['owner'])
                if object_id!=stable_id('reference',reference_owner,mid) or row['state']!=mid:corrupt()
                if reference_owner=='pending':
                    position=self.buffers.rows.get('positions',mid,uow)
                    if event['state']!='ACCEPTED' or position is None or position['entry_id']!=entry_id or position['sequence']!=row['sequence']:corrupt()
                elif reference_owner.startswith('history:'):
                    state=self.buffers.rows.get('entry_state',entry_id,uow)
                    if reference_owner!='history:'+entry_id or state is None or mid not in cast(list[str],data(state)['history']) or event['state']!='CONSUMED':corrupt()
                elif reference_owner.startswith('batch:'):
                    batch=self.buffers.rows.get('batches',reference_owner,uow)
                    member=self.buffers.rows.get('members',stable_id('member',reference_owner,mid),uow)
                    if batch is None or batch['state']=='TERMINAL' or batch['entry_id']!=entry_id or member is None:corrupt()
        if owner=='buffers' and table=='entry_state':
            if entry_id!=object_id:corrupt()
            staged=self.buffers.rows.participate('positions_count',uow,{'entry_id':entry_id,'state':'STAGED'})[0]['count']
            if (row['state']=='NONE')!=(staged==0):corrupt()
            for mid in cast(list[str],values['history']):
                if self.buffers.rows.get('references',stable_id('reference','history:'+entry_id,mid),uow) is None:corrupt()
        if owner=='buffers' and table=='batches':
            if values['config_snapshot_id']!=self.configuration.snapshot_id or (values['material_protocol'],values['template_protocol'],values['participant_protocol'])!=('synthetic_window_base64:1','synthetic_target_refs:1','synthetic_learning:1'):corrupt()
            members=self.buffers.members(uow,entry_id,object_id)
            if tuple(m['role'] for m in members)!=('H',)*cast(int,values['history_count'])+('T',)*cast(int,values['target_count'])+('R',)*cast(int,values['recent_count']):corrupt()
            work=self.rows.get('work',object_id,uow)
            if work is None or work['entry_id']!=entry_id or data(work)['run_id']!=values['run_id'] or (work['state']=='TERMINAL')!=(row['state']=='TERMINAL'):corrupt()
            if row['state']!='TERMINAL':
                for member in members:
                    mid=cast(str,member['message_id'])
                    if self.buffers.rows.get('references',stable_id('reference',object_id,mid),uow) is None:corrupt()
                    if member['role'] in ('T','R'):
                        position=self.buffers.rows.get('positions',mid,uow)
                        if position is None or position['state']!='NORMAL' or position['entry_id']!=entry_id:corrupt()
        if owner=='runtime' and table=='recovery':
            self.verify_checkpoint(row)
        if owner=='runtime' and table=='work':
            batch=self.buffers.rows.get('batches',object_id,uow)
            if batch is None or batch['entry_id']!=entry_id or data(batch)['run_id']!=values['run_id'] or (row['state']=='TERMINAL')!=(batch['state']=='TERMINAL'):corrupt()
            if row['state']=='FROZEN' and (values['provider_operation_key'] is not None or values['provider_request_id'] is not None or values['candidate_id'] is not None):corrupt()

    def verify_checkpoint(self,row:dict[str,object]) -> None:
        """Check retained progress facts without creating progress for their scan.

        Historical page revisions are evidence about that past page, not a
        substitute for rechecking current business rows in this generation.
        """
        values=data(row)
        if (values['configuration_id']!=self.configuration.snapshot_id or values['database_id']!=self.configuration.database_id
                or row['object_id']!=stable_id('recovery',values['generation'],values['scan_owner'],values['scan_table'],values['after'])
                or row['sequence']!=values['generation'] or row['entry_id']!=self.instance_id):
            raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
