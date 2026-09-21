"""Controlled native host operations with closed offset-bearing wire inputs.

Only trusted setup chooses rights. This adapter normalizes each operation's
exact input and keeps lookup, branch selection and command work under one
absolute deadline; the data owners still validate and commit their own facts.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, replace
from types import MappingProxyType
import time
from companion_memory.persistence import Field, RecordSchema, Committed, Found, NotFound
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.state.time_values import parse_reported_time
from companion_memory.retrieval.query_service import QueryPort, QueryService, QueryResult
from companion_memory.retrieval.tickets import CONSUME
from companion_memory.retrieval.delivery import deliver
from .management import HostIdentity, ManagementPort, ManagementAssembly, SCHEMAS
from companion_memory.persistence.record_primitives import ID, TEXT, Record, checked, record, text
from .errors import InformationResult, information_result, InformationError, InformationRejected, InformationNotCommitted, InformationUnconfirmed, rejected

QUERY_OPERATIONS = frozenset(('search_memory', 'deep_recall', 'prepare_reply', 'resolve_recall'))
WRITE_KINDS = {'set_state': 'state_set', 'update_state': 'state_update', 'end_activity': 'state_end',
    'inject_goal': 'goal_inject_external', 'update_goal_status': 'goal_status', 'change_deadline': 'goal_deadline'}


def wire_schema(schema: RecordSchema) -> RecordSchema:
    fields = []
    for field in schema.fields:
        value = TEXT(64) if field.name in ('started_at', 'reported_at', 'deadline', 'used_at') else wire_schema(field.schema) if type(field.schema) is RecordSchema else field.schema
        fields.append(replace(field, schema=value))
    return RecordSchema(tuple(fields))


def normalize_wire(value: Record) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if key in ('started_at', 'reported_at', 'deadline', 'used_at'):
            normalized[key] = parse_reported_time(item).utc_us if item is not None else None
        elif type(item) is MappingProxyType: normalized[key] = normalize_wire(item)
        else: normalized[key] = item
    if 'reported_at' in value:
        offset = parse_reported_time(value['reported_at']).offset_minutes
        if value.get('reported_offset_minutes') != offset:
            raise OwnerFailure('INVALID_INPUT', 'time', 'INVALID_TIME')
    return normalized


@dataclass(frozen=True, slots=True)
class BusinessPending:
    operation_key: str
    confirmation: str
    error: InformationError
    status: str = 'UNCONFIRMED'


type BusinessResult = InformationResult | BusinessPending


@dataclass(frozen=True, slots=True, init=False)
class InformationPort:
    """Opaque trusted host handle; submitted IDs never add operations or rights."""
    _service: BusinessService
    _management: ManagementPort
    _query: QueryPort
    _operations: frozenset[str]

    def __init__(self): raise TypeError('Information handles require trusted host binding.')

    async def search_memory(self, payload: object) -> QueryResult: return await self._query.search_memory(payload)
    async def deep_recall(self, payload: object) -> QueryResult: return await self._query.deep_recall(payload)
    async def prepare_reply(self, payload: object) -> QueryResult: return await self._query.prepare_reply(payload)
    async def resolve_recall(self, payload: object, *, prepared: bool = False, deep: bool = False) -> QueryResult:
        return await self._query.resolve_recall(payload, prepared=prepared, deep=deep)
    async def record_usage(self, payload: object) -> BusinessResult: return await self._service.call(self, 'record_usage', payload)
    async def set_state(self, payload: object) -> BusinessResult: return await self._service.call(self, 'set_state', payload)
    async def update_state(self, payload: object) -> BusinessResult: return await self._service.call(self, 'update_state', payload)
    async def end_activity(self, payload: object) -> BusinessResult: return await self._service.call(self, 'end_activity', payload)
    async def inject_goal(self, payload: object) -> BusinessResult: return await self._service.call(self, 'inject_goal', payload)
    async def update_goal_status(self, payload: object) -> BusinessResult: return await self._service.call(self, 'update_goal_status', payload)
    async def change_deadline(self, payload: object) -> BusinessResult: return await self._service.call(self, 'change_deadline', payload)
    async def resolve_usage(self, payload: object) -> BusinessResult: return await self._service.call(self, 'record_usage', payload, confirm_only=True)
    async def get_state_view(self) -> InformationResult: return await self._service.read(self, 'get_state_view')
    async def list_open_goals(self, cursor: str | None = None) -> InformationResult: return await self._service.read(self, 'list_open_goals', cursor=cursor)
    async def resolve_operation(self, operation: str, payload: object) -> BusinessResult:
        if type(operation) is not str or operation not in WRITE_KINDS:
            return rejected('resolve_management', OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE'))
        return await self._service.call(self, operation, payload, confirm_only=True)


class BusinessService:
    def __init__(self, management: ManagementAssembly, queries: QueryService):
        self.management, self.queries = management, queries
        self.ports: dict[int, InformationPort] = {}
        self.jobs: set[asyncio.Task[object]] = set()

    def bind(self, identity: HostIdentity, *, include_forgotten: bool = False, object_ids: tuple[str, ...] | None = None) -> InformationPort:
        allowed = QUERY_OPERATIONS | frozenset(WRITE_KINDS) | {'record_usage', 'get_state_view', 'list_open_goals'}
        if type(identity) is not HostIdentity or not identity.operations <= allowed or len(self.ports) >= 16:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        kinds = {WRITE_KINDS[operation] for operation in identity.operations if operation in WRITE_KINDS}
        kinds.add('ticket_issue_normal')
        if include_forgotten: kinds.add('ticket_issue_deep')
        if 'record_usage' in identity.operations: kinds.update(('usage_consume', 'usage_change', 'usage_restore'))
        management = self.management.issue(replace(identity, operations=frozenset(kinds)))
        bound = False
        query = None
        try:
            query = self.queries.bind(replace(identity, operations=identity.operations & QUERY_OPERATIONS), include_forgotten=include_forgotten,
                object_ids=object_ids, management=management)
            port = object.__new__(InformationPort)
            for name, value in (('_service', self), ('_management', management), ('_query', query), ('_operations', identity.operations)):
                object.__setattr__(port, name, value)
            self.ports[id(port)] = port
            bound = True
            return port
        finally:
            if not bound:
                if query is not None: self.queries.revoke(query)
                self.management.revoke(management)

    def revoke(self, port: InformationPort) -> None:
        with self.queries.runtime.gate.lock:
            if self.ports.pop(id(port), None) is port:
                self.queries.revoke(port._query)
                self.management.revoke(port._management)

    def authorize(self, port: InformationPort, operation: str) -> None:
        if type(port) is not InformationPort or self.ports.get(id(port)) is not port or operation not in port._operations:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        kind = 'usage_consume' if operation == 'record_usage' else WRITE_KINDS.get(operation)
        if kind is None: raise OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE')
        self.management.verify_confirmation_authority(port._management, kind)

    async def call(self, port: InformationPort, operation: str, payload: object, *, confirm_only: bool = False) -> BusinessResult:
        deadline = bounded_deadline(time.monotonic(), 5)
        try:
            self.authorize(port, operation)
            if self.jobs: raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL', True)
            kind = 'usage_consume' if operation == 'record_usage' else WRITE_KINDS[operation]
            schema = wire_schema(CONSUME if operation == 'record_usage' else SCHEMAS[kind])
            value = checked(RecordSchema((Field('operation_key', ID), *schema.fields)), payload, 4096)
            key = text(value['operation_key']); body = dict(value); body.pop('operation_key')
            native = normalize_wire(MappingProxyType(body))
            # Eliminate shared mutable input and restore only native containers.
            native = decode_content(encode_content(checked(SCHEMAS[kind], native, 4096), 4096), 4096)
            async def run() -> object:
                with DeadlineScope(deadline):
                    try:
                        if operation == 'record_usage':
                            for branch in ('usage_consume', 'usage_change', 'usage_restore'):
                                pending = await self.management.resolve_retained(port._management, branch, key, expected_payload=native)
                                if pending is not None:
                                    return pending
                            for branch in ('usage_consume', 'usage_change', 'usage_restore'):
                                original = await port._management.resolve(branch, key, native)
                                if type(original) is not NotFound: return original
                            existing = await self.management.confirm_usage(port._management, native)
                            if existing is not None: return existing
                            if confirm_only: return NotFound()
                            selected = await self.management.select_usage_branch(port._management, native, int(time.time() * 1000000))
                        else: selected = kind
                        self.authorize(port, operation)
                        return await (port._management.resolve(selected, key, native) if confirm_only else port._management.execute(selected, key, native))
                    except OwnerFailure as failure: return rejected(operation, failure)
            task, outcome = start_owned(run()); self.jobs.add(task); self.queries.runtime.retain_external_work(task)
            def ended(job: asyncio.Task[object]) -> None:
                if not job.cancelled(): job.exception()
                self.jobs.discard(job)
            task.add_done_callback(ended)
            done, _ = await asyncio.wait((outcome,), timeout=max(0, deadline - time.monotonic()))
            if not done: return BusinessPending(key, operation, InformationError('TIMEOUT', operation, 'storage', 'DEADLINE_EXCEEDED', True))
            result = outcome.result()
            if isinstance(result, (InformationRejected, InformationNotCommitted, InformationUnconfirmed)):
                return replace(result, error=replace(result.error, operation=operation))
            return information_result(result, operation)
        except OwnerFailure as failure: return rejected(operation, failure)
        except ValueTooLarge: return rejected(operation, OwnerFailure('INVALID_INPUT', 'input', 'LIMIT_EXCEEDED'))
        except InvalidValue: return rejected(operation, OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE'))

    async def read(self, port: InformationPort, operation: str, *, cursor: str | None = None) -> InformationResult:
        deadline = bounded_deadline(time.monotonic(), 1)
        try:
            def authorize() -> None:
                if self.ports.get(id(port)) is not port or operation not in port._operations:
                    raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
                self.management.verify_confirmation_authority(port._management, 'ticket_issue_normal')
            authorize()
            if self.jobs: raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL', True)
            async def inspect() -> object:
                with DeadlineScope(deadline):
                    try:
                        checkpoint = self.queries.check_mode()
                        now = int(time.time() * 1000000)
                        view = await self.queries.state.view(now) if operation == 'get_state_view' else await self.queries.goals.list_page(now, cursor)
                        if view is None: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                        return Found(deliver(self.queries.runtime.gate, checkpoint, view, deadline, authorize, self.queries.check_mode))
                    except OwnerFailure as failure: return rejected(operation, failure)
                    except ValueTooLarge: return rejected(operation, OwnerFailure('INVALID_INPUT', 'input', 'LIMIT_EXCEEDED'))
                    except InvalidValue: return rejected(operation, OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE'))
            task, outcome = start_owned(inspect()); self.jobs.add(task); self.queries.runtime.retain_external_work(task)
            def ended(job: asyncio.Task[object]) -> None:
                if not job.cancelled(): job.exception()
                self.jobs.discard(job)
            task.add_done_callback(ended)
            done, _ = await asyncio.wait((outcome,), timeout=max(0, deadline - time.monotonic()))
            if not done: return rejected(operation, OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED', True))
            return information_result(outcome.result(), operation)
        except OwnerFailure as failure: return rejected(operation, failure)
