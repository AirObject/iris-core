"""One narrow native import capability for the exact approved original persona.

Only trusted initialization issues the capability. A new local publication and
its source record commit together; the imported text never becomes a local
Provider attempt. Original keys remain independently confirmable on recovery.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import time
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.memory.transactions import MemoryTransactions
from companion_memory.persistence import Field,RecordSchema,ResultBoundCommandDefinition,ResultBoundCommand,PersistenceService,UnitOfWork,Found,NotFound,Committed,Value
from companion_memory.persistence.daily_records import DailyRows,ID,identity
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.schema import InvalidValue,valid_identifier
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure
from companion_memory.persistence.content_codec import encode_content
from .approved_import_evidence import ApprovedPersonaEvidence,evidence_is_native
from .import_records import import_catalog,IMPORT_TABLE,PUBLICATION_TABLE

_ISSUER=object()

@dataclass(frozen=True,slots=True,init=False)
class ApprovedImportGrant:
    """Authority binds one exact source to one new synthetic local instance."""
    object_id:str
    database_id:str
    instance_id:str
    candidate_id:str
    candidate_digest:str
    text_digest:str
    create_self:bool
    _issuer:object
    _owner:ApprovedPersonaImport
    def __init__(self):raise TypeError('Trusted daily initialization issues the import grant.')

class ApprovedPersonaImport:
    """Self-model owner with two static actual-writer branches and bounded cleanup."""
    def __init__(self,memory_catalog:StatementCatalog):
        if memory_catalog.definition.owner_module!='memory' or memory_catalog.definition.schema_version!=5:raise InvalidValue()
        self.catalog=import_catalog();self.memory_catalog=memory_catalog
        self.commands=tuple(self._definition(create) for create in (False,True))
        self.repositories=(self.catalog.definition,)
        self._bound=False;self._closed=False;self._released=False
        self._reader:asyncio.Task|None=None
        self._task:asyncio.Task|None=None;self._grant:ApprovedImportGrant|None=None

    def _definition(self,create:bool):
        kind='import_approved_persona'+('_with_self' if create else '')
        writers=('self_model','memory') if create else ('self_model',)
        requirements,bindings=audits(kind,writers)
        def handle(uow:UnitOfWork,values:MappingProxyType[str,Value]):return self._apply(uow,values,create)
        return ResultBoundCommandDefinition('self_model',kind,1,RecordSchema((Field('operation_id',ID),Field('grant_id',ID))),1,
            result_schema(writers,('IMPORTED_APPROVED',)),(self.catalog.definition,self.memory_catalog.definition),requirements,handle,INTENT,bindings)

    @property
    def bound(self):
        """Whether the native import owner has acquired its original instance lease."""
        return self._bound

    def bind(self,storage:PersistenceService,configuration:StoredDailyConfiguration,memory:MemoryTransactions,
             binding:InitialSelfBinding,evidence:ApprovedPersonaEvidence,*,new_synthetic_instance:bool,create_self:bool=True) -> ApprovedImportGrant|None:
        """Bind original evidence for read/recovery; only new-instance setup gets a grant."""
        if (self._bound or stored_daily_configuration_issue(configuration) is not None or type(memory) is not MemoryTransactions
                or memory.configuration is not configuration or not memory.daily_format or type(binding) is not InitialSelfBinding
                or binding.input_origin!='SYNTHETIC_FIXTURE' or not evidence_is_native(evidence)
                or type(new_synthetic_instance) is not bool or type(create_self) is not bool):raise InvalidValue()
        self.storage=storage;self.configuration=configuration;self.memory=memory;self.binding=binding;self.evidence=evidence
        self._lease=storage.claim_module_owner(self.catalog.definition)
        if self._lease is None or self._lease.database_id!=configuration.database_id:raise InvalidValue()
        self.rows=DailyRows(self.catalog,(IMPORT_TABLE,PUBLICATION_TABLE),storage,configuration.database_id,configuration.scope_id,configuration.snapshot_id)
        self.operations={d.operation_kind:storage.bind_operation(d,configuration.scope_id) for d in self.commands}
        self.import_id=identity('persona-import',configuration.database_id,configuration.scope_id,evidence.original_candidate_id)
        self.publication_id=identity('persona-publication',configuration.database_id,configuration.scope_id,self.import_id)
        self.grant_id=identity('persona-import-grant',configuration.database_id,configuration.scope_id,evidence.original_candidate_id,evidence.original_candidate_digest,evidence.original_text_digest)
        self._bound=True
        if new_synthetic_instance:
            grant=object.__new__(ApprovedImportGrant)
            for name,value in dict(object_id=self.grant_id,database_id=configuration.database_id,instance_id=configuration.scope_id,
                    candidate_id=evidence.original_candidate_id,candidate_digest=evidence.original_candidate_digest,text_digest=evidence.original_text_digest,
                    create_self=create_self,_issuer=_ISSUER,_owner=self).items():object.__setattr__(grant,name,value)
            self._grant=grant
        return self._grant

    def _apply(self,uow:UnitOfWork,values:MappingProxyType[str,Value],create:bool):
        grant=self._grant
        if (not self._bound or self._closed or grant is None or grant._owner is not self or values['grant_id']!=grant.object_id
                or create!=grant.create_self):raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        uow.require_commit_permission(lambda:not self._closed)
        if self.rows.rows.stage('persona_publications_by_instance',uow,{}) or self.rows.rows.stage('persona_imports_by_instance',uow,{}):
            raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        if self.rows.rows.stage('initial_persona_runs_by_instance',uow,{}):
            raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        subject=self.memory.import_self(uow,self.binding,create=create)
        op=self.storage.daily_operation_context(uow,self.catalog.definition)
        if op.operation_key!=values['operation_id']:raise InvalidValue()
        operation={key:getattr(op,key) for key in ('owner_namespace','operation_kind','scope_id','operation_key')}
        now=time.time_ns()//1000;e=self.evidence
        base={'format_version':1,'revision':1,'database_id':self.configuration.database_id,'instance_id':self.configuration.scope_id,
            'config_snapshot_id':self.configuration.snapshot_id,'created_at_us':now,'updated_at_us':now}
        imported={**base,'object_id':self.import_id,**{name:getattr(e,name) for name in ('original_database_id','original_publication_id',
            'original_candidate_id','original_candidate_revision','original_candidate_digest','original_text_digest','original_text_utf8_digest','review_evidence_digest','text')},
            'import_grant_id':self.grant_id,'self_subject_id':subject['subject_id'],'import_operation':operation}
        self.rows.write('persona_imports',uow,imported)
        self.rows.write('persona_publications',uow,{**base,'object_id':self.publication_id,'import_id':self.import_id,
            'self_subject_id':subject['subject_id'],'self_revision':subject['revision'],'publication_origin':'IMPORTED_APPROVED','model_origin':'REMOTE_PROVIDER',
            'approval_ref':e.approval_ref,'publication_operation':operation})
        facts:dict[str,object]={'self_model':{'rows_changed':2,'targets':[target(self.import_id,1),target(self.publication_id,1)]}}
        if create:facts['memory']={'rows_changed':1,'targets':[target(cast(str,subject['subject_id']),1)]}
        return result(cast(str,values['operation_id']),'IMPORTED_APPROVED',facts)

    async def import_approved_persona(self,grant:ApprovedImportGrant,original_key:str):
        """Execute only the exact native source grant; replay returns its original receipt."""
        if (self._closed or type(grant) is not ApprovedImportGrant or grant is not self._grant or grant._issuer is not _ISSUER
                or grant._owner is not self or not valid_identifier(original_key)):raise InvalidValue()
        if self._task is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        definition=self.commands[1 if grant.create_self else 0]
        command=ResultBoundCommand(1,{'operation_id':original_key,'grant_id':grant.object_id},
            {r.event_slot:{'actor':self.binding.actor_ref} for r in definition.required_audits})
        task,logical=start_owned(self.operations[definition.operation_kind].execute(original_key,command))
        self._task=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        return await asyncio.shield(logical)

    async def confirm_import(self,original_key:str):
        """Read both finite original branches; confirming never invokes a write handler."""
        if not self._bound or not valid_identifier(original_key):raise InvalidValue()
        found=[]
        for operation in self.operations.values():
            observed=await operation.read_receipt(original_key)
            if type(observed) is Found:found.append(observed.value)
            elif type(observed) is not NotFound:return observed
        if len(found)>1:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        return Committed(found[0],'EXISTING') if found else NotFound()

    async def read_current(self,deadline:float|None=None):
        """Retain the actual complete local read; timeout does not free its slot."""
        end=time.monotonic()+5 if deadline is None else deadline
        if self._closed or self._reader is not None:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',self._reader is not None)
        if type(end) not in (float,int) or not time.monotonic()<end<float('inf'):raise InvalidValue()
        async def read():
            with DeadlineScope(end):return await self._read_current()
        task,logical=start_owned(read());self._reader=task
        def ended(job):
            if not job.cancelled():job.exception()
            if self._reader is job:self._reader=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,end-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','resource','DEADLINE_EXCEEDED',True)
        return logical.result()

    async def _read_current(self):
        """Verify immutable imported origin against the exact approved evidence."""
        if not self._bound:raise InvalidValue()
        raw=await self.rows.rows.read('persona_publications_by_instance',{})
        if not raw:return NotFound()
        if len(raw)!=1:raise InvalidValue()
        publication=self.rows.decode('persona_publications',raw[0])
        imported=await self.rows.read('persona_imports',cast(str,publication['import_id']))
        if imported is None:raise InvalidValue()
        e=self.evidence
        if (imported['object_id']!=self.import_id or publication['object_id']!=self.publication_id or imported['import_grant_id']!=self.grant_id
                or any(imported[name]!=getattr(e,name) for name in ('text','original_database_id','original_publication_id','original_candidate_id',
                    'original_candidate_revision','original_candidate_digest','original_text_digest','original_text_utf8_digest','review_evidence_digest'))
                or publication['approval_ref']!=e.approval_ref or publication['self_subject_id']!=imported['self_subject_id']):raise InvalidValue()
        # The memory owner supplies the actual current SELF; no private storage port is borrowed.
        subject=await self.memory.read_import_self(cast(str,publication['self_subject_id']))
        if subject is None or cast(int,subject['revision'])<cast(int,publication['self_revision']):raise InvalidValue()
        value=MappingProxyType({'publication_id':publication['object_id'],'revision':publication['revision'],'text':imported['text'],
            'generated_at_us':e.original_generated_at_us,'review':'APPROVED','publication_origin':'IMPORTED_APPROVED','model_origin':'REMOTE_PROVIDER',
            'original_database_id':imported['original_database_id'],'original_publication_id':imported['original_publication_id'],
            'original_candidate_id':imported['original_candidate_id'],'review_evidence_digest':imported['review_evidence_digest'],
            'stale':subject['revision']!=publication['self_revision']})
        encode_content(value,2048)
        return Found(value)

    def participate_current(self,uow:UnitOfWork,publication_id:str,revision:int):
        """Verify the exact imported publication and original receipt in a consumer UoW."""
        if self._closed or not self._bound:raise InvalidValue()
        publications=self.rows.rows.stage('persona_publications_by_instance',uow,{})
        if not publications:return None
        if len(publications)!=1:raise InvalidValue()
        publication=self.rows.decode('persona_publications',publications[0])
        if publication['object_id']!=publication_id or publication['revision']!=revision:return None
        imported=self.rows.get('persona_imports',uow,cast(str,publication['import_id']))
        if imported is None or imported['text']!=self.evidence.text or imported['original_text_digest']!=self.evidence.original_text_digest:raise InvalidValue()
        operation=cast(MappingProxyType,imported['import_operation'])
        definition=next(d for d in self.commands if d.operation_kind==operation['operation_kind'])
        receipt=self.storage.confirm_prior_operation(uow,definition,cast(str,operation['operation_key']))
        if receipt is None:raise InvalidValue()
        subject=self.memory.subject(uow,cast(str,publication['self_subject_id']))
        if subject is None or subject['kind']!='SELF':raise InvalidValue()
        return MappingProxyType({'publication_id':publication['object_id'],'revision':publication['revision'],'text':imported['text'],
            'generated_at_us':self.evidence.original_generated_at_us,'review':'APPROVED','model_origin':'REMOTE_PROVIDER',
            'stale':subject['revision']!=publication['self_revision'],'publication_origin':'IMPORTED_APPROVED',
            'original_database_id':imported['original_database_id'],'original_publication_id':imported['original_publication_id'],
            'original_candidate_id':imported['original_candidate_id'],'review_evidence_digest':imported['review_evidence_digest']})

    def close(self) -> bool:
        """Reject new imports while actual descendants retain the owner's lease."""
        self._closed=True
        if not self._bound or self._released:return True
        if self._task is not None or self._reader is not None:return False
        assert self._lease is not None
        self._released=self._lease.release()
        return self._released
