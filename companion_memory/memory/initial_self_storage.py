"""Memory-owned first SELF and immutable operator input in one transaction.

The trusted binding supplies identity and provenance; generated text cannot name
or register the SELF. The read port exposes only this instance's initial input
and its current SELF, never arbitrary memory rows or historical sources.
"""
from __future__ import annotations
from dataclasses import dataclass
import time
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.persistence import UnitOfWork,Value
from companion_memory.persistence.schema import BoundedTextSchema,InvalidValue,freeze_value
from companion_memory.persistence.text_records import ID,OPERATION,decode_row,isolate_record,stable_identity
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.configuration.text_persistence import StoredTextConfiguration
from .formats import isolate_subject,record
from .initial_self import INITIAL_SELF,input_digest,isolate_initial_self
if TYPE_CHECKING:
    from .transactions import MemoryTransactions


@dataclass(frozen=True,slots=True)
class InitialSelfBinding:
    """Trusted initial identity and actor; no generated or mutable name is accepted."""
    actor_ref: str
    self_subject_id: str
    self_label: str
    input_origin: str

    def __post_init__(self):
        freeze_value(ID,self.actor_ref);freeze_value(ID,self.self_subject_id);freeze_value(BoundedTextSchema(256),self.self_label)
        if not self.self_label.strip() or self.input_origin not in ('ACTUAL_INPUT','SYNTHETIC_FIXTURE'):raise InvalidValue()


@dataclass(frozen=True,slots=True)
class InitialSelfFound:
    """Complete original operator input together with the verified current SELF."""
    input: MappingProxyType[str,Value]
    subject: MappingProxyType[str,Value]


