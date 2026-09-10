"""Trusted persistence assembly and restricted typed transaction/recovery ports.

Create explicit repository/command schemas, then initialize with an immutable
configuration snapshot and retained database identity. Only Committed confirms
success. No production storage layout, business tables or model calls are added.
"""

from .definitions import CommandDefinition, LocalCommand, RepositoryDefinition, StatementDefinition, TableDefinition
from .resources import ConnectionFactory, DatabaseResources
from .results import (
    CloseReport, Committed, ExecutionResult, Failed, Found, Health, InitializationResult,
    InitializationUnconfirmed, NotCommitted, NotFound, OperationIdentity, PersistenceError,
    ReadResult, Ready, Receipt, RecoveryHandle, Rejected, Staged, Unconfirmed,
)
from .schema import BoundedTextSchema, Field, RecordSchema, ScalarSchema, SequenceSchema, Value
from .service import AuditStorageBinding, OperationPort, PersistenceService, StatementPort, UnitOfWork

__all__ = [
    "BoundedTextSchema", "AuditStorageBinding", "CloseReport", "CommandDefinition", "Committed", "ConnectionFactory", "DatabaseResources",
    "ExecutionResult", "Failed", "Field", "Found", "Health", "InitializationResult", "InitializationUnconfirmed",
    "LocalCommand", "NotCommitted", "NotFound", "OperationIdentity", "OperationPort", "PersistenceError",
    "PersistenceService", "ReadResult", "Ready", "Receipt", "RecordSchema", "RecoveryHandle", "Rejected",
    "RepositoryDefinition", "ScalarSchema", "SequenceSchema", "Staged", "StatementDefinition", "StatementPort",
    "TableDefinition", "Unconfirmed", "UnitOfWork", "Value",
]

from .ownership import ModuleOwnerLease
from .provider_repository import ProviderRepository, create_provider_repository

__all__ += ["ModuleOwnerLease", "ProviderRepository", "create_provider_repository"]
