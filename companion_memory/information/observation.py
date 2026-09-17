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
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from companion_memory.retrieval.semantic_work import SemanticWork
    from companion_memory.retrieval.semantic_cache import SemanticQueryCache
from companion_memory.persistence import Found, NotFound, Value
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge
from companion_memory.provider.ports import ObserverPort
from companion_memory.provider.embedding_observation import EmbeddingObserver
from companion_memory.provider.daily_observation import DailyProviderObserver
from companion_memory.runtime.daily_observation import DailyObservations
from companion_memory.provider.values import InvalidData
from companion_memory.provider.ledger import LedgerFailure
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
    _provider: ObserverPort | EmbeddingObserver | DailyProviderObserver | None

    def __init__(self): raise TypeError('Observation requires an explicit native scope.')
    async def read(self, scope: str, query: object): return await self._service.read(self, scope, query)


class InformationObservations:
    def __init__(self, runtime: ContentRuntimeService, tickets: RecallTickets, state: StateOwner, goals: GoalsService,*,
                 semantic:SemanticWork|None=None,cache:SemanticQueryCache|None=None,daily:DailyObservations|None=None):
        self.runtime, self.tickets, self.state, self.goals = runtime, tickets, state, goals
        self.ports: dict[int, InformationObserver] = {}
        self.jobs: set[asyncio.Task[object]] = set()
        self.semantic=semantic;self.cache=cache
        if daily is not None and (type(daily) is not DailyObservations or daily.host.runtime is not runtime):raise InvalidValue()
        self.daily=daily

    def bind(self, scopes: frozenset[str], provider: ObserverPort | EmbeddingObserver | DailyProviderObserver | None = None) -> InformationObserver:
        allowed={'learning','media','goal_dedup'} if self.daily is not None else set()
        if self.daily is not None and self.daily.host.combination.dream_format:allowed|={'dream','maintenance','persona'}
        if type(scopes) is not frozenset or not scopes <= allowed | {'retrieval', 'retrieval/semantic','state', 'goals', 'provider/usage', 'provider/requests', 'provider/budget'} or len(self.ports) >= 16:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        if provider is None and self.daily is not None and any(scope.startswith('provider/') for scope in scopes):provider=self.daily.provider
        if type(provider) is DailyProviderObserver and (self.daily is None or provider is not self.daily.provider):raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        if any(scope.startswith('provider/') for scope in scopes) and type(provider) not in (ObserverPort,EmbeddingObserver,DailyProviderObserver):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        if 'retrieval/semantic' in scopes and (self.semantic is None or self.cache is None):raise OwnerFailure('ACCESS_DENIED','capability','OPERATION_NOT_GRANTED')
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
                        elif scope in ('learning','media','goal_dedup','dream','maintenance','persona'):
                            if self.daily is None:raise InvalidValue()
                            view=await self.daily.read(scope,query)
                        elif scope=='retrieval/semantic':
                            if set(query)-{'after'} or 'after' in query and type(query['after']) is not str:raise InvalidValue()
                            view=await self._semantic_view(query.get('after',''),now)
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
                    except LedgerFailure:return rejected('observe_information',OwnerFailure('STORAGE_FAILED','storage','READ_FAILED'))
                    except OwnerFailure as failure: return rejected('observe_information', failure)
                    except ValueTooLarge: return rejected('observe_information', OwnerFailure('INVALID_INPUT', 'query', 'LIMIT_EXCEEDED'))
                    except (InvalidValue,InvalidData,ValueError): return rejected('observe_information', OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE'))
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

    async def _semantic_view(self,after:str,now:int) -> Record:
        from companion_memory.retrieval.semantic_schema import EMBED_STATES,LOCAL_STATES
        from companion_memory.persistence.semantic_records import string,number
        owner=self.semantic;cache=self.cache;assert owner is not None and cache is not None
        if len(after)>128:raise InvalidValue()
        control=await owner.rows.read('semantic_control',owner.control_id)
        publication=await owner.memory.rows.read('semantic_publication',owner.memory.root_id)
        if control is None or publication is None:raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
        coverage=await owner.memory.information.coverage_view()
        floor=(await owner.memory.rows.statements.read('semantic_floor',{'space_id':owner.space}))[0]
        counts={state:0 for state in (*EMBED_STATES,*LOCAL_STATES)};pending=False
        for row in await owner.rows.statements.read('semantic_work_counts',{}):
            counts[string(row['state'])]=number(row['count']);pending|=bool(row['cleanup_pending'])
        caches={'active':0,'expired':0,'hits':cache.hits,'misses':cache.misses}
        for row in await owner.rows.statements.read('semantic_cache_counts',{}):caches[string(row['state']).lower()]=number(row['count'])
        first=await owner.rows.page('embedding_work',after,8)
        second=await owner.rows.page('embedding_work',string(first[-1]['row_id']),8) if len(first)==8 else ()
        items=tuple(MappingProxyType({'work_id':row['work_id'],'kind':row['kind'],'state':row['state'],
            'request_ref':row.get('request_ref'),'error':row['error'],'cleanup_pending':bool(row['cleanup_pending']) or
                row['kind']=='EMBED' and owner.provider.pending_for(string(row['work_id']))}) for row in (*first,*second))
        pending|=owner.provider.cleanup_pending
        return MappingProxyType({'v':1,'space_id':owner.space,'generation_id':control['current_generation'],'captured_seq':coverage['captured_seq'],
            'material_seq':publication['material_seq'],'published_seq':publication['published_seq'],'first_uncovered_seq':floor['minimum'],'pending_count':floor['count'],
            'work_counts':MappingProxyType(counts),'cache_counts':MappingProxyType(caches),'pause_reason':control['pause_reason'],
            'cleanup_pending':pending,'items':items,'observed_at':now})
