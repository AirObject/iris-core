"""Complete daily cognition host with one storage graph and one Provider owner.

Opening verifies native state without dispatch. Trusted setup supplies finite
entry scopes and independently reviewed send authority. Only a new explicit
resume opens the volatile network gate; original confirmation never does.
"""
from __future__ import annotations
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import time
from types import MappingProxyType
from typing import cast
from companion_memory.configuration import PresentValue
from companion_memory.configuration.daily_resolution import DailyConfigurationCandidate,daily_snapshot_issue
from companion_memory.configuration.daily_persistent_results import ConfigurationCommitted
from companion_memory.persistence import DatabaseResources,Ready,Found,NotFound,Committed,UnitOfWork
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.semantic_admission import SemanticAdmission
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.media.service import MediaResources
from companion_memory.self_model.approved_import_evidence import ApprovedPersonaEvidence
from companion_memory.self_model.daily_current import DailyCurrentPersona
from companion_memory.provider.daily_service import DailyProvider
from companion_memory.provider.daily_network import DailyNetwork
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.values import as_record
from companion_memory.memory.formats import record,sequence
from companion_memory.goals.service import GoalsService
from companion_memory.state.service import StateOwner
from companion_memory.retrieval.index import LocalIndex
from companion_memory.retrieval.semantic_work import SemanticWork
from companion_memory.retrieval.semantic_generation import SemanticGenerations
from companion_memory.retrieval.semantic_cache import SemanticQueryCache
from companion_memory.retrieval.semantic_files import VectorFiles
from companion_memory.retrieval.semantic_recovery import SemanticRecovery
from companion_memory.retrieval.semantic_query import SemanticQuery
from companion_memory.retrieval.query_service import QueryService
from companion_memory.cognition.daily_tools import DailyReadTools
from companion_memory.cognition.fixed_memory import FixedMemorySets,FixedReviewGrant
from companion_memory.cognition.fixed_memory_port import FixedMemoryPort
from companion_memory.information.business import BusinessService
from companion_memory.information.observation import InformationObservations
from companion_memory.information.management import HostIdentity
from companion_memory.management.information_http import InformationHTTP
from .content_service import ContentRuntimeService
from .content_assembly import stable
from .candidate_goals import CandidateGoalEffects
from .daily_assembly import DailyAssembly
from .daily_learning import DailyLearning,DailyEntryScope
from .daily_media import DailyMedia
from .semantic_authorization import SemanticActivation,SemanticAuthorization
from .semantic_management import SemanticManagementPort
from .daily_semantic import DailySemanticManagement

@dataclass(frozen=True,slots=True)
class DailyHostResources:
    """Explicit physical resources and original approved synthetic persona evidence."""
    root:Path
    database:DatabaseResources
    media:MediaResources
    instance_id:str
    configuration_key:str
    protected_directories:dict[str,tuple[str,...]|list[str]]
    initial_self:InitialSelfBinding
    persona_evidence:ApprovedPersonaEvidence|None
    review:FixedReviewGrant
    embedding_transport:ChatTransport
    transports:dict[str,ChatTransport]
    send_authorized:Callable[[str],bool]=lambda key:False
    embedding_authorized:Callable[[object,object],bool]=lambda description,intent:False

