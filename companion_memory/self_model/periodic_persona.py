"""Periodic generation, independent review and current-pointer publication.

Only the same native dream grant may prepare new work. Result consumption uses
original Provider evidence even after dispatch is stopped. Publication rechecks
all current evidence in the transaction which changes the single pointer.
"""
from __future__ import annotations
from hashlib import sha256
from types import MappingProxyType
from typing import cast,TYPE_CHECKING
if TYPE_CHECKING:
    from companion_memory.cognition.dream_review import DreamReview
if TYPE_CHECKING:
    from companion_memory.runtime.dream_mode import DreamMode
import time
import asyncio
from companion_memory.persistence import ResultBoundCommandDefinition, RecordSchema, UnitOfWork, Found,Field
from companion_memory.persistence.daily_records import DailyRows,identity
from companion_memory.persistence.semantic_records import Record,ID,P,N,H,fields,enum
from companion_memory.persistence.schema import InvalidValue,ValueTooLarge,SequenceSchema,Value
from companion_memory.persistence.owned_statements import OwnerFailure,BoundStatements
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.text_records import decode_row
from companion_memory.dream.results import audits,result_schema,result,target,INTENT
from companion_memory.dream.records import validate_run
from companion_memory.memory.dream_view import evidence
from companion_memory.memory.formats import record
from companion_memory.provider.daily_service import DailyResult
from companion_memory.provider.values import freeze
from .periodic_records import TABLES,BASIS
from .periodic_material import freeze_periodic
from .periodic_output import decode_persona_candidate,decode_persona_review
from .periodic_leaves import split_candidate,join_candidate

PREPARE='prepare_periodic_persona'
DEFER='defer_periodic_persona'
GENERATION='store_periodic_generation'
REVIEW='store_periodic_review'
PUBLISH='publish_periodic_persona'
KEEP='keep_periodic_persona'
FAIL='record_periodic_failure'
INVALID='store_periodic_failure'
UNKNOWN='record_periodic_unknown'
RETIRE='retire_periodic_material'
DISCARD='discard_periodic_persona'
ROLES=('PERSONA_DREAM','PERSONA_REVIEW')


