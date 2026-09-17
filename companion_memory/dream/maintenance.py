"""Native memory time settlement and its dream step share one atomic receipt."""
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import ResultBoundCommandDefinition,UnitOfWork,Field,RecordSchema,SequenceSchema
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.semantic_records import ID,P,N,Record,fields
from companion_memory.persistence.daily_records import identity
from companion_memory.persistence.daily_results import FACT,TARGETS
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence import AuditResultBinding,AuditFieldBinding
from companion_memory.runtime.content_assembly import owner_fact
from companion_memory.memory.transactions import MemoryTransactions
from .control import DreamControl
from .records import validate_run
from .results import INTENT,audits,result_schema,target


class DreamMaintenance:
    """Only a current native run grant can settle a current native memory object."""
    def __init__(self,control:DreamControl,participants:tuple):
        self.control=control;self.memory:MemoryTransactions|None=None
        definitions=[]
        for kind,owners in (('observe_dream_retention',('dream',)),('account_dream_time',('dream','memory')),
                            ('decay_dream_memory',('dream','memory','logging_service'))):
            requirements,bindings=audits(kind,tuple(o for o in owners if o!='logging_service'))
            shape=result_schema(tuple(o for o in owners if o!='logging_service'),('APPLIED','UNCHANGED'))
            if 'logging_service' in owners:
                requirement=AuditRequirement('logging_service','object_history',kind.upper(),1,('APPLY',),owner_fact('logging_service'),target_limit=16)
                requirements+=(requirement,)
                bindings+=(AuditResultBinding('object_history',1,(AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),
                    AuditFieldBinding('actor_ref','INTENT',('actor',)),AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),
                    AuditFieldBinding('target_refs','RESULT',('history_targets',)),AuditFieldBinding('change','RESULT',('history_fact',)))),)
                shape=RecordSchema(shape.fields+(Field('history',SequenceSchema(ID,1,1)),Field('history_targets',TARGETS),Field('history_fact',owner_fact('logging_service'))))
            def handle(uow:UnitOfWork,values:Record,operation=kind):
                try:return self.handle(operation,uow,values)
                except OwnerFailure as failure:
                    control.causes.record(operation,values,failure)
                    raise
            definitions.append(ResultBoundCommandDefinition('dream',kind,1,RecordSchema(fields(operation_id=ID,run_id=ID,
                expected_revision=P,mode_epoch=N,memory_id=ID,memory_revision=P,observed_at_us=N)),1,shape,
                participants,requirements,handle,INTENT,bindings))
        self.commands=tuple(definitions)

    def bind(self,memory:MemoryTransactions):
        if self.memory is not None or memory.long_term is None:raise InvalidValue()
        self.memory=memory

    def handle(self,kind:str,uow:UnitOfWork,v:Record):
        control=self.control;memory=self.memory
        if memory is None or memory.long_term is None:raise InvalidValue()
        run=control.require_dispatch(uow,cast(str,v['run_id']),cast(int,v['expected_revision']),cast(int,v['mode_epoch']))
        if not control.settled(run):raise OwnerFailure('RESOURCE_BUSY','run','RUN_ACTIVE')
        now=control._ready(uow)
        if cast(int,v['observed_at_us'])>now or now>=cast(int,run['deadline_at_us']):
            raise OwnerFailure('TIMEOUT','run','DEADLINE_EXCEEDED')
        limits=control.execution_configuration().text.record('dream.resources')
        if cast(int,run['objects_used'])>=cast(int,limits['objects_per_run']):raise OwnerFailure('RESOURCE_BUSY','run','CAPACITY_REACHED')
        oid=cast(str,v['memory_id']);revision=cast(int,v['memory_revision']);run_id=cast(str,run['run_id'])
        current,anchor,elapsed=memory.long_term.preview(uow,oid,revision,run_id,cast(int,v['observed_at_us']))
        from companion_memory.memory.formats import record
        expected='observe_dream_retention' if elapsed.applied_intervals==0 else 'account_dream_time' if elapsed.retention==record(current['scores'])['retention'] else 'decay_dream_memory'
        if kind!=expected:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        applied=None
        if elapsed.applied_intervals:
            _,_,elapsed,applied=memory.long_term.settle(uow,oid,revision,run_id,cast(int,v['observed_at_us']),cast(str,v['operation_id']))
        original=control._operation(uow);ordinal=cast(int,run['steps_completed']);sid=identity('dream-step',control.configuration.database_id,control.configuration.scope_id,run_id,ordinal)
        state='APPLIED' if elapsed.applied_intervals else 'UNCHANGED'
        proof=sha256(encode_content(MappingProxyType({'run_id':run_id,'revision':run['revision'],'epoch':run['mode_epoch'],'memory_id':oid,'revision_read':revision}),4096)).hexdigest()
        control.rows.write('steps',uow,control._base(sid,now)|{'run_id':run_id,'ordinal':ordinal,'kind':'TIME_DECAY','state':state,
            'mode_epoch':run['mode_epoch'],'permit_digest':proof,'origin_event_id':None,'cause_root':v['operation_id'],
            'object_ref':{'object_id':oid,'revision':revision},'accounted_from':anchor['accounted_until'],'accounted_through':elapsed.accounted_until,
            'material_id':None,'material_digest':None,'candidate_id':None,'request_key':None,'request_id':None,'handoff_id':None,
            'execution_key':v['operation_id'],'plan_id':None,'local_confirmation':'CONFIRMED','remote_result':'NONE','cleanup_pending':False,
            'deadline_at_us':min(cast(int,run['deadline_at_us']),now+cast(int,limits['step_timeout_ms'])*1000),
            'result_digest':sha256(encode_content(MappingProxyType({'retention':elapsed.retention,'accounted_until':elapsed.accounted_until,'remaining_intervals':elapsed.remaining_intervals}),4096)).hexdigest(),
            'original_operation':original,'last_operation':original})
        oldrev=cast(int,run['revision'])
        control.rows.write('runs',uow,validate_run(dict(run)|{'revision':oldrev+1,'updated_at_us':max(now,cast(int,run['updated_at_us'])),
            'steps_completed':ordinal+1,'objects_used':cast(int,run['objects_used'])+1,'object_cursor':oid,
            'coverage':'PARTIAL','remaining_work':True,'end_reason':'CLOCK_REGRESSED' if elapsed.clock_regressed else run['end_reason'],'last_operation':original}),oldrev)
        counts=control.storage.transaction_row_changes(uow)
        facts:dict[str,object]={'dream':{'rows_changed':counts['dream'],'targets':(target(sid,1),target(run_id,oldrev+1,oldrev))}}
        if elapsed.applied_intervals:
            newanchor=memory.long_term.rows.get('maintenance_anchors',uow,memory.long_term.anchor_id(oid))
            if newanchor is None:raise InvalidValue()
            targets=[target(cast(str,newanchor['object_id']),cast(int,newanchor['revision']),cast(int,anchor['revision']))]
            if applied is not None:targets.append(target(oid,revision+1,revision))
            facts['memory']={'rows_changed':counts['memory'],'targets':tuple(targets)}
        from .results import result
        value:dict[str,object]=dict(result(cast(str,v['operation_id']),state,facts))
        if applied is not None:
            history=applied.history
            value.update(history=tuple(h['history_id'] for h in history),history_targets=tuple({'object_id':h['object_id'],'previous_revision':h['previous_revision'],'revision':revision+1} for h in history),
                history_fact={'rows_changed':counts['logging_service'],'state':'APPLIED','references':(),
                    'counts':({'name':'history_items','count':len(history)},),'history_ids':tuple(h['history_id'] for h in history)})
        return value
