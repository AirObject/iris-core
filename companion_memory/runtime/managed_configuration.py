"""Managed configuration publication with original frozen work and Provider resources.

Supported values are prepared before the durable decision and published through
their native owners. Resource migrations remain explicit preparation failures.
"""
from __future__ import annotations
from dataclasses import dataclass
import asyncio
from hashlib import sha256
from typing import cast, Any

from companion_memory.configuration.activation_records import CONSUMERS
from companion_memory.configuration.content_codec import dump, decode_content_entry
from companion_memory.configuration.execution_versions import ExecutionVersion
from companion_memory.configuration.managed_codec import candidate_values, candidate_inputs
from companion_memory.configuration.managed_resolution import ManagedConfigurationCandidate, ManagedConfigurationOk, resolve_managed_configuration
from companion_memory.configuration.managed_persistence import ManagedConfigurationAssembly
from companion_memory.persistence.owned_statements import OwnerFailure
from .managed_activation import ManagedActivation, ConfigurationConsumer, PreparationFailure


@dataclass(frozen=True, slots=True)
class PreparedConfiguration:
    version: ExecutionVersion
    consumer: str
    owner: ManagedConfiguration
    resource: object = None


class NativeConfigurationConsumer:
    def __init__(self, manager: ManagedConfiguration, consumer: str):
        self.manager, self.consumer = manager, consumer

    async def prepare(self, version_id: str, candidate: ManagedConfigurationCandidate) -> object:
        manager = self.manager
        manager.require_safe_boundary(candidate)
        version = await manager.work.versions.load(version_id)
        if candidate_values(version.candidate) != candidate_values(candidate):
            raise OwnerFailure('STORAGE_FAILED', 'configuration', 'CONTENT_MISMATCH')
        resource = None
        if self.consumer == 'provider':
            provider = manager.host.provider
            if provider is None or provider.managed_versions is None:
                raise OwnerFailure('INVALID_STATE', 'provider', 'NOT_READY')
            resource = await asyncio.to_thread(provider.managed_versions.prepare, version)
        if self.consumer == 'logging_service' and manager.business.logging is not None:
            try:
                resource = await manager.business.logging.prepare(version)
            except PreparationFailure as failure:
                raise PreparationFailure(PreparedConfiguration(version, self.consumer, manager, failure.resource)) from None
        return PreparedConfiguration(version, self.consumer, manager, resource)

    async def publish(self, version_id: str, resource: object) -> None:
        if type(resource) is not PreparedConfiguration or resource.owner is not self.manager or resource.consumer != self.consumer or resource.version.version_id != version_id:
            raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        self.manager.require_safe_boundary(resource.version.candidate)
        if self.consumer == 'provider':
            provider = self.manager.host.provider
            if provider is None or provider.managed_versions is None:
                raise OwnerFailure('INVALID_STATE', 'provider', 'NOT_READY')
            provider.managed_versions.publish(resource.resource)
        if self.consumer == 'logging_service' and self.manager.business.logging is not None:
            self.manager.business.logging.publish(resource.resource)
        self.manager.prepared_version = resource.version

    async def dispose(self, resource: object) -> bool:
        if type(resource) is not PreparedConfiguration or resource.owner is not self.manager:
            return False
        if self.consumer == 'logging_service' and self.manager.business.logging is not None:
            return await self.manager.business.logging.dispose(resource.resource)
        return True


