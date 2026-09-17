"""Retained configuration activation across durable decisions and consumer recovery.

The caller supplies the complete native consumer set. Preparation must not
publish, publication must be idempotent for a version, and disposal must report
actual completion. Neither a timeout nor a failed consumer reverses a decision.
"""
from __future__ import annotations
import asyncio
from collections.abc import Callable
from typing import Protocol, cast

from companion_memory.configuration.activation_records import CONSUMERS
from companion_memory.configuration.managed_resolution import ManagedConfigurationCandidate
from companion_memory.configuration.managed_versions import ManagedVersions
from companion_memory.persistence import Committed
from companion_memory.persistence.daily_records import identity, Record
from companion_memory.persistence.owned_statements import OwnerFailure


class PreparationFailure(OSError):
    """A failed preparation that still retains an owned cleanup resource."""
    def __init__(self, resource: object):
        super().__init__('Resource preparation did not complete.')
        self.resource = resource


class ConfigurationConsumer(Protocol):
    async def prepare(self, version_id: str, candidate: ManagedConfigurationCandidate) -> object: ...
    async def publish(self, version_id: str, resource: object) -> None: ...
    async def dispose(self, resource: object) -> bool: ...


class ManagedActivation:
    def __init__(self, versions: ManagedVersions, consumers: dict[str, ConfigurationConsumer],
                 fence: Callable[[bool], None]):
        if set(consumers) != set(CONSUMERS):
            raise ValueError('Every declared configuration consumer is required.')
        self.versions, self.consumers, self.fence = versions, dict(consumers), fence
        self.task: asyncio.Task | None = None
        self.prepared: dict[str, object] = {}
        self.cleanup_pending = False
        self.failure: str | None = None
        self.published_version: str | None = None
        self.activation_id: str | None = None
        self.closed = False

    def key(self, activation: str, step: str) -> str:
        return identity('configuration-step', self.versions.rows.database, self.versions.rows.instance, activation, step)

    async def activate(self, activation_id: str, *, timeout_seconds: float):
        """Join one original activation while retaining every unfinished resource."""
        if self.closed:
            raise OwnerFailure('INVALID_STATE', 'configuration', 'SERVICE_CLOSED')
        if self.task is not None and not self.task.done():
            if self.activation_id != activation_id:
                raise OwnerFailure('RESOURCE_BUSY', 'activation', 'CLEANUP_PENDING', True)
        else:
            if (self.cleanup_pending or self.prepared) and self.activation_id != activation_id:
                raise OwnerFailure('RESOURCE_BUSY', 'activation', 'CLEANUP_PENDING', True)
            self.activation_id = activation_id
            self.task = asyncio.create_task(self._activate(activation_id))
        done, _ = await asyncio.wait((self.task,), timeout=timeout_seconds)
        if not done:
            return {'state': 'RECOVERING', 'activation_id': activation_id, 'cleanup_pending': True}
        return self.task.result()

    async def _write(self, kind: str, activation: Record, *, extra: dict | None = None):
        aid = cast(str, activation['object_id'])
        suffix = ':failure' if (extra or {}).get('failure') is not None else ''
        return await self.versions.execute(kind, self.key(aid, kind + ':' + cast(str, (extra or {}).get('consumer', 'all')) + suffix),
            {'activation_id': aid, 'version_id': activation['version_id'],
                'expected_revision': activation['expected_revision'], **(extra or {})}, 'configuration-recovery')

    async def _dispose(self) -> bool:
        for consumer, resource in tuple(self.prepared.items()):
            try:
                if await self.consumers[consumer].dispose(resource):
                    del self.prepared[consumer]
            except (OSError, ValueError, OwnerFailure):
                self.failure = 'RESOURCE_DISPOSAL_FAILED'
        self.cleanup_pending = bool(self.prepared)
        return not self.cleanup_pending

    async def _activate(self, activation_id: str):
        versions = self.versions
        activation = await versions.rows.read('managed_activations', activation_id)
        if activation is None:
            raise OwnerFailure('INVALID_INPUT', 'activation', 'VERSION_MISSING')
        version_id = cast(str, activation['version_id'])
        state = activation['state']
        if state == 'PREPARATION_FAILED':
            if await self._dispose():
                self.fence(False)
            return {'state': state, 'cleanup_pending': self.cleanup_pending}
        active = await versions.rows.read('managed_active', 'active-configuration')
        if state in ('DECIDED', 'APPLIED') and (active is None or active['activation_id'] != activation_id):
            # A completed historical activation is only an observation; replay
            # never republishes it over a later authoritative version.
            if state == 'APPLIED':
                return {'state': 'SUPERSEDED', 'version_id': version_id, 'cleanup_pending': False}
            raise OwnerFailure('STORAGE_FAILED', 'activation', 'DECISION_CHANGED')
        self.fence(True)
        self.failure = None
        candidate = await versions.load(version_id)
        try:
            for consumer in CONSUMERS:
                if consumer not in self.prepared:
                    try:
                        self.prepared[consumer] = await self.consumers[consumer].prepare(version_id, candidate)
                    except PreparationFailure as failure:
                        self.prepared[consumer] = failure.resource
                        self.cleanup_pending = True
                        raise
                    self.cleanup_pending = True
        except (OSError, ValueError, OwnerFailure):
            self.failure = 'RESOURCE_PREPARATION_FAILED'
            if state in ('CANDIDATE', 'PREPARED'):
                failed = await self._write('fail_configuration_preparation', activation,
                    extra={'plan_digest': activation['plan_digest']})
                await self._dispose()
                if type(failed) is Committed and not self.cleanup_pending:
                    self.fence(False)
                return {'state': 'PREPARATION_FAILED' if type(failed) is Committed else 'UNCONFIRMED',
                    'cleanup_pending': self.cleanup_pending, 'failure': self.failure, 'decision': failed}
            return {'state': 'RECOVERING', 'cleanup_pending': bool(self.prepared), 'failure': self.failure}
        versions.prepared = (activation_id, cast(str, activation['plan_digest']))
        if state == 'CANDIDATE':
            prepared = await self._write('prepare_configuration_activation', activation,
                extra={'plan_digest': activation['plan_digest']})
            if type(prepared) is not Committed:
                return {'state': 'UNCONFIRMED', 'decision': prepared, 'cleanup_pending': True}
            state = 'PREPARED'
        if state == 'PREPARED':
            decided = await self._write('decide_configuration_activation', activation,
                extra={'plan_digest': activation['plan_digest']})
            if type(decided) is not Committed:
                return {'state': 'UNCONFIRMED', 'decision': decided, 'cleanup_pending': True}
        observed = {cast(str, row['consumer']): row for row in await versions.runtime.status(activation_id)}
        if set(observed) != set(CONSUMERS):
            raise OwnerFailure('STORAGE_FAILED', 'activation', 'CONSUMER_MISSING')
        for consumer in CONSUMERS:
            try:
                await self.consumers[consumer].publish(version_id, self.prepared[consumer])
            except (OSError, ValueError, OwnerFailure):
                self.failure = 'CONSUMER_PUBLICATION_FAILED'
                failure_record = None
                if observed[consumer]['state'] != 'BOUND':
                    failure_record = await self._write('acknowledge_configuration_consumer', activation,
                        extra={'consumer': consumer, 'failure': self.failure})
                # The original durable decision survives. A local retry or
                # startup reconstructs these same resources and version.
                return {'state': 'RECOVERING', 'version_id': version_id, 'consumer': consumer,
                    'failure': self.failure, 'failure_record': failure_record, 'cleanup_pending': True}
            if observed[consumer]['state'] != 'BOUND':
                versions.published = (activation_id, consumer)
                acknowledged = await self._write('acknowledge_configuration_consumer', activation,
                    extra={'consumer': consumer, 'failure': None})
                versions.published = None
                if type(acknowledged) is not Committed:
                    return {'state': 'UNCONFIRMED', 'decision': acknowledged, 'cleanup_pending': True}
        finished = await self._write('finish_configuration_activation', activation)
        if type(finished) is not Committed:
            return {'state': 'UNCONFIRMED', 'decision': finished, 'cleanup_pending': True}
        self.published_version = version_id
        # A consumer owns its published resource. The coordinator releases only
        # preparation references; published resources are disposed by that owner.
        self.prepared.clear()
        self.cleanup_pending = False
        versions.prepared = None
        self.fence(False)
        return {'state': 'APPLIED', 'version_id': version_id, 'decision': finished, 'cleanup_pending': False}

    async def recover(self, *, timeout_seconds: float):
        active = await self.versions.rows.read('managed_active', 'active-configuration')
        if active is None:
            return {'state': 'BIRTH_CONFIGURATION', 'cleanup_pending': False}
        return await self.activate(cast(str, active['activation_id']), timeout_seconds=timeout_seconds)

    async def close(self) -> bool:
        self.closed = True
        if self.task is not None and not self.task.done():
            return False
        if not await self._dispose():
            return False
        self.versions.prepared = None
        self.versions.published = None
        return True
