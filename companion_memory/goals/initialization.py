"""Actual goals initialization and recovery identity verification.

Only a configuration-issued durable identity can bind the owner. Initialization
writes one metadata row inside the caller's atomic transaction; recovery never
creates rows, manufactures goals or invokes external work.
"""
from dataclasses import dataclass
from companion_memory.configuration.information_persistence import StoredInformationConfiguration
from companion_memory.persistence import PersistenceService, UnitOfWork
from companion_memory.persistence.owned_statements import OwnerFailure, StatementCatalog
from companion_memory.information.repository import OwnedRecords
from companion_memory.information.records import Record, identity, fact, integer, text
from .repository import LAYOUTS


@dataclass(frozen=True, slots=True)
class GoalsBinding:
    database_id: str
    instance_id: str
    config_snapshot_id: str


class GoalsOwner:
    """Single goals data owner; metadata validation precedes every business port."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService,
                 configuration: StoredInformationConfiguration, instance_id: str):
        if type(configuration) is not StoredInformationConfiguration:
            raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        self.binding = GoalsBinding(configuration.database_id, instance_id, configuration.snapshot_id)
        self.configuration = configuration
        self._records = OwnedRecords(catalog, storage, instance_id, LAYOUTS)
        lease = storage.claim_module_owner(catalog.definition)
        if lease is None:
            raise OwnerFailure('RESOURCE_BUSY', 'storage', 'ADMISSION_FULL')
        self._lease = lease
        self.metadata_id = identity('goals_metadata', self.binding.database_id, instance_id)
        self.ready = False

    def initialize(self, uow: UnitOfWork, observed_at: int) -> Record:
        """Stage the sole initialization row; the coordinator must commit all owners."""
        if self._records.rows.stage('metadata_count', uow, {})[0]['count'] != 0:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'NO_CHANGE')
        self._records.write('metadata', uow, {'metadata_id': self.metadata_id, 'database_id': self.binding.database_id,
            'instance_id': self.binding.instance_id, 'config_snapshot_id': self.binding.config_snapshot_id,
            'format_version': 1, 'revision': 1, 'initialized_at_us': observed_at})
        return fact(self.metadata_id, None, 1, observed_at, changed=1)

    async def recover(self, expected_fact: Record) -> Record:
        """Verify the exact original initialization fact without repair or writes."""
        self.ready = False
        rows = await self._records.rows.read('metadata_count', {})
        if rows[0]['count'] != 1:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        raw = await self._records.rows.read('metadata_get', {'metadata_id': self.metadata_id})
        if len(raw) != 1:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        from companion_memory.persistence.content_codec import decode_content
        from companion_memory.persistence.schema import InvalidValue
        try:
            body = decode_content(text(raw[0]['body']).encode(), 1024)
        except (InvalidValue, UnicodeError):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from None
        if type(body) is not dict or set(body) != {f.name for f in self._records.layouts['metadata'].schema.fields}:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if type(body['format_version']) is int and body['format_version'] != 1:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'FORMAT_UNSUPPORTED')
        for name, expected in (('database_id', self.binding.database_id), ('instance_id', self.binding.instance_id),
                               ('config_snapshot_id', self.binding.config_snapshot_id), ('metadata_id', self.metadata_id)):
            if type(body[name]) is not str:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            if body[name] != expected:
                raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        value = self._records.unpack('metadata', raw[0])
        expected = fact(self.metadata_id, None, 1, integer(value['initialized_at_us']), changed=1)
        if value['revision'] != 1 or expected_fact != expected:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        self.ready = True
        return value

    def close(self) -> bool:
        """Return the owner lease only after storage observes its own jobs ended."""
        self.ready = False
        return self._lease.release()