class ManagedConfiguration:
    def __init__(self, business):
        host = business.host
        if host is None or host.stored is None or host.runtime is None or host.assembly.work_configuration is None:
            raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
        assembly = host.combination.configuration
        if type(assembly) is not ManagedConfigurationAssembly or assembly.versions is None:
            raise OwnerFailure('INVALID_STATE', 'configuration', 'OWNER_MISSING')
        self.business, self.host, self.versions = business, host, assembly.versions
        self.work = host.assembly.work_configuration
        self.prepared_version: ExecutionVersion | None = None
        self.previous_state = business.bootstrap.state
        consumers: dict[str, ConfigurationConsumer] = {name: NativeConfigurationConsumer(self, name) for name in CONSUMERS}
        self.coordinator = ManagedActivation(self.versions, consumers, self.fence)

    def fence(self, closed: bool):
        if closed:
            if not self.work.fenced:
                self.previous_state = self.business.bootstrap.state
            self.work.fenced = True
            self.business.bootstrap.state = 'ACTIVATING'
        else:
            if self.coordinator.published_version is not None:
                if self.prepared_version is None or self.prepared_version.version_id != self.coordinator.published_version:
                    raise OwnerFailure('STORAGE_FAILED', 'configuration', 'CONSUMER_VERSION_CHANGED')
                self.work.versions.publish(self.prepared_version)
            self.work.fenced = False
            self.business.bootstrap.state = self.previous_state

    @staticmethod
    def changes(before: ManagedConfigurationCandidate, after: ManagedConfigurationCandidate) -> tuple[dict, ...]:
        old = {d['domain_id']: d for d in candidate_values(before)['domains']}
        changed = []
        for domain in candidate_values(after)['domains']:
            prior = {entry['parameter_key']: entry['body'] for entry in old[domain['domain_id']]['entries']}
            for entry in domain['entries']:
                key = entry['parameter_key']
                if prior[key] == entry['body']:
                    continue
                definition, _, value, _ = decode_content_entry(entry['body'])
                _, _, previous, _ = decode_content_entry(prior[key])
                boundary = ('NEXT_BATCH' if key.startswith('platforms.') else
                    'NEXT_DREAM' if key in ('dream.schedule', 'runtime.timezone', 'memory.long_term_maintenance', 'self_model.initial_persona') else
                    'NEXT_REQUEST' if key in ('provider.profiles', 'provider.transport') else
                    'LOGGER_REBUILD' if key.startswith('logging.') and key != 'logging.file_directory' else
                    'RESTART_REQUIRED' if key in ('storage.database_file', 'logging.file_directory', 'media.root_directory', 'media.staging_directory') else
                    'MIGRATION_REQUIRED' if key == 'retrieval.semantic' else 'RESOURCE_PREPARATION_REQUIRED')
                changed.append({'key': key, 'before': previous, 'after': value, 'boundary': boundary,
                    'consumers': list(definition['consumers'])})
        return tuple(changed)

    def require_safe_boundary(self, candidate: ManagedConfigurationCandidate):
        host = self.host
        runtime = host.runtime
        if runtime is None or runtime.gate.information_checkpoint() is None:
            raise OwnerFailure('MODE_BLOCKED', 'configuration', 'DREAMING')
        if (runtime._jobs or runtime._commands or runtime.external_work_pending or
                host.dream_scheduler.busy or host.business is not None and host.business.jobs or
                host.provider is not None and host.provider.cleanup_pending):
            raise OwnerFailure('RESOURCE_BUSY', 'configuration', 'WORK_PENDING', True)
        changed = self.changes(self.work.versions.birth.candidate, candidate)
        if any(not (change['key'].startswith('platforms.') or (self.business.logging is not None and change['key'].startswith('logging.') and change['key'] != 'logging.file_directory') or change['key'] in ('dream.schedule', 'runtime.timezone', 'memory.long_term_maintenance', 'self_model.initial_persona', 'provider.profiles', 'provider.transport')) for change in changed):
            raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'configuration', 'CONSUMER_PREPARATION_UNAVAILABLE')
        if host.provider is None or host.provider.managed_versions is None:
            raise OwnerFailure('INVALID_STATE', 'provider', 'NOT_READY')
        host.provider.managed_versions.compatible(candidate)

    async def status(self) -> dict:
        active = await self.versions.rows.read('managed_active', 'active-configuration')
        activation = await self.versions.rows.read('managed_activations', cast(str, active['activation_id'])) if active else None
        return {'revision': active['revision'] if active else 0,
            'authoritative_version': active['version_id'] if active else self.work.versions.birth.version_id,
            'published_version': self.work.versions.active.version_id,
            'state': activation['state'] if activation else 'APPLIED',
            'admission_closed': self.work.fenced, 'cleanup_pending': self.coordinator.cleanup_pending,
            'consumers': await self.versions.runtime.status(cast(str, active['activation_id'])) if active else (),
            'failure': self.coordinator.failure}

    async def read(self, version_id: str | None = None):
        status = await self.status()
        selected = version_id or cast(str, status['authoritative_version'])
        candidate = await self.versions.load(selected)
        values = {}
        for domain in candidate_values(candidate)['domains']:
            name = 'platform' if domain['domain_id'].startswith('platform:') else 'text' if domain['domain_id'] == 'daily_cognition' else domain['domain_id']
            values[name] = {entry['parameter_key']: decode_content_entry(entry['body'])[2] for entry in domain['entries']}
        return {'status': status, 'version_id': selected, 'values': values, 'birth_version': self.work.versions.birth.version_id}

    async def history(self, after: str):
        rows = await self.versions.rows.page('managed_versions', after)
        return {'items': [{key: row[key] for key in ('object_id', 'created_at_us', 'parent_revision', 'content_digest', 'actor', 'reason', 'rollback_of')} for row in rows],
            'after': rows[-1]['object_id'] if rows else None}

    async def preview(self, expected_revision: int, patch: dict[str, object]):
        self.host.normal()
        status = await self.status()
        if status['revision'] != expected_revision:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'REVISION_CONFLICT')
        current = await self.versions.load(cast(str, status['authoritative_version']))
        candidate = self.patch(current, patch)
        return candidate, self.plan(current, candidate, expected_revision, cast(str, status['authoritative_version']))

    @staticmethod
    def patch(current: ManagedConfigurationCandidate, patch: dict[str, object]) -> ManagedConfigurationCandidate:
        if type(patch) is not dict or not patch or set(patch) - {'foundation', 'runtime', 'content', 'platform', 'information', 'text'}:
            raise OwnerFailure('INVALID_INPUT', 'configuration', 'INVALID_SHAPE')
        raw = cast(list[Any], list(candidate_inputs(current, dict(current._directories))))
        domains = {'foundation': raw[0], 'runtime': raw[1], 'platform': raw[2][0], 'content': raw[3], 'information': raw[4], 'text': raw[5]}
        for name, changes in patch.items():
            if type(changes) is not dict or not changes:
                raise OwnerFailure('INVALID_INPUT', 'configuration', 'INVALID_SHAPE')
            domains[name]['explicit_values'].update(changes)
        candidate = resolve_managed_configuration(*raw)
        if type(candidate) is not ManagedConfigurationOk:
            raise OwnerFailure('INVALID_INPUT', 'configuration', 'CONFIGURATION_INVALID')
        return candidate.value

    def plan(self, current: ManagedConfigurationCandidate, candidate: ManagedConfigurationCandidate,
             expected_revision: int, previous_version: str, rollback_of: str | None = None):
        changes = self.changes(current, candidate)
        if not changes:
            raise OwnerFailure('INVALID_INPUT', 'configuration', 'NO_CHANGE')
        plan = {'expected_revision': expected_revision, 'previous_version': previous_version, 'rollback_of': rollback_of,
            'content_digest': sha256(dump(candidate_values(candidate)).encode()).hexdigest(), 'changes': changes}
        return {**plan, 'plan_digest': sha256(dump(plan).encode()).hexdigest()}

    async def save(self, key: str, expected_revision: int, patch: dict[str, object], plan_digest: str, *, actor: str, reason: str):
        aid, _ = self.versions.ids(key)
        original = await self.versions.rows.read('managed_activations', aid)
        if original is None:
            candidate, plan = await self.preview(expected_revision, patch)
        else:
            previous = cast(str, original['previous_version_id']) if original['previous_version_id'] is not None else self.work.versions.birth.version_id
            current = await self.versions.load(previous)
            candidate = self.patch(current, patch)
            plan = self.plan(current, candidate, expected_revision, previous)
        if plan_digest != plan['plan_digest']:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'PREVIEW_CHANGED')
        return await self.versions.save(key, candidate, expected_revision, plan_digest, actor=actor, reason=reason)

    async def preview_rollback(self, expected_revision: int, target: str):
        self.host.normal()
        status = await self.status()
        if status['revision'] != expected_revision:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'REVISION_CONFLICT')
        current = await self.versions.load(cast(str, status['authoritative_version']))
        candidate = await self.versions.load(target)
        return candidate, self.plan(current, candidate, expected_revision, cast(str, status['authoritative_version']), target)

    async def save_rollback(self, key: str, expected_revision: int, target: str, plan_digest: str, *, actor: str, reason: str):
        aid, _ = self.versions.ids(key)
        original = await self.versions.rows.read('managed_activations', aid)
        if original is None:
            candidate, plan = await self.preview_rollback(expected_revision, target)
        else:
            previous = cast(str, original['previous_version_id']) if original['previous_version_id'] is not None else self.work.versions.birth.version_id
            current = await self.versions.load(previous)
            candidate = await self.versions.load(target)
            plan = self.plan(current, candidate, expected_revision, previous, target)
        if plan_digest != plan['plan_digest']:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'PREVIEW_CHANGED')
        return await self.versions.save(key, candidate, expected_revision, plan_digest, actor=actor, reason=reason, rollback_of=target)

    async def activate(self, activation_id: str):
        self.host.checkpoint()
        if self.host.runtime is None or self.host.runtime.gate.information_checkpoint() is None:
            raise OwnerFailure('MODE_BLOCKED', 'configuration', 'DREAMING')
        return await self.coordinator.activate(activation_id,
            timeout_seconds=self.business.bootstrap.settings.integer('management.maintenance_timeout_seconds'))

    async def recover(self):
        return await self.coordinator.recover(
            timeout_seconds=self.business.bootstrap.settings.integer('management.maintenance_timeout_seconds'))
