"""Continuous-forgetting deletion reuses the original memory release authority.

Plan creation, exact shared-source release, tombstone, indices and the dream step
commit together. No exposed plan can be retried under a different run intention.
"""
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import ResultBoundCommandDefinition,RecordSchema,Field,SequenceSchema,UnitOfWork,Committed
from companion_memory.persistence.semantic_records import ID,P,N,Record,fields
from companion_memory.persistence.schema import InvalidValue,Value
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.deadlines import check_deadline
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record,sequence,isolate_links
from companion_memory.memory.release_plans import observe_release,read_plan
from companion_memory.runtime.content_assembly import owner_fact,stable
from companion_memory.persistence import AuditResultBinding,AuditFieldBinding
from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence.daily_results import TARGETS
from .results import audits,result_schema,result,target,INTENT
from .records import validate_run

PREFIX='expire_dream_memory_'


class DreamExpiry:
    """A sealed transaction context permits only a currently due original object."""
    def __init__(self,control,content,participants):
        self.control=control;self.content=content;self._active:tuple[UnitOfWork,Record,int]|None=None
        commands=[]
        for mask,extra in (('none',()),('ingress',('ingress',)),('media',('media',)),('ingress_media',('ingress','media'))):
            kind=PREFIX+mask;owners=('dream','memory')+extra
            requirements,bindings=audits(kind,owners)
            requirements+=(AuditRequirement('logging_service','object_history',kind.upper(),1,('APPLY',),owner_fact('logging_service'),target_limit=16),)
            bindings+=(AuditResultBinding('object_history',1,(AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),
                AuditFieldBinding('actor_ref','INTENT',('actor',)),AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),
                AuditFieldBinding('target_refs','RESULT',('history_targets',)),AuditFieldBinding('change','RESULT',('history_fact',)))),)
            shape=RecordSchema(result_schema(owners,('APPLIED',)).fields+(Field('history',SequenceSchema(ID,1,1)),
                Field('history_targets',TARGETS),Field('history_fact',owner_fact('logging_service'))))
            def handle(uow,values,operation=kind):
                try:return self.handle(operation,uow,values)
                except OwnerFailure as failure:
                    control.causes.record(operation,values,failure);raise
            commands.append(ResultBoundCommandDefinition('dream',kind,1,RecordSchema(fields(operation_id=ID,run_id=ID,
                expected_revision=P,mode_epoch=N,memory_id=ID,memory_revision=P,observed_at_us=N)),1,shape,participants,requirements,handle,INTENT,bindings))
        self.commands=tuple(commands)

    def due(self,current,now):
        settings=self.content.configuration.candidate.information.record('memory.usage')
        since=current['forgotten_since_us']
        return bool(settings['expiry_scan_enabled'] and current['lifecycle']=='FORGOTTEN' and since is not None
            and cast(int,since)+cast(int,settings['forgotten_retention_seconds'])*1000000<=now)

    def cutoff(self,now):
        settings=self.content.configuration.candidate.information.record('memory.usage')
        return max(0,now-cast(int,settings['forgotten_retention_seconds'])*1000000)

    async def next_due(self,run):
        information=self.content.memory.information
        if information is None:raise InvalidValue()
        if not self.content.configuration.candidate.information.record('memory.usage')['expiry_scan_enabled']:return None
        page=await information.expired_page(self.cutoff(self.control.now()),run['expiry_after_us'],run['expiry_after_id'],1)
        check_deadline()
        return page[0] if page else None

    def exhausted_cursor(self,uow,run,now):
        information=self.content.memory.information
        if information is None:raise InvalidValue()
        if not information.expired_after(uow,self.cutoff(now),run['expiry_after_us'],run['expiry_after_id']):
            return {'expiry_after_us':0,'expiry_after_id':''}
        return {}

    def verify_active(self,uow):
        if self._active is None or self._active[0] is not uow:raise InvalidValue()
        _,values,now=self._active
        self.control.participate_run(uow,values['run_id'],values['expected_revision'],values['mode_epoch'])
        current=self.content.memory.current(uow,values['memory_id'])
        if current is None or current['revision']!=values['memory_revision'] or not self.due(current,now):
            raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')

    def definition(self,kind):
        if not kind.startswith(PREFIX):return self.content.command_definition(kind)
        found=next((d for d in self.commands if d.operation_kind==kind),None)
        if found is None:raise InvalidValue()
        return found

    async def select(self,current,run):
        """Read only to choose a fixed branch; all holder counts are rechecked."""
        memory=self.content.memory;ingress=False;media=False
        raw=await memory.rows.read('links_get',{'object_id':current['object_id']});check_deadline()
        if len(raw)!=1 or raw[0]['revision']!=current['revision']:raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
        links=isolate_links(decode_content(raw[0]['body'].encode(),2048),current['object_id'],current['revision'])
        for item in sequence(links['sources']):
            source=await memory.rows.read('sources_get',{'source_id':record(item)['source_id']});check_deadline()
            if len(source)!=1:raise InvalidValue()
            row=source[0]
            if row['holder_count']!=1:continue
            if row['body'] is None:raise InvalidValue()
            from companion_memory.memory.fixed_source import is_fixed_source,decode_fixed_source
            from companion_memory.memory.sources import decode_source
            body=decode_fixed_source(row['body']) if is_fixed_source(row['body']) else decode_source(row['body'])
            members=sequence(body.get('ordered_members',()))
            ingress=ingress or bool(members)
            media=media or any(sequence(record(member)['media']) for member in members)
        mask='_'.join(name for name,present in (('ingress',ingress),('media',media)) if present) or 'none'
        key=stable('dream-expiry',run['run_id'],current['object_id'],current['revision'])
        return await self.control.execute(PREFIX+mask,key,{'run_id':run['run_id'],'expected_revision':run['revision'],
            'mode_epoch':run['mode_epoch'],'memory_id':current['object_id'],'memory_revision':current['revision'],
            'observed_at_us':self.control.now()},actor='dream_coordinator')

    def handle(self,kind,uow,v):
        c=self.control;memory=self.content.memory;now=c._ready(uow)
        run=c.require_dispatch(uow,v['run_id'],v['expected_revision'],v['mode_epoch'])
        if not c.settled(run):raise OwnerFailure('RESOURCE_BUSY','run','RUN_ACTIVE')
        if v['observed_at_us']>now or now>=run['deadline_at_us']:raise OwnerFailure('TIMEOUT','run','DEADLINE_EXCEEDED')
        if run['objects_used']>=c.execution_configuration().text.record('dream.resources')['objects_per_run']:
            raise OwnerFailure('RESOURCE_BUSY','run','CAPACITY_REACHED')
        self._active=(uow,v,now)
        try:
            self.verify_active(uow)
            current=memory.current(uow,v['memory_id'])
            if current is None or current['forgotten_since_us'] is None:raise InvalidValue()
            expiry_time=cast(int,current['forgotten_since_us'])
            if (expiry_time,cast(str,v['memory_id']))<=(cast(int,run['expiry_after_us']),cast(str,run['expiry_after_id'])):
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            from companion_memory.persistence.record_primitives import identity
            original=identity('expired_memory',v['memory_id'],v['memory_revision'])
            root=stable('memory_root',c.configuration.database_id,v['memory_id'],original)
            change=MappingProxyType({'change_version':1,'action':'DELETE_OBJECT','target_id':v['memory_id'],
                'expected_revision':v['memory_revision'],'proposed_value':None,'links':None})
            leaves=observe_release(memory,uow,change)
            mask='_'.join(part for part,present in (('ingress',any(leaf['payloads'] for leaf in leaves)),('media',any(leaf['has_media'] for leaf in leaves))) if present) or 'none'
            if kind!=PREFIX+mask:raise OwnerFailure('PRECONDITION_FAILED','source','OWNERSHIP_CHANGED')
            roots=memory.rows.stage('release_roots_get',uow,{'root_id':root});previous=None
            if roots:
                plans=memory.rows.stage('plan_for_root',uow,{'root_id':root,'ordinal':roots[0]['last_ordinal']})
                if len(plans)!=1:raise InvalidValue()
                previous=plans[0]['plan_id']
            values=MappingProxyType({'operation_id':v['operation_id'],'root_id':root,'change':encode_content(change,8192).decode(),
                'authorized_sources':(),'authorized_objects':(),'authorized_subjects':(),'previous_plan':previous})
            plan=self.content.maintenance.handle('plan_memory_change',uow,values,expiry=self)
            pid=plan['plan_id']
            applied,release=self.content.maintenance.handle(kind,uow,MappingProxyType({'operation_id':v['operation_id'],'plan_id':pid}),expiry=self)
        finally:self._active=None
        op=c._operation(uow)
        from companion_memory.persistence.daily_records import identity as dream_identity
        sid=dream_identity('dream-step',c.configuration.database_id,c.configuration.scope_id,run['run_id'],run['steps_completed'])
        c.rows.write('steps',uow,c._base(sid,now)|{'run_id':run['run_id'],'ordinal':run['steps_completed'],'kind':'EXPIRY','state':'APPLIED',
            'mode_epoch':run['mode_epoch'],'permit_digest':sha256(encode_content(v,8192)).hexdigest(),'origin_event_id':None,'cause_root':root,
            'object_ref':{'object_id':v['memory_id'],'revision':v['memory_revision']},'accounted_from':None,'accounted_through':None,
            'material_id':None,'material_digest':None,'candidate_id':None,'request_key':None,'request_id':None,'handoff_id':None,
            'execution_key':v['operation_id'],'plan_id':pid,'local_confirmation':'CONFIRMED','remote_result':'NONE','cleanup_pending':False,
            'deadline_at_us':run['deadline_at_us'],'result_digest':None,'original_operation':op,'last_operation':op})
        c.rows.write('runs',uow,validate_run(dict(run)|{'revision':run['revision']+1,'updated_at_us':max(now,run['updated_at_us']),
            'steps_completed':run['steps_completed']+1,'objects_used':run['objects_used']+1,'expiry_after_us':expiry_time,'expiry_after_id':v['memory_id'],
            'coverage':'PARTIAL','remaining_work':True,'last_operation':op}),run['revision'])
        counts=c.storage.transaction_row_changes(uow)
        facts:dict[str,object]={'dream':{'rows_changed':counts['dream'],'targets':(target(sid,1),target(run['run_id'],run['revision']+1,run['revision']))},
            'memory':{'rows_changed':counts['memory'],'targets':tuple(target(item['object_id'],item['revision'],item['previous_revision']) for item in applied.objects)}}
        for owner,refs in release.audit_targets(uow).items():facts[owner]={'rows_changed':counts[owner],'targets':refs}
        value:dict[str,object]=dict(result(v['operation_id'],'APPLIED',facts))
        value.update(history=tuple(h['history_id'] for h in applied.history),history_targets=tuple(target(h['object_id'],h['previous_revision']+1,h['previous_revision']) for h in applied.history),
            history_fact={'rows_changed':counts['logging_service'],'state':'APPLIED','references':(),'counts':({'name':'history_items','count':len(applied.history)},),
                'history_ids':tuple(h['history_id'] for h in applied.history)})
        return value
