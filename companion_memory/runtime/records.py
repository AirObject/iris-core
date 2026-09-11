"""Finite versioned domain records and module-owned transaction participation.

Each binding contains only its owner's statically registered ports. Safe receipt
results and original payload text remain separate from durable workflow records.
"""
from datetime import datetime,timedelta,timezone
import hashlib
import json
from types import MappingProxyType
from typing import cast

from companion_memory.persistence import Failed,Found,NotFound,PersistenceService,Staged,UnitOfWork,Value
from companion_memory.persistence.runtime_repositories import RuntimeRepository
from companion_memory.persistence.schema import InvalidValue


def json_text(value: object) -> str:
    def ordinary(value):
        if type(value) is MappingProxyType or type(value) is dict:return {k:ordinary(v) for k,v in value.items()}
        if type(value) is tuple or type(value) is list:return [ordinary(v) for v in value]
        if value is None or type(value) in (str,int,bool):return value
        raise InvalidValue()
    return json.dumps(ordinary(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def stable_id(kind: str,*parts: object) -> str:
    return kind+':'+digest(parts)


def record(value: Value) -> MappingProxyType[str,Value]:
    if type(value) is not MappingProxyType:raise InvalidValue()
    return value


def rows(value: Value) -> tuple[MappingProxyType[str,Value],...]:
    if type(value) is not tuple:raise InvalidValue()
    return tuple(record(v) for v in value)


class DomainFailure(Exception):
    """Internal fixed cause; no original data crosses the domain's error boundary."""
    def __init__(self,code:str,field:str,reason:str,cleanup_pending:bool=False):
        self.code,self.field,self.reason=code,field,reason
        self.cleanup_pending=cleanup_pending
        super().__init__()


class OwnedRows:
    """One owner's finite repository operations, scoped to a trusted instance."""
    def __init__(self,repository:RuntimeRepository,storage:PersistenceService,instance_id:str):
        self.owner=repository.definition.owner_module
        self._ports={name:storage.bind_statement(repository.definition,s,instance_id) for name,s in repository.statements}

    def participate(self,name:str,uow:UnitOfWork,parameters:dict[str,object]) -> tuple[MappingProxyType[str,Value],...]:
        result=self._ports[name].participate(uow,parameters)
        if type(result) is not Staged:
            raise DomainFailure('STORAGE_FAILED','storage','WRITE_NOT_COMMITTED')
        return rows(result.value)

    async def read(self,name:str,parameters:dict[str,object]) -> tuple[MappingProxyType[str,Value],...]:
        from .operation_wait import expired
        if expired():raise DomainFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        result=await self._ports[name].read_object(parameters)
        if type(result) is NotFound:
            return ()
        if type(result) is not Found:
            if type(result) is Failed and result.error.reason=='LIMIT_EXCEEDED':raise DomainFailure('INVALID_INPUT','query','LIMIT_EXCEEDED')
            raise DomainFailure('STORAGE_FAILED','storage','READ_FAILED',type(result) is Failed and result.error.cleanup_pending)
        return rows(result.value)

    def get(self,table:str,key:str,uow:UnitOfWork) -> dict[str,object] | None:
        found=self.participate(table+'_get',uow,{'object_id':key})
        return self.decode(found[0],table) if found else None

    async def load(self,table:str,key:str) -> dict[str,object] | None:
        found=await self.read(table+'_get',{'object_id':key})
        return self.decode(found[0],table) if found else None

    def decode(self,row:MappingProxyType[str,Value],table:str | None=None) -> dict[str,object]:
        def unique(pairs):
            result={}
            for k,v in pairs:
                if k in result:raise InvalidValue()
                result[k]=v
            return result
        try:
            raw=json.loads(cast(str,row['body']),object_pairs_hook=unique)
            if type(raw) is not dict or set(raw)!= {'version','data','digest'} or type(raw['version']) is not int or raw['version']!=1 or type(raw['data']) is not dict or raw['digest']!=digest(raw['data']):
                raise InvalidValue()
            if table is not None:
                from .domain_schemas import validate_body
                validate_body(self.owner,table,raw['data'],row['state'])
                for key in ('received_at_us','created_at_us','range_start_us','range_end_us','earliest_received_at_us','latest_received_at_us'):
                    if key in raw['data'] and raw['data'][key] is not None:stored_timestamp(raw['data'][key])
        except (InvalidValue,ValueError,TypeError,KeyError,OverflowError,RecursionError):
            raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE') from None
        return {**dict(row),'data':raw['data']}

    def insert(self,table:str,uow:UnitOfWork,key:str,entry_id:str,sequence:int,state:str,data:dict[str,object]) -> dict[str,object]:
        body=json_text({'version':1,'data':data,'digest':digest(data)})
        result=self.participate(table+'_insert',uow,{'object_id':key,'entry_id':entry_id,'sequence':sequence,'state':state,'revision':1,'body':body})
        if len(result)!=1:raise DomainFailure('PRECONDITION_FAILED','state','REVISION_CHANGED')
        return self.decode(result[0],table)

    def update(self,table:str,uow:UnitOfWork,previous:dict[str,object],state:str,data:dict[str,object]) -> dict[str,object]:
        body=json_text({'version':1,'data':data,'digest':digest(data)})
        result=self.participate(table+'_update',uow,{'object_id':previous['object_id'],'expected_revision':previous['revision'],'revision':cast(int,previous['revision'])+1,'state':state,'body':body})
        if len(result)!=1:raise DomainFailure('PRECONDITION_FAILED','state','REVISION_CHANGED')
        return self.decode(result[0],table)

    def delete(self,table:str,uow:UnitOfWork,previous:dict[str,object]) -> None:
        result=self.participate(table+'_delete',uow,{'object_id':previous['object_id'],'expected_revision':previous['revision']})
        if len(result)!=1:raise DomainFailure('PRECONDITION_FAILED','state','REVISION_CHANGED')


def data(row:dict[str,object]) -> dict[str,object]:
    value=row.get('data')
    if type(value) is not dict:raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
    return value


def stored_timestamp(micros:object) -> str:
    """Render representable durable UTC microseconds without float rounding."""
    if type(micros) is not int or not 0<=micros<=253402300799999999:
        raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
    return (datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(microseconds=micros)).isoformat(timespec='microseconds').replace('+00:00','Z')


def stored_event(text:object):
    """Invalid persisted event bytes are integrity failures, never new input."""
    from companion_memory.ingress.events import decode_event
    try:
        if type(text) is not str:raise InvalidValue()
        return decode_event(text.encode(),8192)
    except (InvalidValue,ValueError,TypeError,RecursionError):
        raise DomainFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE') from None
