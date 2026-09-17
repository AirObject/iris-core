"""Recoverable attachment of the complete business host to protected bootstrap.

The original initialization key and immutable wizard bytes precede root writes.
No send occurs on attach or recovery. Initial input uses memory's public port;
first persona remains on its generation, review and publication protocol.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import time
from typing import cast, TYPE_CHECKING

from companion_memory.configuration.managed_registry import resolve_values
from companion_memory.configuration.managed_resolution import ManagedConfigurationOk, ManagedConfigurationCandidate
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from .content_assembly import stable as stable_identity
from companion_memory.management.identity import IdentityAuthority
from .daily_host import DailyCognitionHost, DailyHostResources
from .managed_bootstrap import ManagedBootstrap
from .managed_host_resources import host_resources
from .managed_resources import ManagedResources

if TYPE_CHECKING:
    from .managed_logging import ManagedLogging

ResourceFactory = Callable[[ManagedResources, ManagedConfigurationCandidate, str, str, Callable[[str], bool]], DailyHostResources]


class ManagedBusiness:
    def __init__(self, bootstrap: ManagedBootstrap, identity: IdentityAuthority, *, resource_factory: ResourceFactory = host_resources):
        self.bootstrap, self.identity, self.resource_factory = bootstrap, identity, resource_factory
        self.host: DailyCognitionHost | None = None
        self.task: asyncio.Task | None = None
        self.sends_enabled = False
        self.initialized = False
        self.logging: ManagedLogging | None = None
        from .managed_configuration import ManagedConfiguration
        self.configuration: ManagedConfiguration | None = None

    async def initialize(self, key: str, expected_revision: int):
        """Start or confirm one immutable setup; never substitute another key."""
        current = await self.identity.read_draft()
        if current['state'] == 'DRAFT':
            if current['revision'] != expected_revision:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            self.candidate(cast(dict[str, object], current['draft']))
            started = await self.identity.advance_wizard(key, expected_revision, 'INITIALIZING', key)
            if type(started) is not Committed:
                return started
        elif current.get('initialization_key') != key:
            raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'wizard', 'CONTENT_MISMATCH')
        return await self.recover()

    def candidate(self, draft: dict[str, object]) -> ManagedConfigurationCandidate:
        from companion_memory.management.managed_application import ManagedApplication
        ManagedApplication.draft_shape(draft, complete=True)
        resources = self.bootstrap.resources
        if resources is None:
            raise OwnerFailure('INVALID_STATE', 'resources', 'NOT_READY')
        parsed = resolve_values(self.bootstrap.settings, resources.protected_directories(),
            cast(str, draft['platform_id']), cast(dict[str, object], draft['configuration']))
        if type(parsed) is not ManagedConfigurationOk:
            raise OwnerFailure('INVALID_INPUT', 'configuration', 'CONFIGURATION_INVALID')
        return parsed.value

    async def recover(self):
        """Resume only local original roots, retaining task ownership on timeout."""
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._recover())
        done, _ = await asyncio.wait((self.task,), timeout=self.bootstrap.settings.integer('management.maintenance_timeout_seconds'))
        if not done:
            return {'state': 'RECOVERING', 'cleanup_pending': True, 'startup_sends': 0}
        return self.task.result()

    async def _recover(self):
        current = await self.identity.read_draft()
        if current['state'] == 'DRAFT':
            return {'state': 'BOOTSTRAP', 'startup_sends': 0}
        draft = cast(dict[str, object], current['draft'])
        resources = self.bootstrap.resources
        if resources is None:
            raise OwnerFailure('INVALID_STATE', 'resources', 'NOT_READY')
        original = cast(str, current['initialization_key'])
        if self.host is None:
            candidate = self.candidate(draft)
            native = self.resource_factory(resources, candidate, original, cast(str, draft['role_name']), lambda key: self.sends_enabled)
            self.host = DailyCognitionHost(candidate, native, managed_assembly=self.bootstrap.assembly)
            self.host.configure_entry(cast(str, draft['entry_id']), cast(str, draft['entry_id']), ('self',),
                ({'kind': 'REAL', 'context_id': None},), writable=('self',))
        host = self.host
        self.bootstrap.state = 'RECOVERING'
        if host.state != 'READY':
            result = await host.initialize('CREATE_NEW' if current['state'] == 'INITIALIZING' else 'OPEN_EXISTING')
            if type(result) is not Found or host.state != 'READY':
                return result
        if current['state'] == 'INITIALIZING':
            registered = await host.register_entry(stable_identity('managed-entry', original), cast(str, draft['entry_id']),
                cast(str, draft['host_id']), cast(str, draft['platform_id']), cast(str, draft['conversation_id']))
            if type(registered) is not Committed:
                return registered
            if host.initial is None:
                raise OwnerFailure('INVALID_STATE', 'wizard', 'NOT_READY')
            initial = await host.initial.register_initial_self(stable_identity('managed-self', original), 'PRESET',
                cast(str, draft['initial_material']), host.resources.initial_self.input_origin)
            if type(initial) is not Committed:
                return initial
            progressed = await self.identity.advance_wizard(stable_identity('managed-awaiting-review', original),
                cast(int, current['revision']), 'AWAITING_REVIEW', original)
            if type(progressed) is not Committed:
                return progressed
        self.initialized = True
        if self.configuration is None:
            from .managed_configuration import ManagedConfiguration
            self.configuration = ManagedConfiguration(self)
        if self.logging is not None:
            await self.logging.bind(self.configuration.work.versions.active, cast(str, draft['entry_id']))
        activated = await self.configuration.recover()
        if activated['state'] not in ('APPLIED', 'BIRTH_CONFIGURATION'):
            self.bootstrap.state = 'RECOVERING'
            return {'state': 'RECOVERING', 'configuration': activated, 'startup_sends': 0}
        ready = await self.business_ready()
        if ready and not await self.complete_wizard():
            self.bootstrap.state = 'RECOVERING'
            return {'state': 'RECOVERING', 'startup_sends': 0, 'business_ready': False}
        self.bootstrap.state = 'READY' if ready else 'AWAITING_REVIEW'
        return {'state': self.bootstrap.state, 'startup_sends': 0, 'business_ready': ready}

    def dispatch_disclosure(self) -> dict[str, object]:
        """Describe every configured remote role without retrieving credentials."""
        from companion_memory.management.identity import digest
        import json
        host = self.host
        if host is None or host.stored is None:
            raise OwnerFailure('INVALID_STATE', 'provider', 'NOT_READY')
        settings = host.stored.candidate.text
        roles = [{key: record[key] for key in ('role', 'origin', 'base_path', 'endpoint_path', 'profile_id', 'account_ref')}
            for record in cast(tuple[dict[str, object], ...], settings.record('provider.transport')['roles'])]
        embedding = settings.record('provider.embedding_transport')
        roles.append({'role': 'EMBEDDING', 'origin': embedding['origin'], 'endpoint_path': embedding['endpoint_path']})
        value = {'snapshot_id': host.stored.snapshot_id, 'destinations': roles,
            'content': '初始材料、对话及来源、相关记忆、状态、目标和persona会按任务经Provider发往所列目的地。',
            'restart_resumes': False}
        return {**value, 'digest': digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))}

    async def set_dispatch(self, key: str, expected_revision: int | None, enabled: bool, disclosure_digest: str):
        """Audit the explicit dispatch decision; original confirmation never resumes work."""
        from companion_memory.management.identity import digest
        import json
        value = self.dispatch_disclosure()
        if disclosure_digest != value['digest']:
            raise OwnerFailure('PRECONDITION_FAILED', 'provider', 'DISCLOSURE_CHANGED')
        old = await self.identity.rows.read('model_dispatch', 'model-dispatch')
        result = await self.identity.write('set_model_dispatch', key, self.identity.row('model-dispatch',
            {'enabled': enabled, 'business_snapshot_id': value['snapshot_id'], 'disclosure_digest': disclosure_digest}, old),
            expected_revision, digest(json.dumps([expected_revision, enabled, disclosure_digest])))
        if type(result) is Committed and result.source == 'NEW':
            self.sends_enabled = enabled
            if not enabled and self.host is not None and self.host.network is not None:
                self.host.network.pause()
        return result

    async def persona(self, action: str, payload: dict[str, object]):
        """Use the native first-persona protocol; generation and review stay explicit."""
        from companion_memory.management.managed_application import fields, text, revision
        from companion_memory.persistence.text_records import stable_identity as native_id
        host = self.host
        if not self.initialized or host is None or host.initial_persona is None or host.stored is None or host.runtime is None:
            raise OwnerFailure('INVALID_STATE', 'persona', 'NOT_READY')
        if action == 'current':
            fields(payload, set())
            if host.current_persona is None:
                raise OwnerFailure('INVALID_STATE', 'persona', 'NOT_READY')
            return await host.current_persona.port.read_current(time.monotonic() + 2)
        port = host.initial_persona
        if action in ('prepare', 'generate', 'retry') and host.assembly.work_configuration is not None and host.assembly.work_configuration.fenced:
            raise OwnerFailure('RESOURCE_BUSY', 'configuration', 'ACTIVATION_PENDING')
        run_id = native_id('persona-run', host.stored.database_id, host.resources.instance_id)
        input_id = native_id('self-input', host.stored.database_id, host.resources.instance_id)
        if action == 'pending':
            fields(payload, set())
            return await port.read_pending(run_id)
        if action == 'prepare':
            fields(payload, {'key', 'self_revision', 'epoch'})
            return await port.prepare(text(payload['key']), input_id, revision(payload['self_revision']), payload['epoch'])
        if action == 'generate':
            fields(payload, {'key', 'generation'})
            if not self.sends_enabled:
                raise OwnerFailure('ACCESS_DENIED', 'provider', 'MODEL_DISPATCH_PAUSED')
            return await port.generate(text(payload['key']), run_id, revision(payload['generation']))
        if action == 'retry':
            fields(payload, {'key', 'run_revision', 'generation', 'candidate_id', 'epoch'})
            return await port.retry(text(payload['key']), run_id, revision(payload['run_revision']),
                revision(payload['generation']), text(payload['candidate_id']), payload['epoch'])
        shared = {'key', 'run_revision', 'candidate_id', 'candidate_revision', 'candidate_digest'}
        if action not in ('review', 'publish'):
            raise OwnerFailure('INVALID_INPUT', 'persona', 'UNSUPPORTED_OPERATION')
        fields(payload, shared | ({'decision'} if action == 'review' else {'epoch'}))
        args = (text(payload['key']), run_id, revision(payload['run_revision']), text(payload['candidate_id']),
            revision(payload['candidate_revision']), text(payload['candidate_digest']))
        if action == 'review':
            if payload['decision'] not in ('APPROVE', 'REJECT'):
                raise OwnerFailure('INVALID_INPUT', 'decision', 'INVALID_SHAPE')
            return await port.review(*args, payload['decision'])
        result = await port.publish(*args, payload['epoch'])
        if type(result) is Committed and await self.business_ready():
            self.bootstrap.state = 'READY' if await self.complete_wizard() else 'RECOVERING'
        return result

    async def complete_wizard(self) -> bool:
        """Confirm the final local wizard step after native persona publication."""
        current = await self.identity.read_draft()
        if current['state'] == 'COMPLETE':
            return True
        original = cast(str, current['initialization_key'])
        result = await self.identity.advance_wizard(stable_identity('managed-wizard-complete', original),
            cast(int, current['revision']), 'COMPLETE', original)
        return type(result) is Committed

    async def business_ready(self) -> bool:
        host = self.host
        if host is None or not self.initialized or host.current_persona is None:
            return False
        return await host.current_persona.port.has_publication(time.monotonic() + 5)

    async def close(self) -> bool:
        self.sends_enabled = False
        if self.configuration is not None and not await self.configuration.coordinator.close():
            return False
        if self.task is not None and not self.task.done():
            return False
        if self.bootstrap.assembly.backup is not None and not self.bootstrap.assembly.backup.close():
            return False
        return self.host is None or await self.host.close()
