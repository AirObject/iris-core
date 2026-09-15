"""Retrieval-owned paused scheduler root in the configuration transaction.

Bootstrap performs no activation. Its writer lease ends before the runtime
retrieval owner binds; original configuration verification remains read-only.
"""
from companion_memory.persistence import PersistenceService,UnitOfWork
from companion_memory.persistence.semantic_catalog import SemanticRecords
from companion_memory.persistence.semantic_records import Record,identity
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure
from .semantic_repository import semantic_retrieval_catalog,TABLES


class SemanticRetrievalInitialization:
    """Create or inspect the original root without issuing activation authority."""
    def __init__(self,catalog: StatementCatalog | None=None):
        self.catalog=catalog if catalog is not None else semantic_retrieval_catalog()
        self._bound=False;self._closed=False

    def bind(self,storage: PersistenceService,scope: str) -> None:
        if self._bound or self._closed: raise ValueError('Retrieval initializer unavailable.')
        lease=storage.claim_module_owner(self.catalog.definition)
        if lease is None: raise ValueError('Retrieval owner is unavailable.')
        self._lease=lease;self.rows=SemanticRecords(self.catalog,TABLES,storage,scope);self._bound=True

    def initialize(self,uow: UnitOfWork,config: Record,space_id: str) -> Record:
        if not self._bound or self._closed: raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        return self.rows.write('semantic_control',uow,{'v':1,'revision':1,
            'row_id':identity('semantic-control',config['instance_id'],space_id),'space_id':space_id,'config':config,
            'scheduler':'PAUSED','pause_reason':'USER','current_generation':None,'building_generation':None,
            'retiring_generation':None,'authorization_digest':None,'gc_cursor':None,'last_cleanup_at':None,'operation_count':0})

    async def verify(self,config: Record,space_id: str) -> bool:
        value=await self.rows.read('semantic_control',identity('semantic-control',config['instance_id'],space_id))
        return value is not None and value['config']==config and value['space_id']==space_id

    def close(self) -> bool:
        self._closed=True
        return not self._bound or self._lease.release()
