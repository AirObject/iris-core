"""Local information owner lifecycle with explicit retained resource identities.

The host restores configuration and Provider before opening media capabilities,
then verifies source owners and recovers original runtime work without dispatch.
Every continuation joins the same bounded initialization or close task. Existing
storage is opened with the original assembly; no migration or repair is offered.
"""
from __future__ import annotations
import asyncio
import time
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.cognition.synthetic_graph import SyntheticGraphInput
from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
from companion_memory.cognition.synthetic_mixed import SyntheticMixedInput
from companion_memory.cognition.goal_proposals import SyntheticGoalInput
from companion_memory.configuration.information_persistence import InformationConfigurationAssembly
from companion_memory.configuration.information_persistent_results import ConfigurationCommitted
from companion_memory.configuration.information_resolution import InformationConfigurationCandidate, information_snapshot_issue
from companion_memory.media.service import MediaResources, MediaService
from companion_memory.persistence import DatabaseResources, PersistenceService, Ready, Found, Committed
from companion_memory.persistence.schema import valid_identifier
from companion_memory.provider import LedgerAssembly, ProviderService, ProviderResources, SimulationAdapter
from .content_assembly import ContentAssembly, stable
from .content_media import MediaPolicy
from .content_modes import ContentPublication
from .content_service import ContentRuntimeService
from .results import Rejected, RuntimeError
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.scheduling import InformationScheduler
from companion_memory.goals.loopback import TestReminderRoute
from companion_memory.information.formed_goals import FormedGoalAuthority, FormedGoalWorkPort
from companion_memory.information.management import HostIdentity, ManagementPort
from companion_memory.information.business import InformationPort
from companion_memory.retrieval.query_service import QueryPort
from companion_memory.information.errors import InformationRejected, rejected as information_rejected
from companion_memory.memory.service import MemoryReadPort
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge


@dataclass(frozen=True, slots=True)
class InformationHostResources:
    """Trusted startup input; expected identities are retained before opening files."""
    database: DatabaseResources
    media: MediaResources
    instance_id: str
    configuration_key: str
    protected_directories: Mapping[str, list[str] | tuple[str, ...]]


