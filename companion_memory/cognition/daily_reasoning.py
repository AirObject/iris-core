"""Durable bounded reasoning turns and original read-only tool receptions.

Every request material and complete result is stored before a later turn can
use it. Native Provider results and actual tool jobs are retained through their
consumer transaction. This owner cannot publish memory or launch HTTP itself.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, stored_cognition_configuration_issue
import asyncio
from hashlib import sha256
import time
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand,UnitOfWork,NotCommitted,Committed,Found
from companion_memory.persistence.daily_records import DailyRows,ID,REVISION,DIGEST,UINT,identity,Record
from companion_memory.persistence.semantic_records import record as schema
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.owned_statements import BoundStatements,OwnerFailure
from companion_memory.provider.daily_service import DailyProvider,DailyResult
from companion_memory.provider.daily_execution import DailyRequest
from companion_memory.provider.daily_protocol import encode_daily_request
from companion_memory.provider.values import plain,freeze,dump,as_record
from companion_memory.memory.formats import record,sequence
from .daily_material import FrozenDailyMaterial,freeze_material
from .daily_material_storage import DailyMaterialStorage
from .reasoning_records import TABLES
from .daily_output import decode_daily_output
from .daily_tools import DailyReadTools,DailyReadGrant

class DailyReasoning:
    """Cognition's single reasoning component shares its existing owner lease."""
    def __init__(self,catalog,materials:DailyMaterialStorage,participants):
        self.catalog=catalog;self.materials=materials;self.bound=False;self.closed=False
        from companion_memory.persistence.owned_statements import OwnerCauses
        self.causes=OwnerCauses()
        self._task:asyncio.Task|None=None;self._drive:asyncio.Task|None=None;self._reader:asyncio.Task|None=None
        self._frozen:FrozenDailyMaterial|None=None;self._receiving:DailyResult|None=None;self._sending:tuple[Record,Record]|None=None
        self._tool_value:tuple[str,Record]|None=None
        layouts={
            'freeze_reasoning_run':schema(operation_id=ID,run_id=ID,context_id=ID,context_digest=DIGEST,authority_digest=DIGEST,deadline_at_us=UINT),
            'prepare_reasoning_turn':schema(operation_id=ID,run_id=ID,expected_revision=REVISION),
            'associate_reasoning_turn':schema(operation_id=ID,run_id=ID,expected_revision=REVISION,turn_id=ID,request_id=ID,request_digest=DIGEST),
            'store_reasoning_result':schema(operation_id=ID,run_id=ID,expected_revision=REVISION,turn_id=ID,request_digest=DIGEST),
            'finish_reasoning_turn':schema(operation_id=ID,run_id=ID,expected_revision=REVISION,turn_id=ID,request_digest=DIGEST),
            'store_reasoning_tool':schema(operation_id=ID,run_id=ID,expected_revision=REVISION,tool_id=ID,result_digest=DIGEST),
            'retire_reasoning_material':schema(operation_id=ID,run_id=ID,context_id=ID,page_offset=UINT),
        }
        definitions=[]
        for kind,shape in layouts.items():
            required,bindings=audits(kind,('cognition',))
            def handle(uow,value,action=kind):
                try:return self.handle(action,uow,value)
                except OwnerFailure as failure:
                    self.causes.record(action,value,failure)
                    raise
            definitions.append(ResultBoundCommandDefinition('cognition',kind,1,shape,1,
                result_schema(('cognition',),('FROZEN','TURN_PREPARED','ASSOCIATED','TURN_CONFIRMED','TOOLS_READY','REMOTE_UNKNOWN','MATERIAL_RELEASED')),
                tuple(participants),required,handle,INTENT,bindings))
        self.commands=tuple(definitions)
    async def observation(self,after:str=''):
        """Read bounded reasoning progress without materials, tools or model output."""
        if not self.bound or self.closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        if type(after) is not str or len(after.encode())>128:raise InvalidValue()
        page=await self.rows.page('reasoning_runs',after)
        keys=('object_id','revision','batch_id','phase','turn_count','tool_count','active_turn_id','candidate_id')
        return MappingProxyType({'items':tuple(MappingProxyType({k:r[k] for k in keys}) for r in page),
            'next_after':page[-1]['object_id'] if len(page)==4 else None})

    def bind(self,configuration:StoredCognitionConfiguration,provider:DailyProvider,tools:DailyReadTools,verify_frozen,allowed):
        if self.bound or stored_cognition_configuration_issue(configuration) is not None or type(provider) is not DailyProvider or provider.configuration is not configuration or type(tools) is not DailyReadTools:raise InvalidValue()
        self.configuration=configuration;self.provider=provider;self.tools=tools;self.storage=provider.storage
        self.verify_frozen=verify_frozen;self.allowed=allowed
        self.rows=DailyRows(self.catalog,TABLES,self.storage,configuration.database_id,configuration.scope_id,configuration.snapshot_id)
        self.provider_views=BoundStatements(self.catalog,self.storage,'provider')
        self.operations={d.operation_kind:self.storage.bind_operation(d,configuration.scope_id) for d in self.commands}
        self.bound=True
    def key(self,name:str,*parts) -> str:
        return identity(name,self.configuration.database_id,self.configuration.scope_id,*parts)
    def operation(self,uow):
        value=self.storage.cognition_operation_context(uow,self.catalog.definition)
        return MappingProxyType({name:getattr(value,name) for name in ('owner_namespace','operation_kind','scope_id','operation_key')})
    def base(self,oid:str,now:int):
        return {'format_version':1,'object_id':oid,'revision':1,'database_id':self.configuration.database_id,'instance_id':self.configuration.scope_id,
            'config_snapshot_id':self.configuration.snapshot_id,'created_at_us':now,'updated_at_us':now}
    def required(self,name:str,uow,oid:str):
        value=self.rows.get(name,uow,oid)
        if value is None:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        return value
    def update(self,name,uow,old,**changes):
        return self.rows.write(name,uow,dict(old)|{'revision':cast(int,old['revision'])+1,'updated_at_us':max(time.time_ns()//1000,cast(int,old['updated_at_us']))}|changes,cast(int,old['revision']))
    def _admit(self,uow,run:Record,*,fresh:bool):
        if self.closed or fresh and time.time_ns()//1000>=cast(int,run['deadline_at_us']) or not self.allowed(run,uow,fresh):raise OwnerFailure('MODE_BLOCKED','state','SCOPE_CHANGED')
        uow.require_commit_permission(lambda:not self.closed and (not fresh or time.time_ns()//1000<cast(int,run['deadline_at_us'])) and self.allowed(run,None,fresh))
    def _material(self,uow,run:Record,kind:str,oid:str,owner:str,body:bytes):
        original=self.materials.participate_material(uow,cast(str,run['context_id']),cast(str,run['context_digest']),cast(str,run['object_id']))
        metadata:dict[str,object]={key:value for key,value in original.manifest.items() if key not in ('leaf_refs','payload_digest','byte_count')}
        now=time.time_ns()//1000
        metadata.update(self.base(oid,now));metadata.update(context_kind=kind,owner_ref=run['object_id'],original_operation=self.operation(uow),terminal_operation=None,
            wire_digest=sha256(encode_daily_request(self.provider.bindings['LEARNING'],body.decode())).hexdigest() if kind=='LEARNING' else None)
        if kind!='LEARNING':metadata.update(ordered_members=(),related_objects=())
        material=freeze_material(metadata,body,dream_format=self.materials.dream_format)
        return material,self.materials.stage_complete(uow,material)
    def handle(self,kind:str,uow:UnitOfWork,v:Record):
        if not self.bound or self.closed:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        targets=[]
        if kind=='retire_reasoning_material':
            run=self.required('reasoning_runs',uow,cast(str,v['run_id']))
            if run['phase']!='TERMINAL' or run['terminal_operation'] is None or not self.provider.generation_consumers_ended(cast(str,run['object_id'])):raise InvalidValue()
            if self._drive is not None or self._reader is not None or self._receiving is not None or self._tool_value is not None:raise OwnerFailure('RESOURCE_BUSY','material','CLEANUP_PENDING',True)
            uow.require_commit_permission(lambda:self.provider.generation_consumers_ended(cast(str,run['object_id'])))
            fact=self.materials.retire_page(uow,cast(str,v['context_id']),cast(str,run['object_id']),cast(int,v['page_offset']))
            return result(cast(str,v['operation_id']),'MATERIAL_RELEASED',{'cognition':fact})
        if kind=='freeze_reasoning_run':
            material=self._frozen
            if (material is None or material.manifest['object_id']!=v['context_id'] or material.manifest['payload_digest']!=v['context_digest']
                    or material.manifest['owner_ref']!=v['run_id'] or material.manifest['context_kind']!='LEARNING'
                    or not self.verify_frozen(uow,material,v['authority_digest'],v['deadline_at_us'])):raise InvalidValue()
            run={**self.base(cast(str,v['run_id']),cast(int,material.manifest['created_at_us'])),'batch_id':material.manifest['batch_id'],
                'context_id':v['context_id'],'context_digest':v['context_digest'],'authority_digest':v['authority_digest'],'phase':'FROZEN',
                'turn_count':0,'tool_count':0,'active_turn_id':None,'candidate_id':None,'deadline_at_us':v['deadline_at_us'],'terminal_operation':None,
                'transcript_digest':sha256(b'[]').hexdigest()}
            self._admit(uow,MappingProxyType(run),fresh=True);self.rows.write('reasoning_runs',uow,run)
            stored=self.materials.stage_complete(uow,material);targets.extend(cast(tuple,stored['targets']));targets.append(target(cast(str,run['object_id']),1));state='FROZEN'
        else:
            run=self.required('reasoning_runs',uow,cast(str,v['run_id']))
            if run['revision']!=v['expected_revision']:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            self._admit(uow,run,fresh=kind=='prepare_reasoning_turn')
            if kind=='prepare_reasoning_turn':state=self._prepare(uow,run,targets)
            elif kind=='associate_reasoning_turn':
                turn=self.required('reasoning_turns',uow,cast(str,v['turn_id']))
                if turn['run_id']!=run['object_id'] or run['active_turn_id']!=turn['object_id'] or turn['phase']!='PREPARED':raise InvalidValue()
                self.provider.participate_original(uow,cast(str,v['request_id']),role='LEARNING',owner_ref=cast(str,run['object_id']),operation_key=cast(str,turn['provider_operation_key']),request_digest=cast(str,v['request_digest']))
                changed=self.update('reasoning_turns',uow,turn,phase='ASSOCIATED',provider_request_id=v['request_id'])
                targets.append(target(cast(str,turn['object_id']),cast(int,changed['revision']),cast(int,turn['revision'])));state='ASSOCIATED'
            elif kind=='store_reasoning_result':state=self._store_result(uow,v,run,targets)
            elif kind=='finish_reasoning_turn':state=self._finish_failure(uow,v,run,targets)
            elif kind=='store_reasoning_tool':state=self._store_tool(uow,v,run,targets)
            else:raise InvalidValue()
        return result(cast(str,v['operation_id']),state,{'cognition':{'rows_changed':self.storage.transaction_row_changes(uow)['cognition'],'targets':targets}})
    def _prepare(self,uow,run,targets):
        count=cast(int,run['turn_count'])
        if run['phase'] not in ('FROZEN','TOOLS_READY') or count>=3:raise InvalidValue()
        original=self.materials.participate_material(uow,cast(str,run['context_id']),cast(str,run['context_digest']),cast(str,run['object_id']))
        transcript=[]
        for ordinal in range(count):
            old=self.required('reasoning_turns',uow,self.key('reasoning-turn',run['object_id'],ordinal))
            if old['phase']!='RESULT_STORED' or old['result_kind']!='TOOL':raise InvalidValue()
            value=self.materials.participate_material(uow,cast(str,old['result_ref']),cast(str,old['result_digest']),cast(str,run['object_id']))
            transcript.append({'kind':'MODEL_SUGGESTION','value':decode_content(value.body,40960)})
            for step_ordinal in range(cast(int,run['tool_count'])):
                step=self.required('reasoning_tools',uow,self.key('reasoning-tool',run['object_id'],step_ordinal))
                if step['turn_id']!=old['object_id']:continue
                if step['state'] not in ('RESULT_STORED','FAILED'):raise InvalidValue()
                stored=self.materials.participate_material(uow,cast(str,step['result_ref']),cast(str,step['result_digest']),cast(str,run['object_id']))
                transcript.append({'kind':'TOOL_RESULT','value':decode_content(stored.body,8192)})
        transcript_body=encode_content(cast(Record,freeze(transcript,196608,owned=True)),196608)
        if count and sha256(transcript_body).hexdigest()!=run['transcript_digest']:raise InvalidValue()
        body=original.body if not count else encode_content(cast(Record,freeze({'initial_context':decode_content(original.body,262144),'transcript':transcript},262144,owned=True)),262144)
        tid=self.key('reasoning-turn',run['object_id'],count);mid=self.key('reasoning-context',tid)
        material,fact=self._material(uow,run,'LEARNING',mid,cast(str,run['object_id']),body);targets.extend(fact['targets'])
        self.rows.write('reasoning_turns',uow,{**self.base(tid,time.time_ns()//1000),'run_id':run['object_id'],'ordinal':count,'phase':'PREPARED',
            'material_id':mid,'material_digest':material.manifest['payload_digest'],'wire_digest':material.manifest['wire_digest'],
            'provider_operation_key':self.key('reasoning-provider',tid),'provider_request_id':None,'handoff_id':None,'result_kind':None,'result_ref':None,'result_digest':None,
            'previous_turn_digest':None if not count else run['transcript_digest'],'original_operation':self.operation(uow)})
        changed=self.update('reasoning_runs',uow,run,phase='TURN_PREPARED',turn_count=count+1,active_turn_id=tid)
        targets.extend((target(tid,1),target(cast(str,run['object_id']),cast(int,changed['revision']),cast(int,run['revision']))))
        return 'TURN_PREPARED'
    def _store_result(self,uow,v,run,targets):
        terminal=self._receiving;turn=self.required('reasoning_turns',uow,cast(str,v['turn_id']))
        if terminal is None or turn['run_id']!=run['object_id'] or turn['phase']!='ASSOCIATED' or run['active_turn_id']!=turn['object_id'] or turn['provider_request_id']!=terminal.request['object_id']:raise InvalidValue()
        self.provider.verify_daily_result(uow,terminal,role='LEARNING',owner_ref=cast(str,run['object_id']),operation_key=cast(str,turn['provider_operation_key']),request_digest=cast(str,v['request_digest']))
        output_bytes=dump(terminal.value,40960).encode();mid=self.key('reasoning-output',turn['object_id'])
        material,fact=self._material(uow,run,'PROVIDER_RESULT',mid,cast(str,turn['object_id']),output_bytes);targets.extend(fact['targets'])
        count=cast(int,run['tool_count']);result_kind='FAILED'
        try:
            output=decode_daily_output(encode_content(cast(Record,terminal.value['output']),24576),turn=cast(int,turn['ordinal'])+1,completed_tools=count)
            result_kind=cast(str,output['kind'])
        except InvalidValue:output=None
        if output is not None and result_kind=='TOOL':
            for tool in sequence(output['tools']):
                tool=record(tool);sid=self.key('reasoning-tool',run['object_id'],count)
                self.rows.write('reasoning_tools',uow,{**self.base(sid,time.time_ns()//1000),'run_id':run['object_id'],'turn_id':turn['object_id'],'ordinal':count,
                    'name':tool['name'],'arguments':tool['arguments'],'state':'PREPARED','grant_digest':run['authority_digest'],
                    'result_ref':None,'result_digest':None,'started_at_us':None,'ended_at_us':None,'failure':None,'original_operation':self.operation(uow)})
                targets.append(target(sid,1));count+=1
        changed=self.update('reasoning_turns',uow,turn,phase='RESULT_STORED',handoff_id=terminal.handoff['object_id'],result_kind=result_kind,result_ref=mid,result_digest=material.manifest['payload_digest'])
        targets.append(target(cast(str,turn['object_id']),cast(int,changed['revision']),cast(int,turn['revision'])))
        changed=self.update('reasoning_runs',uow,run,phase='TURN_CONFIRMED',tool_count=count,transcript_digest=self.transcript_digest(uow,dict(run)|{'tool_count':count}))
        targets.append(target(cast(str,run['object_id']),cast(int,changed['revision']),cast(int,run['revision'])))
        return 'TURN_CONFIRMED'
    def _store_tool(self,uow,v,run,targets):
        pending=self._tool_value;step=self.required('reasoning_tools',uow,cast(str,v['tool_id']))
        if pending is None or pending[0]!=step['object_id'] or step['run_id']!=run['object_id'] or step['state']!='PREPARED' or pending[1]['grant_digest']!=step['grant_digest']:raise InvalidValue()
        body=encode_content(pending[1],8192)
        if sha256(body).hexdigest()!=v['result_digest'] or pending[1]['name']!=step['name'] or pending[1]['arguments']!=step['arguments']:raise InvalidValue()
        mid=self.key('reasoning-tool-result',step['object_id']);material,fact=self._material(uow,run,'TOOL_RESULT',mid,cast(str,step['object_id']),body);targets.extend(fact['targets'])
        if pending[1]['state'] not in ('RESULT_STORED','FAILED'):raise InvalidValue()
        changed=self.update('reasoning_tools',uow,step,state=pending[1]['state'],result_ref=mid,result_digest=v['result_digest'],started_at_us=pending[1]['started_at_us'],ended_at_us=pending[1]['ended_at_us'],failure=pending[1].get('failure'))
        targets.append(target(cast(str,step['object_id']),cast(int,changed['revision']),cast(int,step['revision'])))
        transcript=[];complete=True
        for ordinal in range(cast(int,run['turn_count'])):
            turn=self.required('reasoning_turns',uow,self.key('reasoning-turn',run['object_id'],ordinal))
            output=self.materials.participate_material(uow,cast(str,turn['result_ref']),cast(str,turn['result_digest']),cast(str,run['object_id']))
            transcript.append({'kind':'MODEL_SUGGESTION','value':decode_content(output.body,40960)})
            for n in range(cast(int,run['tool_count'])):
                tool=self.required('reasoning_tools',uow,self.key('reasoning-tool',run['object_id'],n))
                if tool['turn_id']!=turn['object_id']:continue
                if tool['state'] not in ('RESULT_STORED','FAILED'):complete=False;continue
                value=self.materials.participate_material(uow,cast(str,tool['result_ref']),cast(str,tool['result_digest']),cast(str,run['object_id']))
                transcript.append({'kind':'TOOL_RESULT','value':decode_content(value.body,8192)})
        phase='TOOLS_READY' if complete else 'TURN_CONFIRMED'
        changed=self.update('reasoning_runs',uow,run,phase=phase,transcript_digest=sha256(encode_content(cast(Record,freeze(transcript,196608,owned=True)),196608)).hexdigest())
        targets.append(target(cast(str,run['object_id']),cast(int,changed['revision']),cast(int,run['revision'])))
        return phase
    def transcript_digest(self,uow,run) -> str:
        """Hash every original normalized result and completed read in fixed order."""
        transcript=[]
        for ordinal in range(cast(int,run['turn_count'])):
            turn=self.required('reasoning_turns',uow,self.key('reasoning-turn',run['object_id'],ordinal))
            if turn['phase']!='RESULT_STORED':raise InvalidValue()
            output=self.materials.participate_material(uow,cast(str,turn['result_ref']),cast(str,turn['result_digest']),cast(str,run['object_id']))
            transcript.append({'kind':'MODEL_SUGGESTION','value':decode_content(output.body,40960)})
            for n in range(cast(int,run['tool_count'])):
                tool=self.required('reasoning_tools',uow,self.key('reasoning-tool',run['object_id'],n))
                if tool['turn_id']!=turn['object_id'] or tool['state'] not in ('RESULT_STORED','FAILED'):continue
                material=self.materials.participate_material(uow,cast(str,tool['result_ref']),cast(str,tool['result_digest']),cast(str,run['object_id']))
                transcript.append({'kind':'TOOL_RESULT','value':decode_content(material.body,8192)})
        return sha256(encode_content(cast(Record,freeze(transcript,196608,owned=True)),196608)).hexdigest()

    def _finish_failure(self,uow,v,run,targets):
        turn=self.required('reasoning_turns',uow,cast(str,v['turn_id']))
        if turn['run_id']!=run['object_id'] or turn['phase']!='ASSOCIATED' or run['active_turn_id']!=turn['object_id']:raise InvalidValue()
        request=self.provider.participate_original(uow,cast(str,turn['provider_request_id']),role='LEARNING',owner_ref=cast(str,run['object_id']),operation_key=cast(str,turn['provider_operation_key']),request_digest=cast(str,v['request_digest']))
        if request['phase'] not in ('TERMINAL','REMOTE_RESULT_UNKNOWN') or request['outcome']=='SUCCEEDED':raise InvalidValue()
        proof=self.provider.verify_original_receipt(uow,request)
        body=encode_content(cast(Record,freeze({'request':request,'receipt':self.provider.reference(proof)},40960,owned=True)),40960)
        mid=self.key('reasoning-output',turn['object_id']);material,fact=self._material(uow,run,'PROVIDER_RESULT',mid,cast(str,run['object_id']),body);targets.extend(fact['targets'])
        changed=self.update('reasoning_turns',uow,turn,phase='RESULT_STORED',result_kind='SENSITIVE' if request['outcome']=='SENSITIVE_REFUSAL' else 'FAILED',result_ref=mid,result_digest=material.manifest['payload_digest'])
        targets.append(target(cast(str,turn['object_id']),cast(int,changed['revision']),cast(int,turn['revision'])))
        phase='REMOTE_UNKNOWN' if request['phase']=='REMOTE_RESULT_UNKNOWN' else 'TURN_CONFIRMED'
        changed=self.update('reasoning_runs',uow,run,phase=phase,transcript_digest=self.transcript_digest(uow,run))
        targets.append(target(cast(str,run['object_id']),cast(int,changed['revision']),cast(int,run['revision'])))
        return phase

    def authorize_request(self,request:DailyRequest,uow:UnitOfWork|None) -> bool:
        expected=self._sending
        if expected is None or request.binding.role!='LEARNING':return False
        run,turn=expected;description=request.description
        if (self.closed or turn['phase']!='PREPARED' or run['phase']!='TURN_PREPARED' or time.time_ns()//1000>=cast(int,run['deadline_at_us'])
                or description['work_id']!=run['object_id'] or description['original_request_key']!=turn['provider_operation_key']
                or description['material_id']!=turn['material_id'] or description['material_digest']!=turn['material_digest']
                or description['deadline_at_us']!=run['deadline_at_us'] or not self.allowed(run,uow,True)):return False
        if uow is not None:
            for name,old in (('reasoning_runs',run),('reasoning_turns',turn)):
                rows=self.provider_views.stage('provider_read_'+name,uow,{'caller_scope':self.configuration.scope_id,'object_id':old['object_id']})
                if len(rows)!=1 or self.rows.decode(name,rows[0])!=old:return False
        return True

    def verify_received(self,uow:UnitOfWork,request,handoff,proof) -> bool:
        if request['task_role']!='LEARNING' or request['result_owner']!='cognition' or proof['kind']!='store_reasoning_result':return False
        turns=self.provider_views.stage('provider_read_turn_request',uow,{'caller_scope':self.configuration.scope_id,'request_id':request['object_id']})
        if len(turns)!=1:return False
        turn=self.rows.decode('reasoning_turns',turns[0])
        if (turn['phase'] not in ('RESULT_STORED','RELEASED') or turn['handoff_id']!=handoff['object_id'] or turn['result_digest']!=handoff['checksum']
                or proof['key']!=self.key('reasoning-result',turn['object_id']) or as_record(request['attribution'])['run_id']!=turn['run_id']):return False
        definition=next(d for d in self.commands if d.operation_kind=='store_reasoning_result')
        original=self.storage.confirm_cognition_consumer_operation(uow,definition,proof['key'],request['object_id'])
        if original is None or original.fingerprint!=proof['fingerprint']:return False
        self.materials.participate_material(uow,cast(str,turn['result_ref']),cast(str,turn['result_digest']),cast(str,turn['run_id']))
        return True

    async def freeze_run(self,material:FrozenDailyMaterial,authority_digest:str,deadline_at_us:int):
        if type(material) is not FrozenDailyMaterial or self._frozen is not None:raise InvalidValue()
        self._frozen=material
        try:
            return await self.execute('freeze_reasoning_run',self.key('reasoning-freeze',material.manifest['run_id']),
                {'run_id':material.manifest['run_id'],'context_id':material.manifest['object_id'],'context_digest':material.manifest['payload_digest'],
                    'authority_digest':authority_digest,'deadline_at_us':deadline_at_us})
        finally:
            if self._task is not None:self._task.add_done_callback(lambda job:setattr(self,'_frozen',None))
            else:self._frozen=None

    async def process(self,run_id:str,grant:DailyReadGrant,*,allow_first_send:bool):
        """Advance original local steps, up to three generations; confirmation never sends."""
        if self.closed or self._drive is not None or type(grant) is not DailyReadGrant or grant.owner is not self.tools:raise InvalidValue()
        task,logical=start_owned(self._process(run_id,grant,allow_first_send));self._drive=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._drive is job:self._drive=None
        task.add_done_callback(ended)
        run=await self.rows.read('reasoning_runs',run_id)
        if run is None:raise InvalidValue()
        remaining=max(5,(cast(int,run['deadline_at_us'])-time.time_ns()//1000)/1000000)
        done,_=await asyncio.wait((logical,),timeout=remaining)
        return logical.result() if done else Found(MappingProxyType({'state':'PENDING','cleanup_pending':True}))

    async def _process(self,run_id,grant,allow_first_send):
        from companion_memory.persistence import NotFound
        for _ in range(24):
            run=await self.rows.read('reasoning_runs',run_id)
            if run is None or run['authority_digest']!=grant.digest:raise InvalidValue()
            if run['phase'] in ('REMOTE_UNKNOWN','CANDIDATE_STORED','TERMINAL'):return Found(run)
            if run['phase'] in ('FROZEN','TOOLS_READY'):
                if time.time_ns()//1000>=cast(int,run['deadline_at_us']):return Found(MappingProxyType({'state':'DEADLINE','run_id':run_id,'cleanup_pending':False}))
                if not allow_first_send:return Found(MappingProxyType({'state':'PARKED','run_id':run_id,'new_sends':0}))
                prepared=await self.execute('prepare_reasoning_turn',self.key('reasoning-prepare',run_id,run['turn_count']),{'run_id':run_id,'expected_revision':run['revision']})
                if type(prepared) is not Committed:return prepared
                continue
            turn=await self.rows.read('reasoning_turns',cast(str,run['active_turn_id']))
            if turn is None:raise InvalidValue()
            if turn['phase'] in ('PREPARED','ASSOCIATED'):
                response=await self._turn(run,turn,grant,allow_first_send)
                if type(response) is not Committed:return response
                continue
            if turn['phase']!='RESULT_STORED':raise InvalidValue()
            if turn['handoff_id'] is not None:
                original=await self.operations['store_reasoning_result'].read_receipt(self.key('reasoning-result',turn['object_id']))
                if type(original) is not Found:raise InvalidValue()
                cleaned=await self.provider.cleanup_daily(cast(str,turn['provider_request_id']),original.value,time.monotonic()+5)
                if type(cleaned) is not Found:return cleaned
                if cleaned.value.get('cleanup_pending'):return Found(MappingProxyType({'state':'PENDING','run_id':run_id,'cleanup_pending':True}))
            if turn['result_kind'] in ('FINAL','FAILED','SENSITIVE'):
                return Found(MappingProxyType({'state':'FINAL_READY','run_id':run_id,'turn_id':turn['object_id'],'result_ref':turn['result_ref'],
                    'result_digest':turn['result_digest'],'result_kind':turn['result_kind'],'cleanup_pending':self.provider.cleanup_pending or self.tools.pending or self._task is not None}))
            pending=None
            for n in range(cast(int,run['tool_count'])):
                step=await self.rows.read('reasoning_tools',self.key('reasoning-tool',run_id,n))
                if step is None:raise InvalidValue()
                if step['state']=='PREPARED':pending=step;break
            if pending is None:raise InvalidValue()
            if time.time_ns()//1000>=cast(int,run['deadline_at_us']):return Found(MappingProxyType({'state':'DEADLINE','run_id':run_id,'cleanup_pending':False}))
            deadline=time.monotonic()+(cast(int,run['deadline_at_us'])-time.time_ns()//1000)/1000000
            value=await self.tools.read(grant,{'name':pending['name'],'arguments':plain(pending['arguments'])},deadline)
            self._tool_value=cast(str,pending['object_id']),value
            try:
                stored=await self.execute('store_reasoning_tool',self.key('reasoning-tool-store',pending['object_id']),{'run_id':run_id,'expected_revision':run['revision'],
                    'tool_id':pending['object_id'],'result_digest':sha256(encode_content(value,8192)).hexdigest()})
                if type(stored) is not Committed:return stored
            finally:
                if self._task is not None:await asyncio.wait((self._task,))
                self._tool_value=None
        raise OwnerFailure('STORAGE_FAILED','state','INTEGRITY_FAILURE')

    async def _turn(self,run,turn,grant,allow_first_send):
        from companion_memory.persistence import NotFound
        remaining=max(5,(cast(int,run['deadline_at_us'])-time.time_ns()//1000)/1000000)
        lease=await self.materials.borrow(cast(str,turn['material_id']),cast(str,turn['material_digest']),cast(str,run['object_id']),time.monotonic()+remaining)
        request=None
        try:
            request=self.provider.generation_request('LEARNING',lease,cast(str,turn['provider_operation_key']),cast(int,run['deadline_at_us']),(grant.entry_id,))
            original=await self.provider.lookup_daily(cast(str,turn['provider_operation_key']),request.fingerprint,time.monotonic()+5)
            if type(original) is NotFound:
                if time.time_ns()//1000>=cast(int,run['deadline_at_us']):return Found(MappingProxyType({'state':'DEADLINE','run_id':run['object_id'],'cleanup_pending':False}))
                if not allow_first_send:return Found(MappingProxyType({'state':'WAITING_ADMISSION','run_id':run['object_id'],'new_sends':0}))
                self._sending=run,turn
                sent=await self.provider.send_generation(request)
                original=await self.provider.lookup_daily(cast(str,turn['provider_operation_key']),request.fingerprint,time.monotonic()+5)
                if type(original) is not Found:return sent
            if type(original) is not Found:raise InvalidValue()
            observed=original.value
            if turn['phase']=='PREPARED':
                associated=await self.execute('associate_reasoning_turn',self.key('reasoning-associate',turn['object_id']),{'run_id':run['object_id'],'expected_revision':run['revision'],
                    'turn_id':turn['object_id'],'request_id':observed['object_id'],'request_digest':request.fingerprint})
                if type(associated) is not Committed:return associated
            if observed['phase'] in ('TERMINAL','REMOTE_RESULT_UNKNOWN') and observed['outcome']!='SUCCEEDED':
                return await self.execute('finish_reasoning_turn',self.key('reasoning-failure',turn['object_id']),{'run_id':run['object_id'],'expected_revision':run['revision'],'turn_id':turn['object_id'],'request_digest':request.fingerprint})
            if observed['phase']!='TERMINAL':return Found(MappingProxyType({'state':'PENDING','remote_state':observed['phase'],'cleanup_pending':self.provider.cleanup_pending}))
            terminal=await self.provider.recover_daily_result(cast(str,observed['object_id']),time.monotonic()+5);self._receiving=terminal
            try:
                return await self.execute('store_reasoning_result',self.key('reasoning-result',turn['object_id']),{'run_id':run['object_id'],'expected_revision':run['revision'],'turn_id':turn['object_id'],'request_digest':request.fingerprint})
            finally:
                if self._task is not None:await asyncio.wait((self._task,))
                self.provider.release_daily_result(terminal);self._receiving=None
        finally:
            if request is not None:
                await self.provider.wait_generation_actual(request);self.provider.release_unused_generation(request)
            self._sending=None;self.materials.release_reader(lease)
            await self.provider.reconcile_daily_network()

    async def read_candidate_inputs(self,run_id:str,deadline:float):
        """Collect original complete values, retaining actual reads beyond timeout."""
        from .daily_reasoning_snapshot import read_snapshot
        if self.closed or self._reader is not None or self._drive is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        task,logical=start_owned(read_snapshot(self,run_id,deadline));self._reader=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._reader is job:self._reader=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        return logical.result()

    def participate_candidate_inputs(self,uow:UnitOfWork,run_id:str):
        """Compare original current material and native reception receipts atomically."""
        from .daily_reasoning_snapshot import participate_snapshot
        return participate_snapshot(self,uow,run_id)

    def stage_candidate(self,uow:UnitOfWork,run_id:str,candidate_id:str):
        run=self.required('reasoning_runs',uow,run_id)
        if run['phase'] not in ('TURN_CONFIRMED','REMOTE_UNKNOWN') or run['candidate_id'] is not None:raise InvalidValue()
        self._admit(uow,run,fresh=False)
        return self.update('reasoning_runs',uow,run,phase='CANDIDATE_STORED',candidate_id=candidate_id)

    def finish_candidate(self,uow:UnitOfWork,run_id:str,candidate_id:str):
        run=self.required('reasoning_runs',uow,run_id)
        if run['phase']!='CANDIDATE_STORED' or run['candidate_id']!=candidate_id:raise InvalidValue()
        self._admit(uow,run,fresh=False)
        return self.update('reasoning_runs',uow,run,phase='TERMINAL',terminal_operation=self.operation(uow))

    def require_fresh_application(self,uow:UnitOfWork,run:Record):
        """A successful formal mutation must commit inside the original total period."""
        if time.time_ns()//1000>=cast(int,run['deadline_at_us']):raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        uow.require_commit_permission(lambda:time.time_ns()//1000<cast(int,run['deadline_at_us']))

    def expire_original(self,uow:UnitOfWork,run_id:str):
        """Verify actual original absence or reception before local deadline cleanup."""
        run=self.required('reasoning_runs',uow,run_id)
        if run['phase']=='TERMINAL' or time.time_ns()//1000<cast(int,run['deadline_at_us']):raise InvalidValue()
        return self._finish_local_original(uow,run)

    def finish_revision_conflict(self,uow:UnitOfWork,run_id:str):
        """Join only the native application command's verified original conflict."""
        if self.operation(uow)['operation_kind'] not in ('reject_daily_revision','reject_daily_revision_media'):raise InvalidValue()
        run=self.required('reasoning_runs',uow,run_id)
        if run['phase'] not in ('TURN_CONFIRMED','CANDIDATE_STORED'):raise InvalidValue()
        return self._finish_local_original(uow,run)

    def finish_ownership_conflict(self,uow:UnitOfWork,run_id:str):
        """Consume a native failure only after its one release rebuild was exhausted."""
        if self.operation(uow)['operation_kind'] not in ('reject_daily_ownership','reject_daily_ownership_media'):raise InvalidValue()
        run=self.required('reasoning_runs',uow,run_id)
        if run['phase']!='CANDIDATE_STORED':raise InvalidValue()
        return self._finish_local_original(uow,run)

    def finish_scope_conflict(self,uow:UnitOfWork,run_id:str,admission):
        """Reject original work under cleanup authority, without old read permission."""
        if self.operation(uow)['operation_kind'] not in ('reject_daily_scope','reject_daily_scope_media'):raise InvalidValue()
        run=self.required('reasoning_runs',uow,run_id)
        if run['phase']=='TERMINAL' or self.closed or not admission(run,uow):raise OwnerFailure('ACCESS_DENIED','capability','SCOPE_CHANGED')
        uow.require_commit_permission(lambda:not self.closed and admission(run,None))
        return self._finish_local_original(uow,run,scope_rejected=True)

    def verify_unstarted_original(self,uow:UnitOfWork,run_id:str):
        """Attest an absent original run and every possible original request."""
        if self.closed or self._drive is not None or self._reader is not None or self._task is not None or self._frozen is not None:
            raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        if self.rows.get('reasoning_runs',uow,run_id) is not None:raise InvalidValue()
        definition=next(d for d in self.commands if d.operation_kind=='freeze_reasoning_run')
        if self.storage.confirm_prior_operation(uow,definition,self.key('reasoning-freeze',run_id)) is not None:raise InvalidValue()
        for ordinal in range(3):
            turn_id=self.key('reasoning-turn',run_id,ordinal)
            if self.rows.get('reasoning_turns',uow,turn_id) is not None:raise InvalidValue()
            if not self.provider.participate_unsent_daily(uow,self.key('reasoning-provider',turn_id),run_id):
                raise OwnerFailure('RESULT_UNCONFIRMED','request','COMMIT_UNCONFIRMED',True)
        uow.require_commit_permission(lambda:self.provider.generation_consumers_ended(run_id))

    def _finish_local_original(self,uow,run,*,scope_rejected=False):
        run_id=cast(str,run['object_id'])
        if not scope_rejected:self._admit(uow,run,fresh=False)
        if self._drive is not None or self._reader is not None or not self.provider.generation_consumers_ended(run_id):
            raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        terminal='FAILED_DROPPED'
        for n in range(cast(int,run['turn_count'])):
            turn=self.required('reasoning_turns',uow,self.key('reasoning-turn',run_id,n))
            if turn['phase']=='PREPARED':
                if not self.provider.participate_unsent_daily(uow,cast(str,turn['provider_operation_key']),run_id):
                    raise OwnerFailure('RESULT_UNCONFIRMED','request','COMMIT_UNCONFIRMED',True)
            elif turn['phase']=='RESULT_STORED':
                kind='store_reasoning_result' if turn['handoff_id'] is not None else 'finish_reasoning_turn'
                key=self.key('reasoning-result' if turn['handoff_id'] is not None else 'reasoning-failure',turn['object_id'])
                proof=self.storage.confirm_prior_operation(uow,next(d for d in self.commands if d.operation_kind==kind),key)
                if proof is None:raise OwnerFailure('STORAGE_FAILED','receipt','INTEGRITY_FAILURE')
                if turn['result_kind']=='SENSITIVE':terminal='SENSITIVE_DROPPED'
            else:raise OwnerFailure('RESULT_UNCONFIRMED','request','COMMIT_UNCONFIRMED',True)
        uow.require_commit_permission(lambda:self.provider.generation_consumers_ended(run_id))
        return run,self.update('reasoning_runs',uow,run,phase='TERMINAL',terminal_operation=self.operation(uow)),terminal

    async def execute(self,kind:str,key:str,values:dict):
        if self.closed or self._task is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',self._task is not None)
        definition=next(d for d in self.commands if d.operation_kind==kind)
        command=ResultBoundCommand(1,plain(freeze({'operation_id':key,**values},8192,owned=True)),{a.event_slot:{'actor':'daily_cognition'} for a in definition.required_audits})
        async def run():
            outcome,cause=await self.causes.execute_original(self.operations[kind],definition,key,command)
            if type(outcome) is NotCommitted and cause is not None:
                from companion_memory.runtime.results import NotCommitted as DomainNotCommitted,RuntimeError
                return DomainNotCommitted(RuntimeError(cause.code,kind,cause.field,cause.reason,cause.cleanup_pending or outcome.error is not None and outcome.error.cleanup_pending))
            return outcome
        task,logical=start_owned(run());self._task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=5)
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        return logical.result()
    async def wait_actual(self) -> None:
        """Join this reasoning owner's original jobs, never another owner's busy count."""
        for task in (self._drive,self._reader,self._task):
            if task is not None:await asyncio.wait((task,))
        await self.tools.wait_actual()

    async def retire_material(self,run_id:str):
        """Retire complete original pages after atomic business completion only."""
        run=await self.rows.read('reasoning_runs',run_id)
        if run is None or run['phase']!='TERMINAL':raise InvalidValue()
        contexts=[cast(str,run['context_id'])]
        for n in range(cast(int,run['turn_count'])):
            turn=await self.rows.read('reasoning_turns',self.key('reasoning-turn',run_id,n))
            if turn is None:raise InvalidValue()
            contexts.extend(cast(str,turn[key]) for key in ('material_id','result_ref') if turn[key] is not None)
        for n in range(cast(int,run['tool_count'])):
            step=await self.rows.read('reasoning_tools',self.key('reasoning-tool',run_id,n))
            if step is None:raise InvalidValue()
            if step['result_ref'] is not None:contexts.append(cast(str,step['result_ref']))
        for context_id in dict.fromkeys(contexts):
            root=await self.materials.rows.read('learning_contexts',context_id)
            if root is None:raise InvalidValue()
            self.materials.begin_retirement(context_id)
            try:
                for offset in range(0,len(cast(tuple,root['leaf_refs'])),4):
                    result=await self.execute('retire_reasoning_material',self.key('reasoning-retire',context_id,offset),
                        {'run_id':run_id,'context_id':context_id,'page_offset':offset})
                    if type(result) is not Committed:return result
            finally:
                if self._task is not None:await asyncio.wait((self._task,))
                self.materials.end_retirement(context_id)
        return Found(MappingProxyType({'state':'RELEASED','cleanup_pending':False,'new_sends':0}))

    def close(self) -> bool:
        self.closed=True
        return self._task is None and self._drive is None and self._reader is None and self._receiving is None and self._sending is None
