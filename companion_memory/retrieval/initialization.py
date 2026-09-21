"""Retrieval's real coordinator initialization and retained slot accounting.

The coordinator binds the same durable configuration as every ticket and index
generation. Recovery checks actual roots and never expires payload implicitly.
"""
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
from companion_memory.configuration.information_persistence import StoredInformationConfiguration
from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration
from companion_memory.persistence import PersistenceService, UnitOfWork
from companion_memory.persistence.owned_statements import StatementCatalog, OwnerFailure
from companion_memory.persistence.record_primitives import Record, fact, integer
from companion_memory.persistence.record_repository import OwnedRecords
from .repository import layouts


class RetrievalOwner:
    """Own all retrieval rows and keep their lease until actual work has ended."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService, configuration: StoredInformationConfiguration | StoredTextConfiguration | StoredSemanticConfiguration | StoredCognitionConfiguration, instance_id: str):
        self._records = OwnedRecords(catalog, storage, instance_id, layouts((type(configuration) is StoredSemanticConfiguration or type(configuration) is StoredDailyConfiguration or type(configuration) is StoredDreamConfiguration or type(configuration) is StoredManagedConfiguration)))
        self.configuration = configuration
        self.instance_id = instance_id
        lease = storage.claim_module_owner(catalog.definition)
        if lease is None:
            raise OwnerFailure('RESOURCE_BUSY', 'storage', 'ADMISSION_FULL')
        self._lease = lease
        self.ready = False

    def initialize(self, uow: UnitOfWork, at_us: int) -> Record:
        if self._records.rows.stage('coordinator_count', uow, {})[0]['count']:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'NO_CHANGE')
        self._records.write('coordinator', uow, {'instance_id': self.instance_id, 'database_id': self.configuration.database_id,
            'config_snapshot_id': self.configuration.snapshot_id, 'clock_high_water': at_us, 'occupied_tickets': 0,
            'cleanup_cursor': None, 'active_generation': None, 'building_generation': None, 'revision': 1})
        return fact(self.instance_id, None, 1, at_us, changed=1)

    async def recover(self, expected: Record) -> None:
        self.ready = False
        coordinator = await self._records.read('coordinator', {'instance_id': self.instance_id})
        if coordinator is None or coordinator['database_id'] != self.configuration.database_id or coordinator['config_snapshot_id'] != self.configuration.snapshot_id:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if expected != fact(self.instance_id, None, 1, integer(expected['at_us']), changed=1):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        count = (await self._records.rows.read('ticket_count', {}))[0]['count']
        if count != coordinator['occupied_tickets']:
            raise OwnerFailure('STORAGE_FAILED', 'ticket', 'INTEGRITY_FAILURE')
        self.ready = True

    def coordinator(self, uow: UnitOfWork) -> Record:
        value = self._records.get('coordinator', uow, {'instance_id': self.instance_id})
        if value is None:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return value

    def close(self) -> bool:
        self.ready = False
        return self._lease.release()
