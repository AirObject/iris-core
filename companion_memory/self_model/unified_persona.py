"""One current pointer across imported, initial and independently reviewed persona.

The reader shares the original self-model owner's lease. It cannot create a
candidate, approve a review or obtain historical publication bodies from a query.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .periodic_persona import PeriodicPersona
import asyncio
import time
from typing import cast
from types import MappingProxyType
from companion_memory.configuration.dream_persistence import StoredDreamConfiguration
from companion_memory.persistence import Found,NotFound,UnitOfWork
from companion_memory.persistence.daily_records import DailyRows,identity
from companion_memory.persistence.semantic_records import Record
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.content_codec import encode_content
from .periodic_records import TABLES
from .periodic_leaves import join_candidate
from .periodic_output import decode_persona_candidate
from .periodic_projection import project_current
from .approved_import import ApprovedPersonaImport
from .daily_persona import DailyPersona


def initialize_pointer(source,uow:UnitOfWork,publication_id:str,origin:str,operation:Record,now:int):
    """Join the first actual publication; absence on reopen never authorizes repair."""
    config=source.configuration
    if type(config) is not StoredDreamConfiguration:raise InvalidValue()
    rows=DailyRows(source.catalog,TABLES,source.storage,config.database_id,config.scope_id,config.snapshot_id)
    key=identity('current-persona',config.database_id,config.scope_id)
    if rows.get('current_persona',uow,key) is not None:raise OwnerFailure('PRECONDITION_FAILED','persona','ALREADY_PUBLISHED')
    rows.write('current_persona',uow,{'format_version':1,'object_id':key,'revision':1,'database_id':config.database_id,
        'instance_id':config.scope_id,'config_snapshot_id':config.snapshot_id,'created_at_us':now,'updated_at_us':now,
        'publication_id':publication_id,'publication_revision':1,'origin':origin,'last_operation':operation})
    return key


class UnifiedPersona:
    def __init__(self,source:ApprovedPersonaImport|DailyPersona,memory):
        if type(source) not in (ApprovedPersonaImport,DailyPersona) or not source.bound or type(source.configuration) is not StoredDreamConfiguration:raise InvalidValue()
        self.source=source;self.memory=memory;self.configuration=source.configuration;self.storage=source.storage
        self.catalog=source.catalog;self.rows=DailyRows(self.catalog,TABLES,self.storage,self.configuration.database_id,self.configuration.scope_id,self.configuration.snapshot_id)
        self.pointer_id=identity('current-persona',self.configuration.database_id,self.configuration.scope_id)
        self.periodic:PeriodicPersona|None=None
        self.bound=True;self.closed=False;self._reader:asyncio.Task|None=None

    def _periodic(self,pointer,publication,candidate,leaves,objects,*,bases_current:bool):
        if (publication['object_id']!=pointer['publication_id'] or publication['revision']!=pointer['publication_revision']
                or candidate['object_id']!=publication['candidate_id'] or candidate['revision']!=publication['candidate_revision']
                or candidate['manifest']['digest']!=publication['candidate_digest']):raise InvalidValue()
        value=decode_persona_candidate(join_candidate(candidate['manifest'],tuple(leaf['leaf'] for leaf in leaves)),tuple(candidate['basis_refs']))
        stale=not bases_current or any(objects.get(ref['object_id']) is None or objects[ref['object_id']]['revision']!=ref['revision'] or objects[ref['object_id']]['lifecycle']!='ACTIVE' for ref in cast(tuple[Record,...],publication['basis_refs']))
        current=MappingProxyType({'publication_id':publication['object_id'],'revision':pointer['revision'],'text':value['text'],
            'generated_at_us':publication['created_at_us'],'review':'MODEL_REVIEWED','model_origin':'REMOTE_PROVIDER','publication_origin':'PERIODIC_REVIEWED','stale':stale})
        project_current(current)
        return current

    async def read_current(self,deadline:float|None=None):
        if self.closed or self._reader is not None:raise OwnerFailure('RESOURCE_BUSY','persona','CLEANUP_PENDING',self._reader is not None)
        end=time.monotonic()+5 if deadline is None else deadline
        async def read():
            with DeadlineScope(end):
                pointer=await self.rows.read('current_persona',self.pointer_id)
                if pointer is None:return NotFound()
                if pointer['origin']!='PERIODIC_REVIEWED':
                    found=await self.source.read_current(end)
                    if type(found) is not Found or found.value['publication_id']!=pointer['publication_id']:raise InvalidValue()
                    current=MappingProxyType(dict(found.value)|{'revision':pointer['revision']})
                else:
                    publication=await self.rows.read('periodic_persona_publications',cast(str,pointer['publication_id']))
                    if publication is None:raise InvalidValue()
                    candidate=await self.rows.read('periodic_persona_candidates',cast(str,publication['candidate_id']))
                    if candidate is None or self.periodic is None:raise InvalidValue()
                    for value in (pointer,publication,candidate):await self.periodic.verify_original(value)
                    leaves=[]
                    manifest=cast(Record,candidate['manifest'])
                    for ordinal in range(cast(int,manifest['leaf_count'])):
                        key=identity('periodic-leaf',self.configuration.database_id,self.configuration.scope_id,candidate['object_id'],ordinal)
                        leaf=await self.rows.read('periodic_persona_leaves',key)
                        if leaf is None:raise InvalidValue()
                        leaves.append(leaf)
                    if self.memory.information is None:raise InvalidValue()
                    objects={ref['object_id']:await self.memory.information.index_current(ref['object_id']) for ref in cast(tuple[Record,...],publication['basis_refs'])}
                    from companion_memory.memory.dream_view import current_basis_revisions
                    bases_current=True
                    for ref in cast(tuple[Record,...],publication['basis_refs']):
                        if not await current_basis_revisions(self.memory,ref):bases_current=False;break
                    current=self._periodic(pointer,publication,candidate,leaves,objects,bases_current=bases_current)
                if await self.rows.read('current_persona',self.pointer_id)!=pointer:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
                return Found(current)
        actual,logical=start_owned(read());self._reader=actual
        def ended(job):
            if not job.cancelled():job.exception()
            if self._reader is job:self._reader=None
        actual.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,end-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','persona','DEADLINE_EXCEEDED',True)
        return logical.result()

    def participate_current(self,uow:UnitOfWork,publication_id:str,revision:int):
        if self.closed:raise InvalidValue()
        pointer=self.rows.get('current_persona',uow,self.pointer_id)
        if pointer is None or pointer['publication_id']!=publication_id or pointer['revision']!=revision:return None
        if pointer['origin']!='PERIODIC_REVIEWED':
            current=self.source.participate_current(uow,publication_id,cast(int,pointer['publication_revision']))
            return None if current is None else MappingProxyType(dict(current)|{'revision':revision})
        publication=self.rows.get('periodic_persona_publications',uow,publication_id)
        if publication is None:raise InvalidValue()
        candidate=self.rows.get('periodic_persona_candidates',uow,cast(str,publication['candidate_id']))
        if candidate is None or self.periodic is None:raise InvalidValue()
        for value in (pointer,publication,candidate):self.periodic.verify_record(uow,value)
        leaves=[]
        for ordinal in range(cast(int,cast(Record,candidate['manifest'])['leaf_count'])):
            key=identity('periodic-leaf',self.configuration.database_id,self.configuration.scope_id,candidate['object_id'],ordinal)
            leaf=self.rows.get('periodic_persona_leaves',uow,key)
            if leaf is None:raise InvalidValue()
            leaves.append(leaf)
        objects={ref['object_id']:self.memory.current(uow,ref['object_id']) for ref in cast(tuple[Record,...],publication['basis_refs'])}
        from companion_memory.memory.dream_view import current_roots
        bases_current=True
        for current in objects.values():
            if current is None or current['lifecycle']!='ACTIVE':bases_current=False;break
            try:current_roots(self.memory,uow,current,allow_changed=False)
            except OwnerFailure as failure:
                if failure.reason not in ('SOURCE_CHANGED','CAPACITY_REACHED'):raise
                bases_current=False;break
        return self._periodic(pointer,publication,candidate,leaves,objects,bases_current=bases_current)

    def close(self):
        self.closed=True
        return self._reader is None