class InformationHost:
    """Own one complete isolated assembly and expose separately scoped services.

    Constructor inputs include the explicit simulation and candidate source.
    Initialization does not register entries or manufacture user authority.
    Trusted setup registers finite entries before issuing entry/read capabilities.
    """
    def __init__(self, configuration: InformationConfigurationCandidate, resources: InformationHostResources,
                 adapter: SimulationAdapter, candidates: SyntheticCandidateInput | SyntheticGraphInput | SyntheticMutationInput | SyntheticMixedInput | SyntheticGoalInput,
                 learning_profile: str, media_policy: MediaPolicy, publication: ContentPublication | None = None):
        if (information_snapshot_issue(configuration) is not None or type(resources) is not InformationHostResources
                or type(resources.database) is not DatabaseResources or type(resources.media) is not MediaResources
                or not all(valid_identifier(v) for v in (resources.instance_id, resources.configuration_key, learning_profile))
                or type(resources.protected_directories) is not dict
                or any(type(k) is not str or type(v) not in (list, tuple) or any(type(path) is not str for path in v) for k, v in resources.protected_directories.items())
                or type(adapter) is not SimulationAdapter or type(candidates) not in (SyntheticCandidateInput, SyntheticGraphInput, SyntheticMutationInput, SyntheticMixedInput, SyntheticGoalInput)
                or type(media_policy) is not MediaPolicy):
            raise ValueError('Native complete configuration and explicit resources are required.')
        self.configuration = configuration; self.resources = resources
        self._directories = {key: tuple(paths) for key, paths in resources.protected_directories.items()}
        self.adapter = adapter; self.candidates = candidates; self.learning_profile = learning_profile; self.media_policy = media_policy
        self.config_assembly = InformationConfigurationAssembly(); self.media = MediaService()
        self.assembly = ContentAssembly(self.media, self.media.repositories, publication=publication, information_format=True)
        self.ledger = LedgerAssembly(); self.provider = ProviderService(self.ledger)
        from companion_memory.goals.repository import goals_catalog
        from companion_memory.state.repository import state_catalog
        from companion_memory.retrieval.repository import retrieval_catalog
        from companion_memory.information.initialization import InformationInitialization
        self.information_catalogs = (retrieval_catalog(), state_catalog(), goals_catalog())
        from .candidate_goals import add_goal_commands
        add_goal_commands(self.assembly, self.information_catalogs[2].definition)
        domain_repositories = tuple(c.definition for c in self.information_catalogs)
        self.initializer = InformationInitialization(domain_repositories + self.assembly.repositories, self.config_assembly.repositories[0])
        from companion_memory.information.management import ManagementAssembly
        self.management = ManagementAssembly(domain_repositories + self.assembly.repositories)
        self.goals = None; self.current_state = None; self.retrieval = None
        self._information_at = self.assembly.utc_now_us()
        repositories = self.config_assembly.repositories + self.assembly.repositories + self.ledger.repositories + domain_repositories
        commands = self.config_assembly.commands + self.assembly.commands + self.ledger.commands + self.media.commands + self.initializer.commands + self.management.commands + (publication.commands if publication is not None else ())
        from companion_memory.persistence._codec import assembly_value
        self.assembly_budget = len(assembly_value(repositories, commands, assembly_format='LOCAL_INFORMATION_V1'))
        self.storage = PersistenceService(repositories, commands, assembly_format='LOCAL_INFORMATION_V1')
        self.config = None; self.runtime: ContentRuntimeService | None = None
        self._ledger_binding = None; self._provider_resources = None
        self._mode: str | None = None; self.phase = 'STORAGE'; self.state = 'NEW'
        self._initialization: asyncio.Task[object] | None = None; self._close_task: asyncio.Task[bool] | None = None
        self._owners_closed = False
        self.queries = None
        self.business = None
        self.http = None
        self.information_observations = None
        self.scheduler: InformationScheduler | None = None

    async def initialize(self, mode: str):
        """Advance original initialization; an unfinished task keeps every owner."""
        if mode not in ('CREATE_NEW', 'OPEN_EXISTING') or self._mode is not None and mode != self._mode:
            return Rejected(RuntimeError('INVALID_INPUT', 'initialize', 'input', 'BINDING_MISMATCH'))
        if self.state == 'READY': return Found(MappingProxyType({'state': 'READY'}))
        if self.state in ('CLOSING', 'CLOSED'):
            return Rejected(RuntimeError('INVALID_STATE', 'initialize', 'state', 'SERVICE_CLOSED'))
        self._mode = mode; self.state = 'RECOVERING'
        if self._initialization is None or self._initialization.done():
            async def advance() -> object:
                with DeadlineScope(bounded_deadline(time.monotonic(), self.configuration.runtime.integer('runtime.recovery_timeout_ms') / 1000)):
                    return await self._advance()
            self._initialization = asyncio.create_task(advance())
        done, _ = await asyncio.wait((self._initialization,), timeout=self.configuration.runtime.integer('runtime.recovery_timeout_ms') / 1000)
        if not done: return Rejected(RuntimeError('TIMEOUT', 'initialize', 'state', 'DEADLINE_EXCEEDED', True))
        try:
            return self._initialization.result()
        except OwnerFailure as failure:
            from companion_memory.information.errors import rejected
            return rejected('initialize', failure)

    async def _advance(self):
        if self.phase == 'STORAGE':
            result = await self.storage.initialize(self.configuration.foundation, self.resources.database, self._mode)
            if type(result) is not Ready: return result
            self.config = self.config_assembly.bind(self.storage, self.resources.instance_id)
            self.phase = 'CONFIGURATION'
        if self.phase == 'CONFIGURATION':
            if self.config is None:
                raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
            result = await self.config.persist_information_configuration(self.resources.configuration_key, self.configuration,
                actor='content_host', protected_directories=self._directories)
            if type(result) is not ConfigurationCommitted or result.configuration is None: return result
            stored = result.configuration
            content_view = stored.content_view()
            self.media.bind(self.storage, content_view, self.resources.instance_id)
            self.assembly.bind(self.storage, content_view, self.resources.instance_id)
            from companion_memory.goals.service import GoalsService
            from companion_memory.state.service import StateOwner
            from companion_memory.retrieval.index import LocalIndex
            catalogs = {c.definition.owner_module: c for c in self.information_catalogs}
            self.goals = GoalsService(catalogs['goals'], self.storage, stored, self.resources.instance_id)
            from .candidate_goals import CandidateGoalEffects
            self.assembly.goal_effects = CandidateGoalEffects(self.goals, self.assembly.memory)
            self.current_state = StateOwner(catalogs['state'], self.storage, stored, self.resources.instance_id)
            self.retrieval = LocalIndex(catalogs['retrieval'], self.storage, stored, self.resources.instance_id)
            memory = self.assembly.memory.bind_information(stored)
            self.retrieval.bind_memory(memory)
            publisher = self.config
            self.initializer.bind(self.storage, stored, self.resources.instance_id,
                {'goals': self.goals, 'state': self.current_state, 'retrieval': self.retrieval, 'memory': memory},
                lambda uow: publisher.participate_snapshot(uow, stored))
            self.runtime = ContentRuntimeService(self.assembly, self.provider, self.candidates, self.learning_profile, self.media_policy)
            self.management.bind(self.storage, stored, self.resources.instance_id, self.goals, self.current_state, self.retrieval,
                self.runtime.gate, self.runtime.retain_external_work, lambda: self.state == 'RECOVERING' and self.phase == 'RUNTIME')
            self._ledger_binding = self.ledger.bind(self.storage, self.configuration.foundation)
            self._provider_resources = ProviderResources(self.runtime.gate.binding, self.adapter)
            self.phase = 'PROVIDER'
        if self.phase == 'PROVIDER':
            result = await self.provider.initialize(self.configuration.foundation, self._ledger_binding, self._provider_resources)
            if self.provider.get_health().lifecycle != 'READY': return result
            self.phase = 'MEDIA'
        if self.phase == 'MEDIA':
            if self._mode is None:
                raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
            result = await self.media.initialize(self.resources.media, self._mode)
            if type(result) is not Found or result.value['state'] != 'READY': return result
            self.phase = 'RUNTIME_INITIALIZATION'
        if self.phase == 'RUNTIME_INITIALIZATION':
            if self.runtime is None: raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
            if self._mode == 'CREATE_NEW':
                result = await self.runtime.execute('initialize_content_runtime', stable('initialize_runtime', self.resources.instance_id), {})
                if type(result) is not Committed: return result
            self.phase = 'INFORMATION'
        if self.phase == 'INFORMATION':
            key = stable('initialize_information', self.resources.instance_id)
            if self._mode == 'CREATE_NEW':
                created = await self.initializer.initialize(key, self._information_at)
                if type(created) is not Committed: return created
            await self.initializer.recover(key)
            self.phase = 'RUNTIME'
        if self.runtime is None:
            raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
        result = await self.runtime.initialize()
        if type(result) is Found and result.value['state'] == 'READY':
            self._require_recovery_publication()
            if self.goals is None or self.current_state is None or self.retrieval is None:
                raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
            if self.management.local_recovery is None:
                raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
            finished = await self.management.local_recovery.run()
            if type(finished) is not Found: return finished
            # No await separates this check, READY publication and service
            # startup: close on the owning event loop wins or follows this point.
            self._require_recovery_publication()
            self.phase = 'COMPLETE'; self.state = 'READY'
            from companion_memory.retrieval.query_service import QueryService
            self.queries = QueryService(self.retrieval.configuration, self.runtime, self.management, self.retrieval, self.current_state, self.goals)
            from companion_memory.information.business import BusinessService
            self.business = BusinessService(self.management, self.queries)
            from companion_memory.management.information_http import InformationHTTP
            self.http = InformationHTTP(self.retrieval.configuration, self.runtime.gate)
            from companion_memory.information.observation import InformationObservations
            self.information_observations = InformationObservations(self.runtime, self.management.tickets, self.current_state, self.goals)
            self._start_local_workers()
        return result

    def _require_recovery_publication(self) -> None:
        """Recheck live ownership and health after recovery awaits, before startup."""
        if self.state in ('CLOSING', 'CLOSED'):
            raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')
        if self.state != 'RECOVERING' or self.phase != 'RUNTIME' or self.runtime is None or self.runtime.state != 'READY':
            raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        if (self.storage.get_health().lifecycle != 'READY' or self.provider.get_health().lifecycle != 'READY'
                or self.runtime.gate.state not in ('NORMAL', 'DRAINING', 'DREAM_PREPARING', 'DREAM_FOCUSED')
                or self.runtime.gate.integrity_pending()):
            raise OwnerFailure('MODE_BLOCKED', 'state', 'RUNTIME_FAULTED')

    def _start_local_workers(self) -> None:
        if self.state != 'READY' or self.runtime is None or self.goals is None or self.retrieval is None:
            raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        self.scheduler = InformationScheduler(self.runtime, self.management, self.goals, self.retrieval.memory, self.retrieval)
        self.scheduler.start()

    def bind_test_reminder_routes(self, routes: tuple[TestReminderRoute, ...]) -> None:
        """Bind explicit live isolated receivers after recovery; never accepts URLs."""
        if self.state != 'READY' or self.scheduler is None:
            raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        self.scheduler.bind_test_routes(routes)

    async def register_entry(self, key: str, entry_id: str, host_id: str, platform_id: str, external_entry_id: str):
        """Trusted local registration with original-key confirmation and no dispatch."""
        if self.state != 'READY' or self.runtime is None:
            return Rejected(RuntimeError('INVALID_STATE', 'register_entry', 'state', 'NOT_READY'))
        return await self.runtime.execute('register_content_entry', key, {'entry_id': entry_id, 'host_id': host_id,
            'platform_id': platform_id, 'external_entry_id': external_entry_id})

    async def bind_query(self, identity: HostIdentity, *, include_forgotten: bool = False, object_ids: tuple[str, ...] | None = None) -> QueryPort:
        """Trusted host setup supplies registered scope and independent deep rights."""
        from companion_memory.information.management import HostIdentity
        if self.state != 'READY' or self.queries is None or type(identity) is not HostIdentity:
            raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        if not await self.assembly.ingress.verify_host_entry(identity.entry_id, identity.host_id):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        return self.queries.bind(identity, include_forgotten=include_forgotten, object_ids=object_ids)

    async def bind_management(self, identity: HostIdentity) -> ManagementPort:
        """Issue a native host handle only after ingress confirms its registration."""
        from companion_memory.information.management import HostIdentity
        if self.state != 'READY' or type(identity) is not HostIdentity:
            raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        if not await self.assembly.ingress.verify_host_entry(identity.entry_id, identity.host_id):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        return self.management.issue(identity)

    async def bind_formed_goal_work(self, identity: HostIdentity, key: str, payload: object,
                                   memory_port: MemoryReadPort) -> FormedGoalWorkPort | InformationRejected:
        """Trusted work setup freezes a goal and an independently issued basis grant.

        source_id names an actual committed cognition candidate, basis_id its
        current retained object. This native-only port has no HTTP counterpart;
        execution verifies both owning records in the one goals transaction.
        """
        port = None
        try:
            if self.state != 'READY' or self.runtime is None or self.goals is None:
                raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
            if type(identity) is not HostIdentity or identity.operations != frozenset(('goal_inject_internal',)):
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
            work = FormedGoalAuthority.bind(payload, key, self.goals.binding.config_snapshot_id,
                self.assembly.cognition, self.runtime.memory, memory_port)
            port = await self.bind_management(identity)
            self.management.bind_formed_goal(port, work)
            return FormedGoalWorkPort.bind(port, work)
        except OwnerFailure as failure:
            if port is not None: self.management.revoke(port)
            return information_rejected('create_internal_goal', failure)
        except ValueTooLarge:
            return information_rejected('create_internal_goal', OwnerFailure('INVALID_INPUT', 'goal', 'LIMIT_EXCEEDED'))
        except InvalidValue:
            return information_rejected('create_internal_goal', OwnerFailure('INVALID_INPUT', 'goal', 'INVALID_SHAPE'))

    async def bind_business(self, identity: HostIdentity, *, include_forgotten: bool = False, object_ids: tuple[str, ...] | None = None) -> InformationPort:
        """Bind only declared native business operations to the registered host."""
        from companion_memory.information.management import HostIdentity
        if self.state != 'READY' or self.business is None or type(identity) is not HostIdentity:
            raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        if not await self.assembly.ingress.verify_host_entry(identity.entry_id, identity.host_id):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        return self.business.bind(identity, include_forgotten=include_forgotten, object_ids=object_ids)

    async def close(self) -> bool:
        """Close admission immediately; incomplete lower owners retain their leases."""
        if self.state == 'CLOSED': return True
        self.state = 'CLOSING'
        if self.scheduler is not None: self.scheduler.stop()
        if self.http is not None: self.http.stop_admission()
        if self.runtime is not None: self.runtime.gate.close()
        self.media.stop_admission()
        if self._close_task is None or self._close_task.done(): self._close_task = asyncio.create_task(self._close())
        done, _ = await asyncio.wait((self._close_task,), timeout=self.configuration.runtime.integer('runtime.close_timeout_ms') / 1000)
        return bool(done and self._close_task.result())

    async def _close(self) -> bool:
        if self._initialization is not None and not self._initialization.done():
            await asyncio.wait((self._initialization,), timeout=self.configuration.runtime.integer('runtime.close_timeout_ms') / 1000)
            if not self._initialization.done(): return False
        if not self._owners_closed:
            owners = asyncio.create_task(self._close_owners())
            # Storage remains available for legal owner finalization. Its
            # cleanup owner releases only connections whose original workers
            # have ended, allowing their retained runtime tasks to finish.
            self._owners_closed = await self.storage.coordinate_owner_shutdown(owners) if self.runtime is not None else await owners
            if not self._owners_closed: return False
        await self.storage.close()
        if self.storage.get_health().lifecycle != 'CLOSED': return False
        if self.config is not None: self.config.close()
        if self.assembly._bound: self.assembly.close()
        self.state = 'CLOSED'
        return True

    async def _close_owners(self) -> bool:
        """Drain upper owners without waiting to enable retired SQL cleanup."""
        if self.http is not None and not await self.http.close(): return False
        if self.scheduler is not None and not await self.scheduler.close(self.configuration.runtime.integer('runtime.close_timeout_ms') / 1000): return False
        if self.runtime is not None and not await self.runtime.close(): return False
        if self.retrieval is not None and not self.retrieval.close(): return False
        if self.goals is not None and not self.goals.close(): return False
        if self.current_state is not None and not self.current_state.close(): return False
        if self._provider_resources is not None:
            await self.provider.close()
            if self.provider.get_health().lifecycle != 'CLOSED': return False
        if self.media._bound and not await self.media.close(): return False
        if self.assembly._bound: self.assembly.close()
        if self.config is not None: self.config.close()
        return True
