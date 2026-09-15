"""Static owner-local semantic tables with physical unique and reference checks.

Every complete body schema is attached to its table declaration and contributes
to the assembly signature. Bound records validate bodies before and after SQL;
there is no dynamic table, free SQL or cross-owner read interface at runtime.
"""
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from .definitions import TableDefinition,StatementDefinition,RepositoryDefinition
from .schema import RecordSchema,Field,BoundedTextSchema,InvalidValue,Value
from .semantic_records import ID,P,N,Record,isolate,number,string,integer,record
from .content_codec import encode_content,decode_content
from .owned_statements import StatementCatalog,BoundStatements,OwnerFailure
from .service import PersistenceService,UnitOfWork

BODY_ROW = record(row_id=ID,revision=P,body=BoundedTextSchema(8192))
KEY = record(row_id=ID)
PAGE_INPUT = record(after=BoundedTextSchema(128),limit=integer(1,8))


@dataclass(frozen=True,slots=True)
class SemanticTable:
    """One owner's finite table with exact body variants and physical relations."""
    name: str
    schemas: tuple[RecordSchema,...]
    validate: Callable[[object],Record]
    projections: tuple[tuple[str,str,str],...]
    constraints: tuple[str,...]
    mutable: bool = True
    removable: bool = False


def catalog(owner: str, tables: tuple[SemanticTable,...], *, version: int = 1) -> StatementCatalog:
    """Build only trusted startup declarations, with no caller-supplied SQL."""
    declared=[];statements=[]
    for spec in tables:
        name=owner+'_'+spec.name
        columns=['scope_id TEXT NOT NULL','row_id TEXT NOT NULL','revision INTEGER NOT NULL CHECK(revision>0)',
            'body TEXT NOT NULL CHECK(length(CAST(body AS BLOB))<=8192)']
        columns.extend(f"{column} {kind} GENERATED ALWAYS AS ({expression}) STORED" for column,kind,expression in spec.projections)
        columns.extend(('PRIMARY KEY(scope_id,row_id)',
            "CHECK((json_valid(body) AND json_type(body,'$.v')='integer' AND json_extract(body,'$.v')=1 AND json_extract(body,'$.row_id')=row_id AND json_type(body,'$.revision')='integer' AND json_extract(body,'$.revision')=revision) IS TRUE)",*spec.constraints))
        declared.append(TableDefinition(name,'CREATE TABLE '+name+' ('+','.join(columns)+')',spec.schemas))
        prefix=spec.name+'_'
        statements.extend((
            (prefix+'get',StatementDefinition(f'SELECT row_id,revision,body FROM {name} WHERE scope_id=:scope_id AND row_id=:row_id',KEY,BODY_ROW,False)),
            (prefix+'insert',StatementDefinition(f'INSERT INTO {name}(scope_id,row_id,revision,body) VALUES(:scope_id,:row_id,:revision,:body) RETURNING row_id,revision,body',BODY_ROW,BODY_ROW,True)),
            (prefix+'page',StatementDefinition(f'SELECT row_id,revision,body FROM {name} WHERE scope_id=:scope_id AND row_id>:after ORDER BY row_id LIMIT :limit',PAGE_INPUT,BODY_ROW,False)),
            (prefix+'count',StatementDefinition(f'SELECT count(*) AS count FROM {name} WHERE scope_id=:scope_id',record(),record(count=N),False)),
        ))
        if spec.mutable:
            parameters=RecordSchema(BODY_ROW.fields+(Field('expected_revision',P),))
            statements.append((prefix+'update',StatementDefinition(f'UPDATE {name} SET revision=:revision,body=:body WHERE scope_id=:scope_id AND row_id=:row_id AND revision=:expected_revision RETURNING row_id,revision,body',parameters,BODY_ROW,True)))
        if spec.removable:
            statements.append((prefix+'delete',StatementDefinition(f'DELETE FROM {name} WHERE scope_id=:scope_id AND row_id=:row_id AND revision=:expected_revision RETURNING row_id,revision,body',record(row_id=ID,expected_revision=P),BODY_ROW,True)))
    return StatementCatalog(RepositoryDefinition(owner,version,tuple(declared),tuple(s for _,s in statements)),tuple(statements))


def projection(name: str, path: str | None = None, *, numeric: bool = False) -> tuple[str,str,str]:
    return name,'INTEGER' if numeric else 'TEXT',"json_extract(body,'$."+(path or name)+"')"


class SemanticRecords:
    """An owner's private validated point and page statements, bound to one scope."""
    def __init__(self, declaration: StatementCatalog, tables: tuple[SemanticTable,...], storage: PersistenceService, scope: str):
        self.statements=BoundStatements(declaration,storage,scope)
        self._tables={table.name:table for table in tables}

    def decode(self,name: str,row: Record) -> Record:
        body=string(row['body']);value=self._tables[name].validate(decode_content(body.encode('utf-8'),8192))
        if value['row_id']!=row['row_id'] or value['revision']!=row['revision'] or encode_content(value,8192).decode('utf-8')!=body:
            raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        return value

    def get(self,name: str,uow: UnitOfWork,row_id: str) -> Record | None:
        found=self.statements.stage(name+'_get',uow,{'row_id':row_id})
        return self.decode(name,found[0]) if found else None

    async def read(self,name: str,row_id: str) -> Record | None:
        found=await self.statements.read(name+'_get',{'row_id':row_id})
        return self.decode(name,found[0]) if found else None

    async def page(self,name: str,after: str='',limit: int=8) -> tuple[Record,...]:
        found=await self.statements.read(name+'_page',{'after':after,'limit':limit})
        return tuple(self.decode(name,row) for row in found)

    def write(self,name: str,uow: UnitOfWork,value: object,*,expected_revision: int | None=None) -> Record:
        checked=self._tables[name].validate(value)
        parameters: dict[str,Value]={'row_id':checked['row_id'],'revision':checked['revision'],'body':encode_content(checked,8192).decode()}
        if expected_revision is not None:
            parameters['expected_revision']=expected_revision
            if number(checked['revision'])!=expected_revision+1 and name not in ('semantic_gap','semantic_ack'):
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        found=self.statements.stage(name+('_insert' if expected_revision is None else '_update'),uow,parameters)
        if len(found)!=1: raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        return self.decode(name,found[0])

    def remove(self,name: str,uow: UnitOfWork,row_id: str,expected_revision: int) -> Record:
        found=self.statements.stage(name+'_delete',uow,{'row_id':row_id,'expected_revision':expected_revision})
        if len(found)!=1: raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        return self.decode(name,found[0])
