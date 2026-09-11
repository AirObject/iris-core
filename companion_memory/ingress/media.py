"""Optional owned READY-reference participation; never media decoding or retrieval.

A supplied owner validates the complete occurrence against its entry binding in
this same transaction. Buffers protect the enclosing event while any source,
position, history or frozen batch depends on it; release happens only when the
last such reference is gone. No default owner or synthetic success is installed.
"""
from typing import Protocol
from types import MappingProxyType
from companion_memory.persistence import RepositoryDefinition,PersistenceService,UnitOfWork,Value


class MediaParticipant(Protocol):
    owner_module:str
    repositories:tuple[RepositoryDefinition,...]
    def bind(self,storage:PersistenceService,instance_id:str) -> None: ...
    def retain_event(self,uow:UnitOfWork,entry_id:str,message_id:str,media:tuple[Value,...]) -> None: ...
    def settle_event(self,uow:UnitOfWork,message_id:str) -> None: ...
    def release_event(self,uow:UnitOfWork,message_id:str) -> None: ...
