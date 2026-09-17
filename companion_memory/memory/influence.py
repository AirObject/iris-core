"""Memory-owned bounded influence enumeration and original effect confirmation.

A walk freezes its upper dependent key, persists each page and retains deferred
work. Different root events remain distinct; completed effects are causal visits.
"""
from dataclasses import replace
from typing import cast
from companion_memory.persistence import StatementDefinition,RecordSchema,TableDefinition
from companion_memory.persistence.daily_records import BASE,DailyTable,IndexSpec,daily_catalog,identity
from companion_memory.persistence.semantic_records import ID,N,P,B,Record,fields,enum
from companion_memory.persistence.schema import BoundedTextSchema,InvalidValue
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure

WALK=RecordSchema(BASE+fields(event_id=ID,event_sequence=P,cause_root=ID,changed_object_id=ID,
    through=BoundedTextSchema(128),after=BoundedTextSchema(128),complete=B))
WORK=RecordSchema(BASE+fields(event_id=ID,event_sequence=P,cause_root=ID,changed_object_id=ID,
    affected_object_id=ID,observed_revision=P,state=enum('PENDING','APPLIED','UNCHANGED','DEFERRED_CAPACITY','DEFERRED_CONFLICT','FAILED'),
    last_run=(ID,),step_id=(ID,),reason=enum('NONE','CAPACITY_REACHED','SOURCE_CHANGED','PROVIDER_FAILURE','DEPENDENCY_REMOVED','CAUSAL_VISITED','SUPERSEDED')))
VISIT=RecordSchema(BASE+fields(cause_root=ID,affected_object_id=ID,event_id=ID,step_id=ID))
TABLES=(DailyTable('influence_walks',(WALK,),4096,True,(IndexSpec('sequence',('event_sequence',)),)),
    DailyTable('influence_work',(WORK,),4096,True,(IndexSpec('pending',('last_run','event_sequence','affected_object_id')),)),
    DailyTable('influence_visits',(VISIT,),4096,False,(IndexSpec('cause',('cause_root','affected_object_id')),)))


def influence_catalog():
    catalog=daily_catalog('memory',5,TABLES)
    row=RecordSchema(fields(object_id=ID,revision=P,body=BoundedTextSchema(4096)))
    statements=(
        ('influence_next',StatementDefinition("SELECT object_id,revision,body FROM memory_influence_events WHERE scope_id=:scope_id AND json_extract(body,'$.sequence')>:after AND EXISTS (SELECT 1 FROM memory_basis_edges e WHERE e.scope_id=memory_influence_events.scope_id AND e.basis_id=json_extract(memory_influence_events.body,'$.changed_object_id')) ORDER BY json_extract(body,'$.sequence') LIMIT 1",RecordSchema(fields(after=N)),row,False)),
        ('influence_edge_last',StatementDefinition("SELECT dependent_id FROM memory_basis_edges WHERE scope_id=:scope_id AND basis_id=:basis_id ORDER BY dependent_id DESC LIMIT 1",RecordSchema(fields(basis_id=ID)),RecordSchema(fields(dependent_id=ID)),False)),
        ('influence_edge_page',StatementDefinition("SELECT dependent_id,dependent_revision FROM memory_basis_edges WHERE scope_id=:scope_id AND basis_id=:basis_id AND dependent_id>:after AND dependent_id<=:through ORDER BY dependent_id LIMIT 4",RecordSchema(fields(basis_id=ID,after=BoundedTextSchema(128),through=BoundedTextSchema(128))),RecordSchema(fields(dependent_id=ID,dependent_revision=P)),False)),
        ('influence_pending',StatementDefinition("SELECT object_id,revision,body FROM memory_influence_work WHERE scope_id=:scope_id AND json_extract(body,'$.state') IN ('PENDING','DEFERRED_CAPACITY','DEFERRED_CONFLICT','FAILED') AND (json_extract(body,'$.last_run') IS NULL OR json_extract(body,'$.last_run')!=:run_id) ORDER BY CASE WHEN json_extract(body,'$.last_run') IS NULL THEN 0 ELSE 1 END, json_extract(body,'$.updated_at_us'),json_extract(body,'$.event_sequence'),json_extract(body,'$.affected_object_id') LIMIT 1",RecordSchema(fields(run_id=ID)),row,False)))
    combined=catalog.statements+statements
    indexes=(TableDefinition('memory_influence_pending_order',"CREATE INDEX memory_influence_pending_order ON memory_influence_work(scope_id,json_extract(body,'$.event_sequence'),json_extract(body,'$.affected_object_id')) WHERE json_extract(body,'$.state') IN ('PENDING','DEFERRED_CAPACITY','DEFERRED_CONFLICT','FAILED')"),)
    return StatementCatalog(replace(catalog.definition,tables=catalog.definition.tables+indexes,statements=tuple(s for _,s in combined)),combined)


