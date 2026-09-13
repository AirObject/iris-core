"""Cognition-owned context rows staged and released in runtime's original UoW.

Reads and writes retain the bound database, instance and configuration. No port
collects replacement material or sends a request. Release removes all leaves and
marks the manifest in the same transaction as the original work's terminal state.
"""
from dataclasses import dataclass
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import PersistenceService, UnitOfWork, Value
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.text_records import decode_row,isolate_record,OPERATION
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import BoundStatements,StatementCatalog,OwnerFailure
from companion_memory.configuration.text_persistence import StoredTextConfiguration,stored_text_configuration_issue
from companion_memory.memory.formats import record,sequence
from companion_memory.provider.chat_protocol import ChatBinding
from .text_context import FrozenContext,MANIFEST,LEAF,restore_context,decode_retained_context


@dataclass(frozen=True,slots=True)
class StoredContext:
    """Verified canonical stored rows; request reconstruction remains mandatory."""
    manifest: MappingProxyType[str,Value]
    leaves: tuple[MappingProxyType[str,Value],...]


@dataclass(frozen=True,slots=True)
class ReleasedContext:
    """Retained original manifest with no material leaves or sending permission."""
    manifest: MappingProxyType[str,Value]


class ContextStorage:
    """Private cognition participant sharing its owner's native catalog and lease."""
    def __init__(self,catalog: StatementCatalog,storage: PersistenceService,configuration: StoredTextConfiguration,instance_id: str):
        if stored_text_configuration_issue(configuration) is not None or catalog.definition.owner_module!='cognition':raise InvalidValue()
        self._rows=BoundStatements(catalog,storage,instance_id)
        self._configuration=configuration;self._instance_id=instance_id

    def _decode(self,raw: MappingProxyType[str,Value],*,leaf: bool = False):
        return decode_row(raw,LEAF if leaf else MANIFEST,8192,self._configuration.database_id,self._instance_id,self._configuration.snapshot_id)

    def stage(self,uow: UnitOfWork,value: FrozenContext,binding: ChatBinding) -> MappingProxyType[str,Value]:
        """Stage every validated row; the caller atomically binds original runtime work."""
        if type(value) is not FrozenContext:raise InvalidValue()
        verified=restore_context(value.manifest,value.leaves,value.request,binding)
        if verified!=value:raise InvalidValue()
        for item in (value.manifest,*value.leaves):
            if (item['database_id'],item['instance_id'],item['config_snapshot_id'])!=(self._configuration.database_id,self._instance_id,self._configuration.snapshot_id):raise InvalidValue()
        for suffix,item in (('learning_contexts',value.manifest),*(('learning_context_leaves',leaf) for leaf in value.leaves)):
            inserted=self._rows.stage(suffix+'_insert',uow,{'object_id':item['object_id'],'revision':item['revision'],'body':encode_content(item,8192).decode()})
            if len(inserted)!=1 or self._decode(inserted[0],leaf=suffix=='learning_context_leaves')!=item:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
        return value.manifest

    def release(self,uow: UnitOfWork,context_id: str,terminal_operation: object) -> tuple[int,int]:
        """Release the entire leaf set only under the enclosing terminal transaction."""
        operation=isolate_record(OPERATION,terminal_operation,1024)
        if (operation['owner_namespace'],operation['scope_id'])!=('runtime',self._instance_id):raise InvalidValue()
        rows=self._rows.stage('learning_contexts_get',uow,{'object_id':context_id})
        if len(rows)!=1:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
        previous=self._decode(rows[0])
        if previous['state']!='STORED' or previous['terminal_operation'] is not None:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        refs=sequence(previous['leaf_refs'])
        leaves=[]
        for ref in refs:
            leaf=self._rows.stage('learning_context_leaves_get',uow,{'object_id':record(ref)['object_id']})
            if len(leaf)!=1:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
            item=self._decode(leaf[0],leaf=True)
            if item['context_id']!=context_id or item['digest']!=record(ref)['digest']:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
            leaves.append(item)
        decode_retained_context(previous,tuple(leaves))
        deleted=self._rows.stage('learning_context_leaves_delete_by_context_id',uow,{'context_id':context_id})
        if {cast(str,row['object_id']) for row in deleted}!={cast(str,record(ref)['object_id']) for ref in refs}:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
        revision=cast(int,previous['revision'])+1
        current=isolate_record(MANIFEST,{**previous,'revision':revision,'state':'RELEASED','terminal_operation':operation},8192)
        changed=self._rows.stage('learning_contexts_cas',uow,{'object_id':context_id,'revision':revision,'expected_revision':previous['revision'],'body':encode_content(current,8192).decode()})
        if len(changed)!=1:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        return revision,len(deleted)

    def read_stored(self,uow: UnitOfWork,context_id: str) -> StoredContext:
        """Read every original leaf under the enclosing owner's transaction."""
        rows=self._rows.stage('learning_contexts_get',uow,{'object_id':context_id})
        if len(rows)!=1:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
        manifest=self._decode(rows[0]);leaves=[]
        actual=self._rows.stage('learning_context_leaves_by_context',uow,{'context_id':context_id})
        refs=sequence(manifest['leaf_refs'])
        if {cast(str,item['object_id']) for item in actual}!={cast(str,record(ref)['object_id']) for ref in refs}:raise InvalidValue()
        for reference in refs:
            found=self._rows.stage('learning_context_leaves_get',uow,{'object_id':record(reference)['object_id']})
            if len(found)!=1:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
            leaves.append(self._decode(found[0],leaf=True))
        verified,owned,_=decode_retained_context(manifest,tuple(leaves))
        return StoredContext(verified,owned)

    async def load(self,context_id: str,deadline: float) -> StoredContext | ReleasedContext:
        """Apply the original absolute deadline to every actual storage child job."""
        from companion_memory.persistence.deadlines import DeadlineScope
        with DeadlineScope(deadline):
            return await self._load(context_id,deadline)

    async def _load(self,context_id: str,deadline: float) -> StoredContext | ReleasedContext:
        """Load original rows using one absolute deadline and bounded point reads.

        The recovery owner must additionally bind the terminal operation receipt
        for ReleasedContext, or reconstruct StoredContext against original work.
        """
        def timely():
            if time.monotonic()>=deadline:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING')
        timely();rows=await self._rows.read('learning_contexts_get',{'object_id':context_id});timely()
        if len(rows)!=1:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
        manifest=self._decode(rows[0])
        actual=await self._rows.read('learning_context_leaves_by_context',{'context_id':context_id});timely()
        if manifest['state']=='RELEASED':
            if actual or manifest['terminal_operation'] is None:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
            return ReleasedContext(manifest)
        refs=sequence(manifest['leaf_refs'])
        if {cast(str,item['object_id']) for item in actual}!={cast(str,record(ref)['object_id']) for ref in refs}:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
        leaves=[]
        for ref in refs:
            rows=await self._rows.read('learning_context_leaves_get',{'object_id':record(ref)['object_id']});timely()
            if len(rows)!=1:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
            leaf=self._decode(rows[0],leaf=True)
            if leaf['context_id']!=context_id:raise OwnerFailure('INTEGRITY_FAILURE','storage','RECORD_INVALID')
            leaves.append(leaf)
        verified,owned,_=decode_retained_context(manifest,tuple(leaves))
        return StoredContext(verified,owned)
