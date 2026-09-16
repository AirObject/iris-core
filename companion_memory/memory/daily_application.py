"""Memory's revisioned progress for a fixed daily candidate release plan.

The existing immutable release leaves keep full source/ingress/media closure.
This small progress root records actual plan creation and confirmed application;
it is not a second copy of the candidate, its authority, or its model output.
"""
from __future__ import annotations
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import UnitOfWork
from companion_memory.persistence.daily_records import BASE,DailyTable,DailyRows,Field,RecordSchema,ID,DIGEST,REVISION,OPERATION,IndexSpec,daily_catalog,identity,enum,Record
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from .formats import record,sequence
from .release_plans import PLAN,digest,read_plan,observe_change_set_release,CheckedRelease

SCHEMA=RecordSchema(BASE+(Field('batch_id',ID),Field('candidate_id',ID),Field('candidate_digest',DIGEST),Field('authority_digest',DIGEST),
    Field('work_generation',REVISION),Field('work_revision',REVISION),Field('plan_id',ID),Field('command_kind',ID),
    Field('state',enum('PLANNED','APPLIED')),Field('original_operation',OPERATION),Field('terminal_operation',OPERATION,nullable=True)))
TABLE=DailyTable('candidate_applications',(SCHEMA,),4096,True,(IndexSpec('by_candidate',('candidate_id',)),))

def application_catalog():
    return daily_catalog('memory',5,(TABLE,))

