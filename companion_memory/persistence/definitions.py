"""Trusted static repository statements and complete local command definitions.

Only startup assembly handles SQL declarations. Application and module callers
receive bound opaque ports, never a dynamic SQL or table-selection interface.
Handlers run once, synchronously, with local bounded work inside one UoW.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from companion_memory.logging_service.audit_records import AuditRequirement
from .schema import RecordSchema, Value

if TYPE_CHECKING:
    from .service import UnitOfWork


@dataclass(frozen=True, slots=True, eq=False)
class TableDefinition:
    """A module-owned table/index statement for explicit new-database assembly."""

    name: str
    sql: str


@dataclass(frozen=True, slots=True, eq=False)
class StatementDefinition:
    """One finite typed operation; SQL receives an injected immutable scope_id."""

    sql: str
    parameters: RecordSchema
    row_schema: RecordSchema
    writes: bool


@dataclass(frozen=True, slots=True, eq=False)
class RepositoryDefinition:
    """A module's fixed schema and allowed typed operations, issued as capabilities."""

    owner_module: str
    schema_version: int
    tables: tuple[TableDefinition, ...]
    statements: tuple[StatementDefinition, ...]


@dataclass(frozen=True, slots=True, eq=False)
class CommandDefinition:
    """One complete command and its compulsory participants, result and audits.

    The handler must enforce business conditions before filling its audit slots.
    IDs, expected revisions and policy choices belong in the typed input. Every
    audit envelope is frozen with that input before ownership or SQL begins.
    """

    owner_namespace: str
    operation_kind: str
    command_version: int
    input_schema: RecordSchema
    result_schema_version: int
    result_schema: RecordSchema
    participants: tuple[RepositoryDefinition, ...]
    required_audits: tuple[AuditRequirement, ...]
    handler: Callable[[UnitOfWork, MappingProxyType[str, Value]], object]


@dataclass(frozen=True, slots=True)
class LocalCommand:
    """Exact input carrier. Both values and slot-keyed event bodies must be stable."""

    version: int
    values: object
    audit_events: object
