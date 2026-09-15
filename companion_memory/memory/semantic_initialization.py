"""Memory-owned initial semantic coverage in the configuration transaction.

The bootstrap writer relinquishes its lease before the ordinary memory owner
binds. Its retained read ports may verify the original root during recovery.
"""
from companion_memory.persistence import PersistenceService,UnitOfWork
from companion_memory.persistence.semantic_catalog import SemanticRecords
from companion_memory.persistence.semantic_records import Record,identity
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure
from .semantic_repository import semantic_memory_catalog,TABLES


class SemanticMemoryInitialization:
    """Create one native publication root without marking objects or sending."""
    def __init__(self,catalog: StatementCatalog | None=None):
        self.catalog=catalog if catalog is not None else semantic_memory_catalog()
        self._bound=False;self._closed=False

    def bind(self,storage: PersistenceService,scope: str) -> None:
        if self._bound or self._closed: raise ValueError('Memory initializer unavailable.')
        lease=storage.claim_module_owner(self.catalog.definition)
        if lease is None: raise ValueError('Memory owner is unavailable.')
        self._lease=lease;self.rows=SemanticRecords(self.catalog,TABLES,storage,scope);self._bound=True

    def initialize(self,uow: UnitOfWork,config: Record,space_id: str) -> Record:
        if not self._bound or self._closed: raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        return self.rows.write('semantic_publication',uow,{'v':1,'revision':1,
            'row_id':identity('semantic-publication',config['instance_id'],space_id),'space_id':space_id,
            'config':config,'material_seq':0,'published_seq':0,'generation_id':None,'published_at':None})

    async def verify(self,config: Record,space_id: str) -> bool:
        value=await self.rows.read('semantic_publication',identity('semantic-publication',config['instance_id'],space_id))
        return value is not None and value['config']==config and value['space_id']==space_id

    def close(self) -> bool:
        self._closed=True
        return not self._bound or self._lease.release()