class InitialSelfStorage:
    """Private memory participant sharing the native memory owner's real lease."""
    def __init__(self,owner: MemoryTransactions,binding: InitialSelfBinding):
        from .transactions import MemoryTransactions
        if type(owner) is not MemoryTransactions or not owner.text_format or type(owner.configuration) is not StoredTextConfiguration or type(binding) is not InitialSelfBinding:raise InvalidValue()
        self._owner=owner;self._binding=binding
        self.input_id=stable_identity('self-input',owner.configuration.database_id,owner.instance_id)

    def _decode(self,row: MappingProxyType[str,Value]):
        configuration=self._owner.configuration
        return isolate_initial_self(decode_row(row,INITIAL_SELF,8192,configuration.database_id,self._owner.instance_id,configuration.snapshot_id))

    def register(self,uow: UnitOfWork,input_kind: str,body: str,input_origin: str,operation: object,created_at_us: int) -> InitialSelfFound:
        """Stage unique SELF and original input; any later audit failure rolls both back."""
        owner=self._owner;binding=self._binding;configuration=owner.configuration
        key=isolate_record(OPERATION,operation,1024)
        if (key['owner_namespace'],key['operation_kind'],key['scope_id'])!=('memory','register_initial_self',owner.instance_id) or input_origin!=binding.input_origin:raise InvalidValue()
        subject=isolate_subject({'subject_version':1,'subject_id':binding.self_subject_id,'instance_id':owner.instance_id,'kind':'SELF',
            'platform_id':None,'external_subject_id':None,'label':binding.self_label,'revision':1})
        value={'format_version':1,'object_id':self.input_id,'revision':1,'database_id':configuration.database_id,'instance_id':owner.instance_id,
            'config_snapshot_id':configuration.snapshot_id,'created_at_us':created_at_us,'self_subject_id':binding.self_subject_id,'self_revision':1,
            'input_kind':input_kind,'body':body,'actor_ref':binding.actor_ref,'operation':key,'input_origin':input_origin}
        # Isolate the complete schema before deriving its semantic digest.
        provisional=isolate_record(INITIAL_SELF,{**value,'input_digest':'0'*64},8192)
        value=isolate_initial_self({**provisional,'input_digest':input_digest(provisional)})
        if (owner.rows.stage('initial_self_inputs_get',uow,{'object_id':self.input_id}) or
                owner.rows.stage('subject_identity',uow,{'kind':'SELF','platform_id':None,'external_subject_id':None}) or owner.subject(uow,binding.self_subject_id) is not None):
            raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        owner.rows.stage('subjects_insert',uow,{name:subject[name] for name in ('subject_id','kind','platform_id','external_subject_id','revision')}|
            {'body':encode_content(subject,1024).decode()})
        inserted=owner.rows.stage('initial_self_inputs_insert',uow,{'object_id':self.input_id,'revision':1,'body':encode_content(value,8192).decode()})
        if len(inserted)!=1 or self._decode(inserted[0])!=value:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
        return InitialSelfFound(value,subject)

    def read_initial(self,uow: UnitOfWork,input_id: str) -> InitialSelfFound | None:
        """Read this immutable input and actual SELF inside another owner's UoW."""
        if input_id!=self.input_id:raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
        rows=self._owner.rows.stage('initial_self_inputs_get',uow,{'object_id':input_id})
        if not rows:return None
        value=self._decode(rows[0]);subject=self._owner.subject(uow,cast(str,value['self_subject_id']))
        if subject is None or subject['kind']!='SELF' or subject['revision']!=value['self_revision']:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
        return InitialSelfFound(value,subject)

    async def read_original(self,input_id: str,deadline: float) -> InitialSelfFound | None:
        """Read only retained input under the original deadline; never refresh its text."""
        return await self._read_original(input_id,deadline,None)

    async def read_published_source(self,publication: MappingProxyType[str,Value],deadline: float) -> InitialSelfFound | None:
        """Recover original material while exposing a later SELF revision as stale."""
        return await self._read_original(cast(str,publication['input_id']),deadline,publication)

    async def _read_original(self,input_id: str,deadline: float,publication: MappingProxyType[str,Value] | None) -> InitialSelfFound | None:
        if input_id!=self.input_id:raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
        with DeadlineScope(deadline):
            rows=await self._owner.rows.read('initial_self_inputs_get',{'object_id':input_id})
            if time.monotonic()>=deadline:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING')
            if not rows:return None
            value=self._decode(rows[0]);subjects=await self._owner.rows.read('subjects_get',{'subject_id':value['self_subject_id']})
            if len(subjects)!=1 or time.monotonic()>=deadline:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
            from companion_memory.persistence.content_codec import decode_content
            subject=isolate_subject(decode_content(cast(str,subjects[0]['body']).encode(),1024))
            if any(subject[name]!=subjects[0][name] for name in ('subject_id','kind','revision','platform_id','external_subject_id')):raise InvalidValue()
            if publication is None:
                if (subject['subject_id'],subject['instance_id'],subject['kind'],subject['revision'])!=(value['self_subject_id'],self._owner.instance_id,'SELF',value['self_revision']):raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
            else:self._stale(value,subject,publication)
            return InitialSelfFound(value,subject)

    def publication_stale(self,uow: UnitOfWork,publication: MappingProxyType[str,Value]) -> bool:
        """Compare the immutable source with the actual SELF without regenerating.

        The first-input consumer still uses read_initial for exact revision
        matching. A published persona remains readable after a later SELF
        revision, with that difference explicitly visible in its projection.
        """
        if publication['input_id']!=self.input_id:raise InvalidValue()
        rows=self._owner.rows.stage('initial_self_inputs_get',uow,{'object_id':self.input_id})
        if len(rows)!=1:raise InvalidValue()
        source=self._decode(rows[0]);subject=self._owner.subject(uow,cast(str,source['self_subject_id']))
        return self._stale(source,subject,publication)

    @staticmethod
    def _stale(source,subject,publication) -> bool:
        if (subject is None or subject['subject_id']!=source['self_subject_id'] or subject['kind']!='SELF' or subject['instance_id']!=publication['instance_id']
                or any(source[name]!=publication[name] for name in ('input_digest','self_subject_id','self_revision','database_id','instance_id','config_snapshot_id'))
                or subject['revision']<source['self_revision']):raise InvalidValue()
        return subject['revision']!=source['self_revision']

    async def publication_stale_original(self,publication: MappingProxyType[str,Value],deadline: float) -> bool:
        """Use only this initial input and SELF under the original absolute deadline."""
        from companion_memory.persistence.content_codec import decode_content
        if publication['input_id']!=self.input_id:raise InvalidValue()
        with DeadlineScope(deadline):
            rows=await self._owner.rows.read('initial_self_inputs_get',{'object_id':self.input_id})
            if len(rows)!=1:raise InvalidValue()
            source=self._decode(rows[0])
            subjects=await self._owner.rows.read('subjects_get',{'subject_id':source['self_subject_id']})
            if len(subjects)!=1 or time.monotonic()>=deadline:raise InvalidValue()
            subject=isolate_subject(decode_content(cast(str,subjects[0]['body']).encode(),1024))
            if any(subject[name]!=subjects[0][name] for name in ('subject_id','kind','revision','platform_id','external_subject_id')):raise InvalidValue()
            return self._stale(source,subject,publication)
