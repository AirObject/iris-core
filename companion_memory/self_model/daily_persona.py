"""Native first-persona transactions in the complete daily owner graph.

The initial input comes only from memory's issued initialization port. Actual
generation, reception, review and publication keep separate original commands.
The imported-persona path does not bind this owner or acquire another lease.
"""
from __future__ import annotations
from datetime import datetime,timezone
from types import MappingProxyType
from typing import cast
import time
from companion_memory.persistence import ResultBoundCommandDefinition,Found,NotFound,SequenceSchema
from companion_memory.persistence.daily_records import ID,REVISION,DIGEST,UINT,Field,RecordSchema,enum,identity
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.text_records import stable_identity,digest,decode_row
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure,OwnerCauses,BoundStatements
from companion_memory.memory.formats import record,sequence
from companion_memory.cognition.daily_material import freeze_material
from companion_memory.cognition.text_output import isolate_initial_persona
from companion_memory.provider.values import dump,freeze,as_record
from companion_memory.provider.daily_service import DailyResult
from companion_memory.persistence.semantic_records import Record
from .storage import PersonaStorage
from .formats import RUN,CANDIDATE,isolate_run,isolate_candidate,isolate_publication,candidate_digest,projection
from .transitions import review,matching_generation
from .daily_persona_material import first_run,freeze_initial,material_id,original_key

RECORD_DIGEST=RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('digest',DIGEST)))

