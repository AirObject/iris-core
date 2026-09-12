"""Independent read-only information and existing Provider ledger projections.

Observation scopes never grant query text, ticket secrets, activity values or
goal bodies. Provider results use its existing observer capability and bounded
aggregate semantics; this layer neither estimates charges nor invents cursors.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from types import MappingProxyType
import time
from companion_memory.persistence import Found, NotFound, Value
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge
from companion_memory.provider.ports import ObserverPort
from companion_memory.provider.values import Found as ProviderFound, NotFound as ProviderNotFound, Failed as ProviderFailed
from companion_memory.goals.service import GoalsService
from companion_memory.state.service import StateOwner
from companion_memory.retrieval.tickets import RecallTickets
from companion_memory.runtime.content_service import ContentRuntimeService
from .records import Record
from .errors import InformationError, InformationRejected, rejected


def owned_projection(value: object, depth: int = 0) -> Value:
    if depth > 12: raise InvalidValue()
    if value is None or type(value) is str or type(value) is int or type(value) is bool: return value
    if type(value) is tuple: return tuple(owned_projection(item, depth + 1) for item in value)
    if type(value) is MappingProxyType:
        return MappingProxyType({key: owned_projection(item, depth + 1) for key, item in value.items()})
    raise InvalidValue()


@dataclass(frozen=True, slots=True, init=False)
class InformationObserver:
    _service: InformationObservations
    _scopes: frozenset[str]
    _provider: ObserverPort | None

    def __init__(self): raise TypeError('Observation requires an explicit native scope.')
    async def read(self, scope: str, query: object): return await self._service.read(self, scope, query)


class InformationObservations:
    def __init__(self, runtime: ContentRuntimeService, tickets: RecallTickets, state: StateOwner, goals: GoalsService):
        self.runtime, self.tickets, self.state, self.goals = runtime, tickets, state, goals
        self.ports: dict[int, InformationObserver] = {}
        self.jobs: set[asyncio.Task[object]] = set()

    def bind(self, scopes: frozenset[str], provider: ObserverPort | None = None) -> InformationObserver:
        if type(scopes) is not frozenset or not scopes <= {'retrieval', 'state', 'goals', 'provider/usage', 'provider/requests', 'provider/budget'} or len(self.ports) >= 16:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        if any(scope.startswith('provider/') for scope in scopes) and type(provider) is not ObserverPort:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        port = object.__new__(InformationObserver)
        for name, value in (('_service', self), ('_scopes', scopes), ('_provider', provider)): object.__setattr__(port, name, value)
        self.ports[id(port)] = port
        return port

    def revoke(self, port: InformationObserver) -> None:
        self.ports.pop(id(port), None)

    def authorize(self, port: InformationObserver, scope: str) -> None:
        if type(port) is not InformationObserver or self.ports.get(id(port)) is not port or scope not in port._scopes:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        if self.runtime.state in ('CLOSED', 'CLOSING'):
            raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')

    async def read(self, port: InformationObserver, scope: str, query: object):
        try:
            self.authorize(port, scope)
            if len(self.jobs) >= 2: raise OwnerFailure('RESOURCE_BUSY', 'query', 'ADMISSION_FULL', True)
            if type(query) is not dict: raise InvalidValue()
            deadline = bounded_deadline(time.monotonic(), 2)
            async def inspect() -> object:
                with DeadlineScope(deadline):
                    try:
                        now = int(time.time() * 1000000)
                        if scope.startswith('provider/'):
                            provider = port._provider
                            if provider is None: raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
                            if scope == 'provider/usage': result = await provider.query_usage(query)
                            elif scope == 'provider/requests':
                                if set(query) != {'request_id'}: raise InvalidValue()
                                result = await provider.get_request(query['request_id'])
                            else:
                                if query: raise InvalidValue()
                                result = await provider.get_budget_state()
                            if type(result) is ProviderNotFound:
                                self.authorize(port, scope)
                                return NotFound()
                            if type(result) is ProviderFailed:
                                reason = result.error.reason
                                if result.error.code == 'ACCESS_DENIED': raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
                                if result.error.code == 'INVALID_INPUT': raise OwnerFailure('INVALID_INPUT', 'query', 'LIMIT_EXCEEDED' if reason == 'LIMIT_EXCEEDED' else 'INVALID_SHAPE')
                                raise OwnerFailure('STORAGE_FAILED', 'storage', 'READ_FAILED', result.error.cleanup_pending)
                            if type(result) is not ProviderFound: raise InvalidValue()
                            view = owned_projection(result.value)
                        else:
                            if query: raise InvalidValue()
                            if scope == 'retrieval':
                                view = MappingProxyType({'tickets': await self.tickets.occupancy(now), 'coverage': await self.tickets.owner.memory.coverage_view(),
                                    'observed_at': now, 'query_model_calls': 0, 'storage_execution': 'ACTUAL'})
                            elif scope == 'state': view = await self.state.observation(now)
                            else: view = await self.goals.observation(now)
                        self.authorize(port, scope)
                        encode_content(view, 32768)
                        return Found(view)
                    except OwnerFailure as failure: return rejected('observe_information', failure)
                    except ValueTooLarge: return rejected('observe_information', OwnerFailure('INVALID_INPUT', 'query', 'LIMIT_EXCEEDED'))
                    except InvalidValue: return rejected('observe_information', OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE'))
            task, outcome = start_owned(inspect()); self.jobs.add(task); self.runtime.retain_external_work(task)
            def ended(job: asyncio.Task[object]) -> None:
                if not job.cancelled(): job.exception()
                self.jobs.discard(job)
            task.add_done_callback(ended)
            done, _ = await asyncio.wait((outcome,), timeout=max(0, deadline - time.monotonic()))
            if not done: return InformationRejected(InformationError('TIMEOUT', 'observe_information', 'query', 'DEADLINE_EXCEEDED', True))
            return outcome.result()
        except OwnerFailure as failure: return rejected('observe_information', failure)
        except InvalidValue: return rejected('observe_information', OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE'))
