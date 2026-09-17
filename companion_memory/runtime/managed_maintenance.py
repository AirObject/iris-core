"""Owned backup quiescence closes actual consumers while retaining volume ownership.

An original backup request is durable before closing the business host. A new
native assembly reopens the same resources after copying, always with model
sending paused. Timeouts retain the maintenance task and original operation.
"""
from __future__ import annotations
import asyncio
import contextvars
import os
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import Committed, Ready
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.managed_backup import ConsistentBackup
from companion_memory.memory.formats import record
from companion_memory.management.identity import Principal
from .managed_bootstrap import ManagedBootstrap
from .managed_business import ManagedBusiness
from .managed_resources import sync_directory
if TYPE_CHECKING:
    from companion_memory.management.managed_application import ManagedApplication


class ManagedMaintenance:
    def __init__(self, application: ManagedApplication, *, restore_business: bool = True):
        self.application = application
        self.restore_business = restore_business
        self.task: asyncio.Task | None = None
        self.key: str | None = None

    async def create_backup(self, key: str, principal: Principal):
        await self.application.identity.recheck(principal)
        if principal.kind != 'administrator':
            raise OwnerFailure('ACCESS_DENIED', 'backup', 'ADMINISTRATOR_REQUIRED')
        return await self._start(key, 'administrator')

    async def create_local_backup(self, key: str):
        """Deployment-only capability, reachable only under the held volume lease.

        This method is not exposed as HTTP. The local CLI already requires the
        service UID's private volume and stopped-process ownership.
        """
        if self.restore_business:
            raise OwnerFailure('ACCESS_DENIED', 'backup', 'DEPLOYMENT_CAPABILITY_REQUIRED')
        return await self._start(key, 'deployment_backup')

    async def _start(self, key: str, actor: str):
        """Keep one request and its task, including after HTTP confirmation timeout."""
        if self.task is not None and not self.task.done():
            if self.key != key:
                raise OwnerFailure('RESOURCE_BUSY', 'backup', 'MAINTENANCE_ACTIVE', True)
        else:
            app = self.application
            owner = app.bootstrap.assembly.backup
            if owner is None:
                raise OwnerFailure('INVALID_STATE', 'backup', 'NOT_READY')
            requested = await owner.request(key, actor)
            if type(requested) is not Committed:
                return requested
            backup_id = cast(str, record(requested.receipt.result)['backup_id'])
            previous = await owner.rows.read('backups', backup_id)
            if previous is not None and previous['state'] in ('COMPLETE', 'FAILED'):
                return {'state': previous['state'], 'backup_id': backup_id, 'reason': previous['failure'], 'new_sends': 0}
            self.key = key
            app.bootstrap.state = 'MAINTENANCE'
            # A durable maintenance request now owns local close/reopen work.
            # Expiring HTTP authority must not strand its already owned cleanup.
            self.task = asyncio.create_task(self._copy_and_reopen(backup_id, key, actor), context=contextvars.Context())
        done, _ = await asyncio.wait((self.task,), timeout=self.application.bootstrap.settings.integer('management.maintenance_timeout_seconds'))
        if not done:
            return {'state': 'UNCONFIRMED', 'operation_key': key, 'cleanup_pending': True}
        return self.task.result()

    async def recover(self) -> None:
        """Resolve interrupted copies from verified publication, never mix source epochs."""
        app = self.application
        owner = app.bootstrap.assembly.backup
        if owner is None:
            raise ValueError('Backup recovery requires its bound owner.')
        copier = ConsistentBackup(app.resources, lambda: False)
        after = ''
        while True:
            rows = await owner.rows.page('backups', after)
            if not rows:
                return
            for row in rows:
                if row['state'] != 'REQUESTED':
                    continue
                backup_id, actor = cast(str, row['object_id']), cast(str, row['request_actor'])
                destination = app.resources.root / 'backups' / backup_id
                staging = app.resources.root / 'backup_staging' / backup_id
                try:
                    selected = destination if destination.exists() else staging
                    manifest = await asyncio.to_thread(copier.verify, selected)
                    if selected == staging:
                        os.rename(staging, destination)
                        sync_directory(destination.parent)
                        sync_directory(staging.parent)
                    completed = await self._complete(backup_id, actor, manifest)
                    if completed['state'] != 'COMPLETE':
                        raise OwnerFailure('STORAGE_FAILED', 'backup', 'CONFIRMATION_PENDING', True)
                except (OSError, ValueError):
                    failed = await owner.execute('fail_backup', backup_id, {'backup_id': backup_id,
                        'expected_revision': row['revision'], 'failure': 'INTERRUPTED_COPY_NOT_COMPLETE'}, actor)
                    if type(failed) is not Committed:
                        raise OwnerFailure('STORAGE_FAILED', 'backup', 'CONFIRMATION_PENDING', True)
            after = cast(str, rows[-1]['object_id'])

    async def _copy_and_reopen(self, backup_id: str, key: str, actor: str):
        app = self.application
        previous = app.bootstrap
        resources = previous.resources
        if resources is None:
            raise OwnerFailure('INVALID_STATE', 'backup', 'NOT_READY')
        factory = app.business.resource_factory
        # The lease is not released while any owner or worker is unfinished.
        while not (app.identity.close() and await app.business.close()):
            await asyncio.sleep(0.05)
        if app.logging is not None:
            while not await app.logging.close():
                await asyncio.sleep(0.05)
        if previous.assembly.backup is not None:
            previous.assembly.backup.close()
        await previous.assembly.storage.close()
        if previous.assembly.storage.get_health().lifecycle != 'CLOSED':
            raise OwnerFailure('RESOURCE_BUSY', 'backup', 'CLEANUP_PENDING', True)
        copier = ConsistentBackup(resources, lambda: previous.assembly.storage.get_health().lifecycle == 'CLOSED')
        copy_error = None
        manifest = None
        try:
            completed = await asyncio.to_thread(copier.create, backup_id)
            manifest = await asyncio.to_thread(copier.verify, completed)
        except (OSError, ValueError) as failure:
            copy_error = type(failure).__name__
        # Transfer the held descriptor directly; never unlink/reacquire a lock.
        reopened = ManagedBootstrap(previous.settings)
        previous.resources = None
        app.bootstrap = reopened
        opened = await reopened.open(retained_resources=resources)
        if type(opened) is not Ready or reopened.assembly.identity is None:
            reopened.state = 'FAULTED'
            raise OwnerFailure('INVALID_STATE', 'backup', 'REOPEN_FAILED')
        app.identity = reopened.assembly.identity
        app.resources = resources
        app.business = ManagedBusiness(reopened, app.identity, resource_factory=factory)
        from companion_memory.management.managed_host_http import ManagedHostHTTP
        app.host_http = ManagedHostHTTP(app.business, app.identity)
        app.observer = None
        if app.logging is not None:
            app.logging.open(reopened)
            app.business.logging = app.logging
        if self.restore_business:
            await app.business.recover()
            # A bounded public recovery reply may precede actual consumer
            # recovery. This maintenance owner must retain the resource lease
            # and wait before competing for its completion audit transaction.
            if app.business.task is not None:
                await asyncio.shield(app.business.task)
        if app.logging is not None:
            await app.logging.attach(app)
        if copy_error is not None or manifest is None:
            owner = reopened.assembly.backup
            assert owner is not None
            outcome = await owner.execute('fail_backup', backup_id, {'backup_id': backup_id,
                'expected_revision': 1, 'failure': 'COPY_NOT_VERIFIED'}, actor)
            return {'state': 'FAILED' if type(outcome) is Committed else 'UNCONFIRMED',
                'backup_id': backup_id, 'reason': 'COPY_NOT_VERIFIED', 'result': outcome, 'startup_sends': 0}
        return await self._complete(backup_id, actor, manifest)

    async def _complete(self, backup_id: str, actor: str, manifest: dict[str, object]):
        reopened = self.application.bootstrap
        resources = self.application.resources
        owner = reopened.assembly.backup
        assert owner is not None
        from companion_memory.persistence.managed_backup import file_digest
        manifest_digest = (await asyncio.to_thread(file_digest, resources.root / 'backups' / backup_id / 'manifest.json'))[1]
        files = cast(list[dict[str, object]], manifest['files'])
        outcome = await owner.execute('complete_backup', backup_id, {'backup_id': backup_id, 'expected_revision': 1,
            'manifest_digest': manifest_digest, 'file_count': len(files),
            'total_bytes': sum(cast(int, item['bytes']) for item in files)}, actor)
        return {'state': 'COMPLETE' if type(outcome) is Committed else 'UNCONFIRMED', 'backup_id': backup_id,
            'result': outcome, 'startup_sends': 0}
