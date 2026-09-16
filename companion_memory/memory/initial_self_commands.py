"""Audited initial SELF registration through one native local management scope.

Input and SELF are published together with result-bound memory facts and the
original receipt. Confirming a retained key never re-enters the owner handler.
Only the trusted binding supplies actor, SELF identity and fixture provenance.
"""
from __future__ import annotations
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
import time
from typing import cast
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand,Field,RecordSchema,BoundedTextSchema,ScalarSchema,UnitOfWork,Value
from companion_memory.persistence.schema import InvalidValue,valid_identifier
from companion_memory.persistence.text_records import ID
from companion_memory.persistence.text_results import result_schema,audits,INTENT,result
from companion_memory.persistence.owned_statements import StatementCatalog
from companion_memory.persistence.completion import finish_owned,retain_completion
from companion_memory.persistence.owned_statements import OwnerFailure
from .initial_self_storage import InitialSelfStorage,InitialSelfBinding
from .transactions import MemoryTransactions


@dataclass(frozen=True,slots=True,init=False)
class InitialSelfPort:
    """Local initialization capability; it grants no persona or ordinary model work."""
    _owner: InitialSelfCommands

    def __init__(self):raise TypeError('Initial management authority is issued by trusted setup.')

    async def register_initial_self(self,original_key: str,input_kind: str,body: str,input_origin: str):
        """Publish one actual input or confirm its original bounded receipt."""
        owner=getattr(self,'_owner',None)
        if type(owner) is not InitialSelfCommands or owner._port is not self:raise InvalidValue()
        return await owner._register(original_key,input_kind,body,input_origin)

    async def read_initial(self,input_id: str,deadline: float):
        """Read only this instance's first input and original SELF as complete values."""
        from .service import MemoryError
        owner=getattr(self,'_owner',None)
        if type(self) is not InitialSelfPort or type(owner) is not InitialSelfCommands or owner._port is not self:
            return MemoryError('ACCESS_DENIED','read_initial','capability','BINDING_MISMATCH')
        return await owner._read_initial(input_id,deadline)

    def participate_initial(self,uow:UnitOfWork,input_id:str):
        """Read the exact original input and SELF within a native consumer UoW."""
        owner=getattr(self,'_owner',None)
        if type(self) is not InitialSelfPort or type(owner) is not InitialSelfCommands or owner._port is not self or owner._owner is None or owner._closed:
            raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        found=owner._owner.read_initial(uow,input_id)
        if found is None:raise OwnerFailure('PRECONDITION_FAILED','input','NOT_FOUND')
        return found

    def publication_stale(self,uow:UnitOfWork,publication):
        """Compare a native publication source to this instance's actual SELF."""
        owner=getattr(self,'_owner',None)
        if type(self) is not InitialSelfPort or type(owner) is not InitialSelfCommands or owner._port is not self or owner._owner is None or owner._closed:
            raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        return owner._owner.publication_stale(uow,publication)

    async def publication_stale_original(self,publication,deadline:float):
        """Inspect the same immutable source under the caller's original deadline."""
        owner=getattr(self,'_owner',None)
        if type(self) is not InitialSelfPort or type(owner) is not InitialSelfCommands or owner._port is not self or owner._owner is None or owner._closed:
            raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        return await owner._owner.publication_stale_original(publication,deadline)


