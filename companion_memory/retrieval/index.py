"""Transactional index generations, pages and bounded physical object cleanup.

All text is checked against the memory owner's current object in the same UoW.
A page checkpoint advances only after real object material and memory coverage
are recorded; stale workers cannot clear another generation's coverage gap.
"""
from dataclasses import dataclass
from types import MappingProxyType
from hashlib import sha256
from threading import RLock
from companion_memory.persistence import UnitOfWork, Value, OperationPort
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.record_primitives import Record, checked, record, text, integer, identity, fact
from companion_memory.memory.information_tracking import MemoryInformation
from .initialization import RetrievalOwner
from .index_inputs import SCHEMAS
from .lexical import normalize_material, object_text, PREPROCESS_ID, UNICODE_VERSION


@dataclass(frozen=True, slots=True)
class IndexEffect:
    targets: tuple[Record, ...]
    retrieval: Record
    memory: Record | None = None


class IndexTransaction:
    """Owner-local staged writes and their exact row count."""
    def __init__(self, owner: 'LocalIndex', uow: UnitOfWork, now: int):
        self.owner, self.uow, self.now = owner, uow, now
        self.count = 0

    def get(self, name: str, keys: dict[str, Value]) -> Record:
        value = self.owner._records.get(name, self.uow, keys)
        if value is None: raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        return value

    def write(self, name: str, value: dict[str, Value] | Record, before: Record | None = None, projections: dict[str, Value] | None = None) -> Record:
        result = self.owner._records.write(name, self.uow, value, expected_revision=integer(before['revision']) if before else None, projections=projections)
        self.count += 1
        return result

    def update(self, name: str, old: Record, **changes: Value) -> Record:
        value = dict(old); value.update(changes); value['revision'] = integer(old['revision']) + 1
        return self.write(name, value, old)

    def remove(self, name: str, keys: dict[str, Value]) -> None:
        if self.owner._records.remove(name, self.uow, keys) is not None: self.count += 1

    def remove_object(self, generation_id: Value, object_id: Value) -> None:
        self.count += len(self.owner._records.rows.stage('remove_object_postings', self.uow, {'generation_id': generation_id, 'object_id': object_id}))
        self.remove('index_object', {'generation_id': generation_id, 'object_id': object_id})


