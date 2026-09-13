"""Common immutable identity and fixed declarations for bounded owner records.

This module issues no business capabilities or transactions. Owners select their
closed schemas and static point-read indexes before storage opens. Complete JSON
bodies are always checked against physical identity and record capacity.
"""
from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Literal, cast
from .schema import BoundedTextSchema, Field, RecordSchema, ScalarSchema, Value, freeze_value, InvalidValue
from .content_codec import encode_content, decode_content
from .definitions import TableDefinition, StatementDefinition, RepositoryDefinition
from .owned_statements import StatementCatalog

ID = ScalarSchema('identifier')
UINT = ScalarSchema('integer', 0, 2**63-1)
REVISION = ScalarSchema('integer', 1, 2**63-1)
VERSION = ScalarSchema('integer', 1, 1)
DIGEST = BoundedTextSchema(64)
OPERATION = RecordSchema(tuple(Field(name, ID) for name in ('owner_namespace', 'operation_kind', 'scope_id', 'operation_key')))
BASE = (Field('format_version', VERSION), Field('object_id', ID), Field('revision', REVISION),
    Field('database_id', ID), Field('instance_id', ID), Field('config_snapshot_id', ID), Field('created_at_us', UINT))
type IdentityKind = Literal['self-input', 'persona-run', 'persona-candidate', 'persona-publication', 'learning-context', 'context-leaf']


def stable_identity(kind: IdentityKind, database_id: str, instance_id: str, *parts: Value) -> str:
    """Derive owner IDs from the real database, instance and retained semantics."""
    if kind not in ('self-input', 'persona-run', 'persona-candidate', 'persona-publication', 'learning-context', 'context-leaf'):
        raise InvalidValue()
    freeze_value(ID, database_id); freeze_value(ID, instance_id)
    return kind + ':' + hashlib.sha256(encode_content((kind, database_id, instance_id, *parts), 8192)).hexdigest()


def digest(value: Value) -> str:
    """Hash an already frozen semantic value without volatile delivery controls."""
    return hashlib.sha256(encode_content(value, 131072)).hexdigest()


def isolate_record(schema: RecordSchema, value: object, maximum: int) -> MappingProxyType[str, Value]:
    """Own the complete record, including byte limits and digest syntax."""
    isolated = cast(MappingProxyType[str, Value], freeze_value(schema, value, owned=True))
    encode_content(isolated, maximum)
    def inspect(item: Value):
        if type(item) is MappingProxyType:
            for name, child in item.items():
                if (name.endswith('_digest') or name == 'digest') and child is not None:
                    if type(child) is not str or re.fullmatch('[0-9a-f]{64}', child) is None:
                        raise InvalidValue()
                inspect(child)
        elif type(item) is tuple:
            for child in item: inspect(child)
    inspect(isolated)
    return isolated


def decode_row(raw: MappingProxyType[str, Value], schema: RecordSchema, maximum: int,
               database_id: str, instance_id: str, config_snapshot_id: str) -> MappingProxyType[str, Value]:
    """Read one canonical body only after physical and persistent identities agree."""
    body = raw['body']
    if type(body) is not str: raise InvalidValue()
    value = isolate_record(schema, decode_content(body.encode('utf-8'), maximum), maximum)
    if (encode_content(value, maximum).decode('utf-8') != body or value['object_id'] != raw['object_id']
            or value['revision'] != raw['revision'] or value['database_id'] != database_id
            or value['instance_id'] != instance_id or value['config_snapshot_id'] != config_snapshot_id):
        raise InvalidValue()
    return value


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """A trusted fixed JSON-field index and its optional corresponding point read."""
    name: str
    fields: tuple[str, ...]
    unique: bool = True
    nonnull: str | None = None
    point_read: bool = True
    identity_only: bool = False


@dataclass(frozen=True, slots=True)
class RecordTable:
    """Finite owner declaration; no table or field names come from a work port."""
    name: str
    maximum: int
    mutable: bool
    indexes: tuple[IndexSpec, ...]
    delete_by: str | None = None