class InitialSelfCommands:
    """One fixed declaration and a unique trusted identity binding."""
    def __init__(self,catalog: StatementCatalog,utc_now_us: Callable[[],int] = lambda:time.time_ns()//1000):
        if catalog.definition.owner_module!='memory' or catalog.definition.schema_version not in (3,4,5) or not callable(utc_now_us):raise InvalidValue()
        self.catalog=catalog;self._clock=utc_now_us;self._owner: InitialSelfStorage | None=None;self._port: InitialSelfPort | None=None
        requirements,bindings=audits('register_initial_self',('memory',))
        self.commands=(ResultBoundCommandDefinition('memory','register_initial_self',1,
            RecordSchema((Field('operation_id',ID),Field('input_kind',ScalarSchema('enum',choices=('PRESET','NO_PRESET'))),Field('body',BoundedTextSchema(2048)),
                Field('input_origin',ScalarSchema('enum',choices=('ACTUAL_INPUT','SYNTHETIC_FIXTURE'))))),1,result_schema(('memory',),('REGISTERED',)),
            (catalog.definition,),requirements,self._handle,INTENT,bindings),)
        self.admit:Callable[[UnitOfWork],None]|None=None
        self._operation=None;self._closed=False;self._read_task:asyncio.Task|None=None

    def bind(self,memory: MemoryTransactions,binding: InitialSelfBinding) -> InitialSelfPort:
        """Trusted setup binds exactly one native memory owner and one actor scope."""
        if self._owner is not None or type(memory) is not MemoryTransactions or memory._information_catalog is not self.catalog:raise InvalidValue()
        self._owner=InitialSelfStorage(memory,binding);self._binding=binding
        self._operation=memory.storage.bind_operation(self.commands[0],memory.instance_id)
        port=object.__new__(InitialSelfPort);object.__setattr__(port,'_owner',self);self._port=port
        return port

    def _handle(self,uow: UnitOfWork,values: MappingProxyType[str,Value]):
        if self._owner is None:raise InvalidValue()
        if self.admit is not None:self.admit(uow)
        uow.require_commit_permission(lambda:not self._closed)
        operation={'owner_namespace':'memory','operation_kind':'register_initial_self','scope_id':self._owner._owner.instance_id,
            'operation_key':values['operation_id']}
        # The operation_id is the exact retained public key; no independent
        # timestamp or synthesized command key can replace its receipt identity.
        found=self._owner.register(uow,cast(str,values['input_kind']),cast(str,values['body']),cast(str,values['input_origin']),operation,self._clock())
        refs=({'kind':'INPUT','object_id':found.input['object_id'],'revision':1},{'kind':'SELF','object_id':found.subject['subject_id'],'revision':1})
        targets=tuple({'object_id':reference['object_id'],'previous_revision':None,'revision':1} for reference in refs)
        return result(cast(str,values['operation_id']),'REGISTERED',{'memory':{'rows_changed':2,'references':refs}},refs,targets)

    async def _register(self,key: str,input_kind: str,body: str,input_origin: str):
        if self._closed or self._operation is None or not valid_identifier(key):raise InvalidValue()
        return await self._operation.execute(key,ResultBoundCommand(1,{'operation_id':key,'input_kind':input_kind,'body':body,'input_origin':input_origin},
            {'memory_text_learning':{'actor':self._binding.actor_ref}}))

    async def _read_initial(self,input_id: str,deadline: float):
        from .service import MemoryError
        from companion_memory.persistence import Found,NotFound
        operation='read_initial';owner=self._owner
        if not valid_identifier(input_id) or type(deadline) not in (float,int) or not time.monotonic()<deadline<float('inf'):
            return MemoryError('INVALID_INPUT',operation,'input','INVALID_SHAPE')
        if owner is None or input_id!=owner.input_id:return MemoryError('ACCESS_DENIED',operation,'capability','BINDING_MISMATCH')
        if self._closed:return MemoryError('INVALID_STATE',operation,'state','SERVICE_CLOSED')
        if self._read_task is not None:return MemoryError('RESOURCE_BUSY',operation,'state','ADMISSION_FULL',True)
        async def inspect():
            try:
                value=await owner.read_original(input_id,deadline)
                if self._closed:return MemoryError('INVALID_STATE',operation,'state','SERVICE_CLOSED')
                if time.monotonic()>=deadline:return MemoryError('TIMEOUT',operation,'state','DEADLINE_EXCEEDED')
                return NotFound() if value is None else Found(MappingProxyType({'input':value.input,'subject':value.subject}))
            except OwnerFailure as failure:return MemoryError(failure.code,operation,failure.field,failure.reason,failure.cleanup_pending)
            except (InvalidValue,KeyError,TypeError):return MemoryError('STORAGE_FAILED',operation,'storage','INTEGRITY_FAILURE')
        task=asyncio.create_task(finish_owned(inspect()));self._read_task=task;retain_completion(task)
        def ended(job):
            if not job.cancelled():job.exception()
            if self._read_task is job:self._read_task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((task,),timeout=max(0,deadline-time.monotonic()))
        return task.result() if done else MemoryError('TIMEOUT',operation,'state','DEADLINE_EXCEEDED',True)

    def close(self) -> None:
        """Stop new management calls; actual SQLite cleanup belongs to memory/storage."""
        self._closed=True