class LocalIndex(RetrievalOwner):
    """One retrieval owner shared by indexing and the durable ticket service."""
    async def recover(self, expected: Record) -> None:
        from .recovery import verify_retrieval
        await super().recover(expected)
        self.ready = False
        await verify_retrieval(self, self.memory)
        from .publication import verify_publications
        if self._publication_operation is None:
            raise OwnerFailure('INVALID_STATE', 'index', 'NOT_READY')
        await verify_publications(self, self._publication_operation)

    def bind_memory(self, memory: MemoryInformation) -> None:
        if type(memory) is not MemoryInformation or memory.configuration.snapshot_id != self.configuration.snapshot_id:
            raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        self.memory = memory
        self._publication_operation: OperationPort | None = None
        self._readers: dict[str, int] = {}
        self._workers: dict[str, str] = {}
        self._reader_lock = RLock()

    def bind_publication_operation(self, operation: OperationPort) -> None:
        """Trusted assembly supplies the exact scope-bound publication read port."""
        if type(operation) is not OperationPort or self._publication_operation is not None:
            raise OwnerFailure('ACCESS_DENIED', 'index', 'BINDING_MISMATCH')
        self._publication_operation = operation

    def retain_query(self, token: str) -> None:
        """Pin physical generations until the query's actual I/O is finished."""
        with self._reader_lock:
            if token in self._readers or len(self._readers) >= 2:
                raise OwnerFailure('RESOURCE_BUSY', 'query', 'ADMISSION_FULL')
            self._readers[token] = 1

    def release_query(self, token: str) -> None:
        with self._reader_lock:
            self._readers.pop(token, None)

    def retain_work(self, token: str, generation_id: str) -> None:
        """The retrieval owner enforces one actual worker across all coordinators."""
        with self._reader_lock:
            if self._workers: raise OwnerFailure('RESOURCE_BUSY', 'index', 'ADMISSION_FULL', True)
            self._workers[token] = generation_id

    def release_work(self, token: str) -> None:
        with self._reader_lock: self._workers.pop(token, None)

    def step(self, kind: str, uow: UnitOfWork, payload: object, key: str, now: int, epoch: int) -> IndexEffect:
        if not self.ready or kind not in SCHEMAS:
            raise OwnerFailure('INVALID_STATE', 'index', 'NOT_READY')
        value = checked(SCHEMAS[kind], payload, 24576)
        tx = IndexTransaction(self, uow, now)
        coordinator = self.coordinator(uow)
        memory_fact: Record | None = None
        object_target: Record | None = None
        before: Record | None = None
        if kind == 'index_begin':
            if coordinator['building_generation'] is not None or coordinator['active_generation'] != value['expected_generation']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            generations = self._records.rows.stage('recover_generations', uow, {})
            for row in generations:
                old = self._records.unpack('index_generation', row)
                if old['status'] in ('RETIRING', 'FAILED') and self._records.rows.stage('generation_object_count', uow, {'generation_id': old['generation_id']})[0]['count']:
                    raise OwnerFailure('RESOURCE_BUSY', 'index', 'CAPACITY_REACHED')
            generation_id = identity('index_generation', self.instance_id, key)
            sequence = self.memory.sequence(uow)
            after = tx.write('index_generation', {'generation_id': generation_id, 'config_snapshot_id': self.configuration.snapshot_id,
                'preprocess_id': PREPROCESS_ID, 'unicode_version': UNICODE_VERSION, 'format_version': 1, 'revision': 1,
                'captured_seq': sequence['last_seq'], 'contiguous_seq': 0, 'pending_count': 1, 'status': 'BUILDING', 'scan_cursor': None, 'observed_at': now})
            tx.write('lease', {'work_id': generation_id, 'owner_id': self.instance_id, 'instance_id': self.instance_id, 'operation_key': key,
                'epoch': epoch, 'status': 'RUNNING', 'observed_at': now})
            tx.update('coordinator', coordinator, building_generation=generation_id)
            target_id = generation_id
        elif kind == 'index_claim':
            generation = tx.get('index_generation', {'generation_id': value['generation_id']})
            self._generation_current(generation, coordinator)
            if generation['revision'] != value['expected_revision']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            if self._records.rows.stage('unfinished_pages', uow, {'generation_id': generation['generation_id']}):
                raise OwnerFailure('RESOURCE_BUSY', 'index', 'ADMISSION_FULL')
            if not generation['pending_count'] and not self.memory.index_pending_after(uow, text(generation['generation_id'])):
                raise OwnerFailure('PRECONDITION_FAILED', 'index', 'NO_CHANGE')
            target_id = identity('index_page', generation['generation_id'], key)
            scan_cursor = generation['scan_cursor'] if generation['status'] == 'BUILDING' else None
            if scan_cursor is not None and not self.memory.index_pending_after(uow, text(generation['generation_id']), text(scan_cursor)):
                scan_cursor = None
            after = tx.write('index_page', {'page_id': target_id, 'generation_id': generation['generation_id'], 'operation_key': key,
                'owner_id': value['owner_id'], 'revision': 1, 'cursor': scan_cursor,
                'count': 0, 'status': 'RUNNING', 'updated_at': now})
        elif kind == 'index_apply_object':
            generation = tx.get('index_generation', {'generation_id': value['generation_id']})
            self._generation_current(generation, coordinator)
            before = tx.get('index_page', {'page_id': value['page_id']})
            if (before['generation_id'] != generation['generation_id'] or before['revision'] != value['expected_revision'] or before['status'] != 'RUNNING'
                    or integer(before['count']) >= 16 or before['cursor'] is not None and text(value['object_id']) <= text(before['cursor'])):
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            object_id = text(value['object_id']); current = self.memory.current(uow, object_id)
            if value['action'] == 'UPSERT':
                if current is None or current['revision'] != value['object_revision']:
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                material = normalize_material(object_text(current), byte_limit=8192, term_limit=4096)
                digest = sha256(encode_content(current, 4096)).hexdigest()
                if value['body_digest'] != digest or value['normalized_text'] != material.text:
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                previous_object = self._records.get('index_object', uow, {'generation_id': generation['generation_id'], 'object_id': object_id})
                object_target = MappingProxyType({'object_id': object_id, 'previous_revision': previous_object['revision'] if previous_object else None, 'revision': current['revision']})
                tx.remove_object(generation['generation_id'], object_id)
                tx.write('index_object', {'generation_id': generation['generation_id'], 'object_id': object_id,
                    'revision': current['revision'], 'body_digest': digest, 'term_count': len(material.terms)})
                for ordinal, token in enumerate(material.terms):
                    tx.write('posting', {'token': token, 'object_id': object_id, 'revision': current['revision'], 'ordinal': ordinal}, projections={'generation_id': generation['generation_id']})
            else:
                if current is not None or value['normalized_text'] != '' or value['body_digest'] != '':
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                previous_object = self._records.get('index_object', uow, {'generation_id': generation['generation_id'], 'object_id': object_id})
                object_target = MappingProxyType({'object_id': object_id, 'previous_revision': previous_object['revision'] if previous_object else None, 'revision': value['object_revision']})
                tx.remove_object(generation['generation_id'], object_id)
            targets = tuple(text(coordinator[n]) for n in ('active_generation', 'building_generation') if coordinator[n] is not None)
            ack = self.memory.acknowledge(uow, object_id, integer(value['object_revision']), text(generation['generation_id']), targets, text(value['action']))
            after = tx.update('index_page', before, cursor=object_id, count=integer(before['count']) + 1, updated_at=now)
            target_id = text(after['page_id'])
            memory_fact = fact(object_id, integer(value['object_revision']), integer(value['object_revision']), now,
                changed=integer(ack['changed_count']), from_seq=integer(ack['applied_seq']), to_seq=integer(ack['applied_seq']))
        elif kind == 'index_confirm_page':
            generation = tx.get('index_generation', {'generation_id': value['generation_id']})
            self._generation_current(generation, coordinator)
            before = tx.get('index_page', {'page_id': value['page_id']})
            if before['generation_id'] != generation['generation_id'] or before['revision'] != value['expected_revision'] or before['status'] != 'RUNNING':
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            after = tx.update('index_page', before, status='COMMITTED', updated_at=now)
            coverage = self.memory.coverage(uow)
            # End of scan must be verified through memory, never asserted by a host.
            if value['scan_complete'] and self.memory.index_pending_after(uow, text(generation['generation_id']), text(before['cursor']) if before['cursor'] is not None else ''):
                raise OwnerFailure('PRECONDITION_FAILED', 'index', 'REVISION_CONFLICT')
            missing = self.memory.missing_generation(uow, text(generation['generation_id']))
            pending = max(integer(coverage['pending_count']), missing) + (0 if value['scan_complete'] else 1)
            cursor = generation['scan_cursor']
            if generation['status'] == 'BUILDING' and before['cursor'] is not None:
                cursor = max(text(cursor) if cursor is not None else '', text(before['cursor']))
            contiguous = min(integer(generation['contiguous_seq']), integer(coverage['contiguous_seq'])) if missing else coverage['contiguous_seq']
            tx.update('index_generation', generation, scan_cursor=cursor,
                captured_seq=coverage['captured_seq'], contiguous_seq=contiguous, pending_count=pending, observed_at=now)
            target_id = text(after['page_id'])
        elif kind == 'index_publish':
            before = tx.get('index_generation', {'generation_id': value['generation_id']})
            self._generation_current(before, coordinator)
            if (before['status'] != 'BUILDING' or coordinator['building_generation'] != before['generation_id']
                    or before['revision'] != value['expected_revision'] or before['pending_count']
                    or self._records.rows.stage('unfinished_pages', uow, {'generation_id': before['generation_id']})):
                raise OwnerFailure('PRECONDITION_FAILED', 'index', 'REVISION_CONFLICT')
            coverage = self.memory.coverage(uow)
            if (coverage['pending_count'] or before['contiguous_seq'] != coverage['captured_seq']
                    or before['captured_seq'] != coverage['captured_seq']
                    or self.memory.index_pending_after(uow, text(before['generation_id']))):
                raise OwnerFailure('PRECONDITION_FAILED', 'index', 'REVISION_CONFLICT')
            if self._records.rows.stage('recovery_invalid_relations', uow, {})[0]['count']:
                raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
            memory_fact = self.memory.publish(uow, text(before['generation_id']), integer(coverage['captured_seq']), now)
            if coordinator['active_generation'] is not None:
                old = tx.get('index_generation', {'generation_id': coordinator['active_generation']})
                self._generation_current(old, coordinator)
                tx.update('index_generation', old, status='RETIRING', observed_at=now)
            after = tx.update('index_generation', before, status='ACTIVE', observed_at=now)
            pointer = tx.update('coordinator', coordinator, active_generation=before['generation_id'], building_generation=None)
            object_target = MappingProxyType({'object_id': self.instance_id,
                'previous_revision': coordinator['revision'], 'revision': pointer['revision']})
            lease = tx.get('lease', {'work_id': before['generation_id']})
            ended = dict(lease) | {'status': 'ENDED', 'observed_at': now}
            changed = self._records.rows.stage('end_index_lease', uow, {'work_id': lease['work_id'],
                'expected_body': encode_content(lease, 1024).decode(), 'body': encode_content(MappingProxyType(ended), 1024).decode()})
            if len(changed) != 1: raise OwnerFailure('PRECONDITION_FAILED', 'index', 'REVISION_CONFLICT')
            self._records.unpack('lease', changed[0]); tx.count += 1
            target_id = text(after['generation_id'])
        elif kind == 'index_fail':
            with self._reader_lock:
                if value['generation_id'] in self._workers.values():
                    raise OwnerFailure('RESOURCE_BUSY', 'index', 'ADMISSION_FULL', True)
            before = tx.get('index_generation', {'generation_id': value['generation_id']})
            if before['revision'] != value['expected_revision'] or before['status'] != 'BUILDING':
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            after = tx.update('index_generation', before, status='FAILED', observed_at=now)
            tx.update('coordinator', coordinator, building_generation=None)
            for raw in self._records.rows.stage('unfinished_pages', uow, {'generation_id': before['generation_id']}):
                tx.update('index_page', self._records.unpack('index_page', raw), status='FAILED', updated_at=now)
            lease = tx.get('lease', {'work_id': before['generation_id']})
            ended = dict(lease) | {'status': 'ENDED', 'observed_at': now}
            changed = self._records.rows.stage('end_index_lease', uow, {'work_id': lease['work_id'],
                'expected_body': encode_content(lease, 1024).decode(), 'body': encode_content(MappingProxyType(ended), 1024).decode()})
            if len(changed) != 1: raise OwnerFailure('PRECONDITION_FAILED', 'index', 'REVISION_CONFLICT')
            self._records.unpack('lease', changed[0]); tx.count += 1
            target_id = text(after['generation_id'])
        elif kind in ('index_retire_page', 'index_trim_page'):
            generation = tx.get('index_generation', {'generation_id': value['generation_id']})
            with self._reader_lock:
                protected = bool(self._readers) or generation['generation_id'] in self._workers.values()
            if generation['status'] not in ('RETIRING', 'FAILED') or protected:
                raise OwnerFailure('RESOURCE_BUSY', 'index', 'ADMISSION_FULL')
            objects = value['objects']
            if type(objects) is not tuple or tuple(sorted(set(text(o) for o in objects))) != objects:
                raise OwnerFailure('INVALID_INPUT', 'index', 'INVALID_SHAPE')
            # Validate the complete requested physical page before deleting anything.
            for object_id in objects:
                tx.get('index_object', {'generation_id': generation['generation_id'], 'object_id': object_id})
            if value['page_id'] is None:
                if value['expected_revision'] is not None:
                    raise OwnerFailure('INVALID_INPUT', 'revision', 'INVALID_SHAPE')
                after = tx.write('index_page', {'page_id': identity('retire_index_page', generation['generation_id'], key), 'generation_id': generation['generation_id'],
                    'operation_key': key, 'owner_id': self.instance_id, 'revision': 1, 'cursor': objects[-1], 'count': len(objects), 'status': 'COMMITTED', 'updated_at': now})
            else:
                before = tx.get('index_page', {'page_id': value['page_id']})
                if before['generation_id'] != generation['generation_id'] or before['revision'] != value['expected_revision']:
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                after = tx.update('index_page', before, cursor=objects[-1], count=len(objects), status='COMMITTED', updated_at=now)
            for object_id in objects: tx.remove_object(generation['generation_id'], object_id)
            target_id = text(after['page_id'])
        else:
            raise OwnerFailure('INVALID_STATE', 'index', 'NOT_READY')
        summary = fact(target_id, integer(before['revision']) if before else None, integer(after['revision']), now, changed=tx.count,
            from_seq=integer(memory_fact['from_seq']) if kind == 'index_publish' and memory_fact is not None else 0,
            to_seq=integer(memory_fact['to_seq']) if kind == 'index_publish' and memory_fact is not None else 0)
        targets = (MappingProxyType({n: summary[n] for n in ('object_id', 'previous_revision', 'revision')}),)
        if object_target is not None: targets += (object_target,)
        return IndexEffect(tuple(sorted(targets, key=lambda value: text(value['object_id']))), summary, memory_fact)

    def _generation_current(self, generation: Record, coordinator: Record) -> None:
        if (generation['generation_id'] not in (coordinator['active_generation'], coordinator['building_generation'])
                or generation['status'] not in ('BUILDING', 'ACTIVE') or generation['config_snapshot_id'] != self.configuration.snapshot_id
                or generation['preprocess_id'] != PREPROCESS_ID or generation['unicode_version'] != UNICODE_VERSION):
            raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'index', 'INDEX_NOT_READY')

    async def query_generation(self) -> tuple[Record, Record | None]:
        """Observe the active acceleration format; missing is an explicit capability."""
        coordinator = await self._records.read('coordinator', {'instance_id': self.instance_id})
        if coordinator is None: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
        generation = None
        if coordinator['active_generation'] is not None:
            generation = await self._records.read('index_generation', {'generation_id': coordinator['active_generation']})
            if generation is None: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
            self._generation_current(generation, coordinator)
            if generation['status'] != 'ACTIVE': raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'index', 'INDEX_NOT_READY')
        return coordinator, generation

    async def work_page(self, generation_id: str) -> tuple[Record, Record | None]:
        """One durable work checkpoint, read through retrieval's own fixed ports."""
        coordinator = await self._records.read('coordinator', {'instance_id': self.instance_id})
        generation = await self._records.read('index_generation', {'generation_id': generation_id})
        if coordinator is None or generation is None:
            raise OwnerFailure('PRECONDITION_FAILED', 'index', 'REVISION_CONFLICT')
        self._generation_current(generation, coordinator)
        pages = await self._records.rows.read('unfinished_pages', {'generation_id': generation_id})
        if len(pages) > 1: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
        return generation, self._records.unpack('index_page', pages[0]) if pages else None

    async def retirement_page(self) -> tuple[str, tuple[str, ...]] | None:
        """Select one actual physical page; retained generation facts survive trim."""
        generations = await self._records.rows.read('retiring_generation', {})
        if not generations: return None
        generation = self._records.unpack('index_generation', generations[0])
        objects = await self._records.rows.read('generation_objects', {'generation_id': generation['generation_id'], 'after': ''})
        if not objects: return None
        return text(generation['generation_id']), tuple(text(value['object_id']) for value in objects)

    async def posting_candidates(self, generation_id: str, terms: tuple[str, ...]) -> tuple[tuple[str, ...], int, bool]:
        """Visit at most the configured postings and retain at most 128 stable IDs."""
        seen: set[str] = set(); visited = 0; exhausted = False
        for token in terms:
            after = ''
            while visited < 4096 and len(seen) < 128:
                rows = await self._records.rows.read('term_postings', {'generation_id': generation_id, 'token': token, 'after': after})
                for row in rows:
                    posting = self._records.unpack('posting', row)
                    if row['generation_id'] != generation_id or posting['token'] != token:
                        raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
                    visited += 1; seen.add(text(posting['object_id'])); after = text(posting['object_id'])
                    if visited >= 4096 or len(seen) >= 128:
                        exhausted = True; break
                if len(rows) < 128 or exhausted: break
            if exhausted: break
        return tuple(sorted(seen)), visited, exhausted
