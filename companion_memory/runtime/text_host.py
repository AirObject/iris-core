"""Independent text host with explicit borrowed generation and local resources.

Startup verifies the fixed new assembly, durable configuration and local owners
before publishing service capabilities. No initialization path resolves a secret
or calls the transport. Ordinary information services keep their native owners.
"""
from __future__ import annotations
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import time
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.text_resolution import TextConfigurationCandidate,text_learning_snapshot_issue
from companion_memory.configuration.text_persistent_results import ConfigurationCommitted
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.media.service import MediaResources
from companion_memory.persistence import DatabaseResources,Ready,Found,Committed
from companion_memory.persistence.completion import finish_owned
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue,valid_identifier
from companion_memory.provider.generation_resources import RealGenerationResources
from companion_memory.self_model.management import PersonaManagement
from companion_memory.self_model.current import CurrentPersona,Available
from companion_memory.self_model.local_recovery import LocalPersonaRecovery,LocallyRecovered,Pending as PersonaRecoveryPending
from companion_memory.self_model.results import Failed as PersonaFailed
from companion_memory.goals.service import GoalsService
from companion_memory.state.service import StateOwner
from companion_memory.retrieval.index import LocalIndex
from companion_memory.retrieval.query_service import QueryService
from companion_memory.information.management import HostIdentity
from companion_memory.information.business import BusinessService
from companion_memory.information.scheduling import InformationScheduler
from companion_memory.management.information_http import InformationHTTP
from companion_memory.cognition.text_candidates import TextCandidateInput
from .text_assembly import TextLearningAssembly
from .content_gate import ContentGate
from .content_service import ContentRuntimeService
from .text_collection import TextContextCollection
from .content_assembly import stable
from .results import Rejected,RuntimeError


@dataclass(frozen=True,slots=True)
class TextHostResources:
    database: DatabaseResources
    media: MediaResources
    instance_id: str
    configuration_key: str
    protected_directories: Mapping[str,tuple[str,...] | list[str]]
    initial_self: InitialSelfBinding