class Influence:
    def __init__(self,long_term):self.owner=long_term;self.rows=long_term.rows;self.memory=long_term.memory
    def key(self,kind,*parts):return identity(kind,self.rows.database,self.rows.instance,*parts)
    async def next_event(self,after):
        raw=await self.rows.rows.read('influence_next',{'after':after})
        return self.rows.decode('influence_events',raw[0]) if raw else None
    async def pending(self,run_id):
        raw=await self.rows.rows.read('influence_pending',{'run_id':run_id})
        return self.rows.decode('influence_work',raw[0]) if raw else None
    def enumerate(self,uow,event_id,sequence,now):
        event=self.rows.get('influence_events',uow,event_id)
        if event is None or event['sequence']!=sequence:raise InvalidValue()
        wid=self.key('influence-walk',event_id);walk=self.rows.get('influence_walks',uow,wid)
        if walk is not None and walk['complete']:raise OwnerFailure('PRECONDITION_FAILED','source','ALREADY_COMMITTED')
        if walk is None:
            last=self.rows.rows.stage('influence_edge_last',uow,{'basis_id':event['changed_object_id']})
            walk=self.owner.base(wid,now)|{'event_id':event_id,'event_sequence':sequence,'cause_root':event['cause_root'],
                'changed_object_id':event['changed_object_id'],'through':last[0]['dependent_id'] if last else '', 'after':'','complete':False}
            previous=None
        else:previous=cast(int,walk['revision'])
        page=self.rows.rows.stage('influence_edge_page',uow,{'basis_id':walk['changed_object_id'],'after':walk['after'],'through':walk['through']})
        source=self.memory.current(uow,event['changed_object_id'])
        superseded=source is not None and source['revision']!=event['changed_revision'] or source is None and event['reason']!='DELETED'
        targets=[]
        for edge in page:
            oid=edge['dependent_id'];key=self.key('influence-work',event_id,oid)
            visit=self.rows.get('influence_visits',uow,self.key('influence-visit',event['cause_root'],oid))
            status='UNCHANGED' if visit is not None or superseded else 'PENDING'
            item=self.rows.write('influence_work',uow,self.owner.base(key,now)|{'event_id':event_id,'event_sequence':sequence,'cause_root':event['cause_root'],
                'changed_object_id':event['changed_object_id'],'affected_object_id':oid,'observed_revision':edge['dependent_revision'],
                'state':status,'last_run':None,'step_id':None,'reason':'CAUSAL_VISITED' if visit is not None else 'SUPERSEDED' if superseded else 'NONE'})
            targets.append(item)
        complete=len(page)<4 or bool(page and page[-1]['dependent_id']==walk['through'])
        walk=self.rows.write('influence_walks',uow,dict(walk)|{'revision':1 if previous is None else previous+1,'updated_at_us':max(now,walk['updated_at_us']),
            'after':page[-1]['dependent_id'] if page else walk['after'],'complete':complete},previous)
        return event,walk,tuple(targets),len(page)

    def obsolete(self,uow,work):
        """Prove current causal work unnecessary without recording a model effect."""
        from .formats import record,sequence
        event=self.rows.get('influence_events',uow,work['event_id'])
        if event is None:raise InvalidValue()
        source=self.memory.current(uow,work['changed_object_id'])
        if source is not None and source['revision']!=event['changed_revision'] or source is None and event['reason']!='DELETED':return 'SUPERSEDED'
        current=self.memory.current(uow,work['affected_object_id'])
        if current is None:return 'DEPENDENCY_REMOVED'
        links=self.memory.links(uow,work['affected_object_id'],current['revision'])
        if not any(record(link)['basis_id']==work['changed_object_id'] for link in sequence(links['bases'])):return 'DEPENDENCY_REMOVED'
        if self.rows.get('influence_visits',uow,self.key('influence-visit',work['cause_root'],work['affected_object_id'])) is not None:return 'CAUSAL_VISITED'
        return None

    def require_current(self,uow,event_id,object_id):
        work=self.rows.get('influence_work',uow,self.key('influence-work',event_id,object_id))
        if work is None or self.obsolete(uow,work) is not None:raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')

    async def maybe_obsolete(self,work):
        """Preparation is only a hint; the committing command proves the reason."""
        from .formats import isolate_links,record,sequence
        from companion_memory.persistence.content_codec import decode_content
        from companion_memory.persistence.deadlines import check_deadline
        event=await self.rows.read('influence_events',work['event_id']);check_deadline()
        if event is None:raise InvalidValue()
        source=await self.memory.rows.read('objects_get',{'object_id':work['changed_object_id']});check_deadline()
        if source and source[0]['revision']!=event['changed_revision'] or not source and event['reason']!='DELETED':return True
        raw=await self.memory.rows.read('links_get',{'object_id':work['affected_object_id']});check_deadline()
        if not raw:return True
        links=isolate_links(decode_content(raw[0]['body'].encode(),2048),work['affected_object_id'],raw[0]['revision'])
        if not any(record(link)['basis_id']==work['changed_object_id'] for link in sequence(links['bases'])):return True
        visit=await self.rows.read('influence_visits',self.key('influence-visit',work['cause_root'],work['affected_object_id']));check_deadline()
        return visit is not None

    def claim(self,uow,key,run_id,step_id,now,state='PENDING',reason='NONE'):
        work=self.rows.get('influence_work',uow,key)
        if work is None or work['state'] in ('APPLIED','UNCHANGED') or work['last_run']==run_id:raise InvalidValue()
        return self.rows.write('influence_work',uow,dict(work)|{'revision':work['revision']+1,'updated_at_us':max(now,work['updated_at_us']+1),
            'last_run':run_id,'step_id':step_id,'state':state,'reason':reason},work['revision'])

    def finish(self,uow,event_id,object_id,step_id,state,now,observed,result_revision):
        key=self.key('influence-work',event_id,object_id);work=self.rows.get('influence_work',uow,key)
        if work is None or work['step_id']!=step_id or work['state']!='PENDING':raise InvalidValue()
        result=self.rows.write('influence_work',uow,dict(work)|{'revision':work['revision']+1,'updated_at_us':now,'state':state,
            'reason':'CAPACITY_REACHED' if state=='DEFERRED_CAPACITY' else 'SOURCE_CHANGED' if state=='DEFERRED_CONFLICT' else 'PROVIDER_FAILURE' if state=='FAILED' else 'NONE'},work['revision'])
        if state in ('APPLIED','UNCHANGED'):
            visit=self.key('influence-visit',work['cause_root'],object_id)
            if self.rows.get('influence_visits',uow,visit) is None:
                self.rows.write('influence_visits',uow,self.owner.base(visit,now)|{'cause_root':work['cause_root'],'affected_object_id':object_id,'event_id':event_id,'step_id':step_id})
            event=self.rows.get('influence_events',uow,event_id)
            if event is None:raise InvalidValue()
            effect='SUPPORT_LOST' if event['reason'] in ('FORGOTTEN','DELETED') else 'REEVALUATED'
            eid=self.key('influence-effect',event_id,object_id,effect)
            self.rows.write('influence_effects',uow,self.owner.base(eid,now)|{'origin_event_id':event_id,'affected_object_id':object_id,'effect_kind':effect,
                'cause_root':work['cause_root'],'observed_revision':observed,'result_revision':result_revision,'step_id':step_id})
        return result

    def score_proof(self,uow,impact,changes):
        """Only a frozen affected object's unchanged provenance may be retained.

        Unavailable basis bodies are never revived. The explicit model score
        assessment changes no content, source membership or dependency links.
        """
        from .formats import record,sequence
        from .long_term import PreservedProvenance
        if len(changes)!=1 or changes[0]['action']!='SET_SCORES':raise InvalidValue()
        change=changes[0];oid=change['target_id']
        work=self.rows.get('influence_work',uow,self.key('influence-work',impact['origin_event_id'],oid))
        if work is None or work['state']!='PENDING' or work['step_id']!=impact['object_id']:raise InvalidValue()
        if self.rows.get('influence_visits',uow,self.key('influence-visit',work['cause_root'],oid)) is not None:
            raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
        current=self.memory.current(uow,oid)
        if current is None or current['revision']!=change['expected_revision']:raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
        links=self.memory.links(uow,oid,current['revision']);value=record(change['proposed_value']);after=record(change['links'])
        expected={'sources':tuple(dict(record(raw))|{'object_revision':value['revision']} for raw in sequence(links['sources'])),
            'bases':tuple(dict(record(raw))|{'dependent_revision':value['revision']} for raw in sequence(links['bases']))}
        if after!=expected or any(value[key]!=current[key] for key in ('content','origin','kind','created_at_us','retention_policy_ref')):raise InvalidValue()
        if not any(record(raw)['basis_id']==work['changed_object_id'] for raw in sequence(links['bases'])):raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
        self.require_current(uow,work['event_id'],oid)
        event=self.rows.get('influence_events',uow,work['event_id'])
        if event is None:raise InvalidValue()
        proof=object.__new__(PreservedProvenance)
        for name,item in (('owner',self.owner),('uow',uow),('current',current),('links',links),('change',change)):object.__setattr__(proof,name,item)
        self.owner._retention_basis=proof
        return proof
