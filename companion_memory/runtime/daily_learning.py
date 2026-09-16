"""One native learning slot from original FIFO material through formal application.

Trusted entry scopes are retained independently of requests. Reopening restores
the original scope reference and total deadline; it never authorizes another
send. The actual job keeps its read grant until all child operations have ended.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from hashlib import sha256
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Committed,Found,Value
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue,valid_identifier
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record,sequence
from companion_memory.memory.service import MemoryReadPort
from companion_memory.cognition.daily_material import freeze_material
from companion_memory.cognition.daily_candidate_material import restore_candidate_material
from companion_memory.cognition.daily_resources import TRANSFORM_VERSION
from companion_memory.provider.daily_protocol import encode_daily_request
from companion_memory.self_model.current import Available,Matched

@dataclass(frozen=True,slots=True)
class DailyEntryScope:
    """Explicit native current-read scope plus separately granted write targets."""
    memory:MemoryReadPort
    partition_id:str
    subjects:tuple[str,...]
    worlds:tuple[MappingProxyType[str,Value],...]
    related:tuple[str,...]=()
    writable:tuple[str,...]=()
    routes:tuple[str,...]=()

class DailyLearning:
    """Connect the same runtime, cognition, Provider, persona and formal owners."""
    def __init__(self,runtime,reasoning,application,tools,persona,admitted):
        if not runtime.assembly.daily_format or runtime.daily_learning is not None:raise InvalidValue()
        self.runtime=runtime;self.reasoning=reasoning;self.application=application;self.tools=tools;self.persona=persona;self.admitted=admitted
        self.scopes:dict[str,DailyEntryScope]={};self.closed=False;self._task:asyncio.Task|None=None;self._grant=None;self._frozen=None
        self.cleanup_failure:tuple[str,OwnerFailure]|None=None
        self._original_entry:tuple[str,str]|None=None
        reasoning.bind(runtime.assembly.configuration,runtime.provider,tools,self.verify_frozen,self.allowed)
        application.bind(runtime.assembly.configuration,runtime.assembly.storage,self.verify_application,self.verify_failed_scope,self.verify_unstarted_cleanup)
        runtime.daily_learning=self

    def bind_entry(self,entry_id:str,scope:DailyEntryScope):
        """Trusted host setup supplies original authority; model output cannot bind it."""
        if self.closed or self._task is not None or type(scope) is not DailyEntryScope or not valid_identifier(entry_id) or entry_id in self.scopes:raise InvalidValue()
        if len(self.scopes)>=self.runtime.settings.integer('runtime.read_page_size'):raise OwnerFailure('RESOURCE_BUSY','resource','ADMISSION_FULL')
        if len(scope.related)>4 or len(set(scope.related))!=len(scope.related):raise InvalidValue()
        for oid in (*scope.related,*scope.writable):self.runtime.memory.verify_scope_member(scope.memory,oid)
        grant=self.tools.bind(scope.memory,entry_id,scope.partition_id,scope.subjects,scope.worlds,writable_objects=scope.writable,route_ids=scope.routes)
        self.tools.release(grant);self.scopes[entry_id]=scope

    def allowed(self,run,uow,fresh):
        grant=self._grant
        if self.closed or grant is None or run['authority_digest']!=grant.digest:return False
        try:self.runtime.memory.read_grant_reference(grant.memory)
        except OwnerFailure:return False
        if fresh:return self.admitted() and self.runtime.gate.information_checkpoint() is not None
        return self.runtime.state=='RECOVERING' or self.runtime.gate.information_checkpoint() is not None

    def verify_frozen(self,uow,material,digest,deadline):
        if self._frozen is None or material!=self._frozen or self._grant is None or self._grant.digest!=digest:return False
        a=self.runtime.assembly;value=restore_candidate_material(material.body,());batch=cast(str,value.source['batch_id'])
        _,work=a.participate_daily_work(uow,batch)
        if a.participate_daily_batch(uow,batch,cast(int,work['generation']),cast(int,work['revision']))!=value.source:return False
        prep=a.participate_daily_preparation(uow,batch)
        if deadline!=cast(int,prep['started_at_us'])+1200000000 or prep['mode_epoch']!=self.runtime.gate.epoch:return False
        persona=record(value.original['persona'])
        matched=self.persona.verify_current(uow,cast(str,persona['publication_id']),cast(int,persona['revision']))
        if type(matched) is not Matched or matched.value!=persona:return False
        for obj in value.observed:
            if a.memory.current(uow,cast(str,obj['object_id']))!=obj:return False
        for original in sequence(value.original['subjects']):
            original=record(original)
            if a.memory.subject(uow,cast(str,original['subject_id']))!=original:return False
        return True

    def verify_application(self,run,authority,uow):
        grant=self._grant
        if (not self.allowed(run,uow,False) or grant is None or authority.subject_ids!=grant.subject_ids or authority.worlds!=grant.worlds
                or authority.writable_objects!=grant.writable_objects or authority.route_ids!=grant.route_ids):return False
        if uow is not None:
            material=self.reasoning.materials.participate_material(uow,cast(str,run['context_id']),cast(str,run['context_digest']),cast(str,run['object_id']))
            persona=record(restore_candidate_material(material.body,()).original['persona'])
            # During startup the native import owner verifies the publication;
            # no volatile READY gate or new send is required for original work.
            if self.runtime.state=='RECOVERING':
                matched=self.persona.verify_recovery(uow,cast(str,persona['publication_id']),cast(int,persona['revision']))
                if type(matched) is not Matched or matched.value!=persona:return False
            else:
                matched=self.persona.verify_current(uow,cast(str,persona['publication_id']),cast(int,persona['revision']))
                if type(matched) is not Matched or matched.value!=persona:return False
        return True

    def scope_matches(self,run,entry_id):
        scope=self.scopes.get(entry_id)
        if scope is None:return False
        try:
            checksum=self.tools.scope_digest(scope.memory,entry_id,scope.partition_id,scope.subjects,scope.worlds,scope.writable,scope.routes)
        except OwnerFailure as failure:
            if failure.code!='ACCESS_DENIED' or failure.cleanup_pending:raise
            return False
        return run['authority_digest']==checksum

    def verify_failed_scope(self,run,uow):
        """Authorize only original-source disposal after actual trusted scope change."""
        original=self._original_entry
        if self.closed or original is None or original[0]!=run['object_id']:return False
        if self.runtime.state!='RECOVERING' and self.runtime.gate.information_checkpoint() is None:return False
        if uow is not None:
            batch,work=self.runtime.assembly.participate_daily_work(uow,cast(str,run['batch_id']))
            if batch['entry_id']!=original[1] or batch['terminal']!='FROZEN':return False
        return not self.scope_matches(run,original[1])

    def verify_unstarted_cleanup(self,run_id,batch_id,uow):
        """Only the original entry's local cleanup remains valid after its deadline."""
        original=self._original_entry
        if self.closed or original is None or original[0]!=run_id:return False
        if self.runtime.state!='RECOVERING' and self.runtime.gate.information_checkpoint() is None:return False
        if uow is not None:
            batch,_=self.runtime.assembly.participate_daily_work(uow,batch_id)
            if batch['entry_id']!=original[1]:return False
        return True

    async def require_current(self,deadline:float):
        current=await self.persona.read_current(deadline)
        if type(current) is not Available:raise OwnerFailure('PRECONDITION_FAILED','persona','PERSONA_REQUIRED')
        return current.value

    async def collect(self,source,grant,deadline_at_us):
        a=self.runtime.assembly;scope=self.scopes[grant.entry_id]
        deadline=time.monotonic()+max(0,(deadline_at_us-time.time_ns()//1000)/1000000)
        persona=await self.require_current(deadline);members=[];related=[];subjects=[]
        for sid in scope.subjects:
            value=await scope.memory.read_subject(sid)
            if type(value) is not Found:raise OwnerFailure('ACCESS_DENIED','subject','BINDING_MISMATCH')
            subjects.append(record(value.value))
        for oid in scope.related:
            value=await scope.memory.get_current(oid)
            if type(value) is not Found:raise OwnerFailure('PRECONDITION_FAILED','object','BASIS_UNAVAILABLE')
            related.append(record(value.value))
        for raw in sequence(source['ordered_members']):
            member=record(raw);payload=await a.ingress.rows.read('payload',{'message_id':member['message_id']});versions=[]
            if len(payload)!=1:raise InvalidValue()
            for selected in sequence(member['media']):
                if a.media is None:raise InvalidValue()
                version=await a.media.read_daily_interpretation(cast(str,source['batch_id']),record(selected),member)
                versions.append(encode_content(version,2048).decode())
            members.append({'member':member,'payload':payload[0]['body'],'interpretations':tuple(versions)})
        authority={'subject_ids':scope.subjects,'worlds':scope.worlds,'writable_objects':scope.writable,'route_ids':scope.routes,
            'memory_ref':self.runtime.memory.read_grant_reference(scope.memory),'partition_id':scope.partition_id}
        from companion_memory.provider.values import freeze
        context={'source':source,'members':members,'persona':persona,'authority':authority,'authority_digest':grant.digest,'related':related,'subjects':subjects}
        if self.runtime.provider.trial_authorization is not None:
            context['request_constraints']={'memory_target_limit':2}
        body=encode_content(cast(Value,freeze(context,262144,owned=True)),262144)
        rid=cast(str,source['run_id']);mid=self.reasoning.key('initial-context',rid);now=time.time_ns()//1000
        binding=self.runtime.provider.bindings['LEARNING'];config=a.configuration
        role=next(record(r) for r in sequence(config.candidate.text.record('provider.transport')['roles']) if record(r)['role']=='LEARNING')
        metadata={'format_version':1,'object_id':mid,'revision':1,'database_id':config.database_id,'instance_id':config.scope_id,'config_snapshot_id':config.snapshot_id,
            'created_at_us':now,'updated_at_us':now,'context_version':2,'context_kind':'LEARNING','owner_ref':rid,'batch_id':source['batch_id'],'run_id':rid,'source_id':source['source_id'],
            'state':'STORED','persona_publication_id':persona['publication_id'],'persona_revision':persona['revision'],'prompt_ref':role['prompt_ref'],
            'schema_ref':binding.schema_ref,'transform_ref':TRANSFORM_VERSION,'model_binding_digest':sha256(body).hexdigest(),
            'ordered_members':tuple({'role':{'H':'HISTORY','T':'TARGET','R':'RECENT'}[cast(str,record(m)['role'])],'message_id':record(m)['message_id'],'payload_digest':record(m)['payload_digest']} for m in sequence(source['ordered_members'])),
            'related_objects':tuple({'object_id':obj['object_id'],'revision':obj['revision'],'grant_ref':authority['memory_ref'],'snapshot_digest':sha256(encode_content(obj,4096)).hexdigest()} for obj in related),
            'wire_digest':sha256(encode_daily_request(binding,body.decode())).hexdigest(),'input_token_estimate':None,'reservation_input_bound':262144,
            'original_operation':{'owner_namespace':'cognition','operation_kind':'freeze_reasoning_run','scope_id':config.scope_id,'operation_key':self.reasoning.key('reasoning-freeze',rid)},'terminal_operation':None}
        if time.monotonic()>=deadline:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        return freeze_material(metadata,body)

    async def learn_batch(self,source,*,fresh:bool,admission_event:str|None=None):
        if self.closed or self._task is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',self._task is not None)
        async def advance():
            rid=cast(str,source['run_id']);run=await self.reasoning.rows.read('reasoning_runs',rid)
            self._original_entry=(rid,cast(str,source['entry_id']))
            if run is not None and run['phase']=='TERMINAL':
                applied=await self.application.apply(rid)
                return await self.finish_cleanup(rid,applied)
            if run is not None and not self.scope_matches(run,cast(str,source['entry_id'])):
                return await self.finish_cleanup(rid,await self.application.reject_scope(rid))
            if run is None:
                expired=await self.application.expire_unstarted(source)
                if type(expired) is not Found or record(expired.value).get('state')!='PARKED':return expired
            if run is None and (not fresh or not self.admitted()):return Found(MappingProxyType({'state':'PARKED','new_sends':0}))
            scope=self.scopes.get(cast(str,source['entry_id']))
            if scope is None:raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
            grant=self.tools.bind(scope.memory,cast(str,source['entry_id']),scope.partition_id,scope.subjects,scope.worlds,writable_objects=scope.writable,route_ids=scope.routes);self._grant=grant
            try:
                if run is None:
                    preparation=await self.runtime.assembly.read_daily_preparation(cast(str,source['batch_id']))
                    self._frozen=await self.collect(source,grant,cast(int,preparation['started_at_us'])+1200000000)
                    frozen=await self.reasoning.freeze_run(self._frozen,grant.digest,cast(int,preparation['started_at_us'])+1200000000)
                    if type(frozen) is not Committed:return frozen
                for _ in range(4):
                    try:
                        outcome=await self.reasoning.process(rid,grant,allow_first_send=fresh and self.admitted())
                        break
                    except OwnerFailure as failure:
                        network=self.runtime.provider.network
                        if failure.reason!='ADMISSION_FULL' or failure.cleanup_pending or network is None or network.observation().occupied or not fresh:raise
                        original=await self.reasoning.rows.read('reasoning_runs',rid)
                        if original is None:raise InvalidValue()
                        await network.wait_quiet(time.monotonic()+max(0,(cast(int,original['deadline_at_us'])-time.time_ns()//1000)/1000000))
                else:raise OwnerFailure('STORAGE_FAILED','state','INTEGRITY_FAILURE')
                if type(outcome) is not Found:return outcome
                state=record(outcome.value).get('state',record(outcome.value).get('phase'))
                if state in ('FINAL_READY','CANDIDATE_STORED','TERMINAL','REMOTE_UNKNOWN','DEADLINE'):
                    applied=await self.application.apply(rid)
                    return await self.finish_cleanup(rid,applied)
                return outcome
            finally:
                await self.reasoning.wait_actual();await self.application.wait_actual()
                self._frozen=None;self.tools.release(grant);self._grant=None
        task,logical=start_owned(advance());self._task=task;self.runtime.retain_external_work(task)
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None;self._original_entry=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,self.runtime.remaining_request()))
        return logical.result() if done else Found(MappingProxyType({'state':'PENDING','cleanup_pending':True}))

    async def finish_cleanup(self,run_id,applied):
        """Keep a confirmed receipt independent of later cleanup or scope changes."""
        if type(applied) is not Committed:return applied
        await self.reasoning.wait_actual();await self.application.wait_actual()
        try:released=await self.reasoning.retire_material(run_id)
        except InvalidValue:
            failure=OwnerFailure('STORAGE_FAILED','material','INTEGRITY_FAILURE')
            return self.failed_cleanup(run_id,applied,failure)
        except OwnerFailure as failure:
            return self.failed_cleanup(run_id,applied,failure)
        if type(released) is not Found:
            return self.failed_cleanup(run_id,applied,OwnerFailure('RESULT_UNCONFIRMED','material','COMMIT_UNCONFIRMED',True))
        if self.cleanup_failure is not None and self.cleanup_failure[0]==run_id:self.cleanup_failure=None
        return applied

    def failed_cleanup(self,run_id,applied,failure):
        self.cleanup_failure=(run_id,failure)
        network=self.runtime.provider.network
        if network is not None:network.pause()
        return Found(MappingProxyType({'state':'COMMITTED','commit_id':applied.receipt.commit_id,
            'operation_key':applied.receipt.identity.operation_key,'cleanup_pending':failure.cleanup_pending,
            'cleanup_state':'FAILED','cleanup_error':MappingProxyType({'code':failure.code,'operation':'retire_material',
                'field':failure.field,'reason':failure.reason}),'new_sends':0}))

    def close(self):
        self.closed=True
        return self._task is None and self._grant is None
