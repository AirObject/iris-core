"""Ingress-owned registration, immutable event identity and referenced payloads.

The application coordinator supplies the same UoW to buffer and mode owners.
Only transaction facts select sequence/routing; commands contain original inputs.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import UnitOfWork,Value
from companion_memory.runtime.records import OwnedRows,DomainFailure,data,digest,stable_id
from .events import isolate_event,canonical_event,event_identity,plain


class IngressTransactions:
    def __init__(self,rows:OwnedRows):
        self.rows=rows

    def register(self,uow:UnitOfWork,values:MappingProxyType[str,Value]) -> str:
        binding=(values['instance_id'],values['host_id'],values['platform_id'],values['external_entry_id'])
        entry_id=stable_id('entry',*binding)
        existing=self.rows.get('entries',entry_id,uow)
        expected:dict[str,object]={'instance_id':binding[0],'host_id':binding[1],'platform_id':binding[2],'external_entry_id':binding[3]}
        if existing is not None:
            if data(existing)!=expected:raise DomainFailure('IDEMPOTENCY_CONFLICT','entry','CONTENT_MISMATCH')
            return entry_id
        self.rows.insert('entries',uow,entry_id,entry_id,0,'REGISTERED',expected)
        return entry_id

    def accept(self,uow:UnitOfWork,entry:dict[str,object],event:MappingProxyType[str,Value],sequence:int,time_us:int,placement:str,mode:str,epoch:int) -> str:
        binding=tuple(cast(str,data(entry)[k]) for k in ('instance_id','host_id'))+(cast(str,entry['object_id']),)
        message_id,kind,identity=event_identity(binding,event)
        existing=self.rows.get('events',message_id,uow)
        payload=canonical_event(event).decode()
        if existing is not None:
            raise DomainFailure('IDEMPOTENCY_CONFLICT','event','CONTENT_MISMATCH')
        self.rows.insert('events',uow,message_id,cast(str,entry['object_id']),sequence,'ACCEPTED',
            {'kind':kind,'identity':identity,'binding':binding,'payload_digest':digest(payload),'received_at_us':time_us,'accepted_placement':placement,'mode_at_accept':mode,'epoch':epoch})
        self.rows.insert('payloads',uow,message_id,cast(str,entry['object_id']),sequence,'PRESENT',{'payload':payload})
        return message_id

    def release_payload(self,uow:UnitOfWork,message_id:str) -> bool:
        payload=self.rows.get('payloads',message_id,uow)
        if payload is None:return False
        self.rows.delete('payloads',uow,payload)
        return True

    def settle_target(self,uow:UnitOfWork,message_id:str,batch_id:str) -> None:
        """Record one consumption without changing the immutable acceptance facts.

        A source can retain the payload indefinitely. Consumption and deletion
        are therefore distinct ingress facts in the same terminal transaction.
        """
        event=self.rows.get('events',message_id,uow)
        if event is None or event['state']!='ACCEPTED':
            raise DomainFailure('PRECONDITION_FAILED','batch','TARGET_CHANGED')
        self.rows.update('events',uow,event,'CONSUMED',{**data(event),'terminal_batch_id':batch_id})
