"""Administrator adapters over issued native state, goal and dream ports.

The registered host/entry comes from the immutable initialization record. HTTP
input selects an operation and its closed native payload, never an identity or
an owner repository. Stable administrator bindings preserve original receipts.
"""
from __future__ import annotations
import time
from typing import cast
from companion_memory.information.management import HostIdentity
from companion_memory.information.business import InformationPort
from companion_memory.dream.port import DreamPort, OPERATIONS
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.content_assembly import stable
from .information_http import InformationHTTP
from .managed_host_http import ROUTES

BUSINESS = frozenset(('state', 'state/set', 'state/update', 'state/end', 'goals', 'goals/inject', 'goals/status', 'goals/deadline'))


class ManagedOperations:
    def __init__(self, application):
        self.application = application
        self.host = None
        self.business_port: InformationPort | None = None
        self.dream_port: DreamPort | None = None
        self.business_expires = 0.0
        self.content_port = None
        import asyncio
        self.dream_binding_lock = asyncio.Lock()

    async def ready(self):
        app = self.application
        host = app.business.host
        if host is None or not app.business.initialized or host.runtime is None or not await app.business.business_ready():
            raise OwnerFailure('INVALID_STATE', 'instance', 'NOT_READY')
        if self.host is not host:
            self.host = host
            self.business_port = None
            self.dream_port = None
            self.content_port = None
        host.checkpoint()
        draft = (await app.identity.read_draft())['draft']
        return host, draft

    def identity(self, host, draft, operations, kind, expires=None):
        return HostIdentity(stable('managed-administrator-port', host.resources.instance_id, kind),
            'administrator', draft['host_id'], draft['entry_id'], frozenset(operations), (), self.business_expires if expires is None else expires)

    async def state_goals(self, route: str, payload: dict[str, object]):
        if route not in BUSINESS:
            raise OwnerFailure('ACCESS_DENIED', 'route', 'OPERATION_NOT_GRANTED')
        host, draft = await self.ready()
        if host.business is None or host.stored is None:
            raise OwnerFailure('INVALID_STATE', 'instance', 'NOT_READY')
        host_id, entry_id = draft['host_id'], draft['entry_id']
        if set(payload) == {'entry_id', 'host_id', 'input'}:
            from .managed_application import text
            host_id, entry_id = text(payload['host_id']), text(payload['entry_id'])
            if type(payload['input']) is not dict or not await host.assembly.ingress.verify_host_entry(entry_id, host_id):
                raise OwnerFailure('ACCESS_DENIED', 'binding', 'BINDING_MISMATCH')
            payload = payload['input']
        _, operation, method, path = ROUTES[route]
        return await self.application.host_http.native(host_id, entry_id, operation,
            method, path, payload, administrator=True)

    async def dream(self, action: str, payload: dict[str, object]):
        from .managed_application import fields, text, revision
        host, draft = await self.ready()
        if host.dream_ports is None or host.combination.dream is None:
            raise OwnerFailure('INVALID_STATE', 'dream', 'NOT_READY')
        if action == 'status':
            fields(payload, set())
            return {'schedule': await host.combination.dream.schedule(), 'mode_epoch': host.runtime.gate.epoch}
        async with self.dream_binding_lock:
            if self.dream_port is None or time.monotonic() >= self.dream_port.identity.expires_at:
                if host.dream_ports.tasks:
                    raise OwnerFailure('RESOURCE_BUSY', 'dream', 'CLEANUP_PENDING', True)
                if self.dream_port is not None:
                    host.dream_ports.revoke(self.dream_port)
                self.dream_port = await host.bind_dream(self.identity(host, draft, OPERATIONS, 'dream', time.monotonic() + 3600))
        port = self.dream_port
        assert port is not None
        if action == 'inspect':
            fields(payload, {'run_id'})
            return {'run': await port.inspect_dream(text(payload['run_id']))}
        if action == 'confirm':
            fields(payload, {'kind', 'key'})
            return await port.confirm_dream_step(text(payload['kind']), text(payload['key']))
        if action not in ('start', 'pause', 'resume', 'abort'):
            raise OwnerFailure('INVALID_INPUT', 'action', 'UNSUPPORTED_OPERATION')
        fields(payload, {'key', 'run_id', 'expected_revision', 'mode_epoch'} | ({'mode'} if action == 'start' else set()))
        key, run = text(payload['key']), text(payload['run_id'])
        expected = revision(payload['expected_revision'])
        epoch = revision(payload['mode_epoch'])
        assert expected is not None and epoch is not None
        if action == 'start':
            if payload['mode'] not in ('FOCUSED', 'BACKGROUND'):
                raise OwnerFailure('INVALID_INPUT', 'mode', 'INVALID_SHAPE')
            return await port.start_dream(key, run, expected, epoch, mode=cast(str, payload['mode']))
        method = {'pause': port.pause_dream, 'resume': port.resume_dream, 'abort': port.abort_dream}[action]
        return await method(key, run, expected, epoch)

    async def content(self, action: str, query: dict[str, object]):
        host, draft = await self.ready()
        if self.content_port is None:
            self.content_port = host.runtime.observations.bind((draft['entry_id'],), instance_observe=True)
        methods = {'runtime': self.content_port.read_runtime_view, 'entries': self.content_port.read_entry_status,
            'batches': self.content_port.read_batch_status, 'memory': self.content_port.read_memory_status,
            'media': self.content_port.read_media_status}
        if action not in methods:
            raise OwnerFailure('INVALID_INPUT', 'scope', 'UNSUPPORTED_OPERATION')
        return await methods[action](query)

    async def memory(self, action: str, payload: dict[str, object]):
        from .managed_application import fields, text
        host, _ = await self.ready()
        if action == 'list':
            fields(payload, {'after'})
            after = payload['after']
            if type(after) is not str or len(after) > 128:
                raise OwnerFailure('INVALID_INPUT', 'cursor', 'INVALID_SHAPE')
            owner = host.combination.memory_administration
            if owner is None:
                raise OwnerFailure('INVALID_STATE', 'memory', 'NOT_READY')
            host.normal()
            result = await owner.current_page(after)
            host.normal()
            return result
        if action == 'read':
            fields(payload, {'object_id'})
            object_id = text(payload['object_id'])
            port = host.runtime.memory.bind_read((object_id,), ('get_for_deep_read',))
            return await port.get_for_deep_read(object_id)
        if action in ('source', 'source-member'):
            fields(payload, {'object_id', 'source_id'} | ({'ordinal'} if action == 'source-member' else set()))
            object_id, source_id = text(payload['object_id']), text(payload['source_id'])
            port = host.runtime.sources.bind_inspection((object_id,))
            return await (port.read_source_manifest(object_id, source_id) if action == 'source'
                else port.read_source_member(object_id, source_id, payload['ordinal']))
        raise OwnerFailure('INVALID_INPUT', 'action', 'UNSUPPORTED_OPERATION')
