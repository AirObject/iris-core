"""Complete content owner lifecycle with explicit retained resource identities.

The host restores configuration and Provider before opening media capabilities,
then verifies source owners and recovers original runtime work without dispatch.
Every continuation joins the same bounded initialization or close task. Existing
storage is opened with the original assembly; no migration or repair is offered.
"""
from __future__ import annotations
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.cognition.synthetic_graph import SyntheticGraphInput
from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
from companion_memory.cognition.synthetic_mixed import SyntheticMixedInput
from companion_memory.configuration.content_persistence import ContentConfigurationAssembly
from companion_memory.configuration.content_persistent_results import ConfigurationCommitted
from companion_memory.configuration.content_resolution import ContentConfigurationCandidate, content_snapshot_issue
from companion_memory.media.service import MediaResources, MediaService
from companion_memory.persistence import DatabaseResources, PersistenceService, Ready, Found, Committed
from companion_memory.persistence.schema import valid_identifier
from companion_memory.provider import LedgerAssembly, ProviderService, ProviderResources, SimulationAdapter
from .content_assembly import ContentAssembly, stable
from .content_media import MediaPolicy
from .content_modes import ContentPublication
from .content_service import ContentRuntimeService
from .results import Rejected, RuntimeError


@dataclass(frozen=True, slots=True)
class ContentHostResources:
    """Trusted startup input; expected identities are retained before opening files."""
    database: DatabaseResources
    media: MediaResources
    instance_id: str
    configuration_key: str
    protected_directories: Mapping[str, list[str] | tuple[str, ...]]


