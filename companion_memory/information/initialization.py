"""One atomic initialization command spanning four actual information owners.

The coordinator has no private SQL. Each participant stages its own real root,
and persistence derives all mandatory audits from the immutable result. Loading
and recovery only verify the original command and never fill missing roots.
"""
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
from collections.abc import Callable
from types import MappingProxyType
from typing import Protocol
from companion_memory.configuration.information_persistence import StoredInformationConfiguration
from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence import (AuditFieldBinding, AuditResultBinding, Field, RecordSchema, RepositoryDefinition,
    ResultBoundCommandDefinition, ResultBoundCommand, UnitOfWork, PersistenceService, Committed, Found, NotFound, NotCommitted,
    Rejected, Failed, Unconfirmed, RecoveryHandle)
from companion_memory.persistence.owned_statements import OwnerFailure
from .records import Record, ID, TIME, TEXT, FACT, TARGETS, integer, text, record, checked, choice, COMMAND_INPUT, ITEMS


class InitializableOwner(Protocol):
    """Data-owner participation and read-only recovery, with no coordinator SQL."""
    def initialize(self, uow: UnitOfWork, at_us: int, /) -> Record: ...
    async def recover(self, expected: Record, /) -> object: ...


class InformationInitialization:
    """Retain the original root creation command and all four necessary audits."""
    def __init__(self, repositories: tuple[RepositoryDefinition, ...], configuration_repository: RepositoryDefinition):
        names = ('retrieval', 'state', 'goals', 'memory')
        by_owner = {r.owner_module: r for r in repositories}
        self._owners: dict[str, InitializableOwner] = {}
        self._configuration: StoredInformationConfiguration | StoredTextConfiguration | StoredSemanticConfiguration | StoredCognitionConfiguration | None = None
        self._instance = ''
        self._configuration_check: Callable[[UnitOfWork], bool] | None = None
        audits = tuple(AuditRequirement(owner, owner + '_initialize_information_owners', 'INITIALIZE_INFORMATION_OWNERS', 1, ('APPLY',), FACT, target_limit=16) for owner in names)
        inputs = COMMAND_INPUT
        results = RecordSchema((Field('outcome', choice('INITIALIZED')), Field('targets', TARGETS), Field('items', ITEMS), Field('facts', RecordSchema(tuple(Field(n, FACT) for n in names)))))
        bindings = tuple(AuditResultBinding(a.event_slot, 1, (
            AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
            AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
            AuditFieldBinding('change', 'RESULT', ('facts', a.owner_module)))) for a in audits)
        self.definition = ResultBoundCommandDefinition('information', 'initialize_information_owners', 1, inputs, 1, results,
            tuple(by_owner[n] for n in names) + (configuration_repository,), audits, self._handle, RecordSchema((Field('actor', ID),)), bindings)
        self.commands = (self.definition,)

    def bind(self, storage: PersistenceService, configuration: StoredInformationConfiguration | StoredTextConfiguration | StoredSemanticConfiguration | StoredCognitionConfiguration, instance_id: str,
             owners: dict[str, InitializableOwner], configuration_check: Callable[[UnitOfWork], bool]) -> None:
        if self._configuration is not None or set(owners) != {'retrieval', 'state', 'goals', 'memory'}:
            raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        self._configuration, self._instance = configuration, instance_id
        self._owners = dict(owners)
        self._configuration_check = configuration_check
        self._operation = storage.bind_operation(self.definition, instance_id)

    def _handle(self, uow: UnitOfWork, values: Record) -> Record:
        from companion_memory.persistence.content_codec import decode_content
        if self._configuration is None or self._configuration_check is None or not self._configuration_check(uow):
            raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        payload = checked(RecordSchema((Field('database_id', ID), Field('instance_id', ID), Field('config_snapshot_id', ID))),
                          decode_content(text(values['payload']).encode(), 1024), 1024)
        if (values['binding_id'] != self._instance or payload['database_id'] != self._configuration.database_id
                or payload['instance_id'] != self._instance or payload['config_snapshot_id'] != self._configuration.snapshot_id):
            raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        facts = {name: owner.initialize(uow, integer(values['observed_at'])) for name, owner in self._owners.items()}
        return MappingProxyType({'outcome': 'INITIALIZED', 'targets': (MappingProxyType({'object_id': self._instance, 'previous_revision': None, 'revision': 1}),),
                                 'items': (), 'facts': MappingProxyType(facts)})

    def command(self, key: str, at_us: int) -> ResultBoundCommand:
        from companion_memory.persistence.content_codec import encode_content
        if self._configuration is None:
            raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
        payload = encode_content(MappingProxyType({'database_id': self._configuration.database_id, 'instance_id': self._instance,
                                  'config_snapshot_id': self._configuration.snapshot_id}), 1024).decode()
        return ResultBoundCommand(1, {'binding_id': self._instance, 'request_key': key, 'expected': (), 'observed_at': at_us, 'payload': payload},
            {a.event_slot: {'actor': 'information_host'} for a in self.definition.required_audits})

    async def initialize(self, key: str, at_us: int) -> Committed | NotCommitted | Rejected | Unconfirmed:
        """Execute a retained original command; no recovery is inferred from absence."""
        return await self._operation.execute(key, self.command(key, at_us))

    async def recover(self, key: str) -> Committed:
        """Verify the persisted receipt and all owner roots, without executing writes."""
        found = await self._operation.read_receipt(key)
        if type(found) is not Found:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        receipt = found.value
        result = record(receipt.result)
        facts = record(result['facts'])
        at_us = integer(record(facts['goals'])['at_us'])
        handle = self._operation.recovery_handle(key, self.command(key, at_us))
        if type(handle) is not RecoveryHandle or handle.fingerprint != receipt.fingerprint:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        for name in ('memory', 'state', 'goals', 'retrieval'):
            await self._owners[name].recover(record(facts[name]))
        return Committed(receipt, 'EXISTING')
