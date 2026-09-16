"""Shared closed identity for daily owner records and their physical catalogs.

Owners declare all bodies before opening storage. Complete canonical bodies,
identity columns and original operations are checked on every point read and
write; these helpers issue neither a business permission nor an operation port.
"""
from dataclasses import dataclass,replace
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from .schema import Field,RecordSchema,BoundedTextSchema,ScalarSchema,InvalidValue,Value
from .text_records import BASE as ORIGINAL_BASE,OPERATION,ID,UINT,REVISION,DIGEST,RecordTable,IndexSpec,record_catalog,isolate_record
from .content_codec import encode_content,decode_content
from .owned_statements import StatementCatalog,BoundStatements,OwnerFailure
from .service import PersistenceService,UnitOfWork

BASE=ORIGINAL_BASE+(Field('updated_at_us',UINT),)
type Record=MappingProxyType[str,Value]

def enum(*values: str) -> ScalarSchema:
    return ScalarSchema('enum',choices=values)

def identity(kind: str,database: str,instance: str,*parts: Value) -> str:
    """Retain semantic identity; volatile time and compressed leaf order stay out."""
    for value in (kind,database,instance):
        if type(value) is not str:raise InvalidValue()
    return kind+':'+sha256(encode_content((database,instance,*parts),262144)).hexdigest()

@dataclass(frozen=True,slots=True)
class DailyTable:
    """One trusted table with finite closed variants and explicit byte capacity."""
    name: str
    schemas: tuple[RecordSchema,...]
    maximum: int
    mutable: bool
    indexes: tuple[IndexSpec,...]=()

    def isolate(self,value: object) -> Record:
        for schema in self.schemas:
            try:
                result=isolate_record(schema,value,self.maximum)
            except InvalidValue:
                continue
            if cast(int,result['updated_at_us'])<cast(int,result['created_at_us']):raise InvalidValue()
            return result
        raise InvalidValue()


def daily_catalog(owner: str,version: int,tables: tuple[DailyTable,...]) -> StatementCatalog:
    """Attach every complete body schema to the immutable assembly signature."""
    base=record_catalog(owner,version,tuple(RecordTable(t.name,t.maximum,t.mutable,t.indexes) for t in tables))
    by_name={owner+'_'+t.name:t for t in tables}
    definitions=tuple(replace(t,record_schemas=by_name[t.name].schemas) if t.name in by_name else t for t in base.definition.tables)
    return StatementCatalog(replace(base.definition,tables=definitions),base.statements)


class DailyRows:
    """Owner-local bounded statements tied to one native durable configuration."""
    def __init__(self,catalog: StatementCatalog,tables: tuple[DailyTable,...],storage: PersistenceService,
                 database: str,instance: str,snapshot: str):
        self.rows=BoundStatements(catalog,storage,instance)
        self.tables={table.name:table for table in tables}
        self.database=database;self.instance=instance;self.snapshot=snapshot

    def decode(self,name: str,row: Record) -> Record:
        try:
            spec=self.tables[name];body=cast(str,row['body'])
            result=spec.isolate(decode_content(body.encode(),spec.maximum))
            if (result['object_id']!=row['object_id'] or result['revision']!=row['revision']
                    or result['database_id']!=self.database or result['instance_id']!=self.instance
                    or result['config_snapshot_id']!=self.snapshot or encode_content(result,spec.maximum).decode()!=body):raise InvalidValue()
            return result
        except (InvalidValue,ValueError,TypeError,KeyError):
            raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE') from None

    def get(self,name: str,uow: UnitOfWork,object_id: str) -> Record|None:
        values=self.rows.stage(name+'_get',uow,{'object_id':object_id})
        return self.decode(name,values[0]) if values else None

    async def read(self,name: str,object_id: str) -> Record|None:
        values=await self.rows.read(name+'_get',{'object_id':object_id})
        return self.decode(name,values[0]) if values else None

    async def page(self,name: str,after: str='') -> tuple[Record,...]:
        values=await self.rows.read(name+'_recovery_page',{'after':after,'limit':4})
        return tuple(self.decode(name,row) for row in values)

    def write(self,name: str,uow: UnitOfWork,value: object,expected: int|None=None) -> Record:
        spec=self.tables[name];result=spec.isolate(value)
        if (result['database_id']!=self.database or result['instance_id']!=self.instance or result['config_snapshot_id']!=self.snapshot
                or result['revision']!=(1 if expected is None else expected+1)):raise InvalidValue()
        args={'object_id':result['object_id'],'revision':result['revision'],'body':encode_content(result,spec.maximum).decode()}
        if expected is not None:args['expected_revision']=expected
        changed=self.rows.stage(name+('_insert' if expected is None else '_cas'),uow,args)
        if len(changed)!=1:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        return self.decode(name,changed[0])
