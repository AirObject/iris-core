"""Atomic daily candidate staging, fixed planning and formal terminal effects.

Every branch names its actual writers. Memory/source/history, subjects, goals,
reasoning progress and the original FIFO rotation share the final UoW. A
conflicting revision or release closure never publishes a subset of actions.
"""
from __future__ import annotations
import asyncio
from hashlib import sha256
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand,UnitOfWork,Committed,Found,NotCommitted,AuditFieldBinding,AuditResultBinding
from companion_memory.persistence.schema import Field,RecordSchema,SequenceSchema,BoundedTextSchema,InvalidValue,Value
from companion_memory.persistence.daily_records import ID,REVISION,Record,identity
from companion_memory.persistence.daily_results import FACT,TARGETS,INTENT,target
from companion_memory.persistence.text_records import isolate_record
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.completion import start_owned,CompletionScope
from companion_memory.persistence.owned_statements import OwnerFailure,OwnerCauses
from companion_memory.logging_service import AuditRequirement
from companion_memory.memory.formats import record,sequence
from companion_memory.memory.transactions import ApplyScope
from companion_memory.cognition.candidates import Candidate
from companion_memory.cognition.daily_candidates import DailyCandidateTransform
from companion_memory.cognition.daily_candidate_material import restore_candidate_material
from companion_memory.cognition.daily_output import decode_daily_output
from .content_assembly import owner_fact
from .results import NotCommitted as DomainNotCommitted,RuntimeError

def result_shape(owners):
    return RecordSchema((Field('operation_id',ID),Field('state',ID),Field('facts',RecordSchema(tuple(Field(owner,owner_fact(owner) if owner=='logging_service' else FACT) for owner in owners))),Field('targets',TARGETS))+
        ((Field('history_targets',TARGETS),) if 'logging_service' in owners else ()))

