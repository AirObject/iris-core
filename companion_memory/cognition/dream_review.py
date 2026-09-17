"""Native dream review work, original Provider consumption and formal application."""
from hashlib import sha256
from types import MappingProxyType
from typing import cast
import asyncio
from companion_memory.provider.daily_service import DailyResult
from companion_memory.persistence import ResultBoundCommandDefinition,UnitOfWork,Found,Field,RecordSchema,SequenceSchema
from companion_memory.persistence.daily_records import DailyRows
from companion_memory.persistence.semantic_records import ID,P,N,H,Record,fields,enum
from companion_memory.persistence.schema import InvalidValue,Value,ValueTooLarge
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.owned_statements import OwnerFailure,BoundStatements
from companion_memory.persistence.text_records import decode_row
from companion_memory.dream.results import audits,result_schema,result,target,INTENT
from companion_memory.dream.records import validate_run
from companion_memory.memory.formats import record,sequence
from companion_memory.provider.values import freeze
from .dream_records import TABLES,WORK
from .dream_candidates import DreamCandidate,build
from .dream_material import material,collect
from .daily_material import freeze_material

PREPARE='prepare_dream_review'
DEFER='defer_dream_review'
STORE='store_dream_review_result'
PLAN='plan_dream_candidate'
DISPOSE='finish_dream_review'
RETIRE='retire_dream_material'
FAIL='record_dream_review_failure'
DISCARD='discard_dream_review'
IMPACT_PREPARE='prepare_dream_influence'
IMPACT_DEFER='defer_dream_influence'
IMPACT_DISPOSE='finish_dream_influence'
IMPACT_DISCARD='discard_dream_influence'