class TextHost:
    """The new local combination; constructing it grants no live model authority."""
    def __init__(self,configuration: TextConfigurationCandidate,resources: TextHostResources,generation: RealGenerationResources,gate: ContentGate):
        if (text_learning_snapshot_issue(configuration) is not None or type(resources) is not TextHostResources
                or type(resources.database) is not DatabaseResources or type(resources.media) is not MediaResources
                or type(resources.initial_self) is not InitialSelfBinding or type(generation) is not RealGenerationResources
                or type(gate) is not ContentGate or gate.capacity!=1 or gate.state!='RECOVERING' or gate.epoch!=0
                or generation.gate is not gate.binding or not all(valid_identifier(value) for value in (resources.instance_id,resources.configuration_key))
                or type(resources.protected_directories) is not dict or any(type(k) is not str or type(v) not in (tuple,list)
                    or any(type(path) is not str for path in v) for k,v in resources.protected_directories.items())):raise InvalidValue()
        self.configuration=configuration;self.resources=resources;self.generation=generation;self.gate=gate
        self._directories={name:tuple(paths) for name,paths in resources.protected_directories.items()}
        self.combination=TextLearningAssembly();self.storage=self.combination.storage;self.assembly=self.combination.content
        self.management=self.combination.management
        self.provider=self.combination.provider;self.media=self.combination.media
        self.config=None;self.stored=None;self.runtime:ContentRuntimeService|None=None
        self.persona:PersonaManagement|None=None;self.current_persona:CurrentPersona|None=None
        self.persona_recovery:LocalPersonaRecovery|None=None
        self.goals=None;self.current_state=None;self.retrieval=None;self.queries=None;self.business=None;self.http=None;self.scheduler=None
        self.state='NEW';self.phase='STORAGE';self._mode=None;self._initialization:asyncio.Task|None=None;self._close_task:asyncio.Task|None=None
        self._information_at=time.time_ns()//1000;self._owners_closed=False
        self._closed_owners:set[str]=set()
        self._close_report:bool|None=None

    async def initialize(self,mode: str):
        if mode not in ('CREATE_NEW','OPEN_EXISTING') or self._mode is not None and self._mode!=mode:
            return Rejected(RuntimeError('INVALID_INPUT','initialize','input','BINDING_MISMATCH'))
        if self.state=='READY':
            assert self.current_persona is not None
            current=await self.current_persona.port.read_current(time.monotonic()+self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000)
            if self.state!='READY':return Rejected(RuntimeError('INVALID_STATE','initialize','state','SERVICE_CLOSED'))
            return Found(MappingProxyType({'state':'READY','learning_ready':type(current) is Available,'model_adapter':'REMOTE_PROVIDER'}))
        if self.state in ('CLOSING','CLOSED'):return Rejected(RuntimeError('INVALID_STATE','initialize','state','SERVICE_CLOSED'))
        self._mode=mode;self.state='RECOVERING'
        if self._initialization is None or self._initialization.done():
            async def advance():
                with DeadlineScope(time.monotonic()+self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000):
                    try:return await self._advance()
                    except OwnerFailure as failure:return Rejected(RuntimeError(failure.code,'initialize',failure.field,failure.reason,failure.cleanup_pending))
                    except (InvalidValue,KeyError,IndexError):return Rejected(RuntimeError('INTEGRITY_FAILURE','initialize','storage','CONTEXT_UNRECOVERABLE'))
            self._initialization=asyncio.create_task(finish_owned(advance()))
        done,_=await asyncio.wait((self._initialization,),timeout=self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000)
        if not done:return Rejected(RuntimeError('TIMEOUT','initialize','state','DEADLINE_EXCEEDED',True))
        return self._initialization.result()

    def _live(self):
        if self.state!='RECOVERING':raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')

    async def _advance(self):
        c=self.combination;self._live()
        if self.phase=='STORAGE':
            opened=await self.storage.initialize(self.configuration.foundation,self.resources.database,self._mode)
            if type(opened) is not Ready:return opened
            self._live();self.config=c.configuration.bind(self.storage,self.resources.instance_id,self.configuration);self.phase='CONFIGURATION'
        if self.phase=='CONFIGURATION':
            assert self.config is not None
            result=await self.config.persist_text_learning_configuration(self.resources.configuration_key,self.configuration,actor='text_host',protected_directories=self._directories)
            if type(result) is not ConfigurationCommitted or result.configuration is None:return result
            self._live();self.stored=result.configuration;stored=self.stored
            self.media.bind(self.storage,stored,self.resources.instance_id);self.assembly.bind(self.storage,stored,self.resources.instance_id)
            self.runtime=ContentRuntimeService(self.assembly,self.provider,TextCandidateInput(stored),cast(str,self.assembly.text_transactions.profile['profile_id']),None,gate=self.gate)
            c.persona.bind(self.resources.initial_self,self.gate)
            self.persona=PersonaManagement(c.persona,self.runtime.focus);self.current_persona=CurrentPersona(c.persona)
            self.persona_recovery=LocalPersonaRecovery(c.persona)
            self.runtime.text_contexts=TextContextCollection(self.runtime,self.current_persona.port)
            catalogs={catalog.definition.owner_module:catalog for catalog in c.information_catalogs}
            self.goals=GoalsService(catalogs['goals'],self.storage,stored,self.resources.instance_id)
            self.current_state=StateOwner(catalogs['state'],self.storage,stored,self.resources.instance_id)
            self.retrieval=LocalIndex(catalogs['retrieval'],self.storage,stored,self.resources.instance_id)
            memory=self.assembly.memory.bind_information(stored);self.retrieval.bind_memory(memory)
            from .candidate_goals import CandidateGoalEffects
            self.assembly.goal_effects=CandidateGoalEffects(self.goals,self.assembly.memory)
            publisher=self.config
            c.initializer.bind(self.storage,stored,self.resources.instance_id,{'goals':self.goals,'state':self.current_state,'retrieval':self.retrieval,'memory':memory},
                lambda uow:publisher.participate_snapshot(uow,stored))
            c.management.bind(self.storage,stored,self.resources.instance_id,self.goals,self.current_state,self.retrieval,self.gate,
                self.runtime.retain_external_work,lambda:self.state=='RECOVERING' and self.phase=='RUNTIME')
            self._ledger=c.ledger.bind(self.storage,stored);self.phase='PROVIDER'
        if self.phase=='PROVIDER':
            assert self.stored is not None
            result=await self.provider.initialize(self.stored,self._ledger,self.generation)
            if self.provider.get_health().lifecycle!='READY':return result
            self._live();self.phase='MEDIA'
        if self.phase=='MEDIA':
            assert self._mode is not None
            result=await self.media.initialize(self.resources.media,self._mode)
            if type(result) is not Found or result.value['state']!='READY':return result
            self._live();self.phase='OWNERS'
        assert self.runtime is not None and self.persona is not None and self.current_persona is not None
        if self.phase=='OWNERS':
            if self._mode=='CREATE_NEW':
                result=await self.runtime.execute('initialize_content_runtime',stable('initialize_runtime',self.resources.instance_id),{})
                if type(result) is not Committed:return result
                result=await c.initializer.initialize(stable('initialize_information',self.resources.instance_id),self._information_at)
                if type(result) is not Committed:return result
            await c.initializer.recover(stable('initialize_information',self.resources.instance_id))
            self._live();self.phase='PERSONA'
        if self.phase=='PERSONA':
            await self._verify_persona()
            self._live();self.phase='RUNTIME'
        result=await self.runtime.initialize()
        if type(result) is not Found or result.value['state']!='READY':return result
        self._live();assert c.management.local_recovery is not None
        recovered=await c.management.local_recovery.run()
        if type(recovered) is not Found:return recovered
        self._live()
        if (self.storage.get_health().lifecycle!='READY' or self.provider.get_health().lifecycle!='READY' or self.runtime.state!='READY'
                or self.gate.state not in ('NORMAL','DRAINING','DREAM_PREPARING','DREAM_FOCUSED') or self.gate.integrity_pending()):
            raise OwnerFailure('MODE_BLOCKED','state','RUNTIME_FAULTED')
        assert self.stored is not None and self.retrieval is not None and self.current_state is not None and self.goals is not None
        self.queries=QueryService(self.stored,self.runtime,c.management,self.retrieval,self.current_state,self.goals,current_persona=self.current_persona.port)
        self.business=BusinessService(c.management,self.queries);self.http=InformationHTTP(self.stored,self.gate)
        self.scheduler=InformationScheduler(self.runtime,c.management,self.goals,self.retrieval.memory,self.retrieval)
        self.phase='COMPLETE';self.state='READY';self.scheduler.start()
        return Found(MappingProxyType({'state':'READY','learning_ready':self._persona_present,'model_adapter':'REMOTE_PROVIDER'}))

    async def _verify_persona(self):
        assert self.persona_recovery is not None
        deadline=time.monotonic()+self.configuration.runtime.integer('runtime.recovery_timeout_ms')/1000
        result=await self.persona_recovery.recover_local(self.persona_recovery.grant,deadline)
        if type(result) is PersonaRecoveryPending:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',result.cleanup_pending)
        if type(result) is PersonaFailed:raise OwnerFailure(result.error.code,result.error.field,result.error.reason,result.cleanup_pending)
        if type(result) is not LocallyRecovered:raise InvalidValue()
        self._persona_present=result.learning_ready

    def initialization_port(self):
        if self.state!='READY' or self.persona is None:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        return self.persona.port

    async def register_entry(self,key: str,entry_id: str,host_id: str,platform_id: str,external_entry_id: str):
        if self.state!='READY' or self.runtime is None:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        return await self.runtime.execute('register_content_entry',key,{'entry_id':entry_id,'host_id':host_id,'platform_id':platform_id,'external_entry_id':external_entry_id})

    async def bind_query(self,identity: HostIdentity,*,include_forgotten: bool=False,object_ids: tuple[str,...]|None=None):
        if self.state!='READY' or self.queries is None or type(identity) is not HostIdentity:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        if not await self.assembly.ingress.verify_host_entry(identity.entry_id,identity.host_id):raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        if self.state!='READY':raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        return self.queries.bind(identity,include_forgotten=include_forgotten,object_ids=object_ids)

    async def bind_management(self,identity: HostIdentity):
        """Retain the existing ordinary management boundary and registered entry."""
        if self.state!='READY' or type(identity) is not HostIdentity:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        if not await self.assembly.ingress.verify_host_entry(identity.entry_id,identity.host_id):raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        if self.state!='READY':raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        return self.management.issue(identity)

    async def bind_business(self,identity: HostIdentity,*,include_forgotten: bool=False,object_ids: tuple[str,...]|None=None):
        """Issue existing local information work without any persona write right."""
        if self.state!='READY' or self.business is None or type(identity) is not HostIdentity:raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        if not await self.assembly.ingress.verify_host_entry(identity.entry_id,identity.host_id):raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        if self.state!='READY':raise OwnerFailure('INVALID_STATE','state','NOT_READY')
        return self.business.bind(identity,include_forgotten=include_forgotten,object_ids=object_ids)

    async def close(self) -> bool:
        if self.state=='CLOSED':return True if self._close_report is None else self._close_report
        self.state='CLOSING';self.gate.close();self.media.stop_admission()
        if self.scheduler is not None:self.scheduler.stop()
        if self.http is not None:self.http.stop_admission()
        if self.persona is not None:self.persona.work.close();self.persona.transactions.close()
        if self._close_task is None or self._close_task.done():self._close_task=asyncio.create_task(self._close())
        done,_=await asyncio.wait((self._close_task,),timeout=self.configuration.runtime.integer('runtime.close_timeout_ms')/1000)
        report=bool(done and self._close_task.result())
        if self._close_report is None:self._close_report=report
        return self._close_report

    async def _close(self) -> bool:
        timeout=self.configuration.runtime.integer('runtime.close_timeout_ms')/1000
        if self._initialization is not None and not self._initialization.done():
            done,_=await asyncio.wait((self._initialization,),timeout=timeout)
            if not done:return False
        async def owners():
            if self.http is not None and 'http' not in self._closed_owners:
                if not await self.http.close():return False
                self._closed_owners.add('http')
            if self.scheduler is not None and 'scheduler' not in self._closed_owners:
                if not await self.scheduler.close(timeout):return False
                self._closed_owners.add('scheduler')
            if self.persona is not None and 'persona' not in self._closed_owners:
                if not await self.persona.close(time.monotonic()+timeout):return False
                self._closed_owners.add('persona')
            if self.current_persona is not None and 'current_persona' not in self._closed_owners:
                if not await self.current_persona.close(time.monotonic()+timeout):return False
                self._closed_owners.add('current_persona')
            if self.runtime is not None and 'runtime' not in self._closed_owners:
                if not await self.runtime.close():return False
                self._closed_owners.add('runtime')
            if 'provider' not in self._closed_owners and self.provider.get_health().lifecycle not in ('NEW','CLOSED'):
                await self.provider.close()
                if self.provider.get_health().lifecycle!='CLOSED':return False
            self._closed_owners.add('provider')
            if self.media._bound and 'media' not in self._closed_owners:
                if not await self.media.close():return False
                self._closed_owners.add('media')
            for name,owner in (('retrieval',self.retrieval),('goals',self.goals),('state',self.current_state)):
                if owner is not None and name not in self._closed_owners:
                    if not owner.close():return False
                    self._closed_owners.add(name)
            if self.assembly._bound and 'assembly' not in self._closed_owners:
                # Runtime's public memory service has already released its
                # shared memory lease. Release the remaining assembly owners
                # once, and check actual lease state rather than treating that
                # earlier successful release as a cleanup failure.
                self.assembly.close()
                leases=[owner._lease for owner in (self.assembly.memory,self.assembly.cognition,self.assembly.buffers,self.assembly.ingress,self.assembly.history)]
                persona_owner=self.assembly.text_commands.persona;assert persona_owner is not None
                leases.extend((self.assembly._lease,persona_owner._lease))
                if any(lease is not None and lease.is_active() for lease in leases):return False
                self._closed_owners.add('assembly')
            if self.config is not None:self.config.close()
            return True
        if not self._owners_closed:
            task=asyncio.create_task(owners())
            self._owners_closed=await self.storage.coordinate_owner_shutdown(task) if self.runtime is not None else await task
            if not self._owners_closed:return False
        await self.storage.close()
        if self.storage.get_health().lifecycle!='CLOSED':return False
        self.state='CLOSED';return True