class DailyApplication:
    """One actual local coordinator; native owners retain every formal authority."""
    def __init__(self,content,reasoning,participants):
        self.content=content;self.reasoning=reasoning;self.bound=False;self.closed=False
        self.causes=OwnerCauses()
        self._task:asyncio.Task|None=None;self._job:asyncio.Task|None=None;self._draft=None
        definitions=[]
        stage=RecordSchema((Field('operation_id',ID),Field('run_id',ID),Field('generation',REVISION),Field('expected_revision',REVISION),
            Field('manifest',BoundedTextSchema(8192)),Field('leaves',SequenceSchema(BoundedTextSchema(8192),0,8))))
        layouts=[('stage_daily_candidate',('runtime','cognition','ingress'),stage),('stage_daily_candidate_media',('runtime','cognition','ingress','media'),stage),
            ('plan_daily_candidate',('memory',),RecordSchema((Field('operation_id',ID),Field('run_id',ID),Field('candidate_id',ID)))),
            ('replan_daily_candidate',('memory',),RecordSchema((Field('operation_id',ID),Field('run_id',ID),Field('candidate_id',ID))))]
        for terminal in ('empty','failure','sensitive'):
            for media in (False,True):
                layouts.append(('finish_daily_'+terminal+('_media' if media else ''),
                    ('runtime','cognition','ingress','buffers')+(('media',) if media else ()),
                    RecordSchema((Field('operation_id',ID),Field('run_id',ID),Field('candidate_id',ID)))))
        for media in (False,True):
            layouts.append(('expire_unstarted_learning'+('_media' if media else ''),
                ('runtime','ingress','buffers')+(('media',) if media else ()),RecordSchema((Field('operation_id',ID),Field('run_id',ID),Field('batch_id',ID)))))
            layouts.append(('expire_daily_learning'+('_media' if media else ''),
                ('runtime','cognition','ingress','buffers')+(('media',) if media else ()),RecordSchema((Field('operation_id',ID),Field('run_id',ID)))))
            layouts.append(('reject_daily_revision'+('_media' if media else ''),
                ('runtime','cognition','ingress','buffers')+(('media',) if media else ()),RecordSchema((Field('operation_id',ID),Field('run_id',ID)))))
            layouts.append(('reject_daily_ownership'+('_media' if media else ''),
                ('runtime','cognition','ingress','buffers')+(('media',) if media else ()),RecordSchema((Field('operation_id',ID),Field('run_id',ID)))))
            layouts.append(('reject_daily_scope'+('_media' if media else ''),
                ('runtime','cognition','ingress','buffers')+(('media',) if media else ()),RecordSchema((Field('operation_id',ID),Field('run_id',ID)))))
        for history in (False,True):
            for media in (False,True):
                for goals in (False,True):
                    kind='apply_daily_candidate'+('_history' if history else '')+('_media' if media else '')+('_goals' if goals else '')
                    owners=('runtime','cognition','memory','ingress','buffers')+(('logging_service',) if history else ())+(('media',) if media else ())+(('goals',) if goals else ())
                    layouts.append((kind,owners,RecordSchema((Field('operation_id',ID),Field('application_id',ID)))))
        for kind,owners,shape in layouts:
            requirements=tuple(AuditRequirement(owner,'object_history' if owner=='logging_service' else owner+'_daily',kind.upper(),1,('APPLY',),owner_fact(owner) if owner=='logging_service' else FACT,target_limit=16) for owner in owners)
            bindings=tuple(AuditResultBinding(r.event_slot,1,(AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),
                AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),AuditFieldBinding('target_refs','RESULT',('history_targets',) if r.owner_module=='logging_service' else ('facts',r.owner_module,'targets')),
                AuditFieldBinding('change','RESULT',('facts',r.owner_module)))) for r in requirements)
            def handle(uow,values,action=kind):
                try:return self.handle(action,uow,values)
                except OwnerFailure as failure:
                    self.causes.record(action,values,failure)
                    raise
            definitions.append(ResultBoundCommandDefinition('runtime',kind,1,shape,1,result_shape(owners),tuple(participants),requirements,handle,INTENT,bindings))
        self.commands=tuple(definitions)
    def bind(self,configuration,storage,verify_scope,verify_failed_scope=None,verify_unstarted_cleanup=None):
        if self.bound or self.content.configuration is not configuration or self.content.storage is not storage or not self.reasoning.bound:raise InvalidValue()
        self.configuration=configuration;self.storage=storage;self.verify_scope=verify_scope
        self.verify_failed_scope=verify_failed_scope
        self.verify_unstarted_cleanup=verify_unstarted_cleanup
        self.transform=DailyCandidateTransform(configuration);self.operations={d.operation_kind:storage.bind_operation(d,configuration.scope_id) for d in self.commands}
        self.bound=True
    def key(self,kind,*parts):return identity(kind,self.configuration.database_id,self.configuration.scope_id,*parts)
    @staticmethod
    def _basis_ids(output):
        if output is None:return set()
        try:actions=sequence(output['actions'])
        except (InvalidValue,KeyError):return set()
        return {cast(str,record(b)['object_id']) for raw in actions for b in sequence(record(raw)['basis_refs'])}
    def _originals(self,uow,snapshot,work):
        run,turn,initial,output,tools=snapshot;material=restore_candidate_material(initial,tools)
        if not self.verify_scope(run,material.authority,uow):raise OwnerFailure('ACCESS_DENIED','capability','SCOPE_CHANGED')
        source=self.content.participate_daily_source(uow,cast(str,run['batch_id']),cast(int,work['generation']),cast(int,work['revision']))
        if source!=material.source:raise InvalidValue()
        for mid,payload in material.payloads.items():
            member=next(record(m) for m in sequence(source['ordered_members']) if record(m)['message_id']==mid)
            if self.content.ingress.verify_member(uow,cast(str,source['entry_id']),member)!=payload:raise InvalidValue()
        platforms=[]
        for member in sequence(source['ordered_members']):
            member=record(member)
            if member['role']!='T':continue
            event=material.payloads[cast(str,member['message_id'])].event
            subject=self.content.memory.subject_for_platform(uow,cast(str,source['platform_id']),cast(str,record(event['sender'])['subject_id']))
            if subject is not None and subject not in platforms:platforms.append(subject)
        roots={}
        for obj in material.observed:
            if obj['object_id'] in self._basis_ids(output):roots[cast(str,obj['object_id'])]=self.content.memory.participate_basis_roots(uow,obj)
        candidate=self.transform.build(source,work,run,turn,output,material.authority,material.observed,material.payloads,tuple(platforms),roots)
        # Existing mutation/relationship endpoints still require their actual
        # original revision at application, even if their body is only read.
        for leaf in candidate.leaves:
            if leaf['action'] in ('REPLACE_CURRENT','SET_SCORES'):
                old=next(obj for obj in material.observed if obj['object_id']==leaf['target_id'])
                if self.content.memory.current(uow,cast(str,leaf['target_id']))!=old:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        return candidate,material
    def handle(self,kind:str,uow:UnitOfWork,v:Record):
        if self.closed or not self.bound:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        a=self.content;r=self.reasoning;memory=a.memory;plans=memory.daily_application
        if plans is None:raise InvalidValue()
        native_history=None;history_targets=();targets={}
        if kind.startswith('expire_unstarted_learning'):
            if self.verify_unstarted_cleanup is None or not self.verify_unstarted_cleanup(v['run_id'],v['batch_id'],uow):
                raise OwnerFailure('ACCESS_DENIED','capability','SCOPE_CHANGED')
            batch,work=a.participate_daily_work(uow,cast(str,v['batch_id']))
            source=a.participate_daily_source(uow,cast(str,v['batch_id']),cast(int,work['generation']),cast(int,work['revision']))
            prep=a.participate_daily_preparation(uow,cast(str,v['batch_id']))
            if source['run_id']!=v['run_id'] or time.time_ns()//1000<cast(int,prep['started_at_us'])+1200000000:raise InvalidValue()
            if kind!='expire_unstarted_learning'+('_media' if any(record(m)['media'] for m in sequence(source['ordered_members'])) else ''):raise InvalidValue()
            r.verify_unstarted_original(uow,cast(str,v['run_id']))
            for raw in sequence(source['ordered_members']):a.ingress.verify_member(uow,cast(str,source['entry_id']),record(raw))
            uow.require_commit_permission(lambda:self.verify_unstarted_cleanup is not None and self.verify_unstarted_cleanup(v['run_id'],v['batch_id'],None))
            before=a.daily_source_audit_targets(uow,source,include_buffers=True)
            a.expire_daily_work(uow,batch,work,'FAILED_DROPPED');state='FAILED_DROPPED'
            targets['runtime']=(target(cast(str,v['batch_id']),cast(int,work['revision'])+1,cast(int,work['revision'])),)
            targets.update(a.daily_source_audit_targets(uow,source,include_buffers=True,before=before))
        elif kind.startswith(('expire_daily_learning','reject_daily_revision','reject_daily_ownership','reject_daily_scope')):
            if kind.startswith('reject_daily_scope'):
                if self.verify_failed_scope is None:raise InvalidValue()
                run,finished,state=r.finish_scope_conflict(uow,cast(str,v['run_id']),self.verify_failed_scope)
            elif kind.startswith('reject_daily_ownership'):
                snapshot=r.participate_candidate_inputs(uow,cast(str,v['run_id']))
                batch,work=a.participate_daily_work(uow,cast(str,snapshot[0]['batch_id']))
                candidate,material=self._originals(uow,snapshot,work)
                plans.verify_exhausted_replan(uow,candidate,work,material.source,cast(str,snapshot[0]['authority_digest']),self.commands)
                run,finished,state=r.finish_ownership_conflict(uow,cast(str,v['run_id']))
            elif kind.startswith('reject_daily_revision'):
                snapshot=r.participate_candidate_inputs(uow,cast(str,v['run_id']))
                batch,work=a.participate_daily_work(uow,cast(str,snapshot[0]['batch_id']))
                try:self._originals(uow,snapshot,work)
                except OwnerFailure as failure:
                    if failure.reason!='REVISION_CONFLICT' or failure.cleanup_pending:raise
                else:raise OwnerFailure('PRECONDITION_FAILED','candidate','NO_CHANGE')
                run,finished,state=r.finish_revision_conflict(uow,cast(str,v['run_id']))
            else:run,finished,state=r.expire_original(uow,cast(str,v['run_id']))
            batch,work=a.participate_daily_work(uow,cast(str,run['batch_id']))
            source=a.participate_daily_source(uow,cast(str,run['batch_id']),cast(int,work['generation']),cast(int,work['revision']))
            base=next(name for name in ('reject_daily_revision','reject_daily_ownership','reject_daily_scope','expire_daily_learning') if kind.startswith(name))
            if kind!=base+('_media' if any(record(m)['media'] for m in sequence(source['ordered_members'])) else ''):raise InvalidValue()
            before=a.daily_source_audit_targets(uow,source,include_buffers=True)
            a.expire_daily_work(uow,batch,work,state)
            targets.update(runtime=(target(cast(str,run['batch_id']),cast(int,work['revision'])+1,cast(int,work['revision'])),),
                cognition=(target(cast(str,run['object_id']),cast(int,finished['revision']),cast(int,run['revision'])),))
            targets.update(a.daily_source_audit_targets(uow,source,include_buffers=True,before=before))
        elif kind.startswith('stage_daily_candidate'):
            snapshot=r.participate_candidate_inputs(uow,cast(str,v['run_id']));run=snapshot[0]
            batch,work=a.participate_daily_work(uow,cast(str,run['batch_id']))
            if work['generation']!=v['generation'] or work['revision']!=v['expected_revision'] or work['phase'] not in ('FROZEN','PARKED'):raise OwnerFailure('PRECONDITION_FAILED','candidate','WORK_FENCED')
            candidate,material=self._originals(uow,snapshot,work)
            if candidate.manifest['terminal_proposal']=='SUCCEEDED':r.require_fresh_application(uow,run)
            received=a.cognition.isolate(decode_content(cast(str,v['manifest']).encode(),8192),tuple(decode_content(cast(str,leaf).encode(),8192) for leaf in sequence(v['leaves'])))
            if self._draft is None or received!=self._draft or received!=candidate:raise InvalidValue()
            if kind!=('stage_daily_candidate_media' if any(record(m)['media'] for m in sequence(material.source['ordered_members'])) else 'stage_daily_candidate'):raise InvalidValue()
            before=a.daily_source_audit_targets(uow,material.source,include_buffers=False)
            changed=a.stage_daily_candidate(uow,candidate,work,material.authority.route_ids)
            progress=r.stage_candidate(uow,cast(str,run['object_id']),cast(str,candidate.manifest['candidate_id']))
            targets.update(runtime=(target(cast(str,run['batch_id']),cast(int,changed['revision']),cast(int,work['revision'])),),cognition=(target(cast(str,run['object_id']),cast(int,progress['revision']),cast(int,run['revision'])),))
            targets.update(a.daily_source_audit_targets(uow,material.source,include_buffers=False,before=before))
            state='CANDIDATE_STORED'
        elif kind.startswith('finish_daily_'):
            snapshot=r.participate_candidate_inputs(uow,cast(str,v['run_id']));run=snapshot[0]
            batch,work=a.participate_daily_work(uow,cast(str,run['batch_id']))
            candidate=a.cognition.load(uow,cast(str,v['candidate_id']))
            rebuilt,material=self._originals(uow,snapshot,work)
            if candidate!=rebuilt or candidate.leaves or work['candidate_id']!=v['candidate_id']:raise InvalidValue()
            if kind!=self.empty_kind(candidate,material.source):raise InvalidValue()
            if candidate.manifest['terminal_proposal']=='SUCCEEDED':r.require_fresh_application(uow,run)
            before=a.daily_source_audit_targets(uow,material.source,include_buffers=True)
            values=MappingProxyType({'operation_id':v['operation_id'],'batch_id':run['batch_id'],'candidate_id':candidate.manifest['candidate_id'],
                'expected_revision':work['revision'],'generation':work['generation'],'readable_objects':(),'readable_subjects':(),'now_us':time.time_ns()//1000})
            a.finish_candidate(uow,values,'commit_content_without_objects')
            finished=r.finish_candidate(uow,cast(str,run['object_id']),cast(str,candidate.manifest['candidate_id']))
            state=cast(str,candidate.manifest['terminal_proposal'])
            targets.update(runtime=(target(cast(str,run['batch_id']),cast(int,work['revision'])+1,cast(int,work['revision'])),),
                cognition=(target(cast(str,run['object_id']),cast(int,finished['revision']),cast(int,run['revision'])),))
            targets.update(a.daily_source_audit_targets(uow,material.source,include_buffers=True,before=before))
        elif kind in ('plan_daily_candidate','replan_daily_candidate'):
            snapshot=r.participate_candidate_inputs(uow,cast(str,v['run_id']));run=snapshot[0]
            batch,work=a.participate_daily_work(uow,cast(str,run['batch_id']))
            candidate=a.cognition.load(uow,cast(str,v['candidate_id']))
            if not candidate.leaves:raise InvalidValue()
            rebuilt,material=self._originals(uow,snapshot,work)
            if candidate!=rebuilt or run['phase']!='CANDIDATE_STORED' or work['candidate_id']!=v['candidate_id']:raise InvalidValue()
            r.require_fresh_application(uow,run)
            progress=plans.plan(uow,candidate,work,material.source,cast(str,run['authority_digest'])) if kind=='plan_daily_candidate' else plans.replan(uow,candidate,work,material.source,cast(str,run['authority_digest']),self.commands)
            targets['memory']=(target(cast(str,progress['object_id']),cast(int,progress['revision']),cast(int,progress['revision'])-1 if kind=='replan_daily_candidate' else None),);state='PLANNED'
        else:
            original=plans.rows.get('candidate_applications',uow,cast(str,v['application_id']))
            if original is None:raise InvalidValue()
            batch,work=a.participate_daily_work(uow,cast(str,original['batch_id']))
            candidate=a.cognition.load(uow,cast(str,original['candidate_id']));run_id=cast(str,candidate.manifest['reasoning_run_id'])
            snapshot=r.participate_candidate_inputs(uow,run_id);run=snapshot[0]
            rebuilt,material=self._originals(uow,snapshot,work)
            if rebuilt!=candidate:raise InvalidValue()
            if candidate.manifest['terminal_proposal']=='SUCCEEDED':
                r.require_fresh_application(uow,run)
            progress,release=plans.participate(uow,cast(str,v['application_id']),candidate,work,material.source,cast(str,run['authority_digest']),kind)
            source_ids=set()
            for leaf in candidate.leaves:
                if leaf['action'] in ('REPLACE_CURRENT','SET_SCORES'):
                    links=memory.links(uow,cast(str,leaf['target_id']),cast(int,leaf['expected_revision']))
                    source_ids.update(cast(str,record(link)['source_id']) for link in sequence(links['sources']))
            for leaf in candidate.leaves:
                if leaf['links'] is not None:source_ids.update(cast(str,record(link)['source_id']) for link in sequence(record(leaf['links'])['sources']))
            subjects=material.authority.subject_ids|frozenset(cast(str,leaf['target_id']) for leaf in candidate.leaves if leaf['action']=='REGISTER_SUBJECT')|frozenset(cast(str,item['object_id']) for item in map(record,sequence(candidate.manifest['action_mapping'])) if item['reused'])
            scope=ApplyScope(self.configuration.scope_id,cast(str,candidate.manifest['candidate_id']),cast(str,run['batch_id']),
                frozenset(cast(str,obj['object_id']) for obj in material.observed),subjects,material.authority.writable_objects|frozenset(cast(str,leaf['target_id']) for leaf in candidate.leaves
                    if leaf['action'] in ('REGISTER_SUBJECT','CREATE_MEMORY','CREATE_RELATION')),frozenset(source_ids))
            before=a.daily_source_audit_targets(uow,material.source,include_buffers=True,release=release)
            changed=plans.applied(uow,progress)
            values=MappingProxyType({'operation_id':v['operation_id'],'batch_id':run['batch_id'],'candidate_id':candidate.manifest['candidate_id'],
                'expected_revision':work['revision'],'generation':work['generation'],'readable_objects':tuple(scope.readable_objects),'readable_subjects':tuple(scope.readable_subjects),'now_us':time.time_ns()//1000})
            native=a.finish_candidate(uow,values,'commit_content_published' if candidate.leaves else 'commit_content_without_objects',release,scope)
            finished=r.finish_candidate(uow,run_id,cast(str,candidate.manifest['candidate_id']))
            state=cast(str,candidate.manifest['terminal_proposal'])
            targets.update(runtime=(target(cast(str,run['batch_id']),cast(int,work['revision'])+1,cast(int,work['revision'])),),
                cognition=(target(run_id,cast(int,finished['revision']),cast(int,run['revision'])),),memory=(target(cast(str,progress['object_id']),cast(int,changed['revision']),cast(int,progress['revision'])),))
            targets.update(a.daily_source_audit_targets(uow,material.source,include_buffers=True,release=release,before=before))
            goals=tuple(target(cast(str,leaf['target_id']),1) for leaf in candidate.leaves if leaf['action']=='CREATE_GOAL')
            if goals:targets['goals']=goals
            object_targets=tuple(target(cast(str,record(ref)['object_id']),cast(int,record(ref)['revision']),cast(int|None,record(ref)['previous_revision'])) for ref in sequence(native['object_refs']))
            targets['memory']+=object_targets
            if 'logging_service' in self.storage.transaction_audit_owners(uow):
                native_history=record(native['facts'])['logging_service']
                history_targets=tuple(ref for ref in object_targets if ref['previous_revision'] is not None)
        counts=self.storage.transaction_row_changes(uow);owners=self.storage.transaction_audit_owners(uow)
        facts={owner:native_history if owner=='logging_service' else {'rows_changed':counts.get(owner,0),'targets':targets[owner]} for owner in owners}
        all_targets=tuple(item for owner in owners if owner!='logging_service' for item in targets[owner])
        return isolate_record(result_shape(owners),{'operation_id':v['operation_id'],'state':state,'facts':facts,'targets':all_targets,
            **({'history_targets':history_targets} if native_history is not None else {})},32768)

    async def _collect(self,run_id:str,deadline:float):
        snapshot=await self.reasoning.read_candidate_inputs(run_id,deadline);run,turn,initial,output,tools=snapshot
        material=restore_candidate_material(initial,tools)
        if not self.verify_scope(run,material.authority,None):raise OwnerFailure('ACCESS_DENIED','capability','SCOPE_CHANGED')
        frozen=await self.content.read_daily_batch(cast(str,run['batch_id']));work=record(frozen['work'])
        if frozen['source']!=material.source:raise InvalidValue()
        subjects=[]
        for raw in sequence(material.source['ordered_members']):
            member=record(raw)
            if member['role']!='T':continue
            event=material.payloads[cast(str,member['message_id'])].event
            found=await self.content.memory.read_daily_platform_subject(cast(str,material.source['platform_id']),cast(str,record(event['sender'])['subject_id']))
            if found is not None and found not in subjects:subjects.append(found)
        roots={}
        for obj in material.observed:
            if obj['object_id'] in self._basis_ids(output):roots[cast(str,obj['object_id'])]=await self.content.memory.retained_basis_roots(obj,deadline)
        return self.transform.build(material.source,work,run,turn,output,material.authority,material.observed,material.payloads,tuple(subjects),roots),work

    async def apply(self,run_id:str):
        """Advance one original candidate; replay only confirms its original commands."""
        if self.closed or self._job is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        async def advance_candidate():
            run=await self.reasoning.rows.read('reasoning_runs',run_id)
            if run is None:raise InvalidValue()
            if run['phase']=='TERMINAL':
                operation=record(run['terminal_operation']);port=self.operations.get(cast(str,operation['operation_kind']))
                if port is None:raise InvalidValue()
                original=await port.read_receipt(cast(str,operation['operation_key']))
                if type(original) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
                return Committed(original.value,'EXISTING')
            if time.time_ns()//1000>=cast(int,run['deadline_at_us']):
                source=record((await self.content.read_daily_batch(cast(str,run['batch_id'])))['source'])
                kind='expire_daily_learning'+('_media' if any(record(m)['media'] for m in sequence(source['ordered_members'])) else '')
                return await self.execute(kind,self.key('daily-expire',run_id),{'run_id':run_id})
            if run['candidate_id'] is None:
                candidate,work=await self._collect(run_id,time.monotonic()+5);self._draft=candidate
                try:
                    source=record((await self.content.read_daily_batch(cast(str,run['batch_id'])))['source'])
                    kind='stage_daily_candidate_media' if any(record(m)['media'] for m in sequence(source['ordered_members'])) else 'stage_daily_candidate'
                    staged=await self.execute(kind,self.key('daily-candidate-stage',run_id),{'run_id':run_id,'generation':work['generation'],'expected_revision':work['revision'],
                        'manifest':encode_content(candidate.manifest,8192).decode(),'leaves':tuple(encode_content(leaf,8192).decode() for leaf in candidate.leaves)})
                    if type(staged) is not Committed:return staged
                finally:
                    if self._task is not None:await asyncio.wait((self._task,))
                    self._draft=None
                run=await self.reasoning.rows.read('reasoning_runs',run_id)
                if run is None:raise InvalidValue()
            cid=cast(str,run['candidate_id']);plans=self.content.memory.daily_application
            if plans is None:raise InvalidValue()
            snapshot=await self.reasoning.read_candidate_inputs(run_id,time.monotonic()+5)
            material=restore_candidate_material(snapshot[2],snapshot[4])
            candidate,_=await self._collect(run_id,time.monotonic()+5)
            if not candidate.leaves:
                kind=self.empty_kind(candidate,material.source)
                return await self.execute(kind,self.key('daily-empty-end',run_id),{'run_id':run_id,'candidate_id':cid})
            aid=plans.key('daily-application',cid);progress=await plans.rows.read('candidate_applications',aid)
            if progress is None:
                planned=await self.execute('plan_daily_candidate',self.key('daily-candidate-plan',run_id),{'run_id':run_id,'candidate_id':cid})
                if type(planned) is not Committed:return planned
                progress=await plans.rows.read('candidate_applications',aid)
                if progress is None:raise InvalidValue()
            outcome=await self.execute(cast(str,progress['command_kind']),await plans.original_execution(progress),{'application_id':aid})
            if type(outcome) in (NotCommitted,DomainNotCommitted) and outcome.error is not None and outcome.error.reason=='OWNERSHIP_CHANGED' and not outcome.error.cleanup_pending:
                rebuilt=await self.execute('replan_daily_candidate',self.key('daily-candidate-replan',run_id),{'run_id':run_id,'candidate_id':cid})
                if type(rebuilt) is not Committed:return rebuilt
                progress=await plans.rows.read('candidate_applications',aid)
                if progress is None:raise InvalidValue()
                return await self.execute(cast(str,progress['command_kind']),await plans.original_execution(progress),{'application_id':aid})
            return outcome
        async def advance():
            try:
                outcome=await advance_candidate()
                if type(outcome) is not NotCommitted and type(outcome) is not DomainNotCommitted:return outcome
                if outcome.error is None or outcome.error.reason not in ('REVISION_CONFLICT','OWNERSHIP_CHANGED','SCOPE_CHANGED') or outcome.error.cleanup_pending:return outcome
                reason=outcome.error.reason
            except OwnerFailure as failure:
                if failure.reason not in ('REVISION_CONFLICT','OWNERSHIP_CHANGED','SCOPE_CHANGED') or failure.cleanup_pending:raise
                reason=failure.reason
            run=await self.reasoning.rows.read('reasoning_runs',run_id)
            if run is None:raise InvalidValue()
            source=record((await self.content.read_daily_batch(cast(str,run['batch_id'])))['source'])
            base={'REVISION_CONFLICT':'reject_daily_revision','OWNERSHIP_CHANGED':'reject_daily_ownership','SCOPE_CHANGED':'reject_daily_scope'}[reason]
            kind=base+('_media' if any(record(m)['media'] for m in sequence(source['ordered_members'])) else '')
            return await self.execute(kind,self.key(base,run_id),{'run_id':run_id})
        task,logical=start_owned(advance());self._job=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._job is job:self._job=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=5)
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        return logical.result()

    async def reject_scope(self,run_id:str):
        """Confirm the original terminal or prove revoked scope in its fixed command."""
        run=await self.reasoning.rows.read('reasoning_runs',run_id)
        if run is None:raise InvalidValue()
        if run['phase']=='TERMINAL':return await self.apply(run_id)
        source=record((await self.content.read_daily_batch(cast(str,run['batch_id'])))['source'])
        kind='reject_daily_scope'+('_media' if any(record(m)['media'] for m in sequence(source['ordered_members'])) else '')
        return await self.execute(kind,self.key('reject_daily_scope',run_id),{'run_id':run_id})

    async def expire_unstarted(self,source):
        """Return its exact receipt or end an actually expired, never frozen run."""
        from companion_memory.persistence import NotFound
        kind='expire_unstarted_learning'+('_media' if any(record(m)['media'] for m in sequence(source['ordered_members'])) else '')
        key=self.key('expire-unstarted',source['run_id']);port=self.operations[kind]
        receipt=await port.read_receipt(key)
        if type(receipt) is Found:return Committed(receipt.value,'EXISTING')
        if type(receipt) is not NotFound:return receipt
        prep=await self.content.read_daily_preparation(cast(str,source['batch_id']))
        if time.time_ns()//1000<cast(int,prep['started_at_us'])+1200000000:return Found(MappingProxyType({'state':'PARKED','new_sends':0}))
        return await self.execute(kind,key,{'run_id':source['run_id'],'batch_id':source['batch_id']})

    @staticmethod
    def empty_kind(candidate,source):
        terminal={'SUCCEEDED':'empty','FAILED_DROPPED':'failure','SENSITIVE_DROPPED':'sensitive'}[candidate.manifest['terminal_proposal']]
        return 'finish_daily_'+terminal+('_media' if any(record(m)['media'] for m in sequence(source['ordered_members'])) else '')

    async def execute(self,kind,key,values):
        if self._task is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        definition=next(d for d in self.commands if d.operation_kind==kind)
        command=ResultBoundCommand(1,{'operation_id':key,**values},{r.event_slot:{'actor':'daily_cognition'} for r in definition.required_audits})
        async def write():
            port=self.operations[kind];original=await port.resolve_operation(port.recovery_handle(key,command))
            if type(original) is not NotCommitted or original.error is not None:return original
            from companion_memory.persistence.schema import freeze_value
            watched,slot=self.causes.watch(kind,freeze_value(definition.input_schema,command.values,owned=True))
            with CompletionScope() as completion:
                try:
                    outcome=await port.execute(key,command)
                    if type(outcome) is NotCommitted and slot.cause is not None:
                        cause=slot.cause
                        return DomainNotCommitted(RuntimeError(cause.code,kind,cause.field,cause.reason,cause.cleanup_pending or outcome.error is not None and outcome.error.cleanup_pending))
                    return outcome
                finally:
                    await completion.wait()
                    self.causes.release(watched)
        task,logical=start_owned(write());self._task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=5)
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        return logical.result()
    async def wait_actual(self) -> None:
        """Join only this coordinator's actual commands and held deterministic draft."""
        for task in (self._job,self._task):
            if task is not None:await asyncio.wait((task,))

    def close(self):
        self.closed=True
        return self._job is None and self._task is None and self._draft is None