class DreamReview:
    def __init__(self,control,catalog,materials,participants):
        self.control=control;self.catalog=catalog;self.materials=materials;self.bound=False;self.closed=False
        self.receiving:DailyResult|None=None;self.sending:tuple[Record,Record]|None=None;self.job:asyncio.Task|None=None;self.scopes={}
        common=fields(run_id=ID,expected_revision=P,mode_epoch=N)
        layouts=[(IMPACT_PREPARE,('cognition','dream','memory'),fields(object_id=ID,object_revision=P,influence_id=ID)),
            (IMPACT_DEFER,('dream','memory'),fields(object_id=ID,object_revision=P,influence_id=ID)),
            (IMPACT_DISPOSE,('dream','memory'),fields(work_id=ID)),
            (IMPACT_DISCARD,('dream','memory'),fields(work_id=ID,reason=enum('DEADLINE_EXCEEDED','SOURCE_CHANGED','ABORTED_SAFELY'))),
            (PREPARE,('cognition','dream'),fields(object_id=ID,object_revision=P)),
            (DEFER,('dream',),fields(object_id=ID,object_revision=P)),
            (STORE,('cognition','dream'),fields(work_id=ID,request_id=ID,request_digest=H)),
            (PLAN,('memory','dream'),fields(work_id=ID,previous_plan=(ID,))),
            (DISPOSE,('dream',),fields(work_id=ID)),
            (FAIL,('cognition','dream'),fields(work_id=ID,request_id=ID,request_digest=H)),
            (DISCARD,('dream',),fields(work_id=ID,reason=enum('DEADLINE_EXCEEDED','SOURCE_CHANGED','ABORTED_SAFELY'))),
            (RETIRE,('cognition',),fields(work_id=ID,context_id=ID,page_offset=N))]
        for history in (False,True):
            for ingress in (False,True):
                for media in (False,True):
                    for goals in (False,True):
                        name='apply_dream_candidate_memory'+('_history' if history else '')+('_ingress' if ingress else '')+('_media' if media else '')+('_goals' if goals else '')
                        owners=('dream','memory')+(('logging_service',) if history else ())+(('ingress',) if ingress else ())+(('media',) if media else ())+(('goals',) if goals else ())
                        layouts.append((name,owners,fields(work_id=ID,plan_id=ID)))
        layouts.append(('apply_dream_candidate_goals',('dream','goals'),fields(work_id=ID)))
        definitions=[]
        for kind,owners,parameters in layouts:
            native=tuple(owner for owner in owners if owner!='logging_service')
            requirements,bindings=audits(kind,native);shape=result_schema(native,('FROZEN','RESULT_STORED','FAILED','PLANNED','APPLIED','UNCHANGED','DEFERRED_CAPACITY','DEFERRED_CONFLICT','RECOVERY_REQUIRED','MATERIAL_RELEASED'))
            if 'logging_service' in owners:
                from companion_memory.logging_service import AuditRequirement
                from companion_memory.persistence import AuditResultBinding,AuditFieldBinding
                from companion_memory.runtime.content_assembly import owner_fact
                from companion_memory.persistence.daily_results import TARGETS
                requirements+=(AuditRequirement('logging_service','object_history',kind.upper(),1,('APPLY',),owner_fact('logging_service'),target_limit=16),)
                bindings+=(AuditResultBinding('object_history',1,(AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),
                    AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),AuditFieldBinding('target_refs','RESULT',('history_targets',)),AuditFieldBinding('change','RESULT',('history_fact',)))),)
                shape=RecordSchema(shape.fields+(Field('history',SequenceSchema(ID,1,8)),Field('history_targets',TARGETS),Field('history_fact',owner_fact('logging_service'))))
            if kind==STORE:shape=RecordSchema(shape.fields+(Field('consumer_digest',H),))
            def handle(uow,values,operation=kind):
                try:return self.handle(operation,uow,values)
                except ValueTooLarge:
                    if operation not in (PREPARE,IMPACT_PREPARE):raise
                    failure=OwnerFailure('RESOURCE_BUSY','material','CAPACITY_REACHED')
                    self.control.causes.record(operation,values,failure);raise failure from None
                except OwnerFailure as failure:self.control.causes.record(operation,values,failure);raise
            definitions.append(ResultBoundCommandDefinition('dream' if kind==STORE else 'cognition',kind,1,RecordSchema(fields(operation_id=ID)+common+parameters),1,shape,participants,requirements,handle,INTENT,bindings))
        self.commands=tuple(definitions)

    def bind(self,periodic,goals,scopes):
        if self.bound:raise InvalidValue()
        self.current=periodic.current;self.memory=periodic.memory;self.provider=periodic.provider;self.configuration=periodic.configuration;self.storage=periodic.storage
        self.goals=goals;self.scopes=dict(scopes);self.rows=DailyRows(self.catalog,TABLES,self.storage,self.configuration.database_id,self.configuration.scope_id,self.configuration.snapshot_id)
        self.views=BoundStatements(self.catalog,self.storage,'provider');self.bound=True
    def key(self,kind,*parts):
        from companion_memory.persistence.daily_records import identity
        return identity(kind,self.configuration.database_id,self.configuration.scope_id,*parts)
    def base(self,key,now):return self.control._base(key,now)
    def operation(self,uow):
        operation=self.storage.cognition_operation_context(uow,self.catalog.definition)
        return MappingProxyType({k:getattr(operation,k) for k in ('owner_namespace','operation_kind','scope_id','operation_key')})
    def work_id(self,step_id):return self.key('dream-review',step_id)
    def frozen(self,uow,work):
        stored=self.materials.participate_material(uow,work['material_id'],work['material_digest'],work['run_id'])
        return cast(Record,freeze(decode_content(stored.body,262144),262144,owned=True)),stored
    def candidate(self,uow,work):
        body,_=self.frozen(uow,work)
        stored=self.materials.participate_material(uow,work['result_material_id'],work['result_material_digest'],work['run_id'])
        result=record(cast(Value,freeze(decode_content(stored.body,262144),262144,owned=True)))
        output=record(result['terminal'])['output']
        candidate=build(self.configuration,work['run_id'],work['step_id'],work['request_id'],work['handoff_id'],work['material_digest'],
            encode_content(output,24576),cast(tuple[Record,...],body['evidence']),cast(tuple[Record,...],body['subjects']),cast(tuple[str,...],body['routes']),
            cast(int,body['created_at_us']),writable=(cast(str,body['object_id']),),influence=record(body['influence']) if 'influence' in body else None)
        if candidate.digest!=work['candidate_digest'] or candidate.candidate_id!=work['candidate_id']:raise InvalidValue()
        return candidate,body

    def handle(self,kind,uow:UnitOfWork,v):
        if not self.bound or self.closed:raise InvalidValue()
        c=self.control;run=(c.participate_completion if kind in (STORE,FAIL) else c.participate_run)(uow,v['run_id'],v['expected_revision'],v['mode_epoch']);now=c._ready(uow);op=self.operation(uow)
        if kind in (DEFER,IMPACT_DEFER):return self.defer(uow,v,run,now,op)
        if kind in (PREPARE,IMPACT_PREPARE):return self.prepare(uow,v,run,now,op)
        work=self.rows.get('dream_work',uow,v['work_id'])
        if work is None or work['run_id']!=run['run_id']:raise InvalidValue()
        step=c.rows.get('steps',uow,work['step_id'])
        if step is None:raise InvalidValue()
        if kind==RETIRE:
            if step['state'] not in ('APPLIED','UNCHANGED','DEFERRED_CAPACITY','DEFERRED_CONFLICT','FAILED') or v['context_id'] not in (work['material_id'],work['result_material_id']):raise InvalidValue()
            if not self.provider.generation_consumers_ended(run['run_id']):raise OwnerFailure('RESOURCE_BUSY','material','CLEANUP_PENDING',True)
            fact=self.materials.retire_page(uow,v['context_id'],run['run_id'],v['page_offset'])
            return result(v['operation_id'],'MATERIAL_RELEASED',{'cognition':fact})
        if run['active_step_id']!=step['object_id']:raise InvalidValue()
        if kind in (STORE,FAIL) and v['expected_revision']!=work['bound_revision']:raise InvalidValue()
        if kind==STORE:return self.store(uow,v,run,work,step,now,op)
        if kind==FAIL:return self.failure(uow,v,run,work,step,now,op)
        if kind in (DISCARD,IMPACT_DISCARD):
            self.provider.require_dream_exit(uow)
            if work['state']=='RECOVERY_REQUIRED':raise InvalidValue()
            if work['request_id'] is None and not self.provider.participate_unsent_daily(uow,work['request_key'],run['run_id']):raise OwnerFailure('RESULT_UNCONFIRMED','request','COMMIT_UNCONFIRMED',True)
            if v['reason']=='DEADLINE_EXCEEDED':
                if now<work['deadline_at_us']:raise InvalidValue()
            elif v['reason']=='ABORTED_SAFELY':
                if run['end_reason']!='ABORTED_SAFELY' or self.control.dispatch_enabled:raise InvalidValue()
            else:
                body,_=self.frozen(uow,work)
                try:self.recheck(uow,work,body)
                except OwnerFailure as failure:
                    if failure.code!='PRECONDITION_FAILED':raise
                else:raise InvalidValue()
            state='FAILED' if v['reason'] in ('DEADLINE_EXCEEDED','ABORTED_SAFELY') else 'DEFERRED_CONFLICT'
            targets=self.finish(uow,run,step,state,now,op)
            return result(v['operation_id'],state,{'dream':{'rows_changed':self.storage.transaction_row_changes(uow)['dream'],'targets':targets},**self.impact_fact(uow,step)})
        if kind in (DISPOSE,IMPACT_DISPOSE):
            self.provider.require_dream_exit(uow)
            if work['state']=='FAILED':state='DEFERRED_CAPACITY' if work['reason']=='CAPACITY_REACHED' else 'FAILED'
            elif work['state']=='RESULT_STORED' and (work['decision'] in ('KEEP','DEFER') or work['leaf_count']==0):
                if work['decision']!='DEFER':
                    candidate,body=self.candidate(uow,work)
                    if candidate.leaves:raise InvalidValue()
                    self.recheck(uow,work,body)
                state='DEFERRED_CAPACITY' if work['decision']=='DEFER' else 'UNCHANGED'
            else:raise InvalidValue()
            targets=self.finish(uow,run,step,state,now,op)
            return result(v['operation_id'],state,{'dream':{'rows_changed':self.storage.transaction_row_changes(uow)['dream'],'targets':targets},**self.impact_fact(uow,step)})
        c.require_dispatch(uow,run['run_id'],run['revision'],run['mode_epoch'])
        if now>=work['deadline_at_us'] or work['state']!='RESULT_STORED':raise OwnerFailure('TIMEOUT','run','DEADLINE_EXCEEDED')
        candidate,body=self.candidate(uow,work)
        selected,subjects,entry_id,routes=self.recheck(uow,work,body)
        from companion_memory.memory.dream_application import publish_plan,apply
        if kind==PLAN:
            plan=publish_plan(self.memory,uow,candidate,previous_id=v['previous_plan'],definitions=self.commands)
            c.rows.write('steps',uow,dict(step)|{'revision':step['revision']+1,'updated_at_us':now,'plan_id':plan['plan_id'],'execution_key':plan['execution_key'],'last_operation':op},step['revision'])
            return result(v['operation_id'],'PLANNED',{'memory':{'rows_changed':self.storage.transaction_row_changes(uow)['memory'],'targets':(target(cast(str,plan['plan_id']),1),)},
                'dream':{'rows_changed':self.storage.transaction_row_changes(uow)['dream'],'targets':(target(step['object_id'],step['revision']+1,step['revision']),)}})
        if not kind.startswith('apply_dream_candidate'):raise InvalidValue()
        from companion_memory.memory.transactions import ApplyScope
        readable=frozenset(cast(str,record(item['object'])['object_id']) for item in selected)
        source_ids=frozenset(cast(str,record(s)['source_id']) for item in selected for s in sequence(item['sources']))
        scope=ApplyScope(self.configuration.scope_id,candidate.candidate_id,None,readable,frozenset(s['subject_id'] for s in subjects),
            frozenset(cast(str,leaf['target_id']) for leaf in candidate.leaves),source_ids)
        applied,release,goals=apply(self.memory,self.goals,uow,candidate,scope,entry_id,routes,now,v.get('plan_id'),kind,impact=step if step['origin_event_id'] is not None else None)
        targets=self.finish(uow,run,step,'APPLIED',now,op);counts=self.storage.transaction_row_changes(uow)
        facts:dict[str,object]={'dream':{'rows_changed':counts['dream'],'targets':targets}}
        if applied is not None:facts['memory']={'rows_changed':counts['memory'],'targets':tuple(target(item['object_id'],item['revision'],item['previous_revision']) for item in applied.objects)}
        if goals:facts['goals']={'rows_changed':counts['goals'],'targets':tuple(target(item['object_id'],item['revision']) for item in goals)}
        if release is not None:
            for owner,refs in release.audit_targets(uow).items():facts[owner]={'rows_changed':counts[owner],'targets':refs}
        value:dict[str,object]=dict(result(v['operation_id'],'APPLIED',facts))
        if applied is not None and applied.history:
            value.update(history=tuple(h['history_id'] for h in applied.history),history_targets=tuple(target(h['object_id'],h['previous_revision']+1,h['previous_revision']) for h in applied.history),
                history_fact={'rows_changed':counts['logging_service'],'state':'APPLIED','references':(),'counts':({'name':'history_items','count':len(applied.history)},),
                    'history_ids':tuple(h['history_id'] for h in applied.history)})
        return value

    def defer(self,uow,v,run,now,op):
        """Persist only a reproduced complete-material capacity or authority failure."""
        c=self.control;c.require_dispatch(uow,run['run_id'],run['revision'],run['mode_epoch'])
        if not c.settled(run):raise OwnerFailure('RESOURCE_BUSY','run','RUN_ACTIVE')
        sid=self.key('dream-step',run['run_id'],run['steps_completed'])
        impact=self.impact(uow,v,run)
        try:material(self,uow,run,v['object_id'],v['object_revision'],sid,op,now,impact=impact)
        except ValueTooLarge:state='DEFERRED_CAPACITY'
        except OwnerFailure as failure:
            if failure.code=='RESOURCE_BUSY' and failure.reason=='CAPACITY_REACHED':state='DEFERRED_CAPACITY'
            elif failure.code in ('PRECONDITION_FAILED','ACCESS_DENIED') and failure.reason in ('SOURCE_CHANGED','REVISION_CONFLICT','OPERATION_NOT_GRANTED'):state='DEFERRED_CONFLICT'
            else:raise
        else:raise InvalidValue()
        ref={'object_id':v['object_id'],'revision':v['object_revision']}
        step=c._base(sid,now)|{'run_id':run['run_id'],'ordinal':run['steps_completed'],'kind':'DREAM_REVIEW','state':state,
            'mode_epoch':run['mode_epoch'],'permit_digest':sha256(encode_content(v,8192)).hexdigest(),'origin_event_id':None if impact is None else impact['event_id'],'cause_root':sid if impact is None else impact['cause_root'],
            'object_ref':ref,'accounted_from':None,'accounted_through':None,'material_id':None,'material_digest':None,'candidate_id':None,
            'request_key':None,'request_id':None,'handoff_id':None,'execution_key':v['operation_id'],'plan_id':None,
            'local_confirmation':'CONFIRMED','remote_result':'NONE','cleanup_pending':False,'deadline_at_us':run['deadline_at_us'],
            'result_digest':None,'original_operation':op,'last_operation':op}
        c.rows.write('steps',uow,step)
        if impact is not None:self.memory.long_term.influence.claim(uow,v['influence_id'],run['run_id'],sid,now,state,'CAPACITY_REACHED' if state=='DEFERRED_CAPACITY' else 'SOURCE_CHANGED')
        claim=self.key('dream-deferred',run['run_id'],v['object_id'])
        c.rows.write('claims',uow,c._base(claim,now)|{'run_id':run['run_id'],'step_id':sid,'work_kind':'OBJECT','work_id':v['object_id'],
            'observed_revision':v['object_revision'],'status':state,'continuation':v['object_id'],'last_operation':op})
        c.rows.write('runs',uow,validate_run(dict(run)|{'revision':run['revision']+1,'updated_at_us':now,'steps_completed':run['steps_completed']+1,
            'edges_used':run['edges_used']+int(impact is not None),'steps_deferred':run['steps_deferred']+1,'remaining_work':True,'end_reason':'WORK_DEFERRED','last_operation':op}),run['revision'])
        return result(v['operation_id'],state,{'dream':{'rows_changed':self.storage.transaction_row_changes(uow)['dream'],
            'targets':(target(sid,1),target(claim,1),target(run['run_id'],run['revision']+1,run['revision']))},**self.impact_fact(uow,step)})

    def impact(self,uow,v,run):
        if 'influence_id' not in v:return None
        if run['edges_used']>=self.configuration.candidate.text.record('dream.resources')['dependency_edges_per_run']:raise OwnerFailure('RESOURCE_BUSY','run','CAPACITY_REACHED')
        influence=self.memory.long_term.influence
        work=influence.rows.get('influence_work',uow,v['influence_id'])
        if work is None or work['affected_object_id']!=v['object_id'] or work['state'] in ('APPLIED','UNCHANGED') or work['last_run']==run['run_id']:raise InvalidValue()
        event=influence.rows.get('influence_events',uow,work['event_id'])
        if event is None:raise InvalidValue()
        return MappingProxyType({'event_id':work['event_id'],'cause_root':work['cause_root'],'changed_object_id':work['changed_object_id'],
            'event_revision':event['changed_revision'],'event_reason':event['reason'],'event_sequence':event['sequence']})

    def impact_fact(self,uow,step):
        if step['origin_event_id'] is None:return {}
        influence=self.memory.long_term.influence
        work=influence.rows.get('influence_work',uow,influence.key('influence-work',step['origin_event_id'],step['object_ref']['object_id']))
        if work is None:raise InvalidValue()
        return {'memory':{'rows_changed':self.storage.transaction_row_changes(uow)['memory'],
            'targets':(target(work['object_id'],work['revision'],work['revision']-1),)}}

    def recheck(self,uow,work,body):
        if 'influence' in body:self.memory.long_term.influence.require_current(uow,body['influence']['event_id'],work['object_ref']['object_id'])
        selected,subjects,entry_id,routes=collect(self,uow,work['object_ref']['object_id'],work['object_ref']['revision'],influence='influence' in body)
        from companion_memory.memory.dream_view import same_evidence
        if not same_evidence(selected,body['evidence']) or subjects!=body['subjects'] or routes!=body['routes'] or entry_id!=body['entry_id']:raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
        if self.current.participate_current(uow,body['current_persona']['publication_id'],body['current_persona']['revision']) is None:raise OwnerFailure('PRECONDITION_FAILED','persona','REVISION_CONFLICT')
        return selected,subjects,entry_id,routes

    def resume_readiness(self,uow,run):
        work=self.rows.get('dream_work',uow,self.work_id(run['active_step_id']))
        if work is None or work['state'] not in ('FROZEN','RESULT_STORED','FAILED'):raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')

    def failure(self,uow,v,run,work,step,now,op):
        if work['state']!='FROZEN':raise InvalidValue()
        request=self.provider.participate_original(uow,v['request_id'],role='DREAM_REVIEW',owner_ref=run['run_id'],operation_key=work['request_key'],request_digest=v['request_digest'])
        receipt=self.provider.verify_original_receipt(uow,request)
        if request['phase'] not in ('TERMINAL','REMOTE_RESULT_UNKNOWN') or request['outcome']=='SUCCEEDED':raise InvalidValue()
        unknown=request['phase']=='REMOTE_RESULT_UNKNOWN';state='RECOVERY_REQUIRED' if unknown else 'FAILED'
        proof=MappingProxyType({k:getattr(receipt.identity,k) for k in ('owner_namespace','operation_kind','scope_id','operation_key')})
        self.rows.write('dream_work',uow,dict(work)|{'revision':work['revision']+1,'updated_at_us':now,'state':state,
            'request_id':request['object_id'],'request_digest':request['fingerprint'],'provider_receipt':proof,'reason':'REMOTE_RESULT_UNKNOWN' if unknown else 'PROVIDER_FAILURE','last_operation':op},work['revision'])
        self.control.rows.write('steps',uow,dict(step)|{'revision':step['revision']+1,'updated_at_us':now,'state':state,
            'request_id':request['object_id'],'remote_result':'UNKNOWN' if unknown else 'KNOWN','last_operation':op},step['revision'])
        self.control.rows.write('runs',uow,validate_run(dict(run)|{'revision':run['revision']+1,'updated_at_us':now,'state':'RECOVERY_REQUIRED' if unknown else run['state'],
            'remote_result':'UNKNOWN' if unknown else 'KNOWN','model_calls_used':run['model_calls_used']+1,'last_operation':op}),run['revision'])
        counts=self.storage.transaction_row_changes(uow)
        return result(v['operation_id'],state,{'cognition':{'rows_changed':counts['cognition'],'targets':(target(work['object_id'],work['revision']+1,work['revision']),)},
            'dream':{'rows_changed':counts['dream'],'targets':(target(step['object_id'],step['revision']+1,step['revision']),target(run['run_id'],run['revision']+1,run['revision']))}})

    def prepare(self,uow,v,run,now,op):
        c=self.control;c.require_dispatch(uow,run['run_id'],run['revision'],run['mode_epoch'])
        limits=self.configuration.candidate.text.record('dream.resources')
        if not c.settled(run):raise OwnerFailure('RESOURCE_BUSY','run','RUN_ACTIVE')
        if run['model_calls_used']+2>=limits['model_calls_per_run']:raise OwnerFailure('RESOURCE_BUSY','run','CAPACITY_REACHED')
        if now>=run['deadline_at_us']:raise OwnerFailure('TIMEOUT','run','DEADLINE_EXCEEDED')
        sid=self.key('dream-step',run['run_id'],run['steps_completed'])
        impact=self.impact(uow,v,run)
        if impact is not None:self.memory.long_term.influence.require_current(uow,impact['event_id'],v['object_id'])
        try:frozen=material(self,uow,run,v['object_id'],v['object_revision'],sid,op,now,impact=impact)
        except ValueTooLarge:raise OwnerFailure('RESOURCE_BUSY','material','CAPACITY_REACHED') from None
        wid=self.work_id(sid);deadline=min(run['deadline_at_us'],now+limits['step_timeout_ms']*1000)
        work=self.base(wid,now)|{'origin':'DREAM','run_id':run['run_id'],'step_id':sid,'bound_revision':run['revision']+1,'object_ref':{'object_id':v['object_id'],'revision':v['object_revision']},
            'state':'FROZEN','material_id':frozen.manifest['object_id'],'material_digest':frozen.manifest['payload_digest'],
            'request_key':wid,'request_id':None,'request_digest':None,'handoff_id':None,'provider_receipt':None,
            'candidate_id':None,'decision':None,'reason':'','leaf_count':0,'candidate_digest':None,'result_material_id':None,'result_material_digest':None,
            'deadline_at_us':deadline,'original_operation':op,'last_operation':op}
        self.rows.write('dream_work',uow,work)
        if impact is not None:self.memory.long_term.influence.claim(uow,v['influence_id'],run['run_id'],sid,now)
        fact=self.materials.stage_complete(uow,frozen)
        c.rows.write('steps',uow,c._base(sid,now)|{'run_id':run['run_id'],'ordinal':run['steps_completed'],'kind':'DREAM_REVIEW','state':'FROZEN',
            'mode_epoch':run['mode_epoch'],'permit_digest':sha256(encode_content(v,8192)).hexdigest(),'origin_event_id':None if impact is None else impact['event_id'],'cause_root':sid if impact is None else impact['cause_root'],
            'object_ref':work['object_ref'],'accounted_from':None,'accounted_through':None,'material_id':work['material_id'],'material_digest':work['material_digest'],
            'candidate_id':None,'request_key':wid,'request_id':None,'handoff_id':None,'execution_key':v['operation_id'],'plan_id':None,
            'local_confirmation':'CONFIRMED','remote_result':'NONE','cleanup_pending':False,'deadline_at_us':deadline,'result_digest':None,'original_operation':op,'last_operation':op})
        c.rows.write('runs',uow,validate_run(dict(run)|{'revision':run['revision']+1,'updated_at_us':now,'active_step_id':sid,'step_deadline_at_us':deadline,'edges_used':run['edges_used']+int(impact is not None),'last_operation':op}),run['revision'])
        counts=self.storage.transaction_row_changes(uow)
        return result(v['operation_id'],'FROZEN',{'cognition':{'rows_changed':counts['cognition'],'targets':(*fact['targets'],target(wid,1))},
            'dream':{'rows_changed':counts['dream'],'targets':(target(sid,1),target(run['run_id'],run['revision']+1,run['revision']))},**self.impact_fact(uow,{'origin_event_id':None if impact is None else impact['event_id'],'object_ref':work['object_ref']})})

    def store(self,uow,v,run,work,step,now,op):
        terminal=self.receiving
        if terminal is None or work['state']!='FROZEN' or terminal.request['object_id']!=v['request_id'] or terminal.request['fingerprint']!=v['request_digest']:raise InvalidValue()
        self.provider.verify_daily_result(uow,terminal,role='DREAM_REVIEW',owner_ref=run['run_id'],operation_key=work['request_key'],request_digest=v['request_digest'])
        receipt=self.provider.verify_original_receipt(uow,terminal.request)
        proof=MappingProxyType({k:getattr(receipt.identity,k) for k in ('owner_namespace','operation_kind','scope_id','operation_key')})
        body,original=self.frozen(uow,work);candidate=None;failure_reason='INVALID_RESPONSE'
        try:
            candidate=build(self.configuration,run['run_id'],step['object_id'],v['request_id'],cast(str,terminal.request['handoff_id']),work['material_digest'],
                encode_content(cast(Value,terminal.value['output']),24576),cast(tuple[Record,...],body['evidence']),cast(tuple[Record,...],body['subjects']),
                cast(tuple[str,...],body['routes']),cast(int,body['created_at_us']),writable=(cast(str,body['object_id']),),influence=record(body['influence']) if 'influence' in body else None)
        except ValueTooLarge:failure_reason='CAPACITY_REACHED'
        except InvalidValue:pass
        state='FAILED' if candidate is None else 'RESULT_STORED'
        metadata={k:value for k,value in original.manifest.items() if k not in ('leaf_refs','payload_digest','byte_count')}
        metadata.update(object_id=self.key('dream-result',step['object_id']),context_kind='PROVIDER_RESULT',wire_digest=None,reservation_input_bound=0,
            created_at_us=now,updated_at_us=now,original_operation=op)
        retained=freeze_material(metadata,encode_content(cast(Value,freeze({'terminal':terminal.value},262144,owned=True)),262144),dream_format=True)
        fact=self.materials.stage_complete(uow,retained)
        changed=dict(work)|{'revision':work['revision']+1,'updated_at_us':now,'state':state,'request_id':v['request_id'],'request_digest':v['request_digest'],
            'handoff_id':terminal.request['handoff_id'],'provider_receipt':proof,'candidate_id':None if candidate is None else candidate.candidate_id,
            'decision':None if candidate is None else candidate.decision,'reason':failure_reason if candidate is None else candidate.reason,
            'leaf_count':0 if candidate is None else len(candidate.leaves),'candidate_digest':None if candidate is None else candidate.digest,
            'result_material_id':retained.manifest['object_id'],'result_material_digest':retained.manifest['payload_digest'],'last_operation':op}
        changed=self.rows.write('dream_work',uow,changed,work['revision'])
        self.control.rows.write('steps',uow,dict(step)|{'revision':step['revision']+1,'updated_at_us':now,'state':'RESULT_STORED',
            'candidate_id':changed['candidate_id'],'request_id':v['request_id'],'handoff_id':changed['handoff_id'],'remote_result':'KNOWN',
            'result_digest':changed['candidate_digest'],'last_operation':op},step['revision'])
        self.control.rows.write('runs',uow,validate_run(dict(run)|{'revision':run['revision']+1,'updated_at_us':now,
            'model_calls_used':run['model_calls_used']+1,'last_operation':op}),run['revision'])
        counts=self.storage.transaction_row_changes(uow)
        return MappingProxyType(dict(result(v['operation_id'],state,{'cognition':{'rows_changed':counts['cognition'],'targets':(*fact['targets'],target(work['object_id'],cast(int,changed['revision']),work['revision']))},
            'dream':{'rows_changed':counts['dream'],'targets':(target(step['object_id'],step['revision']+1,step['revision']),target(run['run_id'],run['revision']+1,run['revision']))}}))|
            {'consumer_digest':sha256(encode_content(changed,8192)).hexdigest()})

    def finish(self,uow,run,step,state,now,op):
        c=self.control
        if step['origin_event_id'] is not None:
            current=self.memory.current(uow,step['object_ref']['object_id'])
            self.memory.long_term.influence.finish(uow,step['origin_event_id'],step['object_ref']['object_id'],step['object_id'],state,now,step['object_ref']['revision'],step['object_ref']['revision'] if current is None else current['revision'])
        c.rows.write('steps',uow,dict(step)|{'revision':step['revision']+1,'updated_at_us':now,'state':state,'last_operation':op},step['revision'])
        changed=dict(run)|{'revision':run['revision']+1,'updated_at_us':now,'active_step_id':None,'step_deadline_at_us':None,
            'steps_completed':run['steps_completed']+1,'steps_deferred':run['steps_deferred']+int(state.startswith('DEFERRED')),
            'end_reason':'PROVIDER_FAILURE' if state=='FAILED' else 'WORK_DEFERRED' if state.startswith('DEFERRED') else run['end_reason'],
            'last_operation':op}
        if run['state']=='PAUSING':changed['state']='PAUSED'
        c.rows.write('runs',uow,validate_run(changed),run['revision'])
        return (target(step['object_id'],step['revision']+1,step['revision']),target(run['run_id'],run['revision']+1,run['revision']))

    def explicit_send(self,key):
        return self.bound and not self.closed and self.sending is not None and self.sending[1]['request_key']==key and self.control.dispatch_enabled
    def authorize_request(self,request,uow):
        if self.sending is None or not self.explicit_send(request.description['original_request_key']):return False
        run,work=self.sending
        if request.binding.role!='DREAM_REVIEW' or request.description['work_id']!=run['run_id'] or request.description['material_digest']!=work['material_digest']:return False
        if uow is not None:
            raw=self.views.stage('provider_dream_work',uow,{'caller_scope':self.configuration.scope_id,'object_id':work['object_id']})
            if len(raw)!=1:return False
            current=TABLES[0].isolate(decode_row(raw[0],WORK,8192,self.configuration.database_id,self.configuration.scope_id,self.configuration.snapshot_id))
            if current!=work:return False
            self.control.require_provider_dispatch(uow,run)
        return True
    def verify_received(self,uow,request,handoff,proof):
        if request['task_role']!='DREAM_REVIEW' or proof['kind']!=STORE:return False
        raw=self.views.stage('provider_dream_work',uow,{'caller_scope':self.configuration.scope_id,'object_id':request['operation_key']})
        if len(raw)!=1:return False
        work=TABLES[0].isolate(decode_row(raw[0],WORK,8192,self.configuration.database_id,self.configuration.scope_id,self.configuration.snapshot_id))
        if work['request_id']!=request['object_id'] or work['handoff_id']!=handoff['object_id']:return False
        definition=next(d for d in self.commands if d.operation_kind==STORE)
        receipt=self.storage.confirm_cognition_consumer_operation(uow,definition,record(work['last_operation'])['operation_key'],request['object_id'])
        return receipt is not None and receipt.fingerprint==proof['fingerprint'] and receipt.result['consumer_digest']==sha256(encode_content(work,8192)).hexdigest()
    def close(self):
        self.closed=True
        return self.job is None and self.receiving is None and self.sending is None