class DailyCognitionHost:
    """One public owner graph for learning, images, goals and semantic queries."""
    def __init__(self,configuration:DailyConfigurationCandidate,resources:DailyHostResources):
        if daily_snapshot_issue(configuration) is not None or type(resources) is not DailyHostResources:raise ValueError('Complete daily resources required.')
        if (type(resources.database) is not DatabaseResources or type(resources.media) is not MediaResources or type(resources.initial_self) is not InitialSelfBinding
                or type(resources.review) is not FixedReviewGrant or resources.review.claims['instance_id']!=resources.instance_id):raise ValueError('Native daily resource identities differ.')
        self.configuration=configuration;self.resources=resources;self.combination=DailyAssembly();self.storage=self.combination.storage
        self.assembly=self.combination.content;self.media=self.combination.media;self.management=self.combination.management
        self.state='NEW';self.phase='STORAGE';self._mode=None;self._closing=False;self._initialization:asyncio.Task|None=None;self._close_task:asyncio.Task|None=None
        self.admission=None;self.config=None;self.stored=None;self.runtime:ContentRuntimeService|None=None;self.provider:DailyProvider|None=None;self.network:DailyNetwork|None=None
        self.goals=None;self.current_state=None;self.retrieval=None;self.files=None;self.current_persona=None;self.initial_persona=None;self.learning=None;self.tools=None;self.images:DailyMedia|None=None
        self.queries=None;self.business=None;self.http=None;self.observations=None;self.fixed=None;self.initial=None;self.dispatch=None
        self._semantic_recovery=None;self._import_grant=None;self._information_at=time.time_ns()//1000;self._closed_owners:set[str]=set()
        self.authorization:SemanticAuthorization|None=None;self.semantic:SemanticManagementPort|None=None
        self._semantic_enabled=False
        self._scope_setup:dict[str,tuple[tuple[str,...]|None,str,tuple[str,...],tuple,tuple[str,...],tuple[str,...],tuple[str,...]]]={}

    def checkpoint(self):
        if self._closing or self.state in ('NEW','CLOSED') or self.admission is None:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        self.admission.checkpoint()

    def normal(self):
        self.checkpoint()
        if self.state!='READY' or self.runtime is None or self.runtime.gate.information_checkpoint() is None:raise OwnerFailure('MODE_BLOCKED','state','NOT_READY')

    def scheduling(self):
        trial=self.provider.trial_authorization if self.provider is not None else None
        return not self._closing and self.state=='READY' and self.combination.schedule.enabled and (self.learning is None or self.learning.cleanup_failure is None) and self.combination.initial_persona.cleanup_failure is None and self.combination.goal_comparisons.cleanup_failure is None and (trial is None or trial.active and not trial.faulted)

    def semantic_authorized(self,digest:str) -> bool:
        a=self.authorization
        return a is not None and a.activated and a.grant.digest==digest and cast(int,a.grant.binding['expires_at'])>time.time_ns()//1000

    def enable_semantic_dispatch(self) -> None:
        self.normal();self._semantic_enabled=True

    def observe_semantic_control(self,control) -> None:
        """The daily Provider uses the shared physical gate, never a copied clock."""
        if self.stored is None or control['config']['database_id']!=self.stored.database_id:raise InvalidValue()

    def semantic_dispatch_allowed(self,control) -> bool:
        return self.scheduling() and self._semantic_enabled and control['scheduler']=='ENABLED' and not (self.dispatch is not None and self.dispatch.pending)

    def permit_embedding(self,description,intent) -> bool:
        a=self.authorization
        return self.scheduling() and self._semantic_enabled and not (self.dispatch is not None and self.dispatch.pending) and a is not None and self.semantic_authorized(a.grant.digest) and a.permits(description,intent)

    def bind_activation(self,activation:SemanticActivation) -> None:
        """Bind reviewed original semantic slots; binding and reopening never send."""
        self.normal()
        if type(activation) is not SemanticActivation or self.authorization is not None or self.stored is None or self.admission is None:raise InvalidValue()
        b=activation.binding;claims=self.resources.review.claims
        if (b['format']!='DAILY_SEMANTIC_AUTH_V1' or b['instance_id']!=self.resources.instance_id or b['database_id']!=self.stored.database_id
                or b['config_snapshot_id']!=self.stored.snapshot_id or b['set_id']!=claims['set_id'] or b['review_digest']!=claims['review_digest']
                or b['execution']!=self.resources.embedding_transport.execution_kind):raise InvalidValue()
        self.authorization=SemanticAuthorization(self.resources.root,activation,self.admission)

    def bind_trial_activation(self,activation):
        """Bind independently approved mixed-purpose accounting, initially paused."""
        from .daily_trial_authorization import DailyTrialAuthorization
        self.normal()
        if self.provider is None or self.provider.trial_authorization is not None:raise InvalidValue()
        self.provider.trial_authorization=DailyTrialAuthorization(self.resources.root,activation,self.provider)
        return self.provider.trial_authorization

    def _normal_transaction(self,uow:UnitOfWork):
        self.normal()
        if not (self.runtime is not None):raise InvalidValue()
        observed=self.runtime.gate.information_checkpoint()
        uow.require_commit_permission(lambda:not self._closing and self.runtime is not None and self.runtime.gate.information_checkpoint()==observed)

    def configure_entry(self,entry_id:str,partition_id:str,subjects:tuple[str,...],worlds:tuple,*,object_ids:tuple[str,...]|None=None,
                        related:tuple[str,...]=(),writable:tuple[str,...]=(),routes:tuple[str,...]=()):
        """Bind trusted entry authority before startup, including recovery reconstruction."""
        if self.state!='NEW' or entry_id in self._scope_setup:raise ValueError('Entry setup is immutable after opening.')
        from companion_memory.persistence.schema import valid_identifier,freeze_value,SequenceSchema
        from companion_memory.memory.formats import WORLD
        if not all(valid_identifier(v) for v in (entry_id,partition_id,*subjects,*related,*writable,*routes)) or len(self._scope_setup)>=self.configuration.runtime.integer('runtime.read_page_size'):raise ValueError('Invalid entry scope.')
        frozen=freeze_value(SequenceSchema(WORLD,1,16),worlds,owned=True)
        self._scope_setup[entry_id]=(object_ids,partition_id,subjects,cast(tuple,frozen),related,writable,routes)

    async def initialize(self,mode:str):
        """Join bounded local opening; no phase activates network or reads credentials."""
        if mode not in ('CREATE_NEW','OPEN_EXISTING') or self._mode is not None and mode!=self._mode:raise ValueError('Original opening mode differs.')
        if self.state=='READY':return Found(MappingProxyType({'state':'READY','startup_sends':0}))
        if self._closing:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        self._mode=mode;self.state='RECOVERING';timeout=self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000
        if self._initialization is None or self._initialization.done():
            async def advance():
                with DeadlineScope(time.monotonic()+timeout):return await self._advance()
            self._initialization=asyncio.create_task(advance())
        done,_=await asyncio.wait((self._initialization,),timeout=timeout)
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        try:return self._initialization.result()
        except OwnerFailure as failure:
            from .results import Failed,RuntimeError
            return Failed(RuntimeError(failure.code,'initialize',failure.field,failure.reason,failure.cleanup_pending or self.storage.get_health().cleanup_pending))
        except (InvalidValue,ValueError):
            from .results import Failed,RuntimeError
            return Failed(RuntimeError('STORAGE_FAILED','initialize','storage','INTEGRITY_FAILURE',self.storage.get_health().cleanup_pending))

    async def _advance(self):
        if self._closing:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        c=self.combination;r=self.resources;instance=r.instance_id
        if not (self._mode is not None):raise InvalidValue()
        if self.phase=='STORAGE':
            if self.admission is None:
                self.admission=SemanticAdmission(self.configuration,r.root,self._mode);self.storage.bind_semantic_admission(self.admission)
            opened=await self.storage.initialize(self.configuration.foundation,r.database,self._mode)
            if type(opened) is not Ready:return opened
            from companion_memory.configuration.daily_codec import candidate_values
            binding={'configuration':candidate_values(self.configuration),'instance':instance,'configuration_key':r.configuration_key,
                'root':str(r.root),'root_device':r.root.stat().st_dev,'root_inode':r.root.stat().st_ino,'media':r.media.root_id,
                'initial_self':asdict(r.initial_self),'persona':{k:getattr(r.persona_evidence,k) for k in r.persona_evidence.__dataclass_fields__ if not k.startswith('_')} if r.persona_evidence is not None else None,
                'review':dict(r.review.claims)}
            digest=sha256(json.dumps(binding,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            c.initialization.bind(self.storage,r.database.expected_database_id,instance,digest,c.commands,create=self._mode=='CREATE_NEW',persona=r.persona_evidence is not None,configuration_key=r.configuration_key)
            self.phase='INTENT'
        if self.phase=='INTENT':
            intent=await c.initialization.load()
            if type(intent) is not Found:return intent
            self._information_at=c.initialization.original_time
            self.config=c.configuration.bind(self.storage,instance,self.configuration);self.phase='CONFIGURATION'
        if self.phase=='CONFIGURATION':
            if not (self.config is not None):raise InvalidValue()
            value=await self.config.persist_daily_configuration(r.configuration_key,self.configuration,actor='daily_cognition',protected_directories=r.protected_directories)
            if type(value) is not ConfigurationCommitted or value.configuration is None:return value
            self.stored=stored=value.configuration
            progress=await c.initialization.step('configuration',value.receipt)
            if type(progress) is not Committed:return progress
            roots=await self.config.initialize_business_roots(stable('initialize_daily_roots',r.configuration_key),stored)
            if type(roots) is not Committed:return roots
            progress=await c.initialization.step('roots',roots.receipt)
            if type(progress) is not Committed:return progress
            if not self.config.release_bootstrap_writers():raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
            if not c.initialization.release_bootstrap():raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
            self.media.bind(self.storage,stored,instance);self.assembly.bind(self.storage,stored,instance);c.materials.bind(self.storage,stored)
            self.goals=GoalsService(c.information_catalogs[2],self.storage,stored,instance);self.current_state=StateOwner(c.information_catalogs[1],self.storage,stored,instance)
            self.retrieval=LocalIndex(c.retrieval_catalog,self.storage,stored,instance);memory=self.assembly.memory.bind_information(stored);self.retrieval.bind_memory(memory)
            self.assembly.goal_effects=CandidateGoalEffects(self.goals,self.assembly.memory)
            accounts=next(e.state.value for e in self.configuration.foundation.list_entries() if e.definition.key=='provider.accounts' and type(e.state) is PresentValue)
            self.network=DailyNetwork(lambda key:not self._closing and self.state=='READY' and (self.scheduling() or c.initial_persona.explicit_send(key)) and r.send_authorized(key),tuple(cast(str,as_record(a)['account_id']) for a in cast(tuple,accounts)))
            def authorize(request,uow):
                if request.binding.role=='LEARNING':return c.reasoning.authorize_request(request,uow)
                if request.binding.role=='GOAL_DEDUP':return not (self.dispatch is not None and self.dispatch.pending) and c.goal_comparisons.authorize_request(request,uow)
                if request.binding.role=='MEDIA':return self.images is not None and self.images.authorize_request(request,uow)
                if request.binding.role=='PERSONA':return c.initial_persona.authorize_request(request,uow)
                return False
            def received(uow,request,handoff,proof):
                if request['task_role']=='LEARNING':return c.reasoning.verify_received(uow,request,handoff,proof)
                if request['task_role']=='GOAL_DEDUP':return c.goal_comparisons.verify_received(uow,request,handoff,proof)
                if request['task_role']=='MEDIA':return c.image_work.verify_received(uow,request,handoff,proof)
                if request['task_role']=='PERSONA':return c.initial_persona.verify_received(uow,request,handoff,proof)
                return False
            self.provider=DailyProvider(c.ledger.bind(self.storage,stored),stored,instance,c.semantic.commands,r.embedding_transport,self.checkpoint,
                self.permit_embedding,network=self.network,commands=c.daily_provider,
                materials=c.materials,transports=r.transports,authorize=authorize,received=received)
            c.embedding=self.provider;self.runtime=ContentRuntimeService(self.assembly,self.provider,None,cast(str,next(record(p)['profile_id'] for p in sequence(stored.candidate.text.record('provider.transport')['roles']) if record(p)['role']=='LEARNING')),None,embedding=self.provider)
            c.admit_normal=self._normal_transaction;c.initial_self.admit=self._normal_transaction
            publisher=self.config
            c.initializer.bind(self.storage,stored,instance,{'goals':self.goals,'state':self.current_state,'retrieval':self.retrieval,'memory':memory},lambda uow:publisher.participate_snapshot(uow,stored))
            self.management.bind(self.storage,stored,instance,self.goals,self.current_state,self.retrieval,self.runtime.gate,self.runtime.retain_external_work,lambda:self.state=='RECOVERING' and self.phase=='RECOVERY')
            if not (self.assembly.memory.semantic is not None):raise InvalidValue()
            c.work=SemanticWork(c.retrieval_catalog,self.storage,stored,instance,self.assembly.memory.semantic,self.provider,c.commands,self.checkpoint,self.normal,self.semantic_authorized)
            self.files=VectorFiles(Path(cast(str,stored.candidate.text.record('retrieval.semantic_storage')['index_root'])),file_limit_bytes=41943040,index_total_bytes=83886080,reader_limit=2)
            c.generations=SemanticGenerations(c.retrieval_catalog,self.storage,stored,instance,self.assembly.memory.semantic,self.files,self.checkpoint)
            c.cache=SemanticQueryCache(c.retrieval_catalog,self.storage,stored,instance,next(d for d in c.semantic.commands if d.operation_kind=='record_result'))
            self._semantic_recovery=SemanticRecovery(c.work,c.generations)
            fixed_catalog=next(cat for cat in self.assembly.catalogs if cat.definition.owner_module=='cognition')
            c.fixed=FixedMemorySets(fixed_catalog,self.storage,stored,self.assembly.memory,r.review,next(d for d in c.semantic.commands if d.operation_kind=='fixed_establish'),self.normal)
            self.fixed=FixedMemoryPort(c.fixed,c.semantic.commands,self.normal);self.initial=c.initial_self.bind(self.assembly.memory,r.initial_self);c.subject_origin=r.initial_self.input_origin
            self._import_grant=None
            if r.persona_evidence is not None:
                self._import_grant=c.persona.bind(self.storage,stored,self.assembly.memory,r.initial_self,r.persona_evidence,new_synthetic_instance=c.initialization.unfinished)
                self.current_persona=DailyCurrentPersona(c.persona,self.runtime.gate)
            else:
                c.initial_persona.bind(self.storage,stored,self.runtime,self.initial,self.provider,r.initial_self.actor_ref)
                self.initial_persona=c.initial_persona.port
                self.current_persona=DailyCurrentPersona(c.initial_persona,self.runtime.gate)
            semantic=SemanticQuery(c.work,c.cache,c.generations)
            self.tools=DailyReadTools(self.runtime.memory,self.retrieval,semantic,self.goals,self.normal)
            self.learning=DailyLearning(self.runtime,c.reasoning,c.application,self.tools,self.current_persona.port,self.scheduling)
            self.images=DailyMedia(self.runtime,c.image_work,self.scheduling);self.runtime.media=self.images
            for entry_id,(object_ids,partition,subjects,worlds,related,writable,routes) in self._scope_setup.items():
                port=self.runtime.memory.bind_query_scope(include_forgotten=False,object_ids=object_ids)
                self.learning.bind_entry(entry_id,DailyEntryScope(port,partition,subjects,worlds,related,writable,routes))
            c.goal_comparisons.bind(self.goals,stored,self.provider,self.normal);c.schedule.bind(self.storage,stored,self.checkpoint)
            from .daily_dispatch import DailyDispatch
            self.dispatch=DailyDispatch(self.runtime,c.schedule,self.normal,self.scheduling)
            self.phase='OWNERS'
        if self.phase=='OWNERS':
            if not (self.provider is not None and self.runtime is not None):raise InvalidValue()
            await self.provider.initialize();self.checkpoint()
            original_media=await c.initialization.receipt('media','initialize_media_root','media-root')
            if type(original_media) not in (Found,NotFound):return original_media
            media_mode='CREATE_NEW' if type(original_media) is NotFound and c.initialization.unfinished else 'OPEN_EXISTING'
            media=await self.media.initialize(r.media,media_mode,continuation=c.initialization if media_mode=='CREATE_NEW' else None)
            if type(media) is not Found or media.value['state']!='READY':return media
            original_media=await c.initialization.receipt('media','initialize_media_root','media-root')
            if type(original_media) is not Found:return original_media
            progress=await c.initialization.step('media',original_media.value)
            if type(progress) is not Committed:return progress
            if c.initialization.unfinished:
                for kind,key,payload in (('initialize_content_runtime',stable('initialize_runtime',instance),{}),):
                    value=await self.runtime.execute(kind,key,payload)
                    if type(value) is not Committed:return value
                    progress=await c.initialization.step('runtime',value.receipt)
                    if type(progress) is not Committed:return progress
                value=await c.initializer.initialize(stable('initialize_information',instance),self._information_at)
                if type(value) is not Committed:return value
                progress=await c.initialization.step('information',value.receipt)
                if type(progress) is not Committed:return progress
                value=await c.schedule.execute('initialize_daily_schedule',stable('initialize_daily_schedule',instance),{},'daily_cognition')
                if type(value) is not Committed:return value
                progress=await c.initialization.step('schedule',value.receipt)
                if type(progress) is not Committed:return progress
                if r.persona_evidence is not None:
                    if self._import_grant is None:raise ValueError('Original import grant missing.')
                    value=await c.persona.import_approved_persona(self._import_grant,stable('approved_persona_import',instance))
                    if type(value) is not Committed:return value
                    progress=await c.initialization.step('persona',value.receipt)
                    if type(progress) is not Committed:return progress
            self.phase='RECOVERY'
        if self.phase=='RECOVERY':
            if not (self.runtime is not None and self.stored is not None and self.current_persona is not None):raise InvalidValue()
            await c.initializer.recover(stable('initialize_information',instance));self.checkpoint()
            if r.persona_evidence is not None:
                value=await c.persona.read_current()
                if type(value) is not Found:return value
            recovered=await self.runtime.initialize()
            if type(recovered) is not Found or recovered.value['state']!='READY':return recovered
            if c.initial_persona.bound:
                recovered=await c.initial_persona.control.recover()
                if recovered is not None:return recovered
            if self.dispatch is None:raise InvalidValue()
            await self.dispatch.recover()
            goal_recovery=await c.goal_comparisons.recover()
            if goal_recovery is not None:return goal_recovery
            if not (self.management.local_recovery is not None and self._semantic_recovery is not None):raise InvalidValue()
            recovered=await self.management.local_recovery.run()
            if type(recovered) is not Found:return recovered
            deadline=time.monotonic()+self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000
            if self._mode=='OPEN_EXISTING' and c.fixed is not None and not await c.fixed.recover(deadline):raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
            await self._semantic_recovery.run(deadline);self.checkpoint()
            if not (c.work is not None and c.cache is not None and c.generations is not None and self.retrieval is not None and self.current_state is not None and self.goals is not None):raise InvalidValue()
            self.queries=QueryService(self.stored,self.runtime,self.management,self.retrieval,self.current_state,self.goals,current_persona=self.current_persona.port,semantic=SemanticQuery(c.work,c.cache,c.generations))
            self.business=BusinessService(self.management,self.queries);self.http=InformationHTTP(self.stored,self.runtime.gate)
            from .daily_observation import bind_daily_observations
            self.observations=InformationObservations(self.runtime,self.management.tickets,self.current_state,self.goals,semantic=c.work,cache=c.cache,daily=bind_daily_observations(self))
            self.semantic=DailySemanticManagement(self)
            progress=await c.initialization.finish()
            if type(progress) not in (Found,Committed):return progress
            self.phase='COMPLETE';self.state='READY'
            if self.dispatch is None:raise InvalidValue()
            self.dispatch.restore_entries(tuple(self._scope_setup))
            return Found(MappingProxyType({'state':'READY','startup_sends':0,'scheduler':'PAUSED','provider_owner_count':1}))
        raise OwnerFailure('INVALID_STATE','state','NOT_READY')

    async def register_entry(self,key:str,entry_id:str,host_id:str,platform_id:str,external_entry_id:str):
        self.normal()
        if not (self.runtime is not None):raise InvalidValue()
        return await self.runtime.execute('register_content_entry',key,{'entry_id':entry_id,'host_id':host_id,'platform_id':platform_id,'external_entry_id':external_entry_id})

    async def register_initial_subjects(self,key:str,subjects:object,input_origin:str):
        """Register the bounded trusted initial roster through memory's native command."""
        from companion_memory.persistence import ResultBoundCommand
        self.normal();definition=self.combination.initial_subjects
        command=ResultBoundCommand(definition.command_version,{'operation_id':key,'subjects':subjects,'input_origin':input_origin},
            {a.event_slot:{'actor':self.resources.initial_self.actor_ref} for a in definition.required_audits})
        return await self.storage.bind_operation(definition,self.resources.instance_id).execute(key,command)

    def bind_entry(self,entry_id:str):
        self.normal()
        if not (self.runtime is not None):raise InvalidValue()
        if entry_id not in self._scope_setup:raise OwnerFailure('ACCESS_DENIED','entry','BINDING_MISMATCH')
        if self.dispatch is None:raise InvalidValue()
        return self.dispatch.bind(entry_id)

    async def resume_learning(self,key:str):
        self.normal()
        if not (self.runtime is not None and self.network is not None):raise InvalidValue()
        schedule=self.combination.schedule
        original=await schedule.confirm_control('resume_learning',key)
        if original is not None:return original
        if self.provider is not None and self.provider.get_health().unknown_observations:
            raise OwnerFailure('INVALID_STATE','request','REMOTE_RESULT_UNKNOWN')
        if self.learning is not None and self.learning.cleanup_failure is not None:
            failure=self.learning.cleanup_failure[1]
            raise OwnerFailure(failure.code,failure.field,failure.reason,failure.cleanup_pending)
        failure=self.combination.initial_persona.cleanup_failure
        if self.combination.goal_comparisons.cleanup_failure is not None:failure=self.combination.goal_comparisons.cleanup_failure[1]
        if failure is not None:raise OwnerFailure(failure.code,failure.field,failure.reason,failure.cleanup_pending)
        if self.provider is not None and self.provider.trial_authorization is not None and not self.provider.trial_authorization.active:
            raise OwnerFailure('ACCESS_DENIED','authorization','OPERATION_NOT_GRANTED')
        current=await schedule.current()
        if current is None:raise ValueError('Original schedule missing.')
        outcome=await schedule.execute('resume_learning',key,{'expected_revision':current['revision'],'mode_epoch':self.runtime.gate.epoch},'daily_cognition')
        if type(outcome) is Committed and outcome.source=='NEW':
            self.network.resume()
            if self.dispatch is not None:self.dispatch.wake()
        return outcome

    async def pause_learning(self,key:str):
        self.normal()
        if not (self.runtime is not None and self.network is not None):raise InvalidValue()
        schedule=self.combination.schedule
        original=await schedule.confirm_control('pause_learning',key)
        if original is not None:return original
        self.network.pause();current=await schedule.current()
        if current is None:raise ValueError('Original schedule missing.')
        return await schedule.execute('pause_learning',key,{'expected_revision':current['revision'],'mode_epoch':self.runtime.gate.epoch},'daily_cognition')

    async def request_learning(self,key:str,entry_id:str,*,target_through_seq:int|None=None):
        """Trusted internal ACTIVE trigger shares the entry scheduler and FIFO."""
        self.normal()
        if self.dispatch is None or entry_id not in self._scope_setup:raise InvalidValue()
        return await self.dispatch.request(entry_id,key,'ACTIVE',target_through_seq)

    async def compare_goal(self,key:str,task_id:str,task_revision:int,goal_id:str,goal_revision:int):
        """Trusted native goal processing shares the host's admission and Provider."""
        self.normal()
        if self.dispatch is not None and self.dispatch.pending:raise OwnerFailure('RESOURCE_BUSY','goal','ADMISSION_FULL')
        if self.goals is None:raise InvalidValue()
        goal=await self.goals.lookup(goal_id)
        if goal is None or goal['entry_id'] not in self._scope_setup:raise OwnerFailure('ACCESS_DENIED','goal','BINDING_MISMATCH')
        comparisons=self.combination.goal_comparisons
        prepared=await comparisons.prepare(key,task_id,task_revision,goal_id,goal_revision)
        if type(prepared) is not Committed:return prepared
        return await comparisons.drive(comparisons.decision_id(task_id),allow_first_send=self.scheduling())

    async def bind_business(self,identity:HostIdentity,*,include_forgotten:bool=False,object_ids:tuple[str,...]|None=None):
        self.normal()
        if not (self.business is not None):raise InvalidValue()
        if type(identity) is not HostIdentity or not await self.assembly.ingress.verify_host_entry(identity.entry_id,identity.host_id):raise ValueError('Registered host identity required.')
        return self.business.bind(identity,include_forgotten=include_forgotten,object_ids=object_ids)

    async def bind_query(self,identity:HostIdentity,*,include_forgotten:bool=False,object_ids:tuple[str,...]|None=None):
        self.normal()
        if self.queries is None:raise InvalidValue()
        if type(identity) is not HostIdentity or not await self.assembly.ingress.verify_host_entry(identity.entry_id,identity.host_id):raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        return self.queries.bind(identity,include_forgotten=include_forgotten,object_ids=object_ids)

    async def bind_management(self,identity:HostIdentity):
        self.normal()
        if type(identity) is not HostIdentity or not await self.assembly.ingress.verify_host_entry(identity.entry_id,identity.host_id):raise ValueError('Registered host identity required.')
        return self.management.issue(identity)

    async def close(self):
        if self.state=='CLOSED':return True
        self._closing=True;self.state='CLOSING'
        self._semantic_enabled=False
        if self.network is not None:self.network.pause()
        if self.runtime is not None:self.runtime.stop_admission()
        if self.http is not None:self.http.stop_admission()
        if self.current_persona is not None:self.current_persona.close()
        if self.combination.initial_persona.bound:self.combination.initial_persona.control.closed=True
        if self._close_task is None or self._close_task.done():self._close_task=asyncio.create_task(self._close())
        done,_=await asyncio.wait((self._close_task,),timeout=self.configuration.runtime.integer('runtime.close_timeout_ms')/1000)
        return bool(done and self._close_task.result())

    async def _close(self):
        c=self.combination
        if self.dispatch is not None and not self.dispatch.close():return False
        if self._initialization is not None and not self._initialization.done():return False
        if self.semantic is not None and not await self.semantic.wait_actual(5):return False
        if self.learning is not None and not self.learning.close():return False
        if self.images is not None and not self.images.close():return False
        if not c.image_work.close():return False
        if not c.schedule.close() or not c.reasoning.close() or not c.application.close() or not c.goal_comparisons.close():return False
        if self.tools is not None and not self.tools.close():return False
        if self.http is not None and not await self.http.close():return False
        if self.provider is not None and not await self.provider.close(time.monotonic()+5):return False
        if c.cache is not None and not c.cache.close():return False
        if c.generations is not None and c.generations.io_pending:return False
        if self.files is not None and not self.files.close():return False
        if self.fixed is not None:self.fixed.close()
        c.initial_self.close()
        if not c.persona.close() or not c.initial_persona.close() or not c.materials.close():return False
        c.persona_mode.close()
        async def owners():
            if not c.initialization.close():return False
            if self.runtime is not None and not await self.runtime.close():return False
            for name,owner in (('retrieval',self.retrieval),('goals',self.goals),('state',self.current_state)):
                if owner is not None and name not in self._closed_owners:
                    if not owner.close():return False
                    self._closed_owners.add(name)
            if self.stored is not None and 'media' not in self._closed_owners:
                if not await self.media.close():return False
                self._closed_owners.add('media')
            if self.stored is not None:self.assembly.close()
            if self.config is not None:self.config.close()
            return True
        if not await self.storage.coordinate_owner_shutdown(asyncio.create_task(owners())):return False
        await self.storage.close()
        if self.storage.get_health().lifecycle!='CLOSED':return False
        if self.admission is not None:self.admission.close()
        if self.authorization is not None:self.authorization.close()
        self.state='CLOSED';return True
