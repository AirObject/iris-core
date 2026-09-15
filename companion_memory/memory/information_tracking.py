"""Memory-owned monotonic changes and durable multi-generation coverage gaps.

Every actual object mutation stages its change metadata in the same transaction.
Acknowledgements compare the current revision and never move an uncovered start
forward when a later edit coalesces into an already pending object.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .transactions import MemoryTransactions
from companion_memory.configuration.information_persistence import StoredInformationConfiguration
from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from companion_memory.persistence import PersistenceService, UnitOfWork, Value, SequenceSchema, RecordSchema, Field
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import StatementCatalog, OwnerFailure
from companion_memory.information.records import Record, ID, checked, fact, identity, integer, text
from companion_memory.information.repository import OwnedRecords
from .information_repository import LAYOUTS

INDEX_SELECTION = RecordSchema((Field('object_ids', SequenceSchema(ID, 1, 16)),))


class MemoryInformation:
    """Owned by the already bound memory participant, sharing its existing lease."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService, configuration: StoredInformationConfiguration | StoredTextConfiguration | StoredSemanticConfiguration, instance_id: str, objects: MemoryTransactions):
        self._records = OwnedRecords(catalog, storage, instance_id, LAYOUTS)
        self.configuration = configuration
        self.instance_id = instance_id
        self._objects = objects
        self.format_id = identity('memory_information', configuration.database_id, instance_id)

    def initialize(self, uow: UnitOfWork, at_us: int) -> Record:
        if self._records.rows.stage('information_format_count', uow, {})[0]['count'] or self._records.rows.stage('change_sequence_count', uow, {})[0]['count']:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'NO_CHANGE')
        self._records.write('information_format', uow, {'format_id': self.format_id, 'database_id': self.configuration.database_id,
            'instance_id': self.instance_id, 'config_snapshot_id': self.configuration.snapshot_id, 'format_version': 1, 'revision': 1, 'initialized_at_us': at_us})
        self._records.write('change_sequence', uow, {'instance_id': self.instance_id, 'last_seq': 0, 'revision': 1, 'published_generation_id': None, 'published_seq': 0})
        return fact(self.format_id, None, 1, at_us, changed=2)

    async def recover(self, expected: Record) -> None:
        value = await self._records.read('information_format', {'format_id': self.format_id})
        seq = await self._records.read('change_sequence', {'instance_id': self.instance_id})
        if value is None or seq is None or value['database_id'] != self.configuration.database_id or value['instance_id'] != self.instance_id or value['config_snapshot_id'] != self.configuration.snapshot_id:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if expected != fact(self.format_id, None, 1, integer(value['initialized_at_us']), changed=2):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        count = (await self._records.rows.read('information_object_count', {}))[0]['count']
        if integer(count) > integer(self.configuration.candidate.information.record('memory.usage')['existing_object_limit']):
            raise OwnerFailure('STORAGE_FAILED', 'member', 'INTEGRITY_FAILURE')
        from .information_recovery import verify_information
        await verify_information(self, seq)

    async def usage_evidence_count(self) -> int:
        """Support the retrieval owner's reverse mirror check without table access."""
        return integer((await self._records.rows.read('usage_receipt_count', {}))[0]['count'])

    async def index_acknowledgment(self, object_id: str, generation_id: str) -> Record | None:
        return await self._records.read('generation_ack', {'object_id': object_id, 'generation_id': generation_id})

    async def index_current_objects(self, object_ids: tuple[str, ...]) -> tuple[Record, ...]:
        """Recover at most sixteen current objects, checking every original digest."""
        selected = encode_content(checked(INDEX_SELECTION, {'object_ids': object_ids}, 4096)['object_ids'], 4096).decode()
        rows = await self._records.rows.read('information_selected_objects', {'object_ids': selected})
        return tuple(self._objects.decode_current(row) for row in rows)

    async def index_acknowledgments(self, generation_id: str, object_ids: tuple[str, ...]) -> tuple[Record, ...]:
        """Expose one bounded generation page without cross-owner table access."""
        selected = encode_content(checked(INDEX_SELECTION, {'object_ids': object_ids}, 4096)['object_ids'], 4096).decode()
        rows = await self._records.rows.read('information_selected_acknowledgments', {'generation_id': generation_id, 'object_ids': selected})
        return tuple(self._records.unpack('generation_ack', row) for row in rows)

    async def index_acknowledgment_page(self, after_object: str = '', after_generation: str = '') -> tuple[Record, ...]:
        """Expose complete finite coverage leaves for cross-owner recovery checks."""
        rows = await self._records.rows.read('generation_ack_recovery_page', {'after_object_id': after_object, 'after_generation_id': after_generation})
        return tuple(self._records.unpack('generation_ack', row) for row in rows)

    def sequence(self, uow: UnitOfWork) -> Record:
        value = self._records.get('change_sequence', uow, {'instance_id': self.instance_id})
        if value is None:
            raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
        return value

    async def recovery_objects(self, after: str) -> tuple[Record, ...]:
        """Validate four full current objects and their actual source-holder links."""
        from .formats import isolate_links, sequence
        from companion_memory.persistence.content_codec import decode_content
        values = await self._records.rows.read('information_recovery_objects', {'after': after})
        for value in values:
            current = self._objects.decode_current(value)
            if value['links_body'] is None or value['links_revision'] != current['revision']:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            from companion_memory.persistence.schema import InvalidValue
            try: links = isolate_links(decode_content(text(value['links_body']).encode(), 2048), text(current['object_id']), integer(current['revision']))
            except (InvalidValue, UnicodeError):
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from None
            if encode_content(links, 2048).decode() != value['links_body'] or value['linked_holders'] != len(sequence(links['sources'])):
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return values

    async def recovery_source_holders(self, source_id: str, after: str) -> tuple[Record, ...]:
        """Check the same retained source/reverse-link join in one finite page."""
        return await self._records.rows.read('information_recovery_holders', {'source_id': source_id, 'after': after})

    async def recovery_source_members(self, source_id: str) -> tuple[Record, ...]:
        """Read all at most four canonical immutable member leaves together."""
        return await self._records.rows.read('information_recovery_members', {'source_id': source_id})

    async def publication_state(self) -> Record:
        """Read the canonical root for cross-owner publication recovery."""
        value = await self._records.read('change_sequence', {'instance_id': self.instance_id})
        if value is None: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
        return value

    def publish(self, uow: UnitOfWork, generation_id: str, cutoff: int, now: int) -> Record:
        """Record an actual index switch without inventing an object mutation."""
        current = self.sequence(uow)
        if current['published_generation_id'] == generation_id:
            raise OwnerFailure('PRECONDITION_FAILED', 'index', 'NO_CHANGE')
        coverage = self.coverage(uow)
        if (cutoff != current['last_seq'] or coverage['contiguous_seq'] != cutoff
                or coverage['pending_count'] or self.index_pending_after(uow, generation_id)):
            raise OwnerFailure('PRECONDITION_FAILED', 'index', 'REVISION_CONFLICT')
        revision = integer(current['revision'])
        self._records.write('change_sequence', uow, dict(current) | {'published_generation_id': generation_id,
            'published_seq': cutoff, 'revision': revision + 1}, expected_revision=revision)
        return fact(self.instance_id, revision, revision + 1, now, changed=1, from_seq=cutoff, to_seq=cutoff)

    def changed(self, uow: UnitOfWork, object_id: str, revision: int, deleted: bool) -> tuple[int, int]:
        """Advance actual change order and keep the earliest outstanding coverage gap."""
        if not deleted and revision == 1:
            count = integer(self._records.rows.stage('information_object_count', uow, {})[0]['count'])
            if count > integer(self.configuration.candidate.information.record('memory.usage')['existing_object_limit']):
                raise OwnerFailure('RESOURCE_BUSY', 'member', 'CAPACITY_REACHED')
        current = self.sequence(uow)
        previous = integer(current['last_seq'])
        seq = previous + 1
        self._records.write('change_sequence', uow, dict(current) | {'last_seq': seq,
            'revision': integer(current['revision']) + 1}, expected_revision=integer(current['revision']))
        before = self._records.get('index_gap', uow, {'object_id': object_id})
        gap = {'object_id': object_id, 'revision': revision,
               'first_uncovered_seq': before['first_uncovered_seq'] if before else seq,
               'latest_change_seq': seq, 'action': 'REMOVE' if deleted else 'UPSERT'}
        self._records.write('index_gap', uow, gap, expected_revision=integer(before['revision']) if before else None)
        if self._objects.semantic is not None:
            self._objects.semantic.mark(uow,object_id,revision,deleted,previous,seq)
        return previous, seq

    async def current_page(self, after: str = '', limit: int = 16) -> tuple[Record, ...]:
        """Trusted index scan of current objects; never authorizes host delivery."""
        rows = await self._records.rows.read('information_objects', {'after': after, 'limit': limit})
        return tuple(self._objects.decode_current(row) for row in rows)

    async def index_pending_page(self, generation_id: str, after: str = '') -> tuple[Record, ...]:
        """Return only missing current coverage and pending deletion acknowledgments."""
        return await self._records.rows.read('information_pending_index', {'generation_id': generation_id, 'after': after})

    def index_pending_after(self, uow: UnitOfWork, generation_id: str, after: str = '') -> bool:
        return bool(self._records.rows.stage('information_pending_after', uow, {'generation_id': generation_id, 'after': after})[0]['count'])

    async def index_current(self, object_id: str) -> Record | None:
        """Trusted index-only preparation; applying repeats authority in its UoW."""
        rows = await self._objects.rows.read('objects_get', {'object_id': object_id})
        return self._objects.decode_current(rows[0]) if rows else None

    async def expired_page(self, cutoff: int, after_time: int, after_id: str, limit: int = 16) -> tuple[Record, ...]:
        """Trusted finite deletion candidates; execution still checks the revision."""
        from types import MappingProxyType
        rows = await self._records.rows.read('information_expired_objects', {'cutoff': cutoff, 'after_time': after_time, 'after_id': after_id, 'limit': limit})
        result: list[Record] = []
        for row in rows:
            current = self._objects.decode_current(row)
            if current['lifecycle'] != 'FORGOTTEN' or current['forgotten_since_us'] is None or integer(current['forgotten_since_us']) > cutoff:
                raise OwnerFailure('STORAGE_FAILED', 'member', 'INTEGRITY_FAILURE')
            result.append(MappingProxyType({key: current[key] for key in ('object_id', 'revision', 'forgotten_since_us')}))
        return tuple(result)

    def current(self, uow: UnitOfWork, object_id: str) -> Record | None:
        """Authoritative object snapshot for a declared retrieval participant."""
        return self._objects.current(uow, object_id)

    async def gaps(self, limit: int = 128) -> tuple[Record, ...]:
        rows = await self._records.rows.read('index_gap_page', {'after': '', 'limit': limit})
        return tuple(self._records.unpack('index_gap', row) for row in rows)

    def coverage(self, uow: UnitOfWork) -> Record:
        """Continuous coverage stops before the earliest still-uncovered change."""
        from types import MappingProxyType
        sequence = self.sequence(uow)
        gap = self._records.rows.stage('information_gap_floor', uow, {})[0]
        contiguous = integer(sequence['last_seq']) if gap['minimum'] is None else integer(gap['minimum']) - 1
        return MappingProxyType({'captured_seq': sequence['last_seq'], 'contiguous_seq': contiguous, 'pending_count': gap['count']})

    async def structural_page(self, parameters: dict[str, Value]) -> tuple[Record, ...]:
        """Fixed indexed/structured current-object filtering for the query owner."""
        rows = await self._records.rows.read('information_structure', parameters)
        return tuple(self._objects.decode_current(row) for row in rows)

    async def coverage_view(self) -> Record:
        from types import MappingProxyType
        rows = await self._records.rows.read('information_coverage', {'instance_id': self.instance_id})
        if len(rows) != 1: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
        view = rows[0]
        return MappingProxyType({'captured_seq': view['last_seq'], 'contiguous_seq': view['last_seq'] if view['minimum'] is None else integer(view['minimum']) - 1,
            'pending_count': view['count']})

    @staticmethod
    def _source_metadata(rows: tuple[Record, ...], object_id: str, revision: int) -> tuple[Record, ...]:
        from types import MappingProxyType
        if not 1 <= len(rows) <= 2 or any(row['source_count'] != len(rows) or row['source_state'] != 'RETAINED'
                or row['holder'] != object_id or row['source_revision'] is None for row in rows):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if any(row['object_revision'] != revision for row in rows):
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        return tuple(MappingProxyType({key: row[key] for key in ('source_id', 'link_role', 'source_revision')}) for row in rows)

    def source_metadata(self, uow: UnitOfWork, object_id: str, revision: int) -> tuple[Record, ...]:
        """Finite provenance metadata, checked in the final object's transaction."""
        return self._source_metadata(self._records.rows.stage('information_source_metadata', uow, {'object_id': object_id}), object_id, revision)

    async def read_source_metadata(self, object_id: str, revision: int) -> tuple[Record, ...]:
        """Query preparation only; final delivery repeats this inside its UoW."""
        return self._source_metadata(await self._records.rows.read('information_source_metadata', {'object_id': object_id}), object_id, revision)

    def has_after(self, uow: UnitOfWork, after: str) -> bool:
        return bool(self._records.rows.stage('information_has_after', uow, {'after': after})[0]['count'])

    def missing_generation(self, uow: UnitOfWork, generation_id: str) -> int:
        """Count current revisions without this generation's memory-owned receipt."""
        return integer(self._records.rows.stage('information_missing_generation', uow, {'generation_id': generation_id})[0]['count'])

    def transaction_write_count(self, uow: UnitOfWork) -> int:
        """Actual memory row effects within this transaction, excluding other owners."""
        return self._objects.storage.transaction_row_changes(uow).get('memory', 0)

    def preview_usage(self, uow: UnitOfWork, current: Record, now: int) -> tuple[int, str, int, bool]:
        from .usage import preview_usage
        return preview_usage(self._objects, uow, current, now)

    async def read_usage_preview(self, current: Record, now: int) -> tuple[int, str, int, bool]:
        from .usage import preview_values
        previous = await self._records.read('usage_object', {'object_id': current['object_id']})
        return preview_values(self._objects, current, previous['last_used_at'] if previous else None, now)

    def apply_usage(self, uow: UnitOfWork, current: Record, now: int):
        from .usage import apply_usage
        return apply_usage(self._objects, uow, current, now)

    def usage_receipt(self, uow: UnitOfWork, keys: dict[str, Value]) -> Record | None:
        return self._records.get('usage_receipt', uow, keys)

    async def read_usage_receipt(self, keys: dict[str, Value]) -> Record | None:
        """Read one immutable consumption receipt through its memory owner."""
        return await self._records.read('usage_receipt', keys)

    def save_usage_receipt(self, uow: UnitOfWork, value: Record) -> None:
        self._records.write('usage_receipt', uow, value)

    def acknowledge(self, uow: UnitOfWork, object_id: str, revision: int, generation_id: str,
                    target_generations: tuple[str, ...], action: str) -> Record:
        """Verify the real object and record coverage for at most two generations.

        A new generation may acknowledge an unchanged object already covered by
        the old generation. It still writes a real new coverage leaf. Repeated
        identical coverage is a no-change rejection, never a placeholder write.
        """
        from types import MappingProxyType
        if not 1 <= len(target_generations) <= 2 or len(set(target_generations)) != len(target_generations) or generation_id not in target_generations:
            raise OwnerFailure('INVALID_INPUT', 'index', 'INVALID_SHAPE')
        current = self.current(uow, object_id)
        gap = self._records.get('index_gap', uow, {'object_id': object_id})
        if (action == 'UPSERT' and (current is None or current['revision'] != revision)
                or action == 'REMOVE' and (current is not None or gap is None or gap['revision'] != revision or gap['action'] != 'REMOVE')
                or gap is not None and (gap['revision'] != revision or gap['action'] != action)):
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        all_acks = tuple(self._records.unpack('generation_ack', r) for r in self._records.rows.stage('information_object_acks', uow, {'object_id': object_id}))
        if len(all_acks) > 2:
            raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
        seq = integer(gap['latest_change_seq']) if gap else max((integer(a['applied_seq']) for a in all_acks if a['revision'] == revision), default=integer(self.sequence(uow)['last_seq']))
        prior = next((a for a in all_acks if a['generation_id'] == generation_id), None)
        proposed = MappingProxyType({'object_id': object_id, 'generation_id': generation_id, 'revision': revision, 'applied_seq': seq, 'action': action})
        if prior == proposed:
            raise OwnerFailure('PRECONDITION_FAILED', 'index', 'NO_CHANGE')
        changes = 1
        for old in all_acks:
            if old['generation_id'] not in target_generations:
                self._records.remove('generation_ack', uow, {'object_id': object_id, 'generation_id': old['generation_id']}); changes += 1
        self._records.write('generation_ack', uow, proposed, expected_revision=integer(prior['revision']) if prior else None)
        if gap is not None:
            complete = all((a := self._records.get('generation_ack', uow, {'object_id': object_id, 'generation_id': generation})) is not None
                and a['revision'] == revision and a['applied_seq'] == seq and a['action'] == action for generation in target_generations)
            if complete:
                self._records.remove('index_gap', uow, {'object_id': object_id}); changes += 1
                changes += len(self._objects.rows.stage('index_dirty_delete', uow, {'object_id': object_id}))
        return MappingProxyType({'object_id': object_id, 'revision': revision, 'changed_count': changes, 'applied_seq': seq})
