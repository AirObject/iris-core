"""External activity CAS with independent field clocks and durable empty state.

State is written only through the bound host. Replacement ends the previous
activity atomically; reads never infer activities from messages or elapsed time.
"""
from types import MappingProxyType
from companion_memory.configuration.information_persistence import StoredInformationConfiguration
from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from companion_memory.persistence import Field, RecordSchema, PersistenceService, UnitOfWork, Value
from companion_memory.persistence.owned_statements import StatementCatalog, OwnerFailure
from companion_memory.information.records import Record, ID, TIME, TEXT, REVISION, BOOL, checked, fact, identity, integer, text, record, decode
from companion_memory.information.repository import OwnedRecords
from .records import ACTIVITY, POINTER, STATE_FIELD, OFFSET
from .repository import LAYOUTS
from .time_values import duration_view

PATCH_FIELD = RecordSchema((Field('value', TEXT(512)), Field('started_at', TIME, nullable=True, optional=True)))
PATCH_FIELDS = RecordSchema(tuple(Field(name, PATCH_FIELD, nullable=True, optional=True) for name in ('scene', 'progress', 'emotion')))
PATCH = RecordSchema((Field('activity_value', TEXT(512), optional=True), Field('started_at', TIME, nullable=True, optional=True),
    Field('reported_at', TIME), Field('reported_offset_minutes', OFFSET), Field('fields', PATCH_FIELDS, optional=True)))
SET_INPUT = RecordSchema((Field('activity_id', ID, nullable=True), Field('expected_revision', REVISION, nullable=True),
    Field('replace_activity', BOOL), Field('patch', PATCH)))
UPDATE_INPUT = RecordSchema((Field('activity_id', ID), Field('expected_revision', REVISION), Field('patch', PATCH)))
END_INPUT = RecordSchema((Field('activity_id', ID), Field('expected_revision', REVISION)))


