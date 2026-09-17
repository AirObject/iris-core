"""Scoped host requests over issued public entry and information ports.

The authenticated token selects a registered host, permitted entry and operation.
Request JSON cannot create principals, routes or broader query capabilities.
Native ports retain their actual work after an unconfirmed response.
"""
from __future__ import annotations
import time
from typing import cast
from companion_memory.information.business import InformationPort
from companion_memory.information.management import HostIdentity
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.managed_business import ManagedBusiness
from .identity import IdentityAuthority, Principal
from .information_http import InformationHTTP
from companion_memory.runtime.content_assembly import stable

ROUTES = {
    'prepare': ('prepare', 'prepare_reply', 'POST', '/api/host/prepare'),
    'memory/search': ('query', 'search_memory', 'POST', '/api/host/memory/search'),
    'memory/deep-recall': ('query', 'deep_recall', 'POST', '/api/host/memory/deep-recall'),
    'usage': ('feedback', 'record_usage', 'POST', '/api/host/usage'),
    'usage/resolve': ('feedback', 'record_usage', 'POST', '/api/host/usage/resolve'),
    'state': ('state_read', 'get_state_view', 'GET', '/api/host/state'),
    'state/set': ('state_write', 'set_state', 'POST', '/api/host/state/set'),
    'state/update': ('state_write', 'update_state', 'POST', '/api/host/state/update'),
    'state/end': ('state_write', 'end_activity', 'POST', '/api/host/state/end'),
    'goals': ('goal_read', 'list_open_goals', 'GET', '/api/host/goals'),
    'goals/inject': ('goal_write', 'inject_goal', 'POST', '/api/host/goals/inject'),
    'goals/status': ('goal_write', 'update_goal_status', 'POST', '/api/host/goals/status'),
    'goals/deadline': ('goal_write', 'change_deadline', 'POST', '/api/host/goals/deadline'),
}


class ManagedHostHTTP:
    def __init__(self, business: ManagedBusiness, identity: IdentityAuthority):
        self.business, self.identity = business, identity
        self.ports: dict[tuple[str, str, str], InformationPort] = {}
        self.adapter: InformationHTTP | None = None

    async def dispatch(self, principal: Principal, method: str, path: str, payload: dict[str, object]):
        """Validate the closed envelope before binding exact native operation rights."""
        from .managed_application import fields, text
        if principal.kind != 'host' or method != 'POST' or not path.startswith('/api/host/'):
            raise OwnerFailure('ACCESS_DENIED', 'scope', 'OPERATION_NOT_GRANTED')
        fields(payload, {'entry_id', 'input'})
        entry = text(payload['entry_id'])
        supplied = payload['input']
        if type(supplied) is not dict:
            raise OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE')
        route = path.removeprefix('/api/host/')
        capability = 'accept' if route in ('accept', 'accept/resolve') else ROUTES.get(route, ('', '', '', ''))[0]
        special: tuple[str, str, str] | None = None
        if route == 'operations/resolve':
            fields(supplied, {'operation', 'input'})
            operation = text(supplied['operation'])
            if operation not in ('record_usage', 'set_state', 'update_state', 'end_activity', 'inject_goal', 'update_goal_status', 'change_deadline'):
                raise OwnerFailure('ACCESS_DENIED', 'scope', 'OPERATION_NOT_GRANTED')
            capability, special = 'confirm', (operation, 'POST', path)
        elif route == 'recalls/resolve':
            fields(supplied, {'query', 'prepared', 'deep'})
            if type(supplied['prepared']) is not bool or type(supplied['deep']) is not bool or supplied['prepared'] and supplied['deep']:
                raise OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE')
            operation = 'prepare_reply' if supplied['prepared'] else 'deep_recall' if supplied['deep'] else 'search_memory'
            capability, special = 'confirm', (operation, 'POST', path)
        elif route in ('accept/resolve', 'usage/resolve') and 'confirm' in principal.operations:
            capability = 'confirm'
        if capability not in principal.operations or entry not in principal.entries or principal.host_id is None:
            raise OwnerFailure('ACCESS_DENIED', 'scope', 'OPERATION_NOT_GRANTED')
        host = self.business.host
        if not self.business.initialized or host is None or self.business.bootstrap.state != 'READY':
            raise OwnerFailure('INVALID_STATE', 'instance', 'NOT_READY')
        if not await host.assembly.ingress.verify_host_entry(entry, principal.host_id):
            raise OwnerFailure('ACCESS_DENIED', 'binding', 'BINDING_MISMATCH')
        with self.identity.permission(principal, capability, entry):
            if route in ('accept', 'accept/resolve'):
                fields(supplied, {'key', 'event'})
                port = host.bind_entry(entry)
                key = text(supplied['key'])
                if route == 'accept/resolve':
                    return await port.confirm_acceptance(key, supplied['event'])
                return await port.accept_event(key, supplied['event'])
            operation, native_method, native_path = special or ROUTES[route][1:]
            if host.business is None or host.stored is None or host.runtime is None:
                raise OwnerFailure('INVALID_STATE', 'host', 'NOT_READY')
            # Actual descendants retain native authority until they finish. Idle
            # adapters have no durable state and can be retired before rebinding.
            if not host.business.jobs:
                for previous in self.ports.values():
                    host.business.revoke(previous)
                self.ports.clear()
            key = (principal.host_id, entry, operation)
            native = self.ports.get(key)
            if native is None:
                native = await host.bind_business(HostIdentity(stable('managed-host-binding', host.resources.instance_id, principal.host_id, entry, operation),
                    stable('managed-host-principal', host.resources.instance_id, principal.host_id),
                    principal.host_id, entry, frozenset((operation,)), (), time.monotonic() + 30),
                    include_forgotten=operation == 'deep_recall')
                self.ports[key] = native
            if self.adapter is None:
                self.adapter = InformationHTTP(host.stored, host.runtime.gate)
            return await self.adapter.dispatch(native, native_method, native_path, supplied)
