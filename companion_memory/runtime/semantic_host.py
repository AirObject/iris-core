"""Public semantic host with native owners and startup that never sends.

Configuration, information roots and fixed material remain independent from
activation. Explicit management drives one original work at a time. Closing
revokes admission first and retains the database until actual consumers end.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
import time
from types import MappingProxyType
from companion_memory.configuration.semantic_resolution import SemanticConfigurationCandidate,semantic_snapshot_issue
from companion_memory.configuration.semantic_persistent_results import ConfigurationCommitted
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from companion_memory.persistence import DatabaseResources,Ready,Found,Committed,ResultBoundCommand,UnitOfWork
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_records import Record,number,string
from companion_memory.persistence.semantic_admission import SemanticAdmission
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.media.service import MediaResources
from companion_memory.cognition.fixed_memory import FixedReviewGrant,FixedMemorySets
from companion_memory.cognition.fixed_memory_port import FixedMemoryPort
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.provider.embedding_service import EmbeddingProvider
from companion_memory.provider.chat_transport import ChatTransport,WireObservation
from companion_memory.goals.service import GoalsService
from companion_memory.state.service import StateOwner
from companion_memory.retrieval.index import LocalIndex
from companion_memory.retrieval.semantic_files import VectorFiles
from companion_memory.retrieval.semantic_generation import SemanticGenerations
from companion_memory.retrieval.semantic_cache import SemanticQueryCache
from companion_memory.retrieval.semantic_work import SemanticWork
from companion_memory.retrieval.semantic_recovery import SemanticRecovery
from companion_memory.retrieval.query_service import QueryService
from companion_memory.retrieval.semantic_query import SemanticQuery
from companion_memory.information.business import BusinessService
from companion_memory.information.observation import InformationObservations
from companion_memory.information.management import HostIdentity
from companion_memory.management.information_http import InformationHTTP
from .content_service import ContentRuntimeService
from .content_assembly import stable
from .semantic_assembly import SemanticAssembly
from .semantic_authorization import SemanticActivation,SemanticAuthorization
from .semantic_cold_query import ControlledColdGrant,ControlledColdQueries


@dataclass(frozen=True,slots=True)
class SemanticHostResources:
    """Explicit local resources and native reviewed-material authority."""
    root: Path
    database: DatabaseResources
    media: MediaResources
    instance_id: str
    configuration_key: str
    protected_directories: dict[str,tuple[str,...]|list[str]]
    initial_self: InitialSelfBinding
    review: FixedReviewGrant
    transport: ChatTransport|None


class SemanticHost:
    """Own the complete static semantic combination and its local lifecycle."""
    def __init__(self,configuration: SemanticConfigurationCandidate,resources: SemanticHostResources):
        if (semantic_snapshot_issue(configuration) is not None or type(resources) is not SemanticHostResources
                or type(resources.database) is not DatabaseResources or type(resources.media) is not MediaResources
                or type(resources.initial_self) is not InitialSelfBinding or type(resources.review) is not FixedReviewGrant
                or resources.review.claims['instance_id']!=resources.instance_id):raise ValueError('Complete native semantic resources required.')
        self.configuration=configuration;self.resources=resources
        self.combination=SemanticAssembly(usage_only=configuration.text.record('provider.embedding_transport')['v']==2);self.storage=self.combination.storage
        self.assembly=self.combination.content;self.media=self.combination.media;self.management=self.combination.management
        self.state='NEW';self.phase='STORAGE';self._mode: str|None=None;self._closing=False
        self.admission:SemanticAdmission|None=None;self.authorization:SemanticAuthorization|None=None
        self.runtime:ContentRuntimeService|None=None;self.embedding:EmbeddingProvider|None=None
        self.stored:StoredSemanticConfiguration|None=None;self.config=None;self.goals=None;self.current_state=None;self.retrieval=None
        self.fixed:FixedMemoryPort|None=None;self.files:VectorFiles|None=None;self.queries=None;self.business=None;self.http=None
        self.semantic=None;self.initial=None;self._initialization:asyncio.Task[object]|None=None;self._close_task:asyncio.Task[bool]|None=None
        self.observations:InformationObservations|None=None;self._semantic_recovery:SemanticRecovery|None=None
        self._scheduling=False;self._dispatch_control:Record|None=None;self._closed_owners:set[str]=set()
        self._information_at=time.time_ns()//1000
        self.cold:ControlledColdQueries|None=None

    def checkpoint(self) -> None:
        if self._closing and self.phase!='COMPLETE':self._initializing_checkpoint()
        if self.state in ('NEW','CLOSED') or self.admission is None:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        self.admission.checkpoint()

    def normal(self) -> None:
        self.checkpoint()
        if self.state!='READY' or self.runtime is None or self.runtime.gate.information_checkpoint() is None:
            raise OwnerFailure('MODE_BLOCKED','state','NOT_READY')

    def enable_semantic_dispatch(self) -> None:
        """Activate this host's volatile semantic gate after confirmed resume."""
        self._scheduling=True

    def observe_semantic_control(self,control:Record) -> None:
        self._dispatch_control=control

    def semantic_dispatch_allowed(self,control:Record) -> bool:
        return self._scheduling and control['scheduler']=='ENABLED' and (control['last_cleanup_at'] is None or time.time_ns()//1000-number(control['last_cleanup_at'])>=30000000)

    def _authorized(self,digest: str) -> bool:
        authorization=self.authorization
        return (authorization is not None and authorization.grant.digest==digest and authorization._activated
            and number(authorization.grant.binding['expires_at'])>time.time_ns()//1000)

    def _permit(self,description: Record,intent: Record) -> bool:
        try:self.normal()
        except OwnerFailure:return False
        control=self._dispatch_control;authorization=self.authorization
        return (self._scheduling and control is not None and control['scheduler']=='ENABLED' and authorization is not None
            and self._authorized(authorization.grant.digest) and authorization.permits(description,intent)
            and (control['last_cleanup_at'] is None or time.time_ns()//1000-number(control['last_cleanup_at'])>=30000000))

    def _dispatch(self,description:Record,intent:Record,start:Callable[[],asyncio.Future[WireObservation]]) -> asyncio.Future[WireObservation]:
        self.normal();runtime=self.runtime;assert runtime is not None
        with runtime.gate.lock:
            if (self.state!='READY' or not self._scheduling or runtime.gate.information_checkpoint() is None
                    or self.authorization is None or not self.authorization.permits(description,intent)
                    or time.time_ns()//1000>=number(intent['expires_at'])):raise OwnerFailure('MODE_BLOCKED','state','NOT_READY')
            return start()

    def _normal_transaction(self,uow:UnitOfWork) -> None:
        self.normal();runtime=self.runtime;assert runtime is not None
        observed=runtime.gate.information_checkpoint()
        def allowed():
            try:self.normal()
            except OwnerFailure:return False
            return runtime.gate.information_checkpoint()==observed
        uow.require_commit_permission(allowed)

    async def initialize(self,mode: str) -> object:
        """Initialize or verify the original instance without activating its package."""
        if mode not in ('CREATE_NEW','OPEN_EXISTING') or self._mode is not None and self._mode!=mode:raise ValueError('Original opening mode differs.')
        if self.state=='READY':return Found(MappingProxyType({'state':'READY','startup_sends':0}))
        if self.state in ('CLOSING','CLOSED'):raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        self._mode=mode;self.state='RECOVERING'
        if self._initialization is None or self._initialization.done():
            async def advance():
                with DeadlineScope(time.monotonic()+self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000):return await self._advance()
            self._initialization=asyncio.create_task(advance())
        done,_=await asyncio.wait((self._initialization,),timeout=self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000)
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        return self._initialization.result()

    async def _advance(self) -> object:
        self._initializing_checkpoint()
        c=self.combination;instance=self.resources.instance_id
        if self.phase=='STORAGE':
            assert self._mode is not None
            if self.admission is None:
                self.admission=SemanticAdmission(self.configuration,self.resources.root,self._mode)
                self.storage.bind_semantic_admission(self.admission)
            opened=await self.storage.initialize(self.configuration.foundation,self.resources.database,self._mode)
            self._initializing_checkpoint()
            if type(opened) is not Ready:return opened
            self.config=c.configuration.bind(self.storage,instance,self.configuration);self.phase='CONFIGURATION'
        if self.phase=='CONFIGURATION':
            assert self.config is not None
            result=await self.config.persist_semantic_configuration(self.resources.configuration_key,self.configuration,
                actor='semantic_host',protected_directories=self.resources.protected_directories)
            self._initializing_checkpoint()
            if type(result) is not ConfigurationCommitted or result.configuration is None:return result
            self.stored=result.configuration
            if not self.config.release_bootstrap_writers():raise OwnerFailure('RESOURCE_BUSY','storage','CLEANUP_PENDING',True)
            stored=self.stored
            self.media.bind(self.storage,stored,instance);self.assembly.bind(self.storage,stored,instance)
            catalogs={catalog.definition.owner_module:catalog for catalog in c.information_catalogs}
            self.goals=GoalsService(catalogs['goals'],self.storage,stored,instance)
            self.current_state=StateOwner(catalogs['state'],self.storage,stored,instance)
            self.retrieval=LocalIndex(catalogs['retrieval'],self.storage,stored,instance)
            memory=self.assembly.memory.bind_information(stored);self.retrieval.bind_memory(memory)
            from .candidate_goals import CandidateGoalEffects
            self.assembly.goal_effects=CandidateGoalEffects(self.goals,self.assembly.memory)
            self.embedding=EmbeddingProvider(c.ledger.bind(self.storage,stored),stored,instance,c.semantic.commands,self.resources.transport,self.checkpoint,self._permit,self._dispatch)
            c.embedding=self.embedding
            self.runtime=ContentRuntimeService(self.assembly,c.local_provider,SyntheticCandidateInput('disabled_generation',(),0),
                'unavailable_generation',None,embedding=self.embedding)
            c.admit_normal=self._normal_transaction;c.initial_self.admit=self._normal_transaction
            publisher=self.config
            c.initializer.bind(self.storage,stored,instance,{'goals':self.goals,'state':self.current_state,'retrieval':self.retrieval,'memory':memory},
                lambda uow:publisher.participate_snapshot(uow,stored))
            self.management.bind(self.storage,stored,instance,self.goals,self.current_state,self.retrieval,self.runtime.gate,
                self.runtime.retain_external_work,lambda:self.state=='RECOVERING' and self.phase=='RECOVERY')
            assert self.assembly.memory.semantic is not None
            c.work=SemanticWork(c.retrieval_catalog,self.storage,stored,instance,self.assembly.memory.semantic,self.embedding,c.commands,
                self.checkpoint,self.normal,self._authorized)
            self.files=VectorFiles(Path(string(stored.candidate.text.record('retrieval.semantic_storage')['index_root'])),
                file_limit_bytes=41943040,index_total_bytes=83886080,reader_limit=2)
            c.generations=SemanticGenerations(c.retrieval_catalog,self.storage,stored,instance,self.assembly.memory.semantic,self.files,self.checkpoint)
            self._semantic_recovery=SemanticRecovery(c.work,c.generations)
            c.cache=SemanticQueryCache(c.retrieval_catalog,self.storage,stored,instance,next(d for d in c.semantic.commands if d.operation_kind=='record_result'))
            fixed_catalog=next(catalog for catalog in self.assembly.catalogs if catalog.definition.owner_module=='cognition')
            c.fixed=FixedMemorySets(fixed_catalog,self.storage,stored,self.assembly.memory,self.resources.review,
                next(d for d in c.semantic.commands if d.operation_kind=='fixed_establish'),self.normal)
            self.fixed=FixedMemoryPort(c.fixed,c.semantic.commands,self.normal)
            self.initial=c.initial_self.bind(self.assembly.memory,self.resources.initial_self);c.subject_origin=self.resources.initial_self.input_origin
            self.phase='OWNERS'
        if self.phase=='OWNERS':
            assert self.embedding is not None and self.runtime is not None and self._mode is not None
            await self.embedding.initialize()
            self._initializing_checkpoint()
            media=await self.media.initialize(self.resources.media,self._mode)
            self._initializing_checkpoint()
            if type(media) is not Found or media.value['state']!='READY':return media
            if self._mode=='CREATE_NEW':
                initialized=await self.runtime.execute('initialize_content_runtime',stable('initialize_runtime',instance),{})
                self._initializing_checkpoint()
                if type(initialized) is not Committed:return initialized
                initialized=await c.initializer.initialize(stable('initialize_information',instance),self._information_at)
                self._initializing_checkpoint()
                if type(initialized) is not Committed:return initialized
            self.phase='RECOVERY'
        if self.phase=='RECOVERY':
            assert self.runtime is not None
            await c.initializer.recover(stable('initialize_information',instance))
            self._initializing_checkpoint()
            recovered=await self.runtime.initialize()
            self._initializing_checkpoint()
            if type(recovered) is not Found or recovered.value['state']!='READY':return recovered
            assert self.management.local_recovery is not None
            recovered=await self.management.local_recovery.run()
            self._initializing_checkpoint()
            if type(recovered) is not Found:return recovered
            if self._mode=='OPEN_EXISTING':
                assert c.fixed is not None
                if not await c.fixed.recover(time.monotonic()+self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000):
                    raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
                self._initializing_checkpoint()
            assert self._semantic_recovery is not None
            await self._semantic_recovery.run(time.monotonic()+self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000)
            self._initializing_checkpoint()
            assert self.stored is not None and self.retrieval is not None and self.current_state is not None and self.goals is not None
            assert c.work is not None and c.cache is not None and c.generations is not None
            self.queries=QueryService(self.stored,self.runtime,self.management,self.retrieval,self.current_state,self.goals,
                semantic=SemanticQuery(c.work,c.cache,c.generations))
            self.business=BusinessService(self.management,self.queries);self.http=InformationHTTP(self.stored,self.runtime.gate)
            self.observations=InformationObservations(self.runtime,self.management.tickets,self.current_state,self.goals,semantic=c.work,cache=c.cache)
            from .semantic_management import SemanticManagementPort
            self.semantic=SemanticManagementPort(self)
            self.phase='COMPLETE';self.state='READY'
            return Found(MappingProxyType({'state':'READY','startup_sends':0,'embedding':True,'generation':False}))
        raise OwnerFailure('INVALID_STATE','state','NOT_READY')

    def _initializing_checkpoint(self) -> None:
        """A late completion may release its owner, but cannot resume startup."""
        if self._closing:
            raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')

    def bind_activation(self,activation: SemanticActivation) -> None:
        """Bind verified facts; only explicit resume can activate the frozen package."""
        self.normal()
        if self.authorization is not None or self.stored is None or self.admission is None or type(activation) is not SemanticActivation:raise ValueError('One native activation required.')
        binding=activation.binding;claims=self.resources.review.claims
        if (binding['format']=='SEMANTIC_TRIAL_AUTH_V2')!=(self.configuration.text.record('provider.embedding_transport')['v']==2):
            raise ValueError('Activation metering format differs from the frozen configuration.')
        if (binding['instance_id']!=self.resources.instance_id or binding['database_id']!=self.stored.database_id or binding['config_snapshot_id']!=self.stored.snapshot_id
                or binding['set_id']!=claims['set_id'] or binding['review_digest']!=claims['review_digest']):raise ValueError('Activation and reviewed instance differ.')
        documents=binding['document_ids'];assert type(documents) is tuple
        expected_count=4096 if self.configuration.text.record('retrieval.embedding')['qualification_profile']=='OFFLINE_CAPACITY' else 12
        if len(documents)!=expected_count:raise ValueError('Activation purpose count differs from the frozen tier.')
        transport=self.resources.transport
        expected='SIMULATED' if transport is None else 'CONTROLLED' if transport._endpoint.host=='127.0.0.1' else 'REAL'
        if binding['execution']!=expected:raise ValueError('Actual transport and activation origin differ.')
        self.authorization=SemanticAuthorization(self.resources.root,activation,self.admission)

    def bind_controlled_cold_queries(self,grant:ControlledColdGrant) -> None:
        """Bind extra controlled-use authority; real prewarming slots stay closed."""
        self.normal()
        if self.cold is not None or self.queries is None or self.queries.semantic is None:
            raise ValueError('One ready cold-query owner is required.')
        self.cold=ControlledColdQueries(self,grant);self.queries.semantic.cold=self.cold

    async def register_entry(self,key: str,entry_id: str,host_id: str,platform_id: str,external_entry_id: str) -> object:
        self.normal();assert self.runtime is not None
        return await self.runtime.execute('register_content_entry',key,{'entry_id':entry_id,'host_id':host_id,'platform_id':platform_id,'external_entry_id':external_entry_id})

    async def register_initial_subjects(self,key: str,subjects: object,input_origin: str) -> object:
        self.normal();definition=self.combination.initial_subjects
        return await self.storage.bind_operation(definition,self.resources.instance_id).execute(key,ResultBoundCommand(definition.command_version,
            {'operation_id':key,'subjects':subjects,'input_origin':input_origin},{a.event_slot:{'actor':self.resources.initial_self.actor_ref} for a in definition.required_audits}))

    async def bind_query(self,identity: HostIdentity,*,include_forgotten: bool=False,object_ids: tuple[str,...]|None=None):
        self.normal();assert self.queries is not None
        if type(identity) is not HostIdentity or not await self.assembly.ingress.verify_host_entry(identity.entry_id,identity.host_id):raise ValueError('Registered host identity required.')
        return self.queries.bind(identity,include_forgotten=include_forgotten,object_ids=object_ids)

    async def bind_business(self,identity: HostIdentity,*,include_forgotten: bool=False,object_ids: tuple[str,...]|None=None):
        self.normal();assert self.business is not None
        if type(identity) is not HostIdentity or not await self.assembly.ingress.verify_host_entry(identity.entry_id,identity.host_id):raise ValueError('Registered host identity required.')
        return self.business.bind(identity,include_forgotten=include_forgotten,object_ids=object_ids)

    async def bind_management(self,identity:HostIdentity):
        """Issue the original local management port after registration checks."""
        self.normal()
        if type(identity) is not HostIdentity or not await self.assembly.ingress.verify_host_entry(identity.entry_id,identity.host_id):
            raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        return self.management.issue(identity)

    def bind_observation(self,scopes:frozenset[str]):
        """Issue existing observation authority without generation or model work."""
        if self.state!='READY' or self.observations is None:raise ValueError('Observation owner is not ready.')
        from companion_memory.provider.embedding_observation import observer
        assert self.embedding is not None
        return self.observations.bind(scopes,observer(self.embedding) if any(s.startswith('provider/') for s in scopes) else None)

    async def close(self) -> bool:
        """Stop admission immediately; incomplete cleanup retains original owners."""
        if self.state=='CLOSED':return True
        self._closing=True;self.state='CLOSING';self._scheduling=False
        if self.runtime is not None:self.runtime.stop_admission()
        if self.fixed is not None:self.fixed.close()
        self.combination.initial_self.close()
        if self.http is not None:self.http.stop_admission()
        if self._close_task is None or self._close_task.done():self._close_task=asyncio.create_task(self._close())
        done,_=await asyncio.wait((self._close_task,),timeout=self.configuration.runtime.integer('runtime.close_timeout_ms')/1000)
        return bool(done and self._close_task.result())

    async def _close(self) -> bool:
        timeout=self.configuration.runtime.integer('runtime.close_timeout_ms')/1000
        if self._initialization is not None and not self._initialization.done():
            done,_=await asyncio.wait((self._initialization,),timeout=timeout)
            if not done:return False
        if self.http is not None and not await self.http.close():return False
        if self.cold is not None and self.cold.pending:
            assert self.cold._task is not None
            done,_=await asyncio.wait((self.cold._task,),timeout=timeout)
            if not done:return False
        if self.semantic is not None and self.semantic._task is not None and not self.semantic._task.done():
            done,_=await asyncio.wait((self.semantic._task,),timeout=timeout)
            if not done:return False
        if self.semantic is not None and self.semantic._local_task is not None and not self.semantic._local_task.done():
            done,_=await asyncio.wait((self.semantic._local_task,),timeout=timeout)
            if not done:return False
        if self.embedding is not None and not await self.embedding.close(time.monotonic()+timeout):return False
        if self.combination.cache is not None and not self.combination.cache.close():return False
        if self.combination.generations is not None and self.combination.generations.io_pending:return False
        if self.files is not None and not self.files.close():return False
        async def close_owners():
            if self.runtime is not None and not await self.runtime.close():return False
            for name,owner in (('retrieval',self.retrieval),('goals',self.goals),('state',self.current_state)):
                if owner is not None and name not in self._closed_owners:
                    if not owner.close():return False
                    self._closed_owners.add(name)
            if self.media._bound and 'media' not in self._closed_owners:
                if not await self.media.close():return False
                self._closed_owners.add('media')
            if self.assembly._bound:self.assembly.close()
            if self.config is not None:self.config.close()
            return True
        if not await self.storage.coordinate_owner_shutdown(asyncio.create_task(close_owners())):return False
        await self.storage.close()
        if self.storage.get_health().lifecycle!='CLOSED':return False
        if self.authorization is not None:self.authorization.close()
        if self.admission is not None:self.admission.close()
        self.state='CLOSED';return True
