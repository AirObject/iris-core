"""Independent bounded semantic comparison after deterministic goal processing.

The original task is already NEEDS_SEMANTIC_REVIEW before comparison begins.
One complete frozen candidate group is compared once. Any later goal, source or
reminder race keeps the original goals and records a local unresolved outcome.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
import asyncio
from collections.abc import Callable
from hashlib import sha256
import time
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.cognition.daily_material import freeze_material,FrozenDailyMaterial
from companion_memory.cognition.daily_material_storage import DailyMaterialStorage
from companion_memory.cognition.daily_resources import prompt_resource,output_schema
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand,UnitOfWork,RepositoryDefinition,Found,Committed,NotCommitted
from companion_memory.persistence.daily_records import DailyRows,identity,Record,ID,REVISION,UINT
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.semantic_records import record,REQUEST,RECEIPT,H
from companion_memory.persistence.owned_statements import StatementCatalog,BoundStatements,OwnerFailure
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.ingress.events import plain as thaw
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope,check_deadline
from companion_memory.provider.daily_service import DailyProvider,DailyResult
from companion_memory.provider.daily_execution import DailyRequest
from companion_memory.provider.daily_protocol import DailyChatBinding,encode_daily_request
from companion_memory.provider.values import Record as ProviderRecord,plain,freeze
from .service import GoalsService,GoalTransaction
from .semantic_records import TABLE,decision_output
from .semantic_merge import digest,apply_merge

class GoalComparisons:
    """A component of the existing goals owner, with no second writer lease."""
    def __init__(self,catalog:StatementCatalog,materials:DailyMaterialStorage,provider_repository:RepositoryDefinition):
        if catalog.definition.schema_version!=5 or type(materials) is not DailyMaterialStorage:raise InvalidValue()
        self.catalog=catalog;self.materials=materials;self._bound=False;self._closed=False;self._task:asyncio.Task|None=None
        self._drive_task:asyncio.Task|None=None;self._sending:Record|None=None;self._receiving:tuple[DailyResult,str]|None=None
        self._recovery_after=''
        self.cleanup_failure:tuple[str,OwnerFailure]|None=None
        from companion_memory.persistence.owned_statements import OwnerCauses
        self.causes=OwnerCauses()
        layouts={
            'prepare_goal_comparison':record(operation_id=ID,task_id=ID,expected_task_revision=REVISION,goal_id=ID,expected_goal_revision=REVISION,started_at_us=UINT),
            'associate_goal_comparison':record(operation_id=ID,decision_id=ID,expected_revision=REVISION,request_ref=REQUEST,request_digest=H),
            'store_goal_comparison':record(operation_id=ID,decision_id=ID,expected_revision=REVISION,request_digest=H),
            'finish_goal_comparison':record(operation_id=ID,decision_id=ID,expected_revision=REVISION,request_digest=H),
            'apply_goal_comparison':record(operation_id=ID,decision_id=ID,expected_revision=REVISION),
            'expire_goal_comparison':record(operation_id=ID,decision_id=ID,expected_revision=REVISION),
            'retire_goal_material':record(operation_id=ID,decision_id=ID,context_id=ID,page_offset=UINT)}
        definitions=[]
        for kind,schema in layouts.items():
            owners=('goals','cognition') if kind in ('prepare_goal_comparison','store_goal_comparison','retire_goal_material') else ('goals',)
            required,bindings=audits(kind,owners)
            def handle(uow:UnitOfWork,value:Record,action=kind):
                try:return self._handle(action,uow,value)
                except OwnerFailure as failure:
                    self.causes.record(action,value,failure)
                    raise
            definitions.append(ResultBoundCommandDefinition('goals',kind,1,schema,1,
                result_schema(owners,('PREPARED','ASSOCIATED','RESULT_STORED','APPLIED','UNRESOLVED','DEDUP_CONFLICT','MATERIAL_RELEASED')),
                (catalog.definition,materials.catalog.definition,provider_repository),required,handle,INTENT,bindings))
        self.commands=tuple(definitions)

    async def observation(self,after:str='') -> Record:
        """Project decision progress only; frozen goal bodies and reasons stay private."""
        if not self._bound or self._closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        if type(after) is not str or len(after.encode())>128:raise InvalidValue()
        page=await self.rows.page('semantic_decisions',after)
        keys=('object_id','revision','task_id','goal_id','state','decision','provider_request_id')
        return MappingProxyType({'items':tuple(MappingProxyType({k:r[k] for k in keys}) for r in page),
            'next_after':page[-1]['object_id'] if len(page)==4 else None,
            'cleanup_pending':self._task is not None or self._drive_task is not None or self.cleanup_failure is not None})

    def bind(self,goals:GoalsService,configuration:StoredCognitionConfiguration,provider:DailyProvider,normal:Callable[[],None]):
        if self._bound or type(goals) is not GoalsService or type(provider) is not DailyProvider or stored_cognition_configuration_issue(configuration) is not None or goals.configuration is not configuration or provider.configuration is not configuration:
            raise InvalidValue()
        self.goals=goals;self.configuration=configuration;self.provider=provider;self.storage=provider.storage;self.normal=normal
        self.rows=DailyRows(self.catalog,(TABLE,),self.storage,configuration.database_id,configuration.scope_id,configuration.snapshot_id)
        self._provider_view=BoundStatements(self.catalog,self.storage,'provider')
        self.operations={d.operation_kind:self.storage.bind_operation(d,configuration.scope_id) for d in self.commands}
        r=configuration.candidate.text.record('goals.semantic_deduplication')
        self.protocol=DailyChatBinding('GOAL_DEDUP','deepseek-flash',cast(str,r['schema_ref']),cast(str,r['schema_digest']),output_schema('GOAL_DEDUP'),cast(str,r['prompt_digest']),prompt_resource('GOAL_DEDUP'))
        self._bound=True

    def _operation(self,uow:UnitOfWork):
        value=self.storage.cognition_operation_context(uow,self.catalog.definition)
        return MappingProxyType({'owner_namespace':value.owner_namespace,'operation_kind':value.operation_kind,'scope_id':value.scope_id,'operation_key':value.operation_key})

    def _identity(self,kind:str,*parts):
        return identity(kind,self.configuration.database_id,self.configuration.scope_id,*parts)

    def _root(self,uow:UnitOfWork,decision_id:str,expected:int) -> Record:
        value=self.rows.get('semantic_decisions',uow,decision_id)
        if value is None or value['revision']!=expected:raise OwnerFailure('CONFLICT','decision','REVISION_CONFLICT')
        return value

    def _handle(self,kind:str,uow:UnitOfWork,v:Record):
        if not self._bound or self._closed:raise OwnerFailure('INVALID_STATE','goal','NOT_READY')
        if kind=='prepare_goal_comparison':return self._prepare(uow,v)
        if kind=='retire_goal_material':
            decision=self.rows.get('semantic_decisions',uow,cast(str,v['decision_id']))
            if decision is None or decision['state'] not in ('APPLIED','UNRESOLVED') or decision['terminal_operation'] is None or self._sending is not None or self._receiving is not None or not self.provider.generation_consumers_ended(cast(str,decision['object_id'])):raise InvalidValue()
            facts=self.materials.retire_page(uow,cast(str,v['context_id']),cast(str,decision['object_id']),cast(int,v['page_offset']))
            previous=cast(int,decision['revision'])
            self.rows.write('semantic_decisions',uow,dict(decision)|{'revision':previous+1,'updated_at_us':max(time.time_ns()//1000,cast(int,decision['updated_at_us']))},previous)
            return result(cast(str,v['operation_id']),'MATERIAL_RELEASED',{'cognition':facts,'goals':{'rows_changed':1,'targets':(target(cast(str,decision['object_id']),previous+1,previous),)}})
        decision=self._root(uow,cast(str,v['decision_id']),cast(int,v['expected_revision']));before=cast(int,decision['revision']);now=time.time_ns()//1000
        current=dict(decision)|{'revision':before+1,'updated_at_us':max(now,cast(int,decision['updated_at_us']))};state='ASSOCIATED';cognition=None
        if kind=='expire_goal_comparison':
            if decision['state']!='PREPARED' or now<cast(int,decision['deadline_at_us']) or not self.provider.participate_unsent_daily(uow,cast(str,decision['provider_operation_key']),cast(str,decision['object_id'])):raise InvalidValue()
            state='UNRESOLVED';current.update(state=state,reason='DEADLINE_EXCEEDED',terminal_operation=self._operation(uow))
        elif kind=='associate_goal_comparison':
            if decision['state']!='PREPARED':raise InvalidValue()
            request=self.provider.participate_original(uow,cast(str,cast(Record,v['request_ref'])['request_id']),role='GOAL_DEDUP',owner_ref=cast(str,decision['object_id']),
                operation_key=cast(str,decision['provider_operation_key']),request_digest=cast(str,v['request_digest']))
            if cast(Record,v['request_ref'])['attempt_id']!=self._request_attempt(request['object_id']):raise InvalidValue()
            current.update(state='ASSOCIATED',provider_request_id=cast(str,request['object_id']))
        elif kind=='store_goal_comparison':
            active=self._receiving
            if active is None or active[1]!=decision['object_id'] or decision['state']!='ASSOCIATED':raise InvalidValue()
            terminal=active[0]
            self.provider.verify_daily_result(uow,terminal,role='GOAL_DEDUP',owner_ref=cast(str,decision['object_id']),operation_key=cast(str,decision['provider_operation_key']),request_digest=cast(str,v['request_digest']))
            if terminal.request['object_id']!=decision['provider_request_id']:raise InvalidValue()
            current['handoff_id']=cast(str,terminal.handoff['object_id'])
            cognition=self._store_original_output(uow,decision,terminal,now)
            try:
                output=decision_output(encode_content(cast(Record,terminal.value['output']),2048))
                if output['decision']=='MERGE' and output['canonical_id'] not in tuple(c['goal_id'] for c in cast(tuple[Record,...],decision['candidates'])):raise InvalidValue()
                current.update({k:output[k] for k in ('decision','canonical_id','reason')});current['state']='RESULT_STORED';state='RESULT_STORED'
            except InvalidValue:
                current.update(state='UNRESOLVED',terminal_operation=self._operation(uow));state='UNRESOLVED'
        elif kind=='finish_goal_comparison':
            if decision['state']!='ASSOCIATED':raise InvalidValue()
            request=self.provider.participate_original(uow,cast(str,decision['provider_request_id']),role='GOAL_DEDUP',owner_ref=cast(str,decision['object_id']),operation_key=cast(str,decision['provider_operation_key']),request_digest=cast(str,v['request_digest']))
            if request['phase'] not in ('TERMINAL','REMOTE_RESULT_UNKNOWN') or request['outcome']=='SUCCEEDED':raise InvalidValue()
            self.provider.verify_original_receipt(uow,request)
            state='UNRESOLVED';current.update(state=state,terminal_operation=self._operation(uow))
        elif kind=='apply_goal_comparison':
            if decision['state']!='RESULT_STORED':raise InvalidValue()
            return self._apply(uow,v,decision,current,now)
        else:raise InvalidValue()
        self.rows.write('semantic_decisions',uow,current,before)
        facts:dict[str,object]={'goals':{'rows_changed':1,'targets':(target(cast(str,decision['object_id']),before+1,before),)}}
        if cognition is not None:facts['cognition']=cognition
        return result(cast(str,v['operation_id']),state,facts)

    def _store_original_output(self,uow:UnitOfWork,decision:Record,terminal:DailyResult,now:int):
        from companion_memory.provider.values import dump
        material_id=self._identity('goal-result-context',decision['object_id'],cast(str,terminal.handoff['object_id']))
        metadata={'format_version':1,'object_id':material_id,'revision':1,'database_id':self.configuration.database_id,'instance_id':self.configuration.scope_id,
            'config_snapshot_id':self.configuration.snapshot_id,'created_at_us':now,'updated_at_us':now,'context_version':3 if self.materials.dream_format else 2,'context_kind':'PROVIDER_RESULT',
            'owner_ref':decision['object_id'],'batch_id':None,'run_id':None,'source_id':None,'state':'STORED','persona_publication_id':None,'persona_revision':None,
            'prompt_ref':None,'schema_ref':self.protocol.schema_ref,'transform_ref':None,'model_binding_digest':terminal.request['profile_revision'],
            'ordered_members':(),'related_objects':(),'wire_digest':None,'input_token_estimate':None,'reservation_input_bound':0,
            'original_operation':self._operation(uow),'terminal_operation':None}
        metadata['model_binding_digest']=sha256(cast(str,terminal.request['profile_revision']).encode()).hexdigest()
        return self.materials.stage_complete(uow,freeze_material(metadata,dump(terminal.value,40960).encode(),dream_format=self.materials.dream_format))

    def verify_received(self,uow:UnitOfWork,request:ProviderRecord,handoff:ProviderRecord,proof:Record) -> bool:
        """Provider may retire only the exact complete original goals reception."""
        from companion_memory.provider.values import as_record
        if request['task_role']!='GOAL_DEDUP' or request['result_owner']!='goals':return False
        decision_id=cast(str,as_record(request['attribution'])['run_id'])
        values=self._provider_view.stage('provider_read_decision',uow,{'caller_scope':self.configuration.scope_id,'object_id':decision_id})
        if len(values)!=1:return False
        decision=self.rows.decode('semantic_decisions',values[0])
        if (decision['provider_request_id']!=request['object_id'] or decision['handoff_id']!=request['handoff_id']
                or decision['state'] not in ('RESULT_STORED','APPLIED','UNRESOLVED') or proof['kind']!='store_goal_comparison'
                or proof['key']!=self._identity('store-goal',decision_id)):return False
        definition=next(d for d in self.commands if d.operation_kind=='store_goal_comparison')
        receipt=self.storage.confirm_cognition_consumer_operation(uow,definition,cast(str,proof['key']),cast(str,request['object_id']))
        if receipt is None or receipt.fingerprint!=proof['fingerprint']:return False
        material_id=self._identity('goal-result-context',decision_id,cast(str,handoff['object_id']))
        self.materials.participate_material(uow,material_id,cast(str,handoff['checksum']),decision_id)
        return True

    @staticmethod
    def _request_attempt(request_id) -> str:
        from companion_memory.persistence.semantic_records import identity as provider_identity
        return provider_identity('daily-attempt',request_id,1)

    def _prepare(self,uow:UnitOfWork,v:Record):
        self.normal();uow.require_commit_permission(self._allowed)
        tx=GoalTransaction(self.goals,uow,cast(int,v['started_at_us']))
        task=tx.get('dedup_task',{'task_id':v['task_id']});goal=tx.get('goal',{'goal_id':v['goal_id']})
        if (task['revision']!=v['expected_task_revision'] or goal['revision']!=v['expected_goal_revision'] or task['goal_id']!=goal['goal_id']
                or task['status']!='NEEDS_SEMANTIC_REVIEW' or goal['status']!='OPEN' or goal['canonical_id']!=goal['goal_id']):raise OwnerFailure('CONFLICT','goal','DEDUP_CONFLICT')
        candidates,truncated=self.goals.structural_candidates(uow,goal)
        if not candidates:raise OwnerFailure('PRECONDITION_FAILED','goal','NO_CANDIDATES')
        did=self._identity('goal-decision',task['task_id']);cid=self._identity('learning-context',did)
        if self.rows.get('semantic_decisions',uow,did) is not None:raise OwnerFailure('PRECONDITION_FAILED','decision','NO_CHANGE')
        def complete(root):return {'goal':root,'sources':tx.children('source',{'goal_id':root['goal_id']})}
        body=encode_content(MappingProxyType({'schema_version':1,'task':task,'target':MappingProxyType(complete(goal)),
            'candidates':tuple(MappingProxyType(complete(c)) for c in candidates),'truncated':truncated}),262144)
        wire=encode_daily_request(self.protocol,body.decode());now=cast(int,v['started_at_us']);operation=self._operation(uow)
        base={'format_version':1,'revision':1,'database_id':self.configuration.database_id,'instance_id':self.configuration.scope_id,'config_snapshot_id':self.configuration.snapshot_id,
            'created_at_us':now,'updated_at_us':now}
        metadata={**base,'object_id':cid,'context_version':3 if self.materials.dream_format else 2,'context_kind':'GOAL_DEDUP','owner_ref':did,'batch_id':None,'run_id':None,'source_id':None,
            'state':'STORED','persona_publication_id':None,'persona_revision':None,'prompt_ref':self.configuration.candidate.text.record('goals.semantic_deduplication')['prompt_ref'],
            'schema_ref':self.protocol.schema_ref,'transform_ref':None,'model_binding_digest':sha256(encode_content((self.protocol.requested_model,self.protocol.prompt_digest,self.protocol.schema_digest),8192)).hexdigest(),
            'ordered_members':(),'related_objects':(),'wire_digest':sha256(wire).hexdigest(),'input_token_estimate':None,'reservation_input_bound':262144,
            'original_operation':operation,'terminal_operation':None}
        material=freeze_material(metadata,body,dream_format=self.materials.dream_format);cognition=self.materials.stage_complete(uow,material)
        self.rows.write('semantic_decisions',uow,{**base,'object_id':did,'task_id':task['task_id'],'goal_id':goal['goal_id'],'goal_revision':goal['revision'],'state':'PREPARED',
            'candidates':tuple({'goal_id':c['goal_id'],'revision':c['revision'],'digest':digest(c)} for c in candidates),'material_id':cid,'material_digest':sha256(body).hexdigest(),
            'provider_operation_key':self._identity('goal-provider',did),'provider_request_id':None,'handoff_id':None,'decision':None,'canonical_id':None,'reason':None,
            'deadline_at_us':now+60000000,'original_operation':operation,'terminal_operation':None})
        return result(cast(str,v['operation_id']),'PREPARED',{'goals':{'rows_changed':1,'targets':(target(did,1),)},'cognition':cognition})

    def _allowed(self) -> bool:
        try:self.normal();return not self._closed
        except OwnerFailure:return False

    def authorize_request(self,request:DailyRequest,uow:UnitOfWork|None) -> bool:
        """Provider registration consumes this exact native decision and its deadline."""
        expected=self._sending
        if (expected is None or request.binding.role!='GOAL_DEDUP' or request.description['work_id']!=expected['object_id']
                or request.description['original_request_key']!=expected['provider_operation_key'] or request.description['material_id']!=expected['material_id']
                or request.description['material_digest']!=expected['material_digest'] or request.description['deadline_at_us']!=expected['deadline_at_us']
                or expected['state']!='PREPARED' or time.time_ns()//1000>=cast(int,expected['deadline_at_us']) or not self._allowed()):return False
        if uow is not None:
            values=self._provider_view.stage('provider_read_decision',uow,{'caller_scope':self.configuration.scope_id,'object_id':expected['object_id']})
            if len(values)!=1 or self.rows.decode('semantic_decisions',values[0])!=expected:return False
        return True

    def _apply(self,uow:UnitOfWork,v:Record,decision:Record,current:dict,now:int):
        original=self.materials.participate_material(uow,cast(str,decision['material_id']),cast(str,decision['material_digest']),cast(str,decision['object_id']))
        raw=decode_content(original.body,262144)
        if type(raw) is not dict:raise InvalidValue()
        from companion_memory.information.records import checked
        from .records import DAILY_GOAL,DAILY_DEDUP_TASK
        original_task=checked(DAILY_DEDUP_TASK,raw['task'],4096);original_goal=checked(DAILY_GOAL,raw['target']['goal'],4096)
        tx=GoalTransaction(self.goals,uow,now);task=tx.get('dedup_task',{'task_id':decision['task_id']});goal=tx.get('goal',{'goal_id':decision['goal_id']})
        changed=();conflict=not self._allowed() or now>=cast(int,decision['deadline_at_us']) or task!=original_task or goal!=original_goal
        if not conflict:
            conflict=thaw(tx.children('source',{'goal_id':goal['goal_id']}))!=raw['target']['sources']
        if not conflict:
            uow.require_commit_permission(self._allowed)
        if not conflict and decision['decision']=='MERGE':
            kept=next((c for c in raw['candidates'] if c['goal']['goal_id']==decision['canonical_id']),None)
            if kept is None:raise InvalidValue()
            canonical=tx.get('goal',{'goal_id':decision['canonical_id']});frozen=checked(DAILY_GOAL,kept['goal'],4096)
            source_conflict=thaw(tx.children('source',{'goal_id':canonical['goal_id']}))!=kept['sources']
            result_roots=None if source_conflict else apply_merge(self.goals,uow,now,task,goal,canonical,original_task,original_goal,frozen)
            conflict=result_roots is None;changed=result_roots or ()
        elif not conflict and decision['decision']=='DISTINCT':
            changed=(tx.update('goal',goal,dedup_state='DISTINCT',updated_at=now),tx.update('dedup_task',task,status='DISTINCT'))
        state='DEDUP_CONFLICT' if conflict else 'UNRESOLVED' if decision['decision']=='UNSURE' else 'APPLIED'
        current.update(state='UNRESOLVED' if state in ('DEDUP_CONFLICT','UNRESOLVED') else 'APPLIED',terminal_operation=self._operation(uow))
        self.rows.write('semantic_decisions',uow,current,cast(int,decision['revision']))
        targets=[target(cast(str,decision['object_id']),cast(int,current['revision']),cast(int,decision['revision']))]
        for root in changed:
            key=cast(str,root.get('goal_id') if 'canonical_id' in root else root['task_id'])
            targets.append(target(key,cast(int,root['revision']),cast(int,root['revision'])-1))
        return result(cast(str,v['operation_id']),state,{'goals':{'rows_changed':self.storage.transaction_row_changes(uow)['goals'],'targets':tuple(targets)}})

    async def execute(self,kind:str,key:str,payload:dict):
        """Retain each original local command and its actual SQLite completion."""
        if self._task is not None or self._closed:raise OwnerFailure('RESOURCE_BUSY','goal','CLEANUP_PENDING',True)
        definition=next(d for d in self.commands if d.operation_kind==kind)
        command=ResultBoundCommand(1,plain(freeze({'operation_id':key,**payload},8192,owned=True)),{r.event_slot:{'actor':'daily_goals'} for r in definition.required_audits})
        async def run():
            outcome,cause=await self.causes.execute_original(self.operations[kind],definition,key,command)
            if type(outcome) is NotCommitted and cause is not None:
                from companion_memory.information.errors import InformationNotCommitted,InformationError
                return InformationNotCommitted(InformationError(cause.code,kind,cause.field,cause.reason,cause.cleanup_pending or outcome.error is not None and outcome.error.cleanup_pending))
            return outcome
        task,logical=start_owned(run());self._task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=5)
        if not done:raise OwnerFailure('TIMEOUT','goal','DEADLINE_EXCEEDED',True)
        return logical.result()

    async def prepare(self,key:str,task_id:str,task_revision:int,goal_id:str,goal_revision:int):
        """Create the independent 60-second comparison only after the old task ended."""
        original=await self.rows.read('semantic_decisions',self._identity('goal-decision',task_id))
        started=time.time_ns()//1000 if original is None else original['created_at_us']
        return await self.execute('prepare_goal_comparison',key,{'task_id':task_id,'expected_task_revision':task_revision,
            'goal_id':goal_id,'expected_goal_revision':goal_revision,'started_at_us':started})

    def decision_id(self,task_id:str) -> str:
        """Name the one original task comparison without granting or creating it."""
        return self._identity('goal-decision',task_id)

    async def recover(self):
        """Continue bounded original local work; initialization never grants a send."""
        while True:
            check_deadline()
            page=await self.rows.page('semantic_decisions',self._recovery_after)
            if not page:return
            for decision in page:
                observed=await self.drive(cast(str,decision['object_id']),allow_first_send=False)
                if type(observed) not in (Found,Committed):return observed
                if type(observed) is Found and observed.value.get('cleanup_pending'):return observed
                self._recovery_after=cast(str,decision['object_id'])

    async def drive(self,decision_id:str,*,allow_first_send:bool):
        """Join one complete comparison; recovery never grants a first send."""
        if self._drive_task is not None or self._closed:raise OwnerFailure('RESOURCE_BUSY','goal','CLEANUP_PENDING',True)
        async def advance():
            outcome=await self._drive(decision_id,allow_first_send)
            current=await self.rows.read('semantic_decisions',decision_id)
            if current is None:raise InvalidValue()
            if current['state']=='PREPARED' and time.time_ns()//1000>=cast(int,current['deadline_at_us']):
                outcome=await self.execute('expire_goal_comparison',self._identity('expire-goal',decision_id),{'decision_id':decision_id,'expected_revision':current['revision']})
                current=await self.rows.read('semantic_decisions',decision_id)
            if current is not None and current['state'] in ('APPLIED','UNRESOLVED'):
                try:
                    retired=await self.retire_material(current)
                    if type(retired) is not Found:raise OwnerFailure('RESULT_UNCONFIRMED','material','COMMIT_UNCONFIRMED',True)
                except (OwnerFailure,InvalidValue) as error:
                    failure=error if isinstance(error,OwnerFailure) else OwnerFailure('STORAGE_FAILED','material','INTEGRITY_FAILURE')
                    self.cleanup_failure=(decision_id,failure)
                    if self.provider.network is not None:self.provider.network.pause()
                    if type(outcome) is Committed:receipt=outcome.receipt
                    else:
                        operation=cast(Record,current['terminal_operation'])
                        original=await self.operations[cast(str,operation['operation_kind'])].read_receipt(cast(str,operation['operation_key']))
                        if type(original) is not Found:raise failure
                        receipt=original.value
                    return Found(MappingProxyType({'state':'COMMITTED','commit_id':receipt.commit_id,
                        'operation_key':receipt.identity.operation_key,'cleanup_state':'FAILED','cleanup_pending':failure.cleanup_pending,
                        'cleanup_error':MappingProxyType({'code':failure.code,'field':failure.field,'reason':failure.reason}),'new_sends':0}))
                if self.cleanup_failure is not None and self.cleanup_failure[0]==decision_id:self.cleanup_failure=None
            return outcome
        task,logical=start_owned(advance());self._drive_task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._drive_task is job:self._drive_task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=60)
        if not done:return Found(MappingProxyType({'state':'PENDING','cleanup_pending':True}))
        return logical.result()

    async def _drive(self,decision_id:str,allow_first_send:bool):
        if self.materials.versions is not None:
            decision = await self.rows.read('semantic_decisions', decision_id)
            if decision is None:raise InvalidValue()
            version = await self.materials.versions.required(cast(str, decision['material_id']), decision_id)
            with self.materials.versions.versions.use(version):
                return await self._drive_selected(decision_id, allow_first_send)
        return await self._drive_selected(decision_id, allow_first_send)

    async def _drive_selected(self,decision_id:str,allow_first_send:bool):
        from companion_memory.persistence import NotFound
        decision=await self.rows.read('semantic_decisions',decision_id)
        if decision is None:raise InvalidValue()
        if decision['state'] in ('APPLIED','UNRESOLVED','RESULT_STORED') and decision['handoff_id'] is not None:
            cleaned=await self._cleanup_received(decision)
            if type(cleaned) is not Found:return cleaned
            if cleaned.value.get('cleanup_pending'):return cleaned
        if decision['state'] in ('APPLIED','UNRESOLVED'):return Found(decision)
        if decision['state']=='RESULT_STORED':
            return await self.execute('apply_goal_comparison',self._identity('apply-goal',decision_id),{'decision_id':decision_id,'expected_revision':decision['revision']})
        remaining=(cast(int,decision['deadline_at_us'])-time.time_ns()//1000)/1000000
        lease=await self.materials.borrow(cast(str,decision['material_id']),cast(str,decision['material_digest']),decision_id,time.monotonic()+max(5,remaining))
        request=None
        try:
            material=decode_content(lease.material.body,262144)
            if type(material) is not dict:raise InvalidValue()
            request=self.provider.generation_request('GOAL_DEDUP',lease,cast(str,decision['provider_operation_key']),cast(int,decision['deadline_at_us']),
                (material['target']['goal']['entry_id'],))
            original=await self.provider.lookup_daily(cast(str,decision['provider_operation_key']),request.fingerprint,time.monotonic()+5)
            if type(original) is NotFound:
                if not allow_first_send or remaining<=0:return Found(MappingProxyType({'state':'WAITING_ADMISSION','new_sends':0}))
                if self.provider.network is None:raise InvalidValue()
                absolute=time.monotonic()+max(0,(cast(int,decision['deadline_at_us'])-time.time_ns()//1000)/1000000)
                try:await self.provider.network.wait_quiet(absolute)
                except OwnerFailure as failure:
                    if failure.code!='TIMEOUT' or failure.reason!='DEADLINE_EXCEEDED':raise
                    # Quiet admission can predict failure before the deadline.
                    # Keep this original logical owner until its existing expiry;
                    # no network slot, credential or later request is acquired.
                    await asyncio.sleep(max(0,absolute-time.monotonic()))
                    return Found(MappingProxyType({'state':'WAITING_ADMISSION','new_sends':0}))
                self._sending=decision
                sent=await self.provider.send_generation(request)
                if type(sent) is not Committed:
                    original=await self.provider.lookup_daily(cast(str,decision['provider_operation_key']),request.fingerprint,time.monotonic()+5)
                    if type(original) is not Found:return sent
                else:original=await self.provider.lookup_daily(cast(str,decision['provider_operation_key']),request.fingerprint,time.monotonic()+5)
            if type(original) is not Found:raise InvalidValue()
            observed=original.value
            if decision['state']=='PREPARED':
                associated=await self.execute('associate_goal_comparison',self._identity('associate-goal',decision_id),{'decision_id':decision_id,'expected_revision':decision['revision'],
                    'request_ref':{'request_id':observed['object_id'],'attempt_id':request.attempt_id},'request_digest':request.fingerprint})
                if type(associated) is not Committed:return associated
                decision=await self.rows.read('semantic_decisions',decision_id)
                if decision is None:raise InvalidValue()
            if observed['phase'] in ('TERMINAL','REMOTE_RESULT_UNKNOWN') and observed['outcome']!='SUCCEEDED':
                return await self.execute('finish_goal_comparison',self._identity('finish-goal',decision_id),{'decision_id':decision_id,'expected_revision':decision['revision'],'request_digest':request.fingerprint})
            if observed['phase']!='TERMINAL' or observed['outcome']!='SUCCEEDED':
                return Found(MappingProxyType({'state':observed['phase'],'outcome':observed['outcome'],'new_sends':0}))
            terminal=await self.provider.recover_daily_result(cast(str,observed['object_id']),time.monotonic()+5)
            self._receiving=terminal,decision_id
            try:
                stored=await self.execute('store_goal_comparison',self._identity('store-goal',decision_id),{'decision_id':decision_id,'expected_revision':decision['revision'],'request_digest':request.fingerprint})
                if type(stored) is not Committed:return stored
            finally:
                # Actual completion owns the native result until the SQL task ends.
                if self._task is not None:await asyncio.wait((self._task,))
                self.provider.release_daily_result(terminal);self._receiving=None
            decision=await self.rows.read('semantic_decisions',decision_id)
            if decision is None:raise InvalidValue()
            cleaned=await self.provider.cleanup_daily(cast(str,observed['object_id']),stored.receipt,time.monotonic()+5)
            if type(cleaned) is not Found:return cleaned
            if cleaned.value.get('cleanup_pending'):return cleaned
            if decision['state']=='UNRESOLVED':return Found(decision)
            return await self.execute('apply_goal_comparison',self._identity('apply-goal',decision_id),{'decision_id':decision_id,'expected_revision':decision['revision']})
        finally:
            if request is not None:
                await self.provider.wait_generation_actual(request)
                # Unsent request descriptions need an explicit local release.
                self.provider.release_unused_generation(request)
            self._sending=None;self.materials.release_reader(lease)
            await self.provider.reconcile_daily_network()

    async def _cleanup_received(self,decision:Record):
        """Resume original consumption/retirement after any process exit window."""
        key=self._identity('store-goal',decision['object_id'])
        found=await self.operations['store_goal_comparison'].read_receipt(key)
        if type(found) is not Found:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
        return await self.provider.cleanup_daily(cast(str,decision['provider_request_id']),found.value,time.monotonic()+5)

    async def retire_material(self,decision:Record):
        """Release original input/output pages only after actual goal consumption."""
        did=cast(str,decision['object_id']);contexts=[cast(str,decision['material_id'])]
        if decision['handoff_id'] is not None:contexts.append(self._identity('goal-result-context',did,cast(str,decision['handoff_id'])))
        for cid in contexts:
            root=await self.materials.rows.read('learning_contexts',cid)
            if root is None:raise InvalidValue()
            self.materials.begin_retirement(cid)
            try:
                for offset in range(0,len(cast(tuple,root['leaf_refs'])),4):
                    done=await self.execute('retire_goal_material',self._identity('goal-retire',cid,offset),{'decision_id':did,'context_id':cid,'page_offset':offset})
                    if type(done) is not Committed:return done
            finally:
                if self._task is not None:await asyncio.wait((self._task,))
                self.materials.end_retirement(cid)
        return Found(MappingProxyType({'state':'RELEASED','cleanup_pending':False,'new_sends':0}))

    def close(self) -> bool:
        self._closed=True
        return self._task is None and self._drive_task is None and self._sending is None and self._receiving is None
