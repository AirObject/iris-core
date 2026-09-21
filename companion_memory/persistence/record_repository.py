"""Finite declaration helpers used privately by separate information owners.

Trusted startup layouts produce deterministic SQL with typed parameters. The
bound helper verifies canonical bodies against indexed columns; it exposes no
SQL construction or arbitrary statement registration after initialization.
"""
from dataclasses import dataclass
from types import MappingProxyType
from companion_memory.persistence import (Field, RecordSchema, ScalarSchema, BoundedTextSchema,
    RepositoryDefinition, StatementDefinition, TableDefinition, PersistenceService, UnitOfWork, Value)
from companion_memory.persistence.owned_statements import StatementCatalog, BoundStatements, OwnerFailure
from companion_memory.persistence.content_codec import encode_content
from .record_primitives import Record, ID, COUNT, REVISION, checked, decode, text


def valid_digests(value: Record) -> bool:
    """Hash fields in the closed owner records always hold full lowercase SHA256."""
    for name in ('response_digest', 'intent_digest', 'request_digest', 'body_digest'):
        if name in value:
            digest = value[name]
            if type(digest) is not str or len(digest) != 64 or any(character not in '0123456789abcdef' for character in digest): return False
    return True


@dataclass(frozen=True, slots=True)
class Layout:
    """Owner-supplied closed body with fixed indexed projections and constraints."""
    name: str
    schema: RecordSchema
    limit: int
    columns: tuple[Field, ...]
    keys: tuple[str, ...]
    constraints: str = ''


def declarations(owner: str, version: int, layouts: tuple[Layout, ...],
                 indices: tuple[TableDefinition, ...] = (),
                 extra: tuple[tuple[str, StatementDefinition], ...] = ()) -> StatementCatalog:
    """Build static capabilities solely from the owner's fixed startup constants."""
    tables: list[TableDefinition] = []
    statements: list[tuple[str, StatementDefinition]] = []
    for layout in layouts:
        table = owner + '_' + layout.name
        fields = {f.name: f for f in layout.columns}
        columns = ','.join(fields)
        column_sql = ','.join(f.name + (' INTEGER' if type(f.schema) is ScalarSchema and f.schema.kind == 'integer' else ' TEXT')
            + ('' if f.nullable else ' NOT NULL') for f in layout.columns)
        ddl = 'CREATE TABLE ' + table + ' (scope_id TEXT NOT NULL,' + column_sql + ',body TEXT NOT NULL,PRIMARY KEY(scope_id,' + ','.join(layout.keys) + ')' + layout.constraints + ')'
        if len(ddl.encode('ascii')) > 640:
            raise ValueError('Fixed information DDL exceeds its complete SQL budget.')
        tables.append(TableDefinition(table, ddl))
        result = RecordSchema(layout.columns + (Field('body', BoundedTextSchema(layout.limit)),))
        keys = RecordSchema(tuple(fields[k] for k in layout.keys))
        where = 'scope_id=:scope_id AND ' + ' AND '.join(k + '=:' + k for k in layout.keys)
        names = columns + ',body'
        params = result
        statements.extend((
            (layout.name + '_get', StatementDefinition('SELECT ' + names + ' FROM ' + table + ' WHERE ' + where, keys, result, False)),
            (layout.name + '_insert', StatementDefinition('INSERT INTO ' + table + ' VALUES(:scope_id,' + ','.join(':' + f.name for f in layout.columns) + ',:body) RETURNING ' + names, params, result, True)),
            (layout.name + '_delete', StatementDefinition('DELETE FROM ' + table + ' WHERE ' + where + ' RETURNING ' + names, keys, result, True)),
            (layout.name + '_count', StatementDefinition('SELECT count(*) AS count FROM ' + table + ' WHERE scope_id=:scope_id', RecordSchema(()), RecordSchema((Field('count', COUNT),)), False)),
        ))
        if 'revision' in fields:
            update = ','.join(f.name + '=:' + f.name for f in layout.columns if f.name not in layout.keys)
            if update:
                update += ','
            statements.append((layout.name + '_update', StatementDefinition('UPDATE ' + table + ' SET ' + update + 'body=:body WHERE ' + where + ' AND revision=:expected_revision RETURNING ' + names,
                RecordSchema(params.fields + (Field('expected_revision', REVISION),)), result, True)))
    tables.extend(indices)
    statements.extend(extra)
    return StatementCatalog(RepositoryDefinition(owner, version, tuple(tables), tuple(s for _, s in statements)), tuple(statements))


class OwnedRecords:
    """Private owner storage adapter; callers outside that owner receive its ports."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService, instance_id: str, layouts: tuple[Layout, ...]):
        self.rows = BoundStatements(catalog, storage, instance_id)
        self.layouts = {layout.name: layout for layout in layouts}

    def unpack(self, name: str, row: Record) -> Record:
        layout = self.layouts[name]
        result = decode(layout.schema, text(row['body']), layout.limit)
        if not valid_digests(result):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if any(field.name in result and row[field.name] != result[field.name] for field in layout.columns):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return result

    def get(self, name: str, uow: UnitOfWork, keys: dict[str, Value]) -> Record | None:
        rows = self.rows.stage(name + '_get', uow, keys)
        return self.unpack(name, rows[0]) if rows else None

    async def read(self, name: str, keys: dict[str, Value]) -> Record | None:
        rows = await self.rows.read(name + '_get', keys)
        return self.unpack(name, rows[0]) if rows else None

    def write(self, name: str, uow: UnitOfWork, value: object, *, expected_revision: int | None = None,
              projections: dict[str, Value] | None = None) -> Record:
        layout = self.layouts[name]
        result = checked(layout.schema, value, layout.limit)
        if not valid_digests(result):
            raise OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE')
        fields = {f.name: result[f.name] for f in layout.columns if f.name in result}
        fields.update(projections or {})
        fields['body'] = encode_content(result, layout.limit).decode('utf-8')
        operation = 'insert' if expected_revision is None else 'update'
        if expected_revision is not None:
            fields['expected_revision'] = expected_revision
        if len(self.rows.stage(name + '_' + operation, uow, fields)) != 1:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        return result

    def remove(self, name: str, uow: UnitOfWork, keys: dict[str, Value]) -> Record | None:
        rows = self.rows.stage(name + '_delete', uow, keys)
        return self.unpack(name, rows[0]) if rows else None