class PeriodicPersona:
    review_work:DreamReview|None=None
    def __init__(self,control,catalog,materials,participants):
        self.control=control;self.catalog=catalog;self.materials=materials;self.bound=False;self.closed=False
        self.receiving:DailyResult|None=None;self.sending:tuple[Record,Record,str,Record,str]|None=None;self.job:asyncio.Task|None=None
        from companion_memory.dream.expiry import DreamExpiry
        self.expiry:DreamExpiry|None=None
        self.observed=None
        self.mode:DreamMode|None=None
        definitions=[]
        common=fields(run_id=ID,expected_revision=P,mode_epoch=N)
        layouts=((DEFER,('self_model','dream'),()),(PREPARE,('self_model','cognition','dream'),()),
            (GENERATION,('self_model','cognition','dream'),fields(request_id=ID,request_digest=H)),
            (REVIEW,('self_model','dream'),fields(request_id=ID,request_digest=H)),
            (PUBLISH,('self_model','dream'),()),(KEEP,('self_model','dream'),()),
            (DISCARD,('self_model','dream'),fields(reason=enum('DEADLINE_EXCEEDED','SOURCE_CHANGED','ABORTED_SAFELY'))),
            (RETIRE,('cognition',),fields(context_id=ID,page_offset=N)),
            (FAIL,('self_model','dream'),fields(request_id=ID,request_digest=H,role=enum(*ROLES))),
            (UNKNOWN,('self_model','dream'),fields(request_id=ID,request_digest=H,role=enum(*ROLES))),
            (INVALID,('self_model','dream','cognition'),fields(request_id=ID,request_digest=H,role=enum(*ROLES))))
        for kind,owners,parameters in layouts:
            required,bindings=audits(kind,owners)
            def handle(uow,values,operation=kind):
                try:return self.handle(operation,uow,values)
                except ValueTooLarge:
                    if operation!=PREPARE:raise
                    failure=OwnerFailure('RESOURCE_BUSY','material','CAPACITY_REACHED')
                    control.causes.record(operation,values,failure)
                    raise failure from None
                except OwnerFailure as failure:
                    control.causes.record(operation,values,failure);raise
            definitions.append(ResultBoundCommandDefinition('self_model',kind,1,RecordSchema(fields(operation_id=ID)+common+parameters),1,
                result_schema(owners,('FROZEN','GENERATED','REVIEWED','PUBLISHED','KEPT_PREVIOUS','RECOVERY_REQUIRED','MATERIAL_RELEASED','DEFERRED_CAPACITY')),participants,required,handle,INTENT,bindings))
        from dataclasses import replace
        from .daily_persona import RECORD_DIGEST
        self.commands=tuple(replace(d,result_schema=RecordSchema(d.result_schema.fields+(Field('records',SequenceSchema(RECORD_DIGEST,0,4)),))) for d in definitions)

    def bind(self,current,provider):
        if self.bound or current.configuration is not self.control.configuration:raise InvalidValue()
        self.current=current;self.memory=current.memory;self.provider=provider;self.configuration=current.configuration;self.storage=current.storage
        self.control.resume_readiness=self.resume_readiness
        self.control.exit_readiness=self.exit_readiness
        self.rows=current.rows;current.periodic=self;self.views=BoundStatements(self.catalog,self.storage,'provider');self.bound=True

    def key(self,kind,*parts):return identity(kind,self.configuration.database_id,self.configuration.scope_id,*parts)
    def base(self,key,now):return self.control._base(key,now)
    def operation(self,uow):
        original=self.storage.cognition_operation_context(uow,self.catalog.definition)
        return MappingProxyType({k:getattr(original,k) for k in ('owner_namespace','operation_kind','scope_id','operation_key')})
    def work_id(self,run_id):return self.key('periodic-run',run_id)
    def get_work(self,uow,run_id):
        work=self.rows.get('periodic_persona_runs',uow,self.work_id(run_id))
        if work is None:raise InvalidValue()
        return work
    def read_shared(self,uow,name,key):
        raw=self.views.stage('provider_read_'+name,uow,{'caller_scope':self.configuration.scope_id,'object_id':key})
        if len(raw)!=1:raise InvalidValue()
        spec=next(t for t in TABLES if t.name==name)
        return spec.isolate(decode_row(raw[0],spec.schemas[0],spec.maximum,self.configuration.database_id,self.configuration.scope_id,self.configuration.snapshot_id))
    def frozen(self,uow,run_id,role='PERSONA_DREAM'):
        key=self.key('periodic-material',run_id,role)
        root=self.materials.participate_manifest(uow,key,run_id)
        material=self.materials.participate_material(uow,key,cast(str,root['payload_digest']),run_id)
        return cast(Record,freeze(decode_content(material.body,262144),262144,owned=True)),material
    def validate_candidate(self,raw,view,run_id,now):
        """Check the actual future public envelope before independent review."""
        from .periodic_projection import project_current
        output=decode_persona_candidate(raw,view['basis_refs'])
        project_current(MappingProxyType({'publication_id':self.key('periodic-publication',run_id),
            'revision':view['current_pointer_revision']+1,'text':output['text'],'generated_at_us':now,
            'review':'MODEL_REVIEWED','model_origin':'REMOTE_PROVIDER','publication_origin':'PERIODIC_REVIEWED','stale':False}))
        return output

    def candidate(self,uow,work):
        candidate=self.rows.get('periodic_persona_candidates',uow,work['candidate_id'])
        if candidate is None:raise InvalidValue()
        leaves=[]
        for ordinal in range(candidate['manifest']['leaf_count']):
            leaf=self.rows.get('periodic_persona_leaves',uow,self.key('periodic-leaf',candidate['object_id'],ordinal))
            if leaf is None:raise InvalidValue()
            leaves.append(leaf['leaf'])
        return candidate,decode_persona_candidate(join_candidate(candidate['manifest'],tuple(leaves)),candidate['basis_refs'])
    def recheck(self,uow,view,body):
        previous=record(body['previous_persona'])
        current=self.current.participate_current(uow,previous['publication_id'],previous['revision'])
        if current is None:raise OwnerFailure('PRECONDITION_FAILED','persona','REVISION_CONFLICT')
        if evidence(self.memory,uow,view['basis_refs'])!=body['evidence']:
            raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')

    def handle(self,kind,uow:UnitOfWork,v:Record):
        if not self.bound or self.closed:raise InvalidValue()
        control=self.control;now=control._ready(uow);op=self.operation(uow)
        run=(control.participate_completion if kind in (GENERATION,REVIEW,FAIL,INVALID,UNKNOWN) else control.participate_run)(uow,cast(str,v['run_id']),cast(int,v['expected_revision']),cast(int,v['mode_epoch']))
        if kind==DEFER:
            from .periodic_deferral import defer
            return defer(self,uow,v,run,now,op)
        if kind==RETIRE:
            work=self.get_work(uow,run['run_id'])
            allowed={self.key('periodic-material',run['run_id'],role) for role in ROLES}|{self.key('periodic-invalid',run['run_id'],role) for role in ROLES}
            if work['state'] not in ('PUBLISHED','KEPT_PREVIOUS') or run['active_step_id'] is not None or v['context_id'] not in allowed or not self.provider.generation_consumers_ended(run['run_id']):raise InvalidValue()
            fact=self.materials.retire_page(uow,cast(str,v['context_id']),cast(str,run['run_id']),cast(int,v['page_offset']))
            return MappingProxyType(dict(result(cast(str,v['operation_id']),'MATERIAL_RELEASED',{'cognition':fact}))|{'records':()})
        if kind in (PREPARE,PUBLISH):control.require_dispatch(uow,cast(str,run['run_id']),cast(int,run['revision']),cast(int,run['mode_epoch']))
        facts:dict[str,object]={};targets:list[dict[str,object]]=[];dream_targets:list[dict[str,object]]=[];changed=dict(run);work=None
        if kind==PREPARE:
            if not control.settled(run) or now>=cast(int,run['deadline_at_us']):raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
            if cast(int,run['model_calls_used'])+2>cast(int,control.configuration.candidate.text.record('dream.resources')['model_calls_per_run']):raise OwnerFailure('RESOURCE_BUSY','run','CAPACITY_REACHED')
            from .periodic_deferral import collect
            pointer,supplied,watermark,cursor,body=collect(self,uow,run)
            policy=self.configuration.candidate.text.record('self_model.initial_persona')
            material=freeze_periodic(self,'PERSONA_DREAM',run,body,op,now)
            sid=self.key('dream-step',run['run_id'],run['steps_completed']);wid=self.work_id(run['run_id']);vid=self.key('self-view',wid)
            policy_digest=sha256(encode_content(policy,8192)).hexdigest()
            view=self.base(vid,now)|{'run_id':run['run_id'],'step_id':sid,'current_publication_id':pointer['publication_id'],
                'current_pointer_revision':pointer['revision'],'basis_refs':supplied,'watermark':watermark,
                'material_id':material.manifest['object_id'],'material_digest':material.manifest['payload_digest'],'policy_digest':policy_digest,'original_operation':op}
            self.rows.write('persona_self_views',uow,view)
            deadline=min(cast(int,run['deadline_at_us']),now+180000000)
            work=self.base(wid,now)|{'run_id':run['run_id'],'step_id':sid,'view_id':vid,'state':'FROZEN','mode_epoch':run['mode_epoch'],
                'candidate_id':None,'review_id':None,'publication_id':None,'generation_bound_revision':run['revision']+1,'review_bound_revision':None,'generation_key':self.key('periodic-generation',run['run_id']),
                'generation_request_id':None,'generation_handoff_id':None,'generation_receipt':None,'generation_digest':None,
                'review_key':self.key('periodic-review',run['run_id']),'review_request_id':None,'review_handoff_id':None,'review_receipt':None,'review_digest':None,
                'local_confirmation':'CONFIRMED','remote_result':'NONE','cleanup_pending':False,'deadline_at_us':deadline,'original_operation':op,'last_operation':op}
            self.rows.write('periodic_persona_runs',uow,work)
            targets.extend((target(vid,1),target(wid,1)));facts['cognition']=self.materials.stage_complete(uow,material)
            control.rows.write('steps',uow,control._base(sid,now)|{'run_id':run['run_id'],'ordinal':run['steps_completed'],
                'kind':'PERSONA_GENERATION','state':'FROZEN','mode_epoch':run['mode_epoch'],'permit_digest':sha256(encode_content(v,8192)).hexdigest(),
                'origin_event_id':None,'cause_root':None,'object_ref':None,'accounted_from':None,'accounted_through':None,
                'material_id':material.manifest['object_id'],'material_digest':material.manifest['payload_digest'],'candidate_id':None,
                'request_key':work['generation_key'],'request_id':None,'handoff_id':None,'execution_key':v['operation_id'],'plan_id':None,
                'local_confirmation':'CONFIRMED','remote_result':'NONE','cleanup_pending':False,'deadline_at_us':deadline,'result_digest':None,
                'original_operation':op,'last_operation':op})
            dream_targets.append(target(sid,1));changed.update(active_step_id=sid,step_deadline_at_us=deadline,self_cursor=cursor);state='FROZEN'
            information=self.memory.information
            if information is None:raise InvalidValue()
            if not information.has_after(uow,cast(str,run['object_cursor'])):changed['object_cursor']=''
            if self.expiry is not None:changed.update(self.expiry.exhausted_cursor(uow,run,now))
        else:
            work=self.get_work(uow,run['run_id']);before=work['revision'];updated=dict(work)
            if kind in (GENERATION,REVIEW,FAIL,INVALID,UNKNOWN):
                bound='generation_bound_revision' if kind==GENERATION or kind in (FAIL,INVALID,UNKNOWN) and v['role']=='PERSONA_DREAM' else 'review_bound_revision'
                if v['expected_revision']!=work[bound]:raise InvalidValue()
            view=self.rows.get('persona_self_views',uow,work['view_id'])
            if view is None or run['active_step_id']!=work['step_id']:raise InvalidValue()
            if kind in (FAIL,INVALID,UNKNOWN):
                role=v['role'];prefix='generation' if role=='PERSONA_DREAM' else 'review'
                if work['state']!=('FROZEN' if prefix=='generation' else 'GENERATED'):raise InvalidValue()
                request=self.provider.participate_original(uow,v['request_id'],role=role,owner_ref=run['run_id'],operation_key=work[prefix+'_key'],request_digest=v['request_digest'])
                receipt=self.provider.verify_original_receipt(uow,request)
                proof=MappingProxyType({k:getattr(receipt.identity,k) for k in ('owner_namespace','operation_kind','scope_id','operation_key')})
                if kind==UNKNOWN:
                    if request['phase']!='REMOTE_RESULT_UNKNOWN':raise InvalidValue()
                    state='RECOVERY_REQUIRED';changed.update(state=state,remote_result='UNKNOWN');updated['remote_result']='UNKNOWN'
                else:
                    if request['phase']!='TERMINAL':raise InvalidValue()
                    if kind==INVALID:
                        terminal=self.receiving
                        if terminal is None:raise InvalidValue()
                        self.provider.verify_daily_result(uow,terminal,role=role,owner_ref=run['run_id'],operation_key=work[prefix+'_key'],request_digest=v['request_digest'])
                        try:
                            raw=encode_content(cast(Value,terminal.value['output']),16384)
                            if role=='PERSONA_DREAM':self.validate_candidate(raw,view,run['run_id'],now)
                            else:decode_persona_review(raw)
                        except InvalidValue:pass
                        else:raise InvalidValue()
                        from companion_memory.cognition.daily_material import freeze_material
                        original=self.materials.participate_manifest(uow,self.key('periodic-material',run['run_id'],role),run['run_id'])
                        metadata={k:x for k,x in original.items() if k not in ('leaf_refs','payload_digest','byte_count')}
                        metadata.update(object_id=self.key('periodic-invalid',run['run_id'],role),context_kind='PROVIDER_RESULT',wire_digest=None,
                            reservation_input_bound=0,original_operation=op,created_at_us=now,updated_at_us=now)
                        retained=freeze_material(metadata,encode_content(cast(Value,terminal.value),40960),dream_format=True)
                        facts['cognition']=self.materials.stage_complete(uow,retained)
                    elif request['outcome']=='SUCCEEDED':raise InvalidValue()
                    state='KEPT_PREVIOUS';changed['end_reason']='PROVIDER_FAILURE'
                updated.update({prefix+'_request_id':request['object_id'],prefix+'_digest':request['fingerprint'],
                    prefix+'_handoff_id':request['handoff_id'],prefix+'_receipt':proof})
                changed['model_calls_used']=cast(int,run['model_calls_used'])+1
            elif kind in (GENERATION,REVIEW):
                role='PERSONA_DREAM' if kind==GENERATION else 'PERSONA_REVIEW'
                expected='FROZEN' if kind==GENERATION else 'GENERATED'
                if work['state']!=expected:raise InvalidValue()
                terminal=self.receiving
                if terminal is None or terminal.request['object_id']!=v['request_id'] or terminal.request['fingerprint']!=v['request_digest']:raise InvalidValue()
                request=terminal.request;key=work['generation_key' if kind==GENERATION else 'review_key']
                self.provider.verify_daily_result(uow,terminal,role=role,owner_ref=run['run_id'],operation_key=key,request_digest=v['request_digest'])
                receipt=self.provider.verify_original_receipt(uow,request)
                proof=MappingProxyType({k:getattr(receipt.identity,k) for k in ('owner_namespace','operation_kind','scope_id','operation_key')})
                raw=encode_content(cast(Value,terminal.value['output']),16384)
                if kind==GENERATION:
                    output=self.validate_candidate(raw,view,run['run_id'],now);cid=self.key('periodic-candidate',run['run_id'])
                    manifest,leaves=split_candidate(cid,encode_content(output,16384))
                    candidate=self.base(cid,now)|{'run_id':run['run_id'],'view_id':view['object_id'],'manifest':manifest,
                        'basis_refs':output['basis_refs'],'text_digest':sha256(cast(str,output['text']).encode()).hexdigest(),
                        'change_reason':output['change_reason'],'generation_request_id':request['object_id'],'generation_receipt':proof,'original_operation':op}
                    self.rows.write('periodic_persona_candidates',uow,candidate)
                    for ordinal,leaf in enumerate(leaves):self.rows.write('periodic_persona_leaves',uow,self.base(self.key('periodic-leaf',cid,ordinal),now)|{'candidate_id':cid,'ordinal':ordinal,'leaf':leaf})
                    updated['review_bound_revision']=run['revision']+1
                    body,_=self.frozen(uow,run['run_id']);review_material=freeze_periodic(self,'PERSONA_REVIEW',run,dict(body)|{'candidate':output},op,now)
                    facts['cognition']=self.materials.stage_complete(uow,review_material)
                    targets.append(target(cid,1));state='GENERATED';updated.update(candidate_id=cid,generation_request_id=request['object_id'],generation_handoff_id=request['handoff_id'],generation_receipt=proof,generation_digest=request['fingerprint'])
                else:
                    output=decode_persona_review(raw);candidate,_=self.candidate(uow,work);rid=self.key('periodic-review-result',run['run_id'])
                    self.rows.write('periodic_persona_reviews',uow,self.base(rid,now)|{'run_id':run['run_id'],'candidate_id':candidate['object_id'],
                        'candidate_digest':candidate['manifest']['digest'],'decision':output['decision'],'reason':output['reason'],
                        'request_id':request['object_id'],'handoff_id':request['handoff_id'],'receipt':proof,'policy_digest':view['policy_digest'],'original_operation':op})
                    targets.append(target(rid,1));state='REVIEWED';updated.update(review_id=rid,review_request_id=request['object_id'],review_handoff_id=request['handoff_id'],review_receipt=proof,review_digest=request['fingerprint'])
                changed['model_calls_used']=cast(int,run['model_calls_used'])+1
            else:
                if kind==DISCARD:
                    self.check_disposition(uow,run,work,view,v['reason'],now)
                    changed['end_reason']='WORK_DEFERRED' if v['reason']=='SOURCE_CHANGED' else v['reason']
                elif work['state'] not in ('REVIEWED','KEPT_PREVIOUS') or kind==PUBLISH and work['state']!='REVIEWED':raise InvalidValue()
                review=self.rows.get('periodic_persona_reviews',uow,work['review_id']) if work['review_id'] is not None else None
                if kind==PUBLISH and review is None:raise InvalidValue()
                self.provider.require_dream_exit(uow)
                if kind==PUBLISH:
                    if review is None:raise InvalidValue()
                    if review['decision']!='APPROVE' or now>=work['deadline_at_us']:raise OwnerFailure('PRECONDITION_FAILED','persona','PERSONA_REVIEW_REJECTED')
                    body,_=self.frozen(uow,run['run_id']);self.recheck(uow,view,body)
                    candidate,_=self.candidate(uow,work);pointer=self.rows.get('current_persona',uow,self.current.pointer_id)
                    if pointer is None:raise InvalidValue()
                    for role in ROLES:
                        prefix='generation' if role=='PERSONA_DREAM' else 'review'
                        request=self.provider.participate_original(uow,work[prefix+'_request_id'],role=role,owner_ref=run['run_id'],operation_key=work[prefix+'_key'],request_digest=work[prefix+'_digest'])
                        self.provider.verify_original_receipt(uow,request)
                    pid=self.key('periodic-publication',run['run_id'])
                    publication=self.base(pid,now)|{'run_id':run['run_id'],'candidate_id':candidate['object_id'],'candidate_revision':candidate['revision'],
                        'candidate_digest':candidate['manifest']['digest'],'review_id':review['object_id'],'review_revision':review['revision'],
                        'previous_publication_id':pointer['publication_id'],'previous_pointer_revision':pointer['revision'],'basis_refs':candidate['basis_refs'],
                        'generation_request_id':work['generation_request_id'],'review_request_id':work['review_request_id'],
                        'publication_origin':'PERIODIC_REVIEWED','review_status':'MODEL_REVIEWED','original_operation':op}
                    self.rows.write('periodic_persona_publications',uow,publication)
                    self.rows.write('current_persona',uow,dict(pointer)|{'revision':pointer['revision']+1,'updated_at_us':now,'publication_id':pid,
                        'publication_revision':1,'origin':'PERIODIC_REVIEWED','last_operation':op},pointer['revision'])
                    targets.extend((target(pid,1),target(self.current.pointer_id,pointer['revision']+1,pointer['revision'])))
                    updated['publication_id']=pid;changed.update(persona_publication_id=pid);state='PUBLISHED'
                else:state='KEPT_PREVIOUS'
                step=control.rows.get('steps',uow,work['step_id'])
                if step is None:raise InvalidValue()
                control.rows.write('steps',uow,dict(step)|{'revision':step['revision']+1,'updated_at_us':now,'state':'DEFERRED_CONFLICT' if kind==DISCARD and v['reason']=='SOURCE_CHANGED' else 'FAILED' if kind==DISCARD else 'APPLIED' if kind==PUBLISH else 'UNCHANGED',
                    'candidate_id':work['candidate_id'],'request_id':work['review_request_id'],'handoff_id':work['review_handoff_id'],
                    'remote_result':'KNOWN','result_digest':review['candidate_digest'] if review is not None else None,'last_operation':op},step['revision'])
                dream_targets.append(target(work['step_id'],step['revision']+1,step['revision']))
                changed.update(active_step_id=None,step_deadline_at_us=None,steps_completed=cast(int,run['steps_completed'])+1)
                if kind==DISCARD and v['reason']=='SOURCE_CHANGED':changed['steps_deferred']=cast(int,run['steps_deferred'])+1
                if run['state']=='PAUSING':changed['state']='PAUSED'
            updated.update(revision=before+1,updated_at_us=now,state=state,last_operation=op,remote_result='UNKNOWN' if kind==UNKNOWN else 'KNOWN')
            self.rows.write('periodic_persona_runs',uow,updated,before);targets.append(target(work['object_id'],before+1,before))
        revision=cast(int,run['revision']);changed.update(revision=revision+1,updated_at_us=now,last_operation=op)
        control.rows.write('runs',uow,validate_run(changed),revision);dream_targets.append(target(cast(str,run['run_id']),revision+1,revision))
        counts=self.storage.transaction_row_changes(uow)
        facts['self_model']={'rows_changed':counts['self_model'],'targets':tuple(targets)}
        facts['dream']={'rows_changed':counts['dream'],'targets':tuple(dream_targets)}
        proofs=[]
        for changed_target in targets:
            for table in ('periodic_persona_runs','persona_self_views','periodic_persona_candidates','periodic_persona_reviews','periodic_persona_publications','current_persona'):
                current=self.rows.get(table,uow,cast(str,changed_target['object_id']))
                if current is not None:
                    proofs.append(self.record_proof(current));break
            else:raise InvalidValue()
        return MappingProxyType(dict(result(cast(str,v['operation_id']),state,facts))|{'records':tuple(proofs)})

    @staticmethod
    def record_proof(value):
        return MappingProxyType({'object_id':value['object_id'],'revision':value['revision'],'digest':sha256(encode_content(value,16384)).hexdigest()})

    def verify_receipt(self,receipt,value):
        if self.record_proof(value) not in receipt.result['records']:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')

    def verify_record(self,uow,value):
        operation=value['last_operation'] if 'last_operation' in value else value['original_operation']
        definition=next((d for d in self.commands if d.operation_kind==operation['operation_kind']),None)
        if definition is None or operation['owner_namespace']!='self_model' or operation['scope_id']!=self.configuration.scope_id:raise InvalidValue()
        receipt=self.storage.confirm_prior_operation(uow,definition,operation['operation_key'])
        if receipt is None:raise InvalidValue()
        self.verify_receipt(receipt,value)

    async def verify_original(self,value):
        operation=value['last_operation'] if 'last_operation' in value else value['original_operation']
        if operation['operation_kind'] not in {d.operation_kind for d in self.commands} or operation['owner_namespace']!='self_model' or operation['scope_id']!=self.configuration.scope_id:raise InvalidValue()
        receipt=await self.control.operations[operation['operation_kind']].read_receipt(operation['operation_key'])
        if type(receipt) is not Found:raise InvalidValue()
        self.verify_receipt(receipt.value,value)

    def exit_readiness(self,uow,run):
        self.provider.require_dream_exit(uow)
        if self.materials.rows.rows.stage('dream_unreleased_material',uow,{'run_id':run['run_id']}):
            raise OwnerFailure('RESOURCE_BUSY','material','CLEANUP_PENDING',True)

    def exit_evidence(self,uow,run):
        """Expose real completed publication/disposition; never manufacture a publish."""
        deferred=self.rows.get('periodic_persona_deferrals',uow,self.key('periodic-deferral',run['run_id']))
        if deferred is not None:
            self.verify_record(uow,deferred)
            pointer=self.rows.get('current_persona',uow,self.current.pointer_id)
            if pointer is None or pointer['publication_id']!=deferred['publication_id'] or pointer['revision']!=deferred['pointer_revision'] or run['active_step_id'] is not None:raise InvalidValue()
            return 'KEPT_PREVIOUS',None,None
        work=self.get_work(uow,run['run_id'])
        if work['state'] not in ('PUBLISHED','KEPT_PREVIOUS') or run['active_step_id'] is not None:raise InvalidValue()
        for role in ROLES:
            for domain in ('periodic-material','periodic-invalid'):
                root=self.materials.rows.get('learning_contexts',uow,self.key(domain,run['run_id'],role))
                if root is not None and root['state']!='RELEASED':raise OwnerFailure('RESOURCE_BUSY','material','CLEANUP_PENDING',True)
        if work['state']=='PUBLISHED':
            pointer=self.rows.get('current_persona',uow,self.current.pointer_id)
            if pointer is None or pointer['publication_id']!=work['publication_id']:raise InvalidValue()
            return 'PUBLISHED_NEW',pointer['publication_id'],pointer['revision']
        review=self.rows.get('periodic_persona_reviews',uow,work['review_id']) if work['review_id'] is not None else None
        return ('NO_PERSONA_CHANGE' if review is not None and review['decision']=='UNCHANGED' else 'KEPT_PREVIOUS'),None,None

    def check_disposition(self,uow,run,work,view,reason,now):
        """Retain the old pointer only after proving all original request duties."""
        if work['state'] not in ('FROZEN','GENERATED','REVIEWED'):raise InvalidValue()
        self.provider.require_dream_exit(uow)
        for prefix,role in (('generation','PERSONA_DREAM'),('review','PERSONA_REVIEW')):
            if work[prefix+'_request_id'] is None:
                if not self.provider.participate_unsent_daily(uow,work[prefix+'_key'],run['run_id']):
                    raise OwnerFailure('RESULT_UNCONFIRMED','request','COMMIT_UNCONFIRMED',True)
            else:
                request=self.provider.participate_original(uow,work[prefix+'_request_id'],role=role,owner_ref=run['run_id'],operation_key=work[prefix+'_key'],request_digest=work[prefix+'_digest'])
                self.provider.verify_original_receipt(uow,request)
                if request['phase']!='TERMINAL':raise OwnerFailure('RESULT_UNCONFIRMED','request','REMOTE_RESULT_UNKNOWN')
        if reason=='DEADLINE_EXCEEDED':
            if now<work['deadline_at_us']:raise InvalidValue()
        elif reason=='SOURCE_CHANGED':
            body,_=self.frozen(uow,run['run_id'])
            try:self.recheck(uow,view,body)
            except OwnerFailure as failure:
                if failure.code!='PRECONDITION_FAILED' or failure.reason not in ('REVISION_CONFLICT','SOURCE_CHANGED'):raise
            else:raise InvalidValue()
        elif reason=='ABORTED_SAFELY':
            if run['end_reason']!='ABORTED_SAFELY' or self.control.dispatch_enabled:raise OwnerFailure('ACCESS_DENIED','run','RUN_NOT_RESUMABLE')
        else:raise InvalidValue()

    def resume_readiness(self,uow,run):
        """A frozen step may resume; original uncertainty and actual I/O may not."""
        if run['local_confirmation']!='CONFIRMED' or run['remote_result'] in ('PENDING','UNKNOWN') or run['cleanup_pending']:
            raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
        self.provider.require_dream_exit(uow)
        if run['active_step_id'] is not None:
            step=self.control.rows.get('steps',uow,run['active_step_id'])
            if step is not None and step['kind']=='DREAM_REVIEW':
                if self.review_work is None:raise InvalidValue()
                self.review_work.resume_readiness(uow,run);return
            work=self.get_work(uow,run['run_id'])
            if work['step_id']!=run['active_step_id'] or work['state'] not in ('FROZEN','GENERATED','REVIEWED','KEPT_PREVIOUS'):
                raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')

    def explicit_send(self,key):
        return self.bound and not self.closed and self.sending is not None and self.sending[2]==key and self.control.dispatch_enabled
    def authorize_request(self,request,uow):
        if not self.bound or self.closed or self.sending is None:return False
        run,work,key,root,role=self.sending
        if not self.explicit_send(key) or request.binding.role!=role or request.description['work_id']!=run['run_id'] or request.description['original_request_key']!=key or request.description['material_digest']!=root['payload_digest'] or time.time_ns()//1000>=cast(int,work['deadline_at_us']):return False
        if uow is not None:
            current=self.read_shared(uow,'periodic_persona_runs',work['object_id'])
            if current!=work:return False
            # The native dream participant validates mode, revision and the
            # volatile control generation inside Provider registration's UoW.
            self.control.require_provider_dispatch(uow,run)
        return True
    def verify_received(self,uow,request,handoff,proof):
        role=request['task_role']
        if not self.bound or role not in ROLES:return False
        run_id=request['attribution']['run_id'];work=self.read_shared(uow,'periodic_persona_runs',self.work_id(run_id))
        prefix='generation' if role=='PERSONA_DREAM' else 'review';kind=GENERATION if role=='PERSONA_DREAM' else REVIEW
        if work[prefix+'_request_id']!=request['object_id'] or work[prefix+'_handoff_id']!=handoff['object_id'] or proof['kind'] not in (kind,INVALID):return False
        kind=proof['kind']
        definition=next(d for d in self.commands if d.operation_kind==kind)
        receipt=self.storage.confirm_cognition_consumer_operation(uow,definition,proof['key'],request['object_id'])
        return receipt is not None and receipt.fingerprint==proof['fingerprint']
    def close(self):
        self.closed=True
        return self.sending is None and self.receiving is None and self.job is None