class ContentHost:
    """Own one complete isolated assembly and expose separately scoped services.

    Constructor inputs include the explicit simulation and candidate source.
    Initialization does not register entries or manufacture user authority.
    Trusted setup registers finite entries before issuing entry/read capabilities.
    """
    def __init__(self, configuration: ContentConfigurationCandidate, resources: ContentHostResources,
                 adapter: SimulationAdapter, candidates: SyntheticCandidateInput | SyntheticGraphInput | SyntheticMutationInput | SyntheticMixedInput,
                 learning_profile: str, media_policy: MediaPolicy, publication: ContentPublication | None = None):
        if (content_snapshot_issue(configuration) is not None or type(resources) is not ContentHostResources
                or type(resources.database) is not DatabaseResources or type(resources.media) is not MediaResources
                or not all(valid_identifier(v) for v in (resources.instance_id, resources.configuration_key, learning_profile))
                or type(resources.protected_directories) is not dict
                or any(type(k) is not str or type(v) not in (list, tuple) or any(type(path) is not str for path in v) for k, v in resources.protected_directories.items())
                or type(adapter) is not SimulationAdapter or type(candidates) not in (SyntheticCandidateInput, SyntheticGraphInput, SyntheticMutationInput, SyntheticMixedInput)
                or type(media_policy) is not MediaPolicy):
            raise ValueError('Native complete configuration and explicit resources are required.')
        self.configuration = configuration; self.resources = resources
        self._directories = {key: tuple(paths) for key, paths in resources.protected_directories.items()}
        self.adapter = adapter; self.candidates = candidates; self.learning_profile = learning_profile; self.media_policy = media_policy
        self.config_assembly = ContentConfigurationAssembly(); self.media = MediaService()
        self.assembly = ContentAssembly(self.media, self.media.repositories, publication=publication)
        self.ledger = LedgerAssembly(); self.provider = ProviderService(self.ledger)
        repositories = self.config_assembly.repositories + self.assembly.repositories + self.ledger.repositories
        commands = self.config_assembly.commands + self.assembly.commands + self.ledger.commands + self.media.commands + (publication.commands if publication is not None else ())
        from .content_budget import check_content_assembly
        self.assembly_budget = check_content_assembly(repositories, commands)
        self.storage = PersistenceService(repositories, commands)
        self.config = None; self.runtime: ContentRuntimeService | None = None
        self._ledger_binding = None; self._provider_resources = None
        self._mode: str | None = None; self.phase = 'STORAGE'; self.state = 'NEW'
        self._initialization: asyncio.Task | None = None; self._close_task: asyncio.Task | None = None
        self._owners_closed = False

    async def initialize(self, mode: str):
        """Advance original initialization; an unfinished task keeps every owner."""
        if mode not in ('CREATE_NEW', 'OPEN_EXISTING') or self._mode is not None and mode != self._mode:
            return Rejected(RuntimeError('INVALID_INPUT', 'initialize', 'input', 'BINDING_MISMATCH'))
        if self.state == 'READY': return Found(MappingProxyType({'state': 'READY'}))
        if self.state in ('CLOSING', 'CLOSED'):
            return Rejected(RuntimeError('INVALID_STATE', 'initialize', 'state', 'SERVICE_CLOSED'))
        self._mode = mode; self.state = 'RECOVERING'
        if self._initialization is None or self._initialization.done():
            self._initialization = asyncio.create_task(self._advance())
        done, _ = await asyncio.wait((self._initialization,), timeout=self.configuration.runtime.integer('runtime.recovery_timeout_ms') / 1000)
        if not done: return Rejected(RuntimeError('TIMEOUT', 'initialize', 'state', 'DEADLINE_EXCEEDED', True))
        return self._initialization.result()

    async def _advance(self):
        if self.phase == 'STORAGE':
            result = await self.storage.initialize(self.configuration.foundation, self.resources.database, self._mode)
            if type(result) is not Ready: return result
            self.config = self.config_assembly.bind(self.storage, self.resources.instance_id)
            self.phase = 'CONFIGURATION'
        if self.phase == 'CONFIGURATION':
            assert self.config is not None
            result = await self.config.persist_content_configuration(self.resources.configuration_key, self.configuration,
                actor='content_host', protected_directories=self._directories)
            if type(result) is not ConfigurationCommitted or result.configuration is None: return result
            stored = result.configuration
            self.media.bind(self.storage, stored, self.resources.instance_id)
            self.assembly.bind(self.storage, stored, self.resources.instance_id)
            self.runtime = ContentRuntimeService(self.assembly, self.provider, self.candidates, self.learning_profile, self.media_policy)
            self._ledger_binding = self.ledger.bind(self.storage, self.configuration.foundation)
            self._provider_resources = ProviderResources(self.runtime.gate.binding, self.adapter)
            self.phase = 'PROVIDER'
        if self.phase == 'PROVIDER':
            result = await self.provider.initialize(self.configuration.foundation, self._ledger_binding, self._provider_resources)
            if self.provider.get_health().lifecycle != 'READY': return result
            self.phase = 'MEDIA'
        if self.phase == 'MEDIA':
            assert self._mode is not None
            result = await self.media.initialize(self.resources.media, self._mode)
            if type(result) is not Found or result.value['state'] != 'READY': return result
            self.phase = 'RUNTIME'
        assert self.runtime is not None
        if self._mode == 'CREATE_NEW':
            result = await self.runtime.execute('initialize_content_runtime', stable('initialize_runtime', self.resources.instance_id), {})
            if type(result) is not Committed: return result
        result = await self.runtime.initialize()
        if type(result) is Found and result.value['state'] == 'READY' and self.state == 'RECOVERING':
            self.phase = 'COMPLETE'; self.state = 'READY'
        return result

    async def register_entry(self, key: str, entry_id: str, host_id: str, platform_id: str, external_entry_id: str):
        """Trusted local registration with original-key confirmation and no dispatch."""
        if self.state != 'READY' or self.runtime is None:
            return Rejected(RuntimeError('INVALID_STATE', 'register_entry', 'state', 'NOT_READY'))
        return await self.runtime.execute('register_content_entry', key, {'entry_id': entry_id, 'host_id': host_id,
            'platform_id': platform_id, 'external_entry_id': external_entry_id})

    async def close(self) -> bool:
        """Close admission immediately; incomplete lower owners retain their leases."""
        if self.state == 'CLOSED': return True
        self.state = 'CLOSING'
        if self.runtime is not None: self.runtime.gate.close()
        self.media.stop_admission()
        if self._close_task is None or self._close_task.done(): self._close_task = asyncio.create_task(self._close())
        done, _ = await asyncio.wait((self._close_task,), timeout=self.configuration.runtime.integer('runtime.close_timeout_ms') / 1000)
        return bool(done and self._close_task.result())

    async def _close(self):
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
        if self.runtime is not None and not await self.runtime.close(): return False
        if self._provider_resources is not None:
            await self.provider.close()
            if self.provider.get_health().lifecycle != 'CLOSED': return False
        if self.media._bound and not await self.media.close(): return False
        if self.assembly._bound: self.assembly.close()
        if self.config is not None: self.config.close()
        return True
