"""One native page of actual dependency edges, atomically with dream progress."""
from hashlib import sha256
from companion_memory.persistence import ResultBoundCommandDefinition,RecordSchema
from companion_memory.persistence.semantic_records import ID,P,N,fields
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from .results import audits,result_schema,result,target,INTENT
from .records import validate_run

SCAN='scan_dream_influence'
SKIP='settle_obsolete_dream_influence'


class DreamInfluence:
    def __init__(self,control,maintenance,participants):
        self.control=control;self.maintenance=maintenance
        requirements,bindings=audits(SCAN,('memory','dream'))
        self.commands=(ResultBoundCommandDefinition('dream',SCAN,1,RecordSchema(fields(operation_id=ID,run_id=ID,
            expected_revision=P,mode_epoch=N,event_id=ID,event_sequence=P)),1,result_schema(('memory','dream'),('APPLIED',)),
            participants,requirements,self.handle,INTENT,bindings),)
        requirements,bindings=audits(SKIP,('memory','dream'))
        self.commands+=(ResultBoundCommandDefinition('dream',SKIP,1,RecordSchema(fields(operation_id=ID,run_id=ID,expected_revision=P,mode_epoch=N,influence_id=ID)),
            1,result_schema(('memory','dream'),('UNCHANGED',)),participants,requirements,self.skip,INTENT,bindings),)

    def handle(self,uow,v):
        c=self.control;memory=self.maintenance.memory
        if memory is None or memory.long_term is None:raise InvalidValue()
        run=c.require_dispatch(uow,v['run_id'],v['expected_revision'],v['mode_epoch']);now=c._ready(uow)
        if not c.settled(run):raise OwnerFailure('RESOURCE_BUSY','run','RUN_ACTIVE')
        if now>=run['deadline_at_us']:raise OwnerFailure('TIMEOUT','run','DEADLINE_EXCEEDED')
        limits=c.configuration.candidate.text.record('dream.resources')
        if run['edges_used']+4>limits['dependency_edges_per_run']:raise OwnerFailure('RESOURCE_BUSY','run','CAPACITY_REACHED')
        influence=memory.long_term.influence
        raw=influence.rows.rows.stage('influence_next',uow,{'after':run['impact_cursor']})
        if not raw or raw[0]['object_id']!=v['event_id']:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        event,walk,work,count=influence.enumerate(uow,v['event_id'],v['event_sequence'],now)
        sid=influence.key('dream-step',run['run_id'],run['steps_completed']);op=c._operation(uow)
        c.rows.write('steps',uow,c._base(sid,now)|{'run_id':run['run_id'],'ordinal':run['steps_completed'],'kind':'SOURCE_IMPACT','state':'APPLIED',
            'mode_epoch':run['mode_epoch'],'permit_digest':sha256(encode_content(v,8192)).hexdigest(),'origin_event_id':event['object_id'],'cause_root':event['cause_root'],
            'object_ref':None,'accounted_from':None,'accounted_through':None,'material_id':None,'material_digest':None,'candidate_id':None,
            'request_key':None,'request_id':None,'handoff_id':None,'execution_key':v['operation_id'],'plan_id':None,'local_confirmation':'CONFIRMED',
            'remote_result':'NONE','cleanup_pending':False,'deadline_at_us':run['deadline_at_us'],'result_digest':None,'original_operation':op,'last_operation':op})
        c.rows.write('runs',uow,validate_run(dict(run)|{'revision':run['revision']+1,'updated_at_us':now,'steps_completed':run['steps_completed']+1,
            'edges_used':run['edges_used']+max(1,count),'impact_cursor':event['sequence'] if walk['complete'] else run['impact_cursor'],
            'coverage':'PARTIAL','remaining_work':True,'last_operation':op}),run['revision'])
        counts=c.storage.transaction_row_changes(uow)
        return result(v['operation_id'],'APPLIED',{'memory':{'rows_changed':counts['memory'],
            'targets':tuple(target(item['object_id'],item['revision'],item['revision']-1 if item['revision']>1 else None) for item in (walk,*work))},
            'dream':{'rows_changed':counts['dream'],'targets':(target(sid,1),target(run['run_id'],run['revision']+1,run['revision']))}})

    def skip(self,uow,v):
        c=self.control;memory=self.maintenance.memory
        if memory is None or memory.long_term is None:raise InvalidValue()
        run=c.require_dispatch(uow,v['run_id'],v['expected_revision'],v['mode_epoch']);now=c._ready(uow)
        if not c.settled(run):raise OwnerFailure('RESOURCE_BUSY','run','RUN_ACTIVE')
        if now>=run['deadline_at_us']:raise OwnerFailure('TIMEOUT','run','DEADLINE_EXCEEDED')
        if run['edges_used']>=c.configuration.candidate.text.record('dream.resources')['dependency_edges_per_run']:
            raise OwnerFailure('RESOURCE_BUSY','run','CAPACITY_REACHED')
        queue=memory.long_term.influence;work=queue.rows.get('influence_work',uow,v['influence_id'])
        if work is None:raise InvalidValue()
        reason=queue.obsolete(uow,work)
        if reason is None:raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
        sid=queue.key('dream-step',run['run_id'],run['steps_completed']);op=c._operation(uow)
        updated=queue.claim(uow,work['object_id'],run['run_id'],sid,now,'UNCHANGED',reason)
        c.rows.write('steps',uow,c._base(sid,now)|{'run_id':run['run_id'],'ordinal':run['steps_completed'],'kind':'SOURCE_IMPACT','state':'UNCHANGED',
            'mode_epoch':run['mode_epoch'],'permit_digest':sha256(encode_content(v,8192)).hexdigest(),'origin_event_id':work['event_id'],'cause_root':work['cause_root'],
            'object_ref':{'object_id':work['affected_object_id'],'revision':work['observed_revision']},'accounted_from':None,'accounted_through':None,
            'material_id':None,'material_digest':None,'candidate_id':None,'request_key':None,'request_id':None,'handoff_id':None,
            'execution_key':v['operation_id'],'plan_id':None,'local_confirmation':'CONFIRMED','remote_result':'NONE','cleanup_pending':False,
            'deadline_at_us':run['deadline_at_us'],'result_digest':None,'original_operation':op,'last_operation':op})
        c.rows.write('runs',uow,validate_run(dict(run)|{'revision':run['revision']+1,'updated_at_us':max(now,run['updated_at_us']),
            'steps_completed':run['steps_completed']+1,'edges_used':run['edges_used']+1,'last_operation':op}),run['revision'])
        counts=c.storage.transaction_row_changes(uow)
        return result(v['operation_id'],'UNCHANGED',{'memory':{'rows_changed':counts['memory'],'targets':(target(updated['object_id'],updated['revision'],work['revision']),)},
            'dream':{'rows_changed':counts['dream'],'targets':(target(sid,1),target(run['run_id'],run['revision']+1,run['revision']))}})
