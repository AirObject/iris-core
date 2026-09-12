"""Bounded host queries, current-entry reply partitions and confirmed delivery.

Local matching never calls a model. Ticket commitment precedes publication of
immutable current memory; a mode, authorization or object mutation fence can
still discard an encoded response before its first delivery. Old keys return
confirmation metadata only, including when ticket payload has been disposed.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, replace
from hashlib import sha256
import secrets
import time
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.information_persistence import StoredInformationConfiguration
from companion_memory.persistence import Found, Committed, NotFound, Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.content_service import ContentRuntimeService
from companion_memory.state.service import StateOwner
from companion_memory.goals.service import GoalsService
from companion_memory.information.records import Record, integer, text, record
from companion_memory.information.management import ManagementAssembly, ManagementPort, HostIdentity
from companion_memory.information.errors import InformationResult, information_result, InformationError, InformationNotCommitted, RecallCommitted, InformationUnconfirmed, rejected
from .index import LocalIndex
from .tickets import RecallAuthority
from .query_formats import isolate_query
from .delivery import deliver
from .lexical import normalize_material, object_text, matches_structure, has_lexical_match, rank_key, StructuralFilter
from companion_memory.information.payload_limits import bounded_items, bounded_single


@dataclass(frozen=True, slots=True)
class TestPersona:
    """Explicit isolated test participant; never represents a generated persona."""
    text: str
    revision: int
    generated_at: int

    def __post_init__(self):
        if (type(self.text) is not str or len(self.text.encode()) > 8192 or type(self.revision) is not int or not 1 <= self.revision < 2**63
                or type(self.generated_at) is not int or not -(2**62) <= self.generated_at < 2**62):
            raise ValueError('A bounded explicit synthetic persona is required.')


@dataclass(frozen=True, slots=True)
class RecallPending:
    request_key: str
    intent_digest: str
    error: InformationError
    status: str = 'UNCONFIRMED'


type QueryResult = InformationResult | RecallPending


@dataclass(frozen=True, slots=True, init=False)
class QueryPort:
    """Trusted identity plus explicit public operations; no arbitrary owner access."""
    _service: QueryService
    _management: ManagementPort
    _authority: RecallAuthority
    _operations: frozenset[str]
    _entry_id: str
    _owns_management: bool

    def __init__(self): raise TypeError('Query ports are issued by trusted host setup.')

    async def search_memory(self, payload: object) -> QueryResult:
        return await self._service.call(self, 'search_memory', payload)

    async def deep_recall(self, payload: object) -> QueryResult:
        return await self._service.call(self, 'deep_recall', payload)

    async def prepare_reply(self, payload: object) -> QueryResult:
        return await self._service.call(self, 'prepare_reply', payload)

    async def resolve_recall(self, payload: object, *, prepared: bool = False, deep: bool = False) -> QueryResult:
        return await self._service.call(self, 'resolve_recall', payload, prepared=prepared, deep=deep)


class QueryService:
    """Two admitted queries, zero waiting queue, and one inherited I/O deadline."""
    def __init__(self, configuration: StoredInformationConfiguration, runtime: ContentRuntimeService, management: ManagementAssembly,
                 index: LocalIndex, state: StateOwner, goals: GoalsService, *, test_persona: TestPersona | None = None):
        if test_persona is not None and type(test_persona) is not TestPersona:
            raise ValueError('Only explicitly identified test persona material is supported.')
        self.configuration, self.runtime, self.management, self.index, self.state, self.goals = configuration, runtime, management, index, state, goals
        self.test_persona = test_persona
        self._ports: dict[int, QueryPort] = {}
        self.jobs: set[asyncio.Task[object]] = set()

    def bind(self, identity: HostIdentity, *, include_forgotten: bool = False, object_ids: tuple[str, ...] | None = None, management: ManagementPort | None = None) -> QueryPort:
        allowed = frozenset(('search_memory', 'deep_recall', 'prepare_reply', 'resolve_recall'))
        if type(identity) is not HostIdentity or not identity.operations.issubset(allowed) or len(self._ports) >= 16:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        if 'deep_recall' in identity.operations and not include_forgotten:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        kinds = frozenset(('ticket_issue_normal', 'ticket_issue_deep') if include_forgotten else ('ticket_issue_normal',))
        owns_management = management is None
        if management is None: management = self.management.issue(replace(identity, operations=kinds))
        read = None
        authority = None
        bound = False
        try:
            for kind in kinds: self.management.verify_confirmation_authority(management, kind)
            read = self.runtime.memory.bind_query_scope(include_forgotten=include_forgotten, object_ids=object_ids)
            authority = RecallAuthority(management.binding_id, identity.host_id, identity.entry_id, self.runtime.memory, read)
            self.management.bind_recall_authority(management, authority)
            port = object.__new__(QueryPort)
            for name, value in (('_service', self), ('_management', management), ('_authority', authority), ('_operations', identity.operations), ('_entry_id', identity.entry_id), ('_owns_management', owns_management)):
                object.__setattr__(port, name, value)
            self._ports[id(port)] = port
            bound = True
            return port
        finally:
            if not bound:
                if authority is not None: self.management.release_recall_authority(management, authority)
                if read is not None: self.runtime.memory.release_query_scope(read)
                if owns_management: self.management.revoke(management)

    def revoke(self, port: QueryPort) -> None:
        with self.runtime.gate.lock:
            if self._ports.get(id(port)) is port:
                self._ports.pop(id(port))
                self.management.release_recall_authority(port._management, port._authority)
                self.runtime.memory.release_query_scope(port._authority.memory_port)
                if port._owns_management: self.management.revoke(port._management)

    def _authorize(self, port: QueryPort, operation: str) -> None:
        if type(port) is not QueryPort or self._ports.get(id(port)) is not port:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        if operation not in port._operations:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        self.management.verify_confirmation_authority(port._management, 'ticket_issue_normal')

    def check_mode(self) -> tuple[int, int]:
        gate = self.runtime.gate
        checkpoint = gate.information_checkpoint()
        if checkpoint is not None: return checkpoint
        if gate.state in ('DREAM_PREPARING', 'DREAM_FOCUSED'):
            raise OwnerFailure('MODE_BLOCKED', 'state', 'DREAMING')
        if gate.state in ('CLOSED', 'CLOSING'):
            raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')
        if gate.state in ('NORMAL', 'DRAINING'):
            raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL')
        raise OwnerFailure('MODE_BLOCKED', 'state', 'RUNTIME_FAULTED' if gate.state == 'FAULTED' else 'RECOVERING')

    async def call(self, port: QueryPort, operation: str, payload: object, *, prepared: bool = False, deep: bool = False) -> QueryResult:
        started = time.monotonic()
        deadline = bounded_deadline(started, 1)
        try:
            self._authorize(port, operation)
            prepare = operation == 'prepare_reply' or operation == 'resolve_recall' and prepared
            query, selected = isolate_query(payload, prepare)
            if query['entry_id'] != port._entry_id:
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
            mode = 'DEEP' if operation == 'deep_recall' or operation == 'resolve_recall' and deep else 'NORMAL'
            if mode == 'DEEP' and 'deep_recall' not in port._operations:
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
            intent = sha256(encode_content(MappingProxyType({'query': query, 'mode': mode, 'prepare': prepare}), 8192)).hexdigest()
            if len(self.jobs) >= 2:
                raise OwnerFailure('RESOURCE_BUSY', 'query', 'ADMISSION_FULL', True)
            attempted_ticket = False
            async def run() -> object:
                nonlocal attempted_ticket
                with DeadlineScope(deadline):
                    try:
                        kind = 'ticket_issue_deep' if mode == 'DEEP' else 'ticket_issue_normal'
                        confirmation = await self.management.resolve_retained(port._management, kind, text(query['request_key']), ticket_intent=intent)
                        if confirmation is not None:
                            return confirmation
                        original = await self.management.tickets.original(port._authority, text(query['request_key']), intent)
                        if original is not None:
                            key = self.management.operation_key(port._management, kind, text(query['request_key']))
                            receipt = await self.management.operations[kind].read_receipt(key)
                            if type(receipt) is not Found: raise OwnerFailure('STORAGE_FAILED', 'ticket', 'INTEGRITY_FAILURE')
                            return Found(MappingProxyType({'availability': 'CONFIRMED_ONLY', 'recall_id': original['recall_id'], 'request_key': query['request_key'],
                                'commit_id': receipt.value.commit_id, 'payload_available': 'expires_at_us' in original,
                                'member_count': original['member_count'], 'intent_digest': intent}))
                        if operation == 'resolve_recall': return NotFound()
                        checkpoint = self.check_mode()
                        if prepare and self.test_persona is None and (query['require_complete'] or not query['allow_partial']):
                            raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'state', 'PUBLICATION_MISSING')
                        for attempt in range(2):
                            objects, coverage, reasons = await self._candidates(port, query, selected, mode == 'DEEP')
                            sections, section_reasons, omitted_memories = await self._sections(port, query, prepare, objects)
                            objects = tuple(record(value) for value in cast(tuple[Value, ...], sections['memories']))
                            reasons = tuple(dict.fromkeys((*reasons, *section_reasons)))
                            if reasons and (query['require_complete'] or not query['allow_partial']):
                                if 'SECTION_LIMIT' in section_reasons:
                                    raise OwnerFailure('INVALID_INPUT', 'query', 'LIMIT_EXCEEDED')
                                raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'index', 'INDEX_NOT_READY')
                            self._authorize(port, operation)
                            if self.runtime.gate.information_checkpoint() != checkpoint:
                                self.check_mode()
                                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                            now = int(time.time() * 1000000)
                            recall_id = 'recall:' + secrets.token_hex(24) if objects else None
                            binding = MappingProxyType({'database_id': self.configuration.database_id, 'instance_id': self.index.instance_id,
                                'principal_binding_id': port._authority.principal_binding_id, 'host_id': port._authority.host_id,
                                'entry_id': port._entry_id, 'request_key': query['request_key'], 'config_snapshot_id': self.configuration.snapshot_id})
                            digest = sha256(encode_content(MappingProxyType({'binding': binding, 'sections': sections}), 131072)).hexdigest()
                            response = MappingProxyType({'response_version': 1, 'request_id': query['request_key'], 'recall_id': recall_id,
                                'availability': 'DEGRADED' if reasons else 'COMPLETE', 'observed_at': now, 'mode_epoch': checkpoint[0],
                                'config_snapshot_id': self.configuration.snapshot_id, 'retrieval_mode': 'LOCAL_LEXICAL_V1',
                                'sections': sections, 'coverage': coverage, 'truncation': MappingProxyType({'reasons': reasons, 'omitted_memories': omitted_memories}),
                                'capabilities': MappingProxyType({'generative_query': False, 'embedding': False, 'rerank': False,
                                    'semantic_equivalence': False, 'real_persona': False, 'persona_origin': 'SYNTHETIC' if self.test_persona else 'UNAVAILABLE'})})
                            encode_content(response, 131072)
                            if objects:
                                root = MappingProxyType(dict(binding) | {'recall_id': recall_id, 'version': 1, 'query_mode': mode,
                                    'issued_at_us': now, 'expires_at_us': now + 86400000000, 'clock_observation': now,
                                    'response_digest': digest, 'intent_digest': intent, 'member_count': len(objects)})
                                members = tuple(MappingProxyType({'recall_id': recall_id, 'object_id': obj['object_id'],
                                    'returned_revision': obj['revision'], 'returned_lifecycle': obj['lifecycle']})
                                    for obj in sorted(objects, key=lambda item: text(item['object_id'])))
                                attempted_ticket = True
                                kind = 'ticket_issue_deep' if mode == 'DEEP' else 'ticket_issue_normal'
                                result = await port._management.execute(kind, text(query['request_key']), {'ticket': root, 'members': members})
                                if type(result) is InformationNotCommitted and result.error.reason == 'REVISION_CONFLICT' and attempt == 0:
                                    checkpoint = self.check_mode(); continue
                                if type(result) is InformationUnconfirmed: return result
                                if type(result) is not RecallCommitted: return result
                                actual = {text(obj['object_id']): obj for obj in result.objects}
                                if any(actual.get(text(obj['object_id'])) != obj for obj in objects):
                                    raise OwnerFailure('STORAGE_FAILED', 'ticket', 'INTEGRITY_FAILURE')
                                sections = MappingProxyType(dict(sections) | {'memories': tuple(actual[text(obj['object_id'])] for obj in objects)})
                                response = MappingProxyType(dict(response) | {'sections': sections})
                            response = MappingProxyType(dict(response) | {'timing': MappingProxyType({'base_ms': int((time.monotonic() - started) * 1000), 'budget_ms': 1000})})
                            return Found(deliver(self.runtime.gate, checkpoint, response, deadline,
                                lambda: self._authorize(port, operation), self.check_mode))
                        raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                    except OwnerFailure as failure: return rejected(operation, failure)
                    except ValueTooLarge: return rejected(operation, OwnerFailure('INVALID_INPUT', 'query', 'LIMIT_EXCEEDED'))
                    except (InvalidValue, UnicodeError): return rejected(operation, OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE'))
            reader_token = secrets.token_hex(16)
            self.index.retain_query(reader_token)
            task, outcome = start_owned(run()); self.jobs.add(task); self.runtime.retain_external_work(task)
            def ended(job: asyncio.Task[object]) -> None:
                if not job.cancelled(): job.exception()
                self.jobs.discard(job)
                self.index.release_query(reader_token)
            task.add_done_callback(ended)
            done, _ = await asyncio.wait((outcome,), timeout=max(0, deadline - time.monotonic()))
            if not done:
                error = InformationError('TIMEOUT', operation, 'query', 'DEADLINE_EXCEEDED', True)
                if attempted_ticket: return RecallPending(text(query['request_key']), intent, error)
                return rejected(operation, OwnerFailure(error.code, error.field, error.reason, True))
            return information_result(outcome.result(), operation)
        except OwnerFailure as failure: return rejected(operation, failure)
        except ValueTooLarge: return rejected(operation, OwnerFailure('INVALID_INPUT', 'query', 'LIMIT_EXCEEDED'))
        except InvalidValue: return rejected(operation, OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE'))

    async def _candidates(self, port: QueryPort, query: Record, selected: StructuralFilter, deep: bool) -> tuple[tuple[Record, ...], Record, tuple[str, ...]]:
        material = normalize_material(text(query['query_text']), byte_limit=8192, term_limit=4096)
        coordinator, generation = await self.index.query_generation()
        ids: dict[str, None] = {}; reasons: list[str] = []; visits = 0
        def add(oid: str) -> None:
            if self.runtime.memory.query_allowed(port._authority.memory_port, oid, deep=deep):
                if len(ids) < 128: ids[oid] = None
                elif oid not in ids and 'CANDIDATE_LIMIT' not in reasons: reasons.append('CANDIDATE_LIMIT')
        for oid in selected.object_ids:
            if not self.runtime.memory.query_allowed(port._authority.memory_port, oid, deep=deep):
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
            add(oid)
        if selected.specified and not selected.object_ids:
            after = ''
            parameters: dict[str, Value] = {'subjects': encode_content(selected.subject_ids, 2048).decode(), 'category': selected.category,
                'world_kind': selected.world_scope.kind if selected.world_scope else None, 'world_context': selected.world_scope.context_id if selected.world_scope else None,
                'start_us': selected.start_us, 'end_us': selected.end_us, 'after': after}
            for _ in range(8):
                rows = await self.index.memory.structural_page(parameters | {'after': after})
                for row in rows: add(text(row['object_id'])); after = text(row['object_id'])
                if len(rows) < 16: break
            else: reasons.append('CANDIDATE_LIMIT')
        gaps = await self.index.memory.gaps(128)
        for gap in gaps:
            if gap['action'] == 'UPSERT': add(text(gap['object_id']))
        if len(gaps) == 128: reasons.append('INDEX_LAG_PARTIAL')
        if generation is None:
            reasons.append('INDEX_BUILDING')
            # Before a first publication, a build acknowledgement alone cannot
            # make current objects disappear from the bounded degraded path.
            after = ''
            for _ in range(16):
                rows = await self.index.memory.current_page(after, 8)
                for row in rows:
                    after = text(row['object_id']); add(after)
                if len(rows) < 8: break
            else: reasons.append('CANDIDATE_LIMIT')
        elif material.terms:
            candidates, visits, exhausted = await self.index.posting_candidates(text(generation['generation_id']), material.terms)
            for oid in candidates: add(oid)
            if exhausted: reasons.append('CANDIDATE_LIMIT')
        objects = []
        for oid in ids:
            result = await (port._authority.memory_port.get_for_deep_read(oid) if deep else port._authority.memory_port.get_current(oid))
            if type(result) is NotFound: continue
            if type(result) is not Found:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'READ_FAILED', bool(getattr(result, 'cleanup_pending', False)))
            current = record(result.value)
            if not matches_structure(current, selected): continue
            try: normalized = normalize_material(object_text(current), byte_limit=8192, term_limit=4096)
            except OwnerFailure as failure:
                if failure.reason != 'LIMIT_EXCEEDED': raise
                reasons.append('INDEX_FORMAT_LIMIT'); continue
            if material.terms and not has_lexical_match(normalized, material): continue
            objects.append((rank_key(oid, normalized, material, selected), current))
        objects.sort(key=lambda item: item[0])
        coverage = await self.index.memory.coverage_view()
        covered = MappingProxyType(dict(coverage) | {'generation': generation['generation_id'] if generation else None,
            'preprocess_id': 'LOCAL_LEXICAL_V1', 'unicode_version': material.unicode_version, 'posting_visits': visits,
            'candidate_count': len(ids), 'observed_at': int(time.time() * 1000000)})
        projections = tuple([await self.runtime.memory.query_projection(port._authority.memory_port, obj, deep=deep) for _, obj in objects[:8]])
        return projections, covered, tuple(dict.fromkeys(reasons))

    async def _sections(self, port: QueryPort, query: Record, prepare: bool, objects: tuple[Record, ...]) -> tuple[Record, tuple[str, ...], int]:
        now = int(time.time() * 1000000); reasons: list[str] = []
        settings = self.configuration.candidate.information.record('retrieval.reply')
        objects, omitted_memories = bounded_items(objects, integer(settings['memory_limit']) * (4096 + 1024))
        if omitted_memories: reasons.append('SECTION_LIMIT')
        sections: dict[str, Value] = {'memories': objects}
        if query['include_state']:
            state = await self.state.view(now)
            sections['current_state'], limited = bounded_single(MappingProxyType({'availability': 'ABSENT' if state is None or state['activity'] is None else 'AVAILABLE', 'view': state, 'observed_at': now}), integer(settings['state_max_bytes']), now)
            if limited: reasons.append('SECTION_LIMIT')
        if query['include_goals']:
            goal_page = await self.goals.list_page(now)
            sections['goals'] = goal_page
            if goal_page['has_more']: reasons.append('SECTION_LIMIT')
        if prepare:
            persona = self.test_persona
            sections['persona'], limited = bounded_single(MappingProxyType({'availability': 'AVAILABLE' if persona else 'UNAVAILABLE', 'text': persona.text if persona else None,
                'revision': persona.revision if persona else None, 'generated_at': persona.generated_at if persona else None,
                'review_status': 'TEST_ONLY' if persona else 'PUBLICATION_MISSING', 'origin': 'SYNTHETIC' if persona else 'UNAVAILABLE'}), integer(settings['persona_max_bytes']), now)
            if limited: reasons.append('SECTION_LIMIT')
            if persona is None: reasons.append('PUBLICATION_MISSING')
            platform_id = await self.runtime.assembly.ingress.reply_platform(port._entry_id, port._authority.host_id)
            platform = self.configuration.content_view().candidate.platform(platform_id)
            refs = await self.runtime.assembly.buffers.reply_references(port._entry_id, platform.count('recent_context_count'), platform.count('target_count'))
            members = refs['members']; recent: list[Record] = []
            if type(members) is not tuple: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            for raw in members:
                ref = record(raw)
                event = await self.runtime.assembly.ingress.reply_event(port._entry_id, text(ref['message_id']))
                recent.append(MappingProxyType(dict(event) | {'role': ref['role'], 'state': 'FROZEN' if ref['frozen'] else 'PENDING'}))
            recent_metadata: dict[str, Value] = {'scope': 'CURRENT_ENTRY_ONLY', 'revision': refs['revision'],
                'has_more': False, 'omitted_count': 2**63 - 1, 'observed_at': now}
            included, omitted = bounded_items(tuple(reversed(recent)), integer(settings['recent_limit']) * (2048 + 512), recent_metadata)
            sections['recent_context'] = MappingProxyType(recent_metadata | {'items': tuple(reversed(included)),
                'has_more': bool(refs['has_more']) or bool(omitted), 'omitted_count': integer(refs['omitted_count']) + omitted})
            if refs['has_more'] or omitted: reasons.append('SECTION_LIMIT')
            pending = await self.runtime.assembly.buffers.other_pending(port._entry_id)
            times = await self.runtime.assembly.ingress.other_pending_times(port._entry_id)
            if pending['messages'] != times['messages']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            sections['other_pending'] = MappingProxyType({'entry_count': pending['entries'], 'earliest_received_at': times['earliest'],
                'latest_received_at': times['latest'], 'observed_at': now, 'coverage': 'PERSISTED_ACCEPTANCE_ONLY'})
            sections['runtime'] = MappingProxyType({'mode': self.runtime.gate.state, 'mode_epoch': self.runtime.gate.epoch, 'observed_at': now,
                'entry_terminals': await self.runtime.reply_entry_status(port._entry_id, port._authority.host_id),
                'storage_execution': 'ACTUAL', 'model_adapter': 'SIMULATED', 'candidate_origin': 'SYNTHETIC'})
        return MappingProxyType(sections), tuple(reasons), omitted_memories