class DailyPersona:
    """One native self-model owner for generated originals and explicit review."""
    def __init__(self,catalog,materials,mode,participants):
        self.catalog=catalog;self.materials=materials;self.mode=mode;self.bound=False;self.closed=False
        self.causes=OwnerCauses()
        self._receiving:DailyResult|None=None
        self._sending:tuple[Record,Record]|None=None
        self._observed_request:Record|None=None
        self._task=None;self._job=None
        self.cleanup_failure:OwnerFailure|None=None
        layouts={
            'prepare_initial_persona':(('self_model','runtime','cognition'),{'input_id':ID,'expected_self_revision':REVISION,'expected_epoch':REVISION}),
            'associate_initial_persona_request':(('self_model',),{'run_id':ID,'expected_revision':REVISION,'generation':REVISION,'expected_epoch':REVISION}),
            'confirm_initial_persona_request':(('self_model',),{'run_id':ID,'expected_revision':REVISION,'generation':REVISION,'request_id':ID,'request_digest':DIGEST}),
            'record_initial_persona_resolution':(('self_model',),{'run_id':ID,'expected_revision':REVISION,'generation':REVISION}),
            'record_initial_persona_resolution_with_result':(('self_model','cognition'),{'run_id':ID,'expected_revision':REVISION,'generation':REVISION}),
            'mark_initial_persona_unknown':(('self_model',),{'run_id':ID,'expected_revision':REVISION,'generation':REVISION}),
            'review_initial_persona':(('self_model',),{'run_id':ID,'expected_revision':REVISION,'candidate_id':ID,'candidate_revision':REVISION,'candidate_digest':DIGEST,'decision':enum('APPROVE','REJECT')}),
            'retry_initial_persona':(('self_model','runtime','cognition'),{'run_id':ID,'expected_revision':REVISION,'generation':REVISION,'candidate_id':ID,'expected_epoch':REVISION}),
            'publish_initial_persona':(('self_model','runtime'),{'run_id':ID,'expected_revision':REVISION,'candidate_id':ID,'candidate_revision':REVISION,'candidate_digest':DIGEST,'expected_epoch':REVISION}),
            'retire_initial_persona_material':(('cognition',),{'run_id':ID,'context_id':ID,'page_offset':UINT}),
        }
        definitions=[]
        for kind,(owners,fields) in layouts.items():
            required,bindings=audits(kind,owners)
            def handle(uow,values,action=kind):
                try:return self.handle(action,uow,values)
                except OwnerFailure as failure:
                    self.causes.record(action,values,failure)
                    raise
            base=result_schema(owners,('PREPARED','REQUEST_ASSOCIATED','WAITING_REVIEW','APPROVED','KNOWN_FAILED','USER_REJECTED','REMOTE_UNKNOWN','PUBLISHED','MATERIAL_RELEASED'))
            definitions.append(ResultBoundCommandDefinition('self_model',kind,1,RecordSchema((Field('operation_id',ID),)+tuple(Field(name,shape) for name,shape in fields.items())),1,
                RecordSchema(base.fields+(Field('records',SequenceSchema(RECORD_DIGEST,0,3)),)),
                participants,required,handle,INTENT,bindings))
        self.commands=tuple(definitions)

    def bind(self,storage,configuration,runtime,initial,provider,actor):
        if self.bound or runtime.assembly.configuration is not configuration or runtime.provider is not provider:raise InvalidValue()
        self.storage=storage;self.configuration=configuration;self.runtime=runtime;self.initial=initial;self.provider=provider;self.actor=actor
        self.owner=PersonaStorage(self.catalog,storage,configuration,configuration.scope_id)
        self.provider_views=BoundStatements(self.catalog,storage,'provider')
        self.operations={d.operation_kind:storage.bind_operation(d,configuration.scope_id) for d in self.commands}
        self.bound=True;self.mode.bind(self,runtime)
        from .daily_persona_control import DailyPersonaControl
        self.control=DailyPersonaControl(self);self.port=self.control.port

    def key(self,kind,*parts):return identity(kind,self.configuration.database_id,self.configuration.scope_id,*parts)
    def operation(self,uow):
        value=self.storage.cognition_operation_context(uow,self.catalog.definition)
        return MappingProxyType({name:getattr(value,name) for name in ('owner_namespace','operation_kind','scope_id','operation_key')})
    def participate_run(self,uow,run_id):
        found=self.owner.read(uow,'run',run_id)
        if found is None:raise OwnerFailure('PRECONDITION_FAILED','persona','NOT_FOUND')
        self.verify_record_receipt(self.confirm_local(uow,record(found.value['last_operation'])),found.value)
        return found.value
    def candidate(self,uow,run):
        found=self.owner.read(uow,'candidate',cast(str,run['resolution_id']))
        if found is None:raise InvalidValue()
        matching_generation(run,found.value)
        self.verify_record_receipt(self.confirm_local(uow,self.candidate_operation(found.value)),found.value)
        return found.value

    def candidate_operation(self,candidate):
        if candidate['review_operation'] is not None:return record(candidate['review_operation'])
        return MappingProxyType({'owner_namespace':'self_model','scope_id':self.configuration.scope_id,
            'operation_kind':'record_initial_persona_resolution_with_result' if candidate['handoff_id'] is not None else 'record_initial_persona_resolution',
            'operation_key':self.key('persona-resolution',candidate['run_id'],candidate['generation'])})
    def source(self,uow,run):
        found=self.initial.participate_initial(uow,cast(str,run['input_id']))
        if any(found.input[name]!=run[name] for name in ('input_digest','self_subject_id','self_revision')):raise InvalidValue()
        return found.input
    def admit(self,uow,run=None,epoch=None):
        if self.closed or not self.bound:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        mode=self.mode.current(uow)
        if run is not None and (mode['run_id']!=run['object_id'] or mode['state']!='DREAM_FOCUSED' or mode['publication_id'] is not None):
            raise OwnerFailure('MODE_BLOCKED','mode','STATE_MISMATCH')
        if epoch is not None and mode['epoch']!=epoch:raise OwnerFailure('PRECONDITION_FAILED','mode','REVISION_CONFLICT')
        uow.require_commit_permission(lambda:not self.closed)
        return mode

    def handle(self,kind,uow,v):
        if not self.bound or self.closed:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        if kind in ('prepare_initial_persona','review_initial_persona','retry_initial_persona','publish_initial_persona'):
            if self.control.closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
            uow.require_commit_permission(lambda:not self.control.closed)
        now=time.time_ns()//1000;op=self.operation(uow);facts={};targets=[];records=[]
        if kind=='prepare_initial_persona':
            found=self.initial.participate_initial(uow,v['input_id'])
            if found.subject['revision']!=v['expected_self_revision']:raise OwnerFailure('PRECONDITION_FAILED','self','REVISION_CONFLICT')
            if self.owner.current_publication(uow) is not None:raise OwnerFailure('PRECONDITION_FAILED','persona','ALREADY_PUBLISHED')
            mode=self.admit(uow,epoch=v['expected_epoch'])
            run,material=first_run(self.configuration,self.provider,found.input,v['expected_epoch']+1,op,now)
            self.owner.stage_run(uow,run)
            changed=self.mode.effect(uow,'PREPARE',run['object_id'],v['expected_epoch'],now)
            facts['cognition']=self.materials.stage_complete(uow,material)
            facts['runtime']={'rows_changed':1,'targets':(target('instance_mode',changed['epoch'],mode['epoch']),)}
            targets.append(target(cast(str,run['object_id']),1));records.append(run);state='PREPARED'
        elif kind=='retire_initial_persona_material':
            run=self.participate_run(uow,v['run_id'])
            if run['state'] not in ('WAITING_REVIEW','APPROVED','KNOWN_FAILED','USER_REJECTED','PUBLISHED') or not self.provider.generation_consumers_ended(v['run_id']):raise InvalidValue()
            allowed={material_id(self.configuration,run['object_id'],n) for n in range(1,cast(int,run['generation'])+1)}
            result_end=cast(int,run['generation'])+1 if run['state'] in ('PUBLISHED','USER_REJECTED','KNOWN_FAILED') else cast(int,run['generation'])
            allowed.update(self.key('persona-result',run['object_id'],n) for n in range(1,result_end))
            if v['context_id'] not in allowed:raise InvalidValue()
            facts['cognition']=self.materials.retire_page(uow,v['context_id'],run['object_id'],v['page_offset'])
            return MappingProxyType(dict(result(v['operation_id'],'MATERIAL_RELEASED',facts))|{'records':()})
        else:
            run=self.participate_run(uow,v['run_id'])
            if run['revision']!=v['expected_revision'] or 'generation' in v and run['generation']!=v['generation']:
                raise OwnerFailure('PRECONDITION_FAILED','persona','REVISION_CONFLICT')
            mode=self.admit(uow,run,v.get('expected_epoch'));self.source(uow,run)
            changed=dict(run)|{'revision':cast(int,run['revision'])+1,'updated_at_us':max(now,cast(int,run['updated_at_us'])),'last_operation':op}
            if kind=='associate_initial_persona_request':
                if run['state']!='PREPARED':raise InvalidValue()
                changed.update(state='REQUEST_ASSOCIATED',mode_epoch=mode['epoch'])
                self.owner.update_run(uow,run,changed)
            elif kind=='confirm_initial_persona_request':
                if run['state']!='REQUEST_ASSOCIATED' or run['provider_request_id'] is not None:raise InvalidValue()
                self.provider.participate_original(uow,v['request_id'],role='PERSONA',owner_ref=run['object_id'],operation_key=run['provider_operation_key'],request_digest=v['request_digest'])
                changed.update(provider_request_id=v['request_id']);self.owner.update_run(uow,run,changed)
            elif kind=='mark_initial_persona_unknown':
                proof=self._observed_request
                if proof is None or run['state']!='REQUEST_ASSOCIATED':raise InvalidValue()
                request=self.provider.participate_original(uow,run['provider_request_id'],role='PERSONA',owner_ref=run['object_id'],operation_key=run['provider_operation_key'],request_digest=proof['fingerprint'])
                if request['phase']!='REMOTE_RESULT_UNKNOWN':raise InvalidValue()
                self.provider.verify_original_receipt(uow,request);changed.update(state='REMOTE_UNKNOWN');self.owner.update_run(uow,run,changed)
            elif kind.startswith('record_initial_persona_resolution'):
                changed,candidate,material=self.resolve(uow,run,changed,kind,op,now)
                self.owner.stage_resolution(uow,run,changed,candidate)
                records.append(candidate)
                targets.append(target(cast(str,candidate['object_id']),1))
                if material is not None:facts['cognition']=self.materials.stage_complete(uow,material)
            elif kind=='review_initial_persona':
                candidate=self.candidate(uow,run)
                if candidate['object_id']!=v['candidate_id']:raise InvalidValue()
                reviewed=review(run,candidate,v['expected_revision'],v['candidate_revision'],v['candidate_digest'],v['decision'],self.actor,op,now)
                self.owner.stage_review(uow,run,candidate,reviewed);changed=reviewed.run
                records.append(reviewed.candidate)
                targets.append(target(cast(str,candidate['object_id']),cast(int,reviewed.candidate['revision']),cast(int,candidate['revision'])))
            elif kind=='retry_initial_persona':
                candidate=self.candidate(uow,run)
                if run['state'] not in ('USER_REJECTED','KNOWN_FAILED') or candidate['object_id']!=v['candidate_id'] or cast(int,run['generation'])>=3:raise InvalidValue()
                if not self.provider.generation_consumers_ended(run['object_id']):raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
                self.confirm_local(uow,record(run['last_operation']))
                self.provider.verify_persona_retry(uow,run)
                generation=cast(int,run['generation'])+1
                material,account,checksum=freeze_initial(self.configuration,self.provider,self.source(uow,run),run['object_id'],generation,op,now)
                actual=self.mode.effect(uow,'RETRY',run['object_id'],v['expected_epoch'],now)
                changed.update(state='PREPARED',generation=generation,provider_operation_key=original_key(self.configuration,run['object_id'],generation),
                    provider_request_id=None,resolution_id=None,mode_epoch=actual['epoch'],binding_digest=checksum)
                self.owner.update_run(uow,run,changed);facts['cognition']=self.materials.stage_complete(uow,material)
                facts['runtime']={'rows_changed':1,'targets':(target('instance_mode',actual['epoch'],mode['epoch']),)}
            elif kind=='publish_initial_persona':
                candidate=self.candidate(uow,run)
                if run['state']!='APPROVED' or candidate['review']!='APPROVED' or candidate['object_id']!=v['candidate_id'] or candidate['revision']!=v['candidate_revision'] or candidate_digest(candidate)!=v['candidate_digest']:raise InvalidValue()
                material=self.result_material(uow,run,candidate)
                output=as_record(freeze(decode_content(material.body,40960),40960,owned=True));self.verify_candidate_output(run,candidate,output)
                for operation in (candidate['review_operation'],run['last_operation']):self.confirm_local(uow,record(operation))
                request=self.provider.participate_original(uow,candidate['provider_request_id'],role='PERSONA',owner_ref=run['object_id'],operation_key=run['provider_operation_key'],request_digest=material.manifest['model_binding_digest'])
                self.provider.verify_original_receipt(uow,request)
                elapsed=datetime.fromisoformat(cast(str,request['updated_at']))-datetime(1970,1,1,tzinfo=timezone.utc)
                generated=(elapsed.days*86400+elapsed.seconds)*1000000+elapsed.microseconds
                pid=stable_identity('persona-publication',self.configuration.database_id,self.configuration.scope_id)
                publication=isolate_publication({'format_version':1,'object_id':pid,'revision':1,'database_id':self.configuration.database_id,'instance_id':self.configuration.scope_id,
                    'config_snapshot_id':self.configuration.snapshot_id,'created_at_us':now,'run_id':run['object_id'],'generation':run['generation'],
                    'candidate_id':candidate['object_id'],'candidate_revision':candidate['revision'],'candidate_digest':candidate_digest(candidate),
                    **{k:candidate[k] for k in ('input_id','input_digest','provider_request_id','handoff_id','text','reviewed_by','review_operation')},
                    **{k:run[k] for k in ('self_subject_id','self_revision','prompt_ref','schema_ref','transform_ref')},
                    **{k:output[k] for k in ('requested_model_id','reported_model_id','resolved_model_id')},'generated_at_us':generated,'publication_operation':op})
                changed.update(state='PUBLISHED',publication_id=pid);self.owner.stage_publication(uow,run,changed,publication)
                from companion_memory.configuration.dream_persistence import StoredDreamConfiguration
                if type(self.configuration) is StoredDreamConfiguration:
                    from .unified_persona import initialize_pointer
                    pointer=initialize_pointer(self,uow,pid,'INITIAL_APPROVED',op,now)
                    targets.append(target(pointer,1))
                records.append(publication)
                actual=self.mode.effect(uow,'PUBLISH',run['object_id'],v['expected_epoch'],now,pid)
                facts['runtime']={'rows_changed':1,'targets':(target('instance_mode',actual['epoch'],mode['epoch']),)}
                targets.append(target(pid,1))
            else:raise InvalidValue()
            state=cast(str,changed['state']);targets.append(target(cast(str,run['object_id']),cast(int,changed['revision']),cast(int,run['revision'])))
            records.append(isolate_run(changed))
        counts=self.storage.transaction_row_changes(uow)
        facts['self_model']={'rows_changed':counts['self_model'],'targets':tuple(targets)}
        return MappingProxyType(dict(result(v['operation_id'],state,facts))|{'records':tuple(MappingProxyType({'object_id':value['object_id'],'revision':value['revision'],'digest':digest(value)}) for value in records)})

    @staticmethod
    def verify_record_receipt(receipt,value):
        proof=record(receipt.result)
        expected={'object_id':value['object_id'],'revision':value['revision'],'digest':digest(value)}
        if sum(item==expected for item in sequence(proof['records']))!=1:raise InvalidValue()

    async def verify_record_original(self,operation,value):
        if operation['owner_namespace']!='self_model' or operation['scope_id']!=self.configuration.scope_id or operation['operation_kind'] not in self.operations:raise InvalidValue()
        found=await self.operations[operation['operation_kind']].read_receipt(operation['operation_key'])
        if type(found) is not Found:raise InvalidValue()
        self.verify_record_receipt(found.value,value)

    def confirm_local(self,uow,operation):
        definition=next(d for d in self.commands if d.operation_kind==operation['operation_kind'])
        receipt=self.storage.confirm_prior_operation(uow,definition,operation['operation_key'])
        if receipt is None:raise InvalidValue()
        return receipt

    def result_material(self,uow,run,candidate):
        cid=self.key('persona-result',run['object_id'],run['generation'])
        root=self.materials.participate_manifest(uow,cid,run['object_id'])
        return self.materials.participate_material(uow,cid,root['payload_digest'],run['object_id'])

    @staticmethod
    def verify_candidate_output(run,candidate,output):
        value=isolate_initial_persona(output['output'],run['input_id'])
        if value['text']!=candidate['text'] or digest(value['text'])!=candidate['text_digest']:raise InvalidValue()

    def resolve(self,uow,run,changed,kind,op,now):
        if run['state'] not in ('REQUEST_ASSOCIATED','REMOTE_UNKNOWN'):raise InvalidValue()
        material=None;terminal=self._receiving;request=None;resolution='NOT_SENT';reason='NONE';text=None;handoff=None;receipt=op
        if kind.endswith('_with_result'):
            if terminal is None:raise InvalidValue()
            request=terminal.request
            self.provider.verify_daily_result(uow,terminal,role='PERSONA',owner_ref=run['object_id'],operation_key=run['provider_operation_key'],request_digest=request['fingerprint'])
            if request['object_id']!=run['provider_request_id']:raise InvalidValue()
            handoff=request['handoff_id'];receipt=self.receipt_operation(self.provider.verify_original_receipt(uow,request))
            resolution='KNOWN_FAILED';reason='INVALID_RESPONSE'
            try:text=isolate_initial_persona(terminal.value['output'],run['input_id'])['text'];resolution='SUCCEEDED';reason='NONE'
            except InvalidValue:pass
            original=self.materials.participate_manifest(uow,material_id(self.configuration,run['object_id'],run['generation']),run['object_id'])
            metadata={key:value for key,value in original.items() if key not in ('leaf_refs','payload_digest','byte_count')}
            metadata.update(object_id=self.key('persona-result',run['object_id'],run['generation']),context_kind='PROVIDER_RESULT',created_at_us=now,updated_at_us=now,wire_digest=None,
                reservation_input_bound=0,original_operation=op,model_binding_digest=request['fingerprint'])
            material=freeze_material(metadata,dump(terminal.value,40960).encode(),dream_format=self.materials.dream_format)
        elif run['provider_request_id'] is None:
            if not self.provider.participate_unsent_daily(uow,run['provider_operation_key'],run['object_id']):raise InvalidValue()
        else:
            proof=self._observed_request
            if proof is None:raise InvalidValue()
            request=self.provider.participate_original(uow,run['provider_request_id'],role='PERSONA',owner_ref=run['object_id'],operation_key=run['provider_operation_key'],request_digest=proof['fingerprint'])
            receipt=self.receipt_operation(self.provider.verify_original_receipt(uow,request))
            if request['phase']=='REMOTE_RESULT_UNKNOWN':
                raise OwnerFailure('RESULT_UNCONFIRMED','request','REMOTE_UNKNOWN')
            if request['phase']!='TERMINAL' or request['outcome']=='SUCCEEDED':raise InvalidValue()
            failure=request['first_error'];actual=as_record(failure)['reason'] if failure is not None else request['outcome']
            resolution='KNOWN_FAILED';reason=actual if actual in ('OTHER_REFUSAL','OUTPUT_LIMIT','AUTHENTICATION_FAILED','CANCELLED','TIMED_OUT','PAUSED_BUDGET','MODE_BLOCKED','CONFIGURATION_REJECTED') else 'INVALID_RESPONSE'
        cid=stable_identity('persona-candidate',self.configuration.database_id,self.configuration.scope_id,run['object_id'],run['generation'])
        candidate=isolate_candidate({'format_version':1,'object_id':cid,'revision':1,'database_id':self.configuration.database_id,'instance_id':self.configuration.scope_id,
            'config_snapshot_id':self.configuration.snapshot_id,'created_at_us':run['created_at_us'],'run_id':run['object_id'],'generation':run['generation'],
            'provider_operation_key':run['provider_operation_key'],'provider_request_id':run['provider_request_id'],'handoff_id':handoff,'terminal_receipt':receipt,
            'resolution':resolution,'failure_reason':reason,'text':text,'text_digest':digest(text) if text is not None else None,
            'input_id':run['input_id'],'input_digest':run['input_digest'],'binding_digest':run['binding_digest'],'review':'PENDING' if resolution=='SUCCEEDED' else 'NOT_APPLICABLE',
            'reviewed_by':None,'reviewed_at_us':None,'review_operation':None})
        changed.update(state='WAITING_REVIEW' if resolution=='SUCCEEDED' else 'KNOWN_FAILED',resolution_id=cid)
        return isolate_run(changed),candidate,material

    @staticmethod
    def receipt_operation(receipt):
        return MappingProxyType({name:getattr(receipt.identity,name) for name in ('owner_namespace','operation_kind','scope_id','operation_key')})

    def participate_publication(self,uow):
        found=self.owner.current_publication(uow)
        if found is None:return None
        self.verify_record_receipt(self.confirm_local(uow,record(found.value['publication_operation'])),found.value)
        run=self.participate_run(uow,found.value['run_id']);self.candidate(uow,run)
        return projection(found.value,self.initial.publication_stale(uow,found.value))

    def participate_current(self,uow,publication_id,revision):
        current=self.participate_publication(uow)
        return current if current is not None and current['publication_id']==publication_id and current['revision']==revision else None

    async def read_current(self,deadline=None):
        deadline=time.monotonic()+5 if deadline is None else deadline
        found=await self.owner.current_original(deadline)
        if found is not None:await self.verify_record_original(record(found.value['publication_operation']),found.value)
        return NotFound() if found is None else Found(projection(found.value,await self.initial.publication_stale_original(found.value,deadline)))

    def authorize_request(self,request,uow):
        sending=self._sending
        if self.closed or not self.bound or sending is None:return False
        run,root=sending;description=request.description
        if (request.binding.role!='PERSONA' or description['work_id']!=run['object_id'] or description['original_request_key']!=run['provider_operation_key']
                or description['material_id']!=root['object_id'] or description['material_digest']!=root['payload_digest']
                or description['deadline_at_us']!=cast(int,root['created_at_us'])+60000000 or time.time_ns()//1000>=description['deadline_at_us']
                or run['state']!='REQUEST_ASSOCIATED' or run['provider_request_id'] is not None or self.runtime.gate.state!='DREAM_FOCUSED'
                or self.runtime.gate.epoch!=run['mode_epoch']):return False
        if uow is not None:
            rows=self.provider_views.stage('provider_read_initial_persona_runs',uow,{'caller_scope':self.configuration.scope_id,'object_id':run['object_id']})
            if len(rows)!=1 or isolate_run(decode_row(rows[0],RUN,4096,self.configuration.database_id,self.configuration.scope_id,self.configuration.snapshot_id))!=run:return False
        return True

    def explicit_send(self,key):
        """Expose only the original key of the active explicit initialization call."""
        return (self.bound and not self.closed and not self.control.closed and self.cleanup_failure is None and self._sending is not None
            and self._sending[0]['provider_operation_key']==key and self.runtime.gate.state=='DREAM_FOCUSED')

    def verify_received(self,uow,request,handoff,proof):
        if not self.bound or request['task_role']!='PERSONA' or request['result_owner']!='self_model':return False
        run_id=record(request['attribution'])['run_id']
        rows=self.provider_views.stage('provider_read_initial_persona_runs',uow,{'caller_scope':self.configuration.scope_id,'object_id':run_id})
        if len(rows)!=1:return False
        run=isolate_run(decode_row(rows[0],RUN,4096,self.configuration.database_id,self.configuration.scope_id,self.configuration.snapshot_id))
        if run['provider_request_id']!=request['object_id'] or run['resolution_id'] is None:return False
        rows=self.provider_views.stage('provider_read_initial_persona_candidates',uow,{'caller_scope':self.configuration.scope_id,'object_id':run['resolution_id']})
        if len(rows)!=1:return False
        candidate=isolate_candidate(decode_row(rows[0],CANDIDATE,4096,self.configuration.database_id,self.configuration.scope_id,self.configuration.snapshot_id))
        matching_generation(run,candidate)
        if candidate['handoff_id']!=handoff['object_id'] or proof['kind']!='record_initial_persona_resolution_with_result' or proof['key']!=self.key('persona-resolution',run_id,run['generation']):return False
        definition=next(d for d in self.commands if d.operation_kind==proof['kind'])
        receipt=self.storage.confirm_cognition_consumer_operation(uow,definition,proof['key'],request['object_id'])
        if receipt is None or receipt.fingerprint!=proof['fingerprint']:return False
        self.materials.participate_material(uow,self.key('persona-result',run_id,run['generation']),handoff['checksum'],run_id)
        return True

    def close(self):
        self.closed=True
        if not self.bound:return True
        self.control.closed=True
        return self._job is None and self._task is None and self._sending is None and self._receiving is None and self.owner.close()