class DailyMemoryApplication:
    """Use the memory owner's lease and statements; acquire no parallel owner."""
    def __init__(self,memory,catalog):
        if not memory.daily_format:raise InvalidValue()
        self.memory=memory;c=memory.configuration
        self.rows=DailyRows(catalog,(TABLE,),memory.storage,c.database_id,c.scope_id,c.snapshot_id)
    def key(self,kind,*parts):
        c=self.memory.configuration
        return identity(kind,c.database_id,c.scope_id,*parts)
    def operation(self,uow):
        original=self.memory.storage.daily_operation_context(uow,self.memory.repository_definition())
        return MappingProxyType({k:getattr(original,k) for k in ('owner_namespace','operation_kind','scope_id','operation_key')})
    @staticmethod
    def branch(candidate,leaves,source):
        history=any(leaf['action'] in ('SET_SCORES','REPLACE_CURRENT') for leaf in candidate.leaves)
        goals=any(leaf['action']=='CREATE_GOAL' for leaf in candidate.leaves)
        media=any(record(m)['media'] for m in sequence(source['ordered_members'])) or any(leaf['has_media'] for leaf in leaves)
        return 'apply_daily_candidate'+('_history' if history else '')+('_media' if media else '')+('_goals' if goals else '')
    def plan(self,uow:UnitOfWork,candidate,work,source,authority_digest:str):
        m=self.memory;manifest=candidate.manifest
        aid=self.key('daily-application',manifest['candidate_id']);pid=self.key('daily-release',aid)
        if self.rows.get(TABLE.name,uow,aid) is not None:raise OwnerFailure('PRECONDITION_FAILED','candidate','NO_CHANGE')
        leaves=observe_change_set_release(m,uow,tuple(leaf for leaf in candidate.leaves if leaf['action']!='CREATE_GOAL'))
        kind=self.branch(candidate,leaves,source)
        from companion_memory.persistence.schema import freeze_value
        plan=record(freeze_value(PLAN,{'plan_version':1,'plan_id':pid,'root_id':aid,'ordinal':1,
            'semantic_digest':manifest['manifest_digest'],'mask':('INGRESS_MEDIA' if any(leaf['has_media'] for leaf in leaves) else 'INGRESS') if any(leaf['payloads'] for leaf in leaves) else 'MEDIA' if any(leaf['has_media'] for leaf in leaves) else 'NONE',
            'command_kind':kind,'execution_key':self.key('daily-apply',aid),'previous_plan':None,'leaf_digests':tuple(digest(leaf,8192) for leaf in leaves)}))
        m.rows.stage('release_plans_insert',uow,{'plan_id':pid,'root_id':aid,'ordinal':1,'execution_key':plan['execution_key'],'command_kind':kind,'body':encode_content(plan,4096).decode()})
        for ordinal,leaf in enumerate(leaves):m.rows.stage('release_leaves_insert',uow,{'plan_id':pid,'ordinal':ordinal,'body':encode_content(leaf,8192).decode()})
        now=time.time_ns()//1000;c=m.configuration
        return self.rows.write(TABLE.name,uow,{'format_version':1,'object_id':aid,'revision':1,'database_id':c.database_id,'instance_id':c.scope_id,
            'config_snapshot_id':c.snapshot_id,'created_at_us':now,'updated_at_us':now,'batch_id':manifest['batch_id'],'candidate_id':manifest['candidate_id'],
            'candidate_digest':manifest['manifest_digest'],'authority_digest':authority_digest,'work_generation':work['generation'],'work_revision':work['revision'],
            'plan_id':pid,'command_kind':kind,'state':'PLANNED','original_operation':self.operation(uow),'terminal_operation':None})
    def participate(self,uow:UnitOfWork,application_id:str,candidate,work,source,authority_digest:str,command_kind:str):
        m=self.memory;progress=self.rows.get(TABLE.name,uow,application_id)
        if (progress is None or progress['state']!='PLANNED' or progress['candidate_id']!=candidate.manifest['candidate_id']
                or progress['candidate_digest']!=candidate.manifest['manifest_digest'] or progress['authority_digest']!=authority_digest
                or progress['work_generation']!=work['generation'] or progress['work_revision']!=work['revision']):
            raise OwnerFailure('PRECONDITION_FAILED','candidate','WORK_FENCED')
        plan,leaves=read_plan(m,uow,cast(str,progress['plan_id']))
        if plan['root_id']!=application_id or plan['semantic_digest']!=progress['candidate_digest'] or command_kind!=progress['command_kind'] or command_kind!=self.branch(candidate,leaves,source):raise InvalidValue()
        if observe_change_set_release(m,uow,tuple(leaf for leaf in candidate.leaves if leaf['action']!='CREATE_GOAL'))!=leaves:
            raise OwnerFailure('PRECONDITION_FAILED','source','OWNERSHIP_CHANGED')
        operation=m.storage.daily_operation_context(uow,m.repository_definition())
        if operation.operation_key!=plan['execution_key']:raise InvalidValue()
        return progress,CheckedRelease(m,m.sources,leaves)
    async def original_execution(self,progress):
        """Return only the stored original execution identity, including its replan."""
        rows=await self.memory.rows.read('release_plans_get',{'plan_id':progress['plan_id']})
        if len(rows)!=1:raise InvalidValue()
        from companion_memory.persistence.content_codec import decode_content
        from companion_memory.persistence.schema import freeze_value
        plan=record(freeze_value(PLAN,decode_content(cast(str,rows[0]['body']).encode(),4096),owned=True))
        if plan['root_id']!=progress['object_id'] or plan['command_kind']!=progress['command_kind']:raise InvalidValue()
        return cast(str,plan['execution_key'])

    def replan(self,uow:UnitOfWork,candidate,work,source,authority_digest:str,definitions):
        """Rebuild one changed holder closure after original application is absent."""
        m=self.memory;aid=self.key('daily-application',candidate.manifest['candidate_id'])
        progress=self.rows.get(TABLE.name,uow,aid)
        if (progress is None or progress['state']!='PLANNED' or progress['candidate_digest']!=candidate.manifest['manifest_digest']
                or progress['authority_digest']!=authority_digest or progress['work_generation']!=work['generation'] or progress['work_revision']!=work['revision']):
            raise OwnerFailure('PRECONDITION_FAILED','candidate','WORK_FENCED')
        old,old_leaves=read_plan(m,uow,cast(str,progress['plan_id']))
        definition=next(d for d in definitions if d.operation_kind==old['command_kind'])
        if m.storage.confirm_prior_operation(uow,definition,cast(str,old['execution_key'])) is not None:
            raise OwnerFailure('PRECONDITION_FAILED','candidate','ALREADY_COMMITTED')
        if old['ordinal']!=1:raise OwnerFailure('PRECONDITION_FAILED','source','OWNERSHIP_CHANGED')
        leaves=observe_change_set_release(m,uow,tuple(leaf for leaf in candidate.leaves if leaf['action']!='CREATE_GOAL'))
        if leaves==old_leaves:raise OwnerFailure('PRECONDITION_FAILED','source','NO_CHANGE')
        kind=self.branch(candidate,leaves,source);pid=self.key('daily-release-rebuilt',aid)
        from companion_memory.persistence.schema import freeze_value
        plan=record(freeze_value(PLAN,{'plan_version':1,'plan_id':pid,'root_id':aid,'ordinal':2,
            'semantic_digest':candidate.manifest['manifest_digest'],'mask':('INGRESS_MEDIA' if any(leaf['has_media'] for leaf in leaves) else 'INGRESS') if any(leaf['payloads'] for leaf in leaves) else 'MEDIA' if any(leaf['has_media'] for leaf in leaves) else 'NONE',
            'command_kind':kind,'execution_key':self.key('daily-apply-rebuilt',aid),'previous_plan':old['plan_id'],'leaf_digests':tuple(digest(leaf,8192) for leaf in leaves)}))
        m.rows.stage('release_plans_insert',uow,{'plan_id':pid,'root_id':aid,'ordinal':2,'execution_key':plan['execution_key'],'command_kind':kind,'body':encode_content(plan,4096).decode()})
        for ordinal,leaf in enumerate(leaves):m.rows.stage('release_leaves_insert',uow,{'plan_id':pid,'ordinal':ordinal,'body':encode_content(leaf,8192).decode()})
        return self.rows.write(TABLE.name,uow,dict(progress)|{'revision':cast(int,progress['revision'])+1,'updated_at_us':time.time_ns()//1000,
            'plan_id':pid,'command_kind':kind},cast(int,progress['revision']))
    def applied(self,uow:UnitOfWork,progress:Record):
        return self.rows.write(TABLE.name,uow,dict(progress)|{'revision':cast(int,progress['revision'])+1,'updated_at_us':time.time_ns()//1000,
            'state':'APPLIED','terminal_operation':self.operation(uow)},cast(int,progress['revision']))

    def verify_exhausted_replan(self,uow:UnitOfWork,candidate,work,source,authority_digest:str,definitions):
        """Prove both original applications absent and the single rebuild stale."""
        aid=self.key('daily-application',candidate.manifest['candidate_id'])
        progress=self.rows.get(TABLE.name,uow,aid)
        if (progress is None or progress['state']!='PLANNED' or progress['revision']!=2
                or progress['candidate_digest']!=candidate.manifest['manifest_digest']
                or progress['authority_digest']!=authority_digest
                or progress['work_generation']!=work['generation'] or progress['work_revision']!=work['revision']):
            raise OwnerFailure('PRECONDITION_FAILED','candidate','WORK_FENCED')
        current,leaves=read_plan(self.memory,uow,cast(str,progress['plan_id']))
        if current['ordinal']!=2 or current['previous_plan'] is None:raise InvalidValue()
        previous,_=read_plan(self.memory,uow,cast(str,current['previous_plan']))
        if previous['ordinal']!=1:raise InvalidValue()
        for plan in (previous,current):
            if plan['root_id']!=aid or plan['semantic_digest']!=progress['candidate_digest']:raise InvalidValue()
            definition=next(d for d in definitions if d.operation_kind==plan['command_kind'])
            if self.memory.storage.confirm_prior_operation(uow,definition,cast(str,plan['execution_key'])) is not None:
                raise OwnerFailure('PRECONDITION_FAILED','candidate','ALREADY_COMMITTED')
        actual=observe_change_set_release(self.memory,uow,tuple(leaf for leaf in candidate.leaves if leaf['action']!='CREATE_GOAL'))
        if actual==leaves:raise OwnerFailure('PRECONDITION_FAILED','source','NO_CHANGE')
