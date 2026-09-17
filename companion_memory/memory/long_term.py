"""Memory-owned accounting anchors and immutable influence events.

Every event joins the actual object mutation and its existing monotonic change
sequence. Ordinary reads never settle time or acknowledge an influence effect.
"""
from typing import TYPE_CHECKING, cast
from dataclasses import dataclass
from companion_memory.persistence import UnitOfWork
from companion_memory.persistence.daily_records import BASE,DailyRows,DailyTable,IndexSpec,daily_catalog,identity
from companion_memory.persistence.semantic_records import Record,ID,N,P,fields,enum
from companion_memory.persistence.schema import RecordSchema,InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.configuration.dream_persistence import StoredDreamConfiguration
from companion_memory.runtime.dream_clock import settle_decay
if TYPE_CHECKING:
    from .transactions import MemoryTransactions

ANCHOR=RecordSchema(BASE+fields(memory_id=ID,memory_revision=P,accounted_until=N,last_used_at=(N,),
    lifecycle=enum('ACTIVE','FORGOTTEN','DELETED'),last_decay_run=(ID,),run_intervals=N))
EVENT=RecordSchema(BASE+fields(origin_event_id=ID,changed_object_id=ID,changed_revision=P,
    reason=enum('CREATED','CONTENT_CHANGED','FORGOTTEN','RESTORED','DELETED'),cause_root=ID,sequence=P))
EFFECT=RecordSchema(BASE+fields(origin_event_id=ID,affected_object_id=ID,effect_kind=enum('SUPPORT_LOST','REEVALUATED'),
    cause_root=ID,observed_revision=P,result_revision=P,step_id=ID))
TABLES=(DailyTable('maintenance_anchors',(ANCHOR,),4096,True,(IndexSpec('memory',('memory_id',)),)),
    DailyTable('influence_events',(EVENT,),4096,False,(IndexSpec('sequence',('sequence',)),)),
    DailyTable('influence_effects',(EFFECT,),4096,False,(IndexSpec('effect',('origin_event_id','affected_object_id','effect_kind')),)))


def maintenance_catalog():
    from .influence import influence_catalog
    from companion_memory.persistence.text_records import extend_catalog
    return extend_catalog(daily_catalog('memory',5,TABLES),influence_catalog(),5)


@dataclass(frozen=True,slots=True,init=False)
class PreservedProvenance:
    """One native score settlement preserves provenance without renewing support."""
    owner:'LongTermMemory'
    uow:UnitOfWork
    current:Record
    links:Record
    change:Record
    def __init__(self):raise TypeError('Only the native memory settlement owner issues this proof.')