def record_catalog(owner: str, version: int, records: tuple[RecordTable, ...]) -> StatementCatalog:
    """Build fixed four-column tables and only their declared bounded operations."""
    tables: list[TableDefinition] = []
    statements: list[tuple[str, StatementDefinition]] = []
    for spec in records:
        table = owner + '_' + spec.name
        if re.fullmatch('[a-z][a-z0-9_]*', table) is None: raise InvalidValue()
        columns = 'object_id,revision,body'
        row = RecordSchema((Field('object_id', ID), Field('revision', REVISION), Field('body', BoundedTextSchema(spec.maximum))))
        key = RecordSchema((Field('object_id', ID),))
        table_sql = (f'CREATE TABLE {table} (scope_id TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>=1),'
            f'body TEXT NOT NULL CHECK(length(CAST(body AS BLOB))<={spec.maximum}),PRIMARY KEY(scope_id,object_id),'
            "CHECK((json_valid(body) AND json_extract(body,'$.object_id')=object_id AND json_type(body,'$.revision')='integer' "
            "AND json_extract(body,'$.revision')=revision) IS TRUE))")
        tables.append(TableDefinition(table, table_sql))
        def add(suffix: str, sql: str, parameters: RecordSchema, output: RecordSchema, writes: bool):
            statements.append((spec.name+'_'+suffix, StatementDefinition(sql, parameters, output, writes)))
        add('get', f'SELECT {columns} FROM {table} WHERE scope_id=:scope_id AND object_id=:object_id', key, row, False)
        add('insert', f'INSERT INTO {table}(scope_id,object_id,revision,body) VALUES(:scope_id,:object_id,:revision,:body) RETURNING {columns}', row, row, True)
        add('recovery_page', f'SELECT {columns} FROM {table} WHERE scope_id=:scope_id AND object_id>:after ORDER BY object_id LIMIT :limit',
            RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer',1,4)))), row, False)
        if spec.mutable:
            add('cas', f'UPDATE {table} SET revision=:revision,body=:body WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:expected_revision RETURNING {columns}',
                RecordSchema(row.fields+(Field('expected_revision',REVISION),)), row, True)
        for index in spec.indexes:
            for name in index.fields + ((index.nonnull,) if index.nonnull is not None else ()):
                if name != 'scope_id' and re.fullmatch(r'[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?',name) is None: raise InvalidValue()
            def expression(name):
                return 'scope_id' if name == 'scope_id' else f"json_extract(body,'$.{name}')"
            predicate = f" WHERE {expression(index.nonnull)} IS NOT NULL" if index.nonnull is not None else ''
            tables.append(TableDefinition(table+'_'+index.name, f'CREATE {"UNIQUE " if index.unique else ""}INDEX {table}_{index.name} ON {table}(scope_id'+
                ''.join(','+expression(name) for name in index.fields if name != 'scope_id')+')'+predicate))
            if index.point_read:
                fields = tuple(name for name in index.fields if name != 'scope_id')
                params = tuple(Field(name.replace('.','_'),REVISION if name=='generation' else ID) for name in fields)
                where = ' AND '.join(expression(name)+'=:'+name.replace('.','_') for name in fields)
                selected = 'object_id' if index.identity_only else columns
                add(index.name, f'SELECT {selected} FROM {table} WHERE scope_id=:scope_id'+(' AND '+where if where else '')+' ORDER BY object_id LIMIT 8', RecordSchema(params), key if index.identity_only else row, False)
        if spec.delete_by is not None:
            field = spec.delete_by
            if re.fullmatch('[a-z][a-z0-9_]*',field) is None:raise InvalidValue()
            add('delete_by_'+field, f"DELETE FROM {table} WHERE scope_id=:scope_id AND json_extract(body,'$.{field}')=:{field} RETURNING object_id",
                RecordSchema((Field(field,ID),)), key, True)
    return StatementCatalog(RepositoryDefinition(owner,version,tuple(tables),tuple(s for _,s in statements)),tuple(statements))


def extend_catalog(original: StatementCatalog, addition: StatementCatalog, version: int) -> StatementCatalog:
    """Preserve original declarations while extending one explicit owner format."""
    if original.definition.owner_module != addition.definition.owner_module:
        raise InvalidValue()
    statements = original.statements + addition.statements
    if len({name for name,_ in statements}) != len(statements): raise InvalidValue()
    return StatementCatalog(RepositoryDefinition(original.definition.owner_module,version,
        original.definition.tables+addition.definition.tables,tuple(s for _,s in statements)),statements)
