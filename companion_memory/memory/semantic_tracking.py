"""Memory-owned semantic coverage joined to the original change transaction.

The canonical lexical sequence remains the only mutation clock. A late artifact
cannot clear a newer gap, and local deletion requires the retained tombstone.
Publication moves separately after the retrieval owner verifies its entire file.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import TYPE_CHECKING
from companion_memory.persistence import UnitOfWork
from companion_memory.persistence.content_codec import decode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_catalog import SemanticRecords
from companion_memory.persistence.semantic_records import Record,identity,number,string,isolate,RECEIPT
from .formats import TOMBSTONE_SCHEMA,record
from .semantic_repository import TABLES
if TYPE_CHECKING:
    from .transactions import MemoryTransactions
    from .information_tracking import MemoryInformation


class MemorySemanticCoverage:
    """Finite read and transaction ports sharing the existing memory lease."""
    def __init__(self,objects: MemoryTransactions,information: MemoryInformation,space_id: str):
        if not objects.semantic_format or objects.information is not information:
            raise ValueError('The bound semantic memory owner is required.')
        self.objects=objects;self.information=information;self.space_id=space_id
        self.rows=SemanticRecords(objects._information_catalog,TABLES,objects.storage,objects.instance_id)
        self.root_id=identity('semantic-publication',objects.instance_id,space_id)
        self._summary_uow: UnitOfWork | None=None
        self._from=0;self._to=0;self._delta=0

    def _root(self,uow: UnitOfWork) -> Record:
        value=self.rows.get('semantic_publication',uow,self.root_id)
        if value is None:raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
        return value

    def _gap_id(self,object_id: str) -> str:
        return identity('semantic-gap',self.space_id,object_id)

    def _ack_id(self,object_id: str) -> str:
        return identity('semantic-ack',self.space_id,object_id)

    def _summary(self,uow: UnitOfWork,previous: int,current: int,delta: int) -> None:
        if self._summary_uow is not uow:
            self._summary_uow=uow;self._from=previous;self._delta=0
        self._to=current;self._delta+=delta
        if not -8<=self._delta<=8:raise OwnerFailure('RESOURCE_BUSY','member','CAPACITY_REACHED')

    def _synchronize(self,uow: UnitOfWork,last_seq: int) -> Record:
        root=self._root(uow)
        floor=self.rows.statements.stage('semantic_floor',uow,{'space_id':self.space_id})[0]['minimum']
        material=last_seq if floor is None else number(floor)-1
        if number(root['material_seq'])>material or number(root['published_seq'])>material:
            raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
        return self.rows.write('semantic_publication',uow,dict(root)|{'revision':number(root['revision'])+1,
            'material_seq':material},expected_revision=number(root['revision']))

    def mark(self,uow: UnitOfWork,object_id: str,revision: int,deleted: bool,previous: int,seq: int) -> None:
        """Mark only an actual MEMORY change, retaining the earliest gap start."""
        current=self.objects.current(uow,object_id)
        if deleted:
            tombstone=self.deletion(uow,object_id)
            if current is not None or tombstone['deletion_revision']!=revision:
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            kind=tombstone['kind']
        else:
            if current is None or current['revision']!=revision:
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            kind=current['kind']
        delta=0
        if kind=='MEMORY':
            old=self.rows.get('semantic_gap',uow,self._gap_id(object_id))
            if old is not None and (number(old['object_revision'])>=revision or number(old['latest_change_seq'])>=seq):
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            self.rows.write('semantic_gap',uow,{'v':1,'revision':revision,'row_id':self._gap_id(object_id),
                'space_id':self.space_id,'object_id':object_id,'object_revision':revision,
                'first_uncovered_seq':old['first_uncovered_seq'] if old else seq,'latest_change_seq':seq,
                'action':'DELETE' if deleted else 'UPSERT'},expected_revision=number(old['revision']) if old else None)
            delta=int(old is None)
        self._synchronize(uow,seq);self._summary(uow,previous,seq,delta)

    def deletion(self,uow: UnitOfWork,object_id: str) -> Record:
        """Return the original complete tombstone; absence is never proof."""
        rows=self.objects.rows.stage('tombstones_get',uow,{'object_id':object_id})
        if len(rows)!=1:raise OwnerFailure('PRECONDITION_FAILED','object','SOURCE_CHANGED')
        value=isolate(TOMBSTONE_SCHEMA,decode_content(string(rows[0]['body']).encode(),1024),1024)
        if value['object_id']!=object_id:raise OwnerFailure('STORAGE_FAILED','storage','INTEGRITY_FAILURE')
        return value

    def current(self,uow: UnitOfWork,object_id: str) -> tuple[Record | None,Record | None]:
        """Authoritative current object and exact gap in the enclosing UoW."""
        return self.objects.current(uow,object_id),self.rows.get('semantic_gap',uow,self._gap_id(object_id))

    async def semantic_current(self,object_id: str) -> tuple[Record | None,Record | None]:
        """Preparation reads; every application must repeat them transactionally."""
        return await self.information.index_current(object_id),await self.rows.read('semantic_gap',self._gap_id(object_id))

    def coverage(self,uow: UnitOfWork) -> Record:
        root=self._root(uow);sequence=self.information.sequence(uow)
        floor=self.rows.statements.stage('semantic_floor',uow,{'space_id':self.space_id})[0]
        expected=sequence['last_seq'] if floor['minimum'] is None else number(floor['minimum'])-1
        if root['material_seq']!=expected:raise OwnerFailure('STORAGE_FAILED','index','INTEGRITY_FAILURE')
        return MappingProxyType({'captured_seq':sequence['last_seq'],'material_seq':root['material_seq'],
            'published_seq':root['published_seq'],'generation_id':root['generation_id'],
            'first_uncovered_seq':floor['minimum'],'pending_count':floor['count']})

    async def ack_page(self,after_object_id: str='') -> tuple[Record,...]:
        rows=await self.rows.statements.read('semantic_ack_objects',{'space_id':self.space_id,'after':after_object_id})
        return tuple(self.rows.decode('semantic_ack',row) for row in rows)

    def index_members(self,uow: UnitOfWork,after_object_id: str='') -> tuple[tuple[Record,Record],...]:
        """Read at most eight current MEMORYs and their exact applied revisions.

        Scanning authoritative objects detects missing acknowledgments. Deleted
        acknowledgments and non-memory objects never become generation members.
        This private index port grants no host delivery permission.
        """
        result=[];after=after_object_id
        while len(result)<8:
            rows=self.information._records.rows.stage('information_objects',uow,{'after':after,'limit':8})
            if not rows:break
            for row in rows:
                current=self.objects.decode_current(row);after=string(current['object_id'])
                if current['kind']!='MEMORY':continue
                ack=self.rows.get('semantic_ack',uow,self._ack_id(after))
                if ack is None or ack['action']!='UPSERT' or ack['object_revision']!=current['revision']:
                    raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
                result.append((current,ack))
                if len(result)==8:break
            if len(rows)<8:break
        return tuple(result)

    def acknowledge(self,uow: UnitOfWork,object_id: str,revision: int,seq: int,*,
                    artifact_id: str | None,material_digest: str | None,evidence: Record,
                    deletion_operation: str | None=None) -> tuple[Record,Record]:
        """Apply the exact current gap; the coordinator proves artifact ownership.

        Only a declared native retrieval participant can join this owner in a
        UoW. Its original apply receipt identity is retained by this record.
        """
        receipt=isolate(RECEIPT,evidence)
        operation,fingerprint=self.objects.storage.semantic_operation_context(uow,self.objects._information_catalog.definition)
        if operation.operation_kind!='apply' or receipt!={'kind':'apply','key':operation.operation_key,'fingerprint':fingerprint}:
            raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        current,gap=self.current(uow,object_id)
        if gap is None or gap['object_revision']!=revision or gap['latest_change_seq']!=seq:
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        deleted=gap['action']=='DELETE'
        if deleted:
            tombstone=self.deletion(uow,object_id)
            valid=(current is None and tombstone['kind']=='MEMORY' and tombstone['deletion_revision']==revision
                and deletion_operation is not None and tombstone['operation_ref']==deletion_operation
                and artifact_id is None and material_digest is None)
        else:
            valid=(current is not None and current['kind']=='MEMORY' and current['revision']==revision
                and artifact_id is not None and material_digest is not None and deletion_operation is None)
        if not valid:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        old=self.rows.get('semantic_ack',uow,self._ack_id(object_id))
        ack=self.rows.write('semantic_ack',uow,{'v':1,'revision':1 if old is None else number(old['revision'])+1,
            'row_id':self._ack_id(object_id),'space_id':self.space_id,'object_id':object_id,'object_revision':revision,
            'resolved_from_seq':gap['first_uncovered_seq'],'applied_seq':seq,'action':gap['action'],
            'material_digest':material_digest,'artifact_id':artifact_id,'evidence_receipt':receipt},
            expected_revision=number(old['revision']) if old else None)
        self.rows.remove('semantic_gap',uow,string(gap['row_id']),number(gap['revision']))
        last=number(self.information.sequence(uow)['last_seq'])
        root=self._synchronize(uow,last);self._summary(uow,last,last,-1)
        return ack,root

    def publish(self,uow: UnitOfWork,generation_id: str,captured_seq: int,observed_at: int) -> Record:
        """Advance only a completely covered frozen generation's publication root."""
        coverage=self.coverage(uow)
        if coverage['pending_count'] or coverage['captured_seq']!=captured_seq:
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        root=self._root(uow)
        if root['generation_id']==generation_id:raise OwnerFailure('PRECONDITION_FAILED','index','NO_CHANGE')
        result=self.rows.write('semantic_publication',uow,dict(root)|{'revision':number(root['revision'])+1,
            'published_seq':captured_seq,'generation_id':generation_id,'published_at':observed_at},
            expected_revision=number(root['revision']))
        self._summary(uow,captured_seq,captured_seq,0)
        return result

    def audit_fact(self,uow: UnitOfWork) -> Record:
        """Read actual semantic effects; no new gap is invented for subjects."""
        if self._summary_uow is not uow:
            last=number(self.information.sequence(uow)['last_seq'])
            self._summary(uow,last,last,0)
        return MappingProxyType({'semantic_root':self.root_id,'semantic_from_seq':self._from,
            'semantic_to_seq':self._to,'semantic_gap_delta':self._delta})