class LongTermMemory:
    """A participant of the existing memory owner, using its current source checks."""
    def __init__(self, memory: 'MemoryTransactions'):
        if type(memory.configuration) is not StoredDreamConfiguration:raise InvalidValue()
        self.memory=memory;self.configuration=memory.configuration
        from .influence import TABLES as INFLUENCE_TABLES,Influence
        self.rows=DailyRows(memory._information_catalog,TABLES+INFLUENCE_TABLES,memory.storage,self.configuration.database_id,
                            self.configuration.scope_id,self.configuration.snapshot_id)
        self.settings=self.configuration.candidate.text.record('memory.long_term_maintenance')
        self._retention_basis:PreservedProvenance|None=None
        self.influence=Influence(self)

    def base(self,key:str,now:int):
        return {'format_version':1,'object_id':key,'revision':1,'database_id':self.configuration.database_id,
            'instance_id':self.configuration.scope_id,'config_snapshot_id':self.configuration.snapshot_id,
            'created_at_us':now,'updated_at_us':now}

    def anchor_id(self,object_id:str):
        return identity('maintenance-anchor',self.configuration.database_id,self.configuration.scope_id,object_id)

    def changed(self,uow:UnitOfWork,object_id:str,revision:int,deleted:bool,sequence:int,
                *,cause_root:str|None=None,used_at:int|None=None):
        """Record one real revision without moving its outstanding time anchor."""
        current=self.memory.current(uow,object_id)
        old=self.rows.get('maintenance_anchors',uow,self.anchor_id(object_id))
        if deleted:
            if current is not None or old is None:raise InvalidValue()
            from .formats import TOMBSTONE_SCHEMA,isolate
            from companion_memory.persistence.content_codec import decode_content
            raw=self.memory.rows.stage('tombstones_get',uow,{'object_id':object_id})
            if len(raw)!=1:raise InvalidValue()
            tombstone=isolate(TOMBSTONE_SCHEMA,decode_content(cast(str,raw[0]['body']).encode(),1024),1024)
            if tombstone['deletion_revision']!=revision:raise InvalidValue()
            now=cast(int,tombstone['deleted_at_us']);lifecycle='DELETED'
        else:
            if current is None or current['revision']!=revision:raise InvalidValue()
            now=cast(int,current['modified_at_us']);lifecycle=cast(str,current['lifecycle'])
        if old is None:
            if current is None or revision!=1:raise InvalidValue()
            value=self.base(self.anchor_id(object_id),cast(int,current['created_at_us'])) | {
                'memory_id':object_id,'memory_revision':revision,'accounted_until':current['created_at_us'],
                'last_used_at':used_at,'lifecycle':lifecycle,'last_decay_run':None,'run_intervals':0}
            previous=None
        else:
            if old['memory_revision']!=revision-1:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            previous=cast(int,old['revision'])
            value=dict(old)|{'revision':previous+1,'updated_at_us':max(now,cast(int,old['updated_at_us'])),
                'memory_revision':revision,'lifecycle':lifecycle,
                'last_used_at':max(used_at,cast(int,old['last_used_at'] or 0)) if used_at is not None else old['last_used_at']}
        self.rows.write('maintenance_anchors',uow,value,previous)
        eid=identity('influence-event',self.configuration.database_id,self.configuration.scope_id,sequence)
        reason='CREATED' if old is None else 'DELETED' if deleted else 'FORGOTTEN' if lifecycle=='FORGOTTEN' and old['lifecycle']!='FORGOTTEN' else 'RESTORED' if lifecycle=='ACTIVE' and old['lifecycle']=='FORGOTTEN' else 'CONTENT_CHANGED'
        self.rows.write('influence_events',uow,self.base(eid,now)|{'origin_event_id':eid,'changed_object_id':object_id,
            'changed_revision':revision,'reason':reason,'cause_root':cause_root or eid,'sequence':sequence})

    def preview(self,uow:UnitOfWork,object_id:str,revision:int,run_id:str,now:int):
        current=self.memory.current(uow,object_id)
        anchor=self.rows.get('maintenance_anchors',uow,self.anchor_id(object_id))
        if current is None or anchor is None or current['revision']!=revision or anchor['memory_revision']!=revision:
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        return current,anchor,self.elapsed(current,anchor,run_id,now)

    def elapsed(self,current:Record,anchor:Record,run_id:str,now:int):
        """Disabled settlement leaves the complete outstanding debt at its owner."""
        from .formats import record
        from companion_memory.runtime.dream_clock import DecaySettlement
        elapsed=settle_decay(retention=cast(int,record(current['scores'])['retention']),
            accounted_until=cast(int,anchor['accounted_until']),now_us=now,
            interval_seconds=cast(int,self.settings['interval_seconds']),decrement=cast(int,self.settings['retention_decrement']),
            max_intervals=cast(int,self.settings['max_intervals_per_object_run']),
            already_applied_in_run=cast(int,anchor['run_intervals']) if anchor['last_decay_run']==run_id else 0)
        if self.settings['decay_enabled']:return elapsed
        return DecaySettlement(cast(int,record(current['scores'])['retention']),cast(int,anchor['accounted_until']),0,
            elapsed.remaining_intervals+elapsed.applied_intervals,elapsed.clock_regressed)

    def settle(self,uow:UnitOfWork,object_id:str,revision:int,run_id:str,now:int,operation_key:str):
        """Settle time, native score/lifecycle/history and the anchor in one UoW."""
        from .formats import record,sequence,transition
        from .changes import isolate_change
        from .transactions import ApplyScope
        if not self.settings['decay_enabled']:raise OwnerFailure('ACCESS_DENIED','configuration','OPERATION_NOT_GRANTED')
        current,anchor,elapsed=self.preview(uow,object_id,revision,run_id,now)
        if elapsed.applied_intervals==0:return current,anchor,elapsed,None
        applied=None
        if elapsed.retention!=record(current['scores'])['retention']:
            links=self.memory.links(uow,object_id,revision);body=record(current['content']);subject_ids=set()
            readable={cast(str,record(b)['basis_id']) for b in sequence(links['bases'])}
            if current['kind']=='MEMORY':
                subject_ids.update(cast(tuple[str,...],body['subject_ids']))
                if body['speaker_subject_id'] is not None:subject_ids.add(cast(str,body['speaker_subject_id']))
            else:
                for ref in (record(body['from_ref']),record(body['to_ref'])):
                    (subject_ids if ref['type']=='SUBJECT' else readable).add(cast(str,ref['id']))
            world=record(body['world_scope'])
            if world['context_id'] is not None:subject_ids.add(cast(str,world['context_id']))
            lifecycle,since=transition(current,elapsed.retention,now,self.memory._settings.integer('memory.forget_below'),self.memory._settings.integer('memory.restore_at'))
            value=dict(current)|{'revision':revision+1,'modified_at_us':max(now,cast(int,current['modified_at_us'])),
                'scores':dict(record(current['scores']))|{'retention':elapsed.retention},'lifecycle':lifecycle,'forgotten_since_us':since}
            updated_links={'sources':tuple(dict(record(link))|{'object_revision':revision+1} for link in sequence(links['sources'])),
                'bases':tuple(dict(record(link))|{'dependent_revision':revision+1} for link in sequence(links['bases']))}
            change=isolate_change({'change_version':1,'action':'SET_SCORES','target_id':object_id,'expected_revision':revision,
                'proposed_value':value,'links':updated_links},8192,text_format=self.memory.text_format)
            origin=record(current['origin'])
            scope=ApplyScope(self.configuration.scope_id,cast(str|None,origin['candidate_id']),cast(str|None,origin['batch_id']),
                frozenset(readable),frozenset(subject_ids),frozenset((object_id,)),frozenset(cast(str,record(link)['source_id']) for link in sequence(links['sources'])))
            proof=object.__new__(PreservedProvenance)
            for name,item in (('owner',self),('uow',uow),('current',current),('links',links),('change',change)):object.__setattr__(proof,name,item)
            self._retention_basis=proof
            try:applied=self.memory.apply_change_set(uow,scope,(change,),None,None,now,operation_key,'MAINTENANCE',cause_root=operation_key,retention_basis=proof)
            finally:self._retention_basis=None
        after=self.rows.get('maintenance_anchors',uow,self.anchor_id(object_id))
        if after is None:raise InvalidValue()
        previous=cast(int,after['revision'])
        self.rows.write('maintenance_anchors',uow,dict(after)|{'revision':previous+1,'updated_at_us':max(now,cast(int,after['updated_at_us'])),
            'accounted_until':elapsed.accounted_until,'last_decay_run':run_id,
            'run_intervals':(cast(int,anchor['run_intervals']) if anchor['last_decay_run']==run_id else 0)+elapsed.applied_intervals},previous)
        return current,anchor,elapsed,applied

    def consume_retention_basis(self,proof:object,uow:UnitOfWork,changes:tuple[Record,...]) -> PreservedProvenance:
        """This exact computed change cannot be reused for a model or other write."""
        if (type(proof) is not PreservedProvenance or proof is not self._retention_basis or proof.owner is not self
                or proof.uow is not uow or changes!=(proof.change,)):
            raise OwnerFailure('ACCESS_DENIED','capability','BINDING_MISMATCH')
        self._retention_basis=None
        return proof