class StateOwner:
    """Native state owner with an explicit initial pointer and one writer host."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService, configuration: StoredInformationConfiguration | StoredTextConfiguration | StoredSemanticConfiguration, instance_id: str):
        self._records = OwnedRecords(catalog, storage, instance_id, LAYOUTS)
        self.configuration = configuration
        self.instance_id = instance_id
        lease = storage.claim_module_owner(catalog.definition)
        if lease is None:
            raise OwnerFailure('RESOURCE_BUSY', 'storage', 'ADMISSION_FULL')
        self._lease = lease
        self.ready = False

    def initialize(self, uow: UnitOfWork, at_us: int) -> Record:
        if self._records.rows.stage('current_pointer_count', uow, {})[0]['count']:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'NO_CHANGE')
        self._records.write('current_pointer', uow, {'instance_id': self.instance_id, 'revision': 1, 'activity_id': None,
            'last_ended_id': None, 'host_id': None, 'initialized_at_us': at_us})
        return fact(self.instance_id, None, 1, at_us, changed=1)

    async def recover(self, expected: Record) -> None:
        self.ready = False
        if (await self._records.rows.read('current_pointer_count', {}))[0]['count'] != 1:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        pointer = await self._records.read('current_pointer', {'instance_id': self.instance_id})
        if pointer is None or pointer['instance_id'] != self.instance_id or expected != fact(self.instance_id, None, 1, integer(pointer['initialized_at_us']), changed=1):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if pointer['activity_id'] is not None:
            current = await self._records.read('activity', {'activity_id': pointer['activity_id']})
            if current is None or current['ended_at'] is not None or current['instance_id'] != self.instance_id:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if pointer['last_ended_id'] is not None:
            ended = await self._records.read('activity', {'activity_id': pointer['last_ended_id']})
            if ended is None or ended['ended_at'] is None or ended['instance_id'] != self.instance_id or ended['activity_id'] == pointer['activity_id']:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        after = ''
        while page := await self._records.rows.read('activity_recovery_page', {'after': after}):
            for raw in page:
                activity = self._records.unpack('activity', raw)
                if (activity['instance_id'] != self.instance_id or activity['last_host_id'] != pointer['host_id']
                        or (activity['ended_at'] is None) != (activity['activity_id'] == pointer['activity_id'])
                        or integer(activity['first_reported_at']) > integer(activity['reported_at'])):
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                for item in record(activity['fields']).values():
                    if item is not None:
                        field = record(item)
                        if not integer(field['first_reported_at']) <= integer(field['updated_at']) <= integer(activity['reported_at']):
                            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                after = text(activity['activity_id'])
        self.ready = True

    def apply(self, kind: str, uow: UnitOfWork, payload: object, host_id: str, entry_id: str, key: str, at_us: int) -> tuple[Record, ...]:
        """Stage set/update/end with actual revisions, or reject the entire write."""
        value = checked({'state_set': SET_INPUT, 'state_update': UPDATE_INPUT, 'state_end': END_INPUT}[kind], payload, 4096)
        pointer = self._records.get('current_pointer', uow, {'instance_id': self.instance_id})
        if not self.ready or pointer is None:
            raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        if pointer['host_id'] is not None and pointer['host_id'] != host_id:
            raise OwnerFailure('ACCESS_DENIED', 'state', 'BINDING_MISMATCH')
        previous = self._records.get('activity', uow, {'activity_id': pointer['activity_id']}) if pointer['activity_id'] is not None else None
        if previous is not None and previous['revision'] != value['expected_revision'] or previous is None and value['expected_revision'] is not None:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        if kind != 'state_set' and (previous is None or value['activity_id'] != previous['activity_id']):
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        updated_pointer = dict(pointer)
        updated_pointer['revision'] = integer(pointer['revision']) + 1
        updated_pointer['host_id'] = host_id
        targets: list[Record] = []
        if kind == 'state_end' or kind == 'state_set' and previous is not None:
            if kind == 'state_set' and not value['replace_activity']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            if previous is None:
                raise OwnerFailure('PRECONDITION_FAILED', 'state', 'NO_CHANGE')
            started = previous['started_at']
            if started is not None and integer(started) > at_us:
                raise OwnerFailure('INVALID_INPUT', 'time', 'INVALID_TIME')
            ended = dict(previous); ended['ended_at'] = at_us; ended['revision'] = integer(previous['revision']) + 1
            self._records.write('activity', uow, ended, expected_revision=integer(previous['revision']))
            targets.append(fact(text(previous['activity_id']), integer(previous['revision']), integer(ended['revision']), at_us, changed=1))
            updated_pointer['activity_id'] = None; updated_pointer['last_ended_id'] = previous['activity_id']
        if kind != 'state_end':
            patch = record(value['patch'])
            tolerance = integer(self.configuration.candidate.information.record('state.external')['future_tolerance_seconds']) * 1000000
            reported = integer(patch['reported_at'])
            if reported > at_us + tolerance:
                raise OwnerFailure('INVALID_INPUT', 'time', 'INVALID_TIME')
            if kind == 'state_set':
                if value['activity_id'] != (previous['activity_id'] if previous else None) or 'activity_value' not in patch:
                    raise OwnerFailure('INVALID_INPUT', 'state', 'INVALID_SHAPE')
                oid = identity('activity', self.instance_id, key)
                if self._records.get('activity', uow, {'activity_id': oid}) is not None:
                    raise OwnerFailure('PRECONDITION_FAILED', 'state', 'NO_CHANGE')
                activity: dict[str, object] = {'instance_id': self.instance_id, 'activity_id': oid, 'last_host_id': host_id, 'last_entry_id': entry_id,
                    'revision': 1, 'activity_value': patch['activity_value'], 'started_at': patch.get('started_at'), 'ended_at': None,
                    'first_reported_at': reported, 'reported_at': reported, 'received_at': at_us,
                    'reported_offset_minutes': patch['reported_offset_minutes'], 'fields': {'scene': None, 'progress': None, 'emotion': None}}
            else:
                if previous is None:
                    raise OwnerFailure('PRECONDITION_FAILED', 'state', 'NO_CHANGE')
                activity = dict(previous); oid = text(previous['activity_id'])
                if reported < integer(previous['reported_at']):
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                activity.update(revision=integer(previous['revision']) + 1, last_host_id=host_id, last_entry_id=entry_id,
                                reported_at=reported, received_at=at_us, reported_offset_minutes=patch['reported_offset_minutes'])
                for name in ('activity_value', 'started_at'):
                    if name in patch: activity[name] = patch[name]
            fields: dict[str, Value] = dict(record(previous['fields'])) if kind == 'state_update' and previous else {'scene': None, 'progress': None, 'emotion': None}
            for name, field in record(patch.get('fields', MappingProxyType({}))).items():
                if field is None:
                    fields[name] = None
                    continue
                submitted = record(field); old = record(fields[name]) if fields[name] is not None else None
                same = old is not None and old['value'] == submitted['value']
                fields[name] = checked(STATE_FIELD, {'value': submitted['value'], 'started_at': submitted.get('started_at', old['started_at'] if same and old else None),
                    'first_reported_at': old['first_reported_at'] if same and old else reported, 'updated_at': reported,
                    'offset_minutes': patch['reported_offset_minutes']}, 1024)
            activity['fields'] = fields
            final = checked(ACTIVITY, activity, 4096)
            for started in (final['started_at'], *(record(f)['started_at'] for f in record(final['fields']).values() if f is not None)):
                if started is not None and integer(started) > at_us + tolerance:
                    raise OwnerFailure('INVALID_INPUT', 'time', 'INVALID_TIME')
            if kind == 'state_update' and previous is not None and {k: v for k, v in final.items() if k != 'revision'} == {k: v for k, v in previous.items() if k != 'revision'}:
                raise OwnerFailure('PRECONDITION_FAILED', 'state', 'NO_CHANGE')
            self._records.write('activity', uow, final, expected_revision=integer(previous['revision']) if kind == 'state_update' and previous else None)
            updated_pointer['activity_id'] = oid
            targets.append(fact(oid, integer(previous['revision']) if kind == 'state_update' and previous else None, integer(final['revision']), at_us, changed=1))
        self._records.write('current_pointer', uow, updated_pointer, expected_revision=integer(pointer['revision']))
        return tuple(targets)

    async def view(self, at_us: int) -> Record | None:
        """Return an observation, preserving reported starts and ended/absent state."""
        rows = await self._records.rows.read('current_view', {'instance_id': self.instance_id})
        if len(rows) != 1:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        pointer = decode(POINTER, text(rows[0]['pointer_body']), 1024)
        last_ended: Record | None = None
        if pointer['last_ended_id'] is not None:
            if rows[0]['ended_body'] is None: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            ended = decode(ACTIVITY, text(rows[0]['ended_body']), 4096)
            if ended['activity_id'] != pointer['last_ended_id'] or ended['ended_at'] is None or ended['instance_id'] != self.instance_id:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            elapsed = duration_view(integer(ended['ended_at']), integer(ended['started_at']) if ended['started_at'] is not None else None, integer(ended['first_reported_at']))
            last_ended = MappingProxyType({'activity_id': ended['activity_id'], 'revision': ended['revision'], 'ended_at': ended['ended_at'],
                'duration_us': elapsed.elapsed_us, 'duration_basis': elapsed.basis, 'clock': elapsed.clock})
        if pointer['activity_id'] is None:
            return MappingProxyType({'activity': None, 'last_ended_id': pointer['last_ended_id'], 'last_ended': last_ended, 'observed_at': at_us,
                                     'pointer_revision': pointer['revision']})
        if rows[0]['activity_body'] is None:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        value = decode(ACTIVITY, text(rows[0]['activity_body']), 4096)
        if value['activity_id'] != pointer['activity_id'] or value['ended_at'] is not None:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        start = integer(value['started_at']) if value['started_at'] is not None else None
        elapsed = duration_view(at_us, start, integer(value['first_reported_at']))
        stale_ms = integer(self.configuration.candidate.information.record('state.external')['stale_after_seconds'])
        durations: dict[str, Value] = {}
        for name, supplied in record(value['fields']).items():
            if supplied is None: durations[name] = None; continue
            field = record(supplied)
            duration = duration_view(at_us, integer(field['started_at']) if field['started_at'] is not None else None, integer(field['first_reported_at']))
            durations[name] = MappingProxyType({'duration_us': duration.elapsed_us, 'duration_basis': duration.basis, 'clock': duration.clock})
        return MappingProxyType({'activity': value, 'duration_us': elapsed.elapsed_us, 'duration_basis': elapsed.basis,
                                 'clock': elapsed.clock, 'stale': at_us - integer(value['received_at']) > stale_ms * 1000000,
                                 'observed_at': at_us, 'pointer_revision': pointer['revision'], 'last_ended': last_ended,
                                 'field_durations': MappingProxyType(durations)})

    def close(self) -> bool:
        self.ready = False
        return self._lease.release()

    async def observation(self, now: int) -> Record:
        """A redacted status projection never exposes activity or child values."""
        view = await self.view(now)
        return MappingProxyType({'has_activity': view is not None and view['activity'] is not None,
            'revision': view['pointer_revision'] if view is not None else None, 'observed_at': now})
