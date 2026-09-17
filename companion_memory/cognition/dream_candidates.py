"""Formal-object grounded dream proposals with no invented input batch or source.

This pure transform owns no write authority. The native consumer must repeat
current object/source checks and use memory/goals participants in one UoW.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.daily_records import identity
from companion_memory.persistence.schema import InvalidValue,ValueTooLarge,Value
from companion_memory.persistence.semantic_records import Record
from companion_memory.memory.formats import record,sequence,MEMORY_CONTENT,RELATION_CONTENT,transition
from companion_memory.memory.changes import isolate_change
from .goal_proposals import isolate_goal_change
from .dream_output import decode_dream_output


@dataclass(frozen=True,slots=True)
class DreamCandidate:
    """The independent DREAM origin remains separate from daily batch manifests."""
    candidate_id:str
    run_id:str
    step_id:str
    request_id:str
    handoff_id:str
    material_digest:str
    decision:str
    reason:str
    leaves:tuple[Record,...]
    origin:str='DREAM'

    @property
    def digest(self):
        return sha256(encode_content((self.origin,self.candidate_id,self.run_id,self.step_id,self.request_id,
            self.handoff_id,self.material_digest,self.decision,self.reason,self.leaves),73728)).hexdigest()


def build(configuration,run_id:str,step_id:str,request_id:str,handoff_id:str,material_digest:str,
          raw:bytes,evidence:tuple[Record,...],subjects:tuple[Record,...],routes:tuple[str,...],now:int,*,writable:tuple[str,...],influence:Record|None=None) -> DreamCandidate:
    """Use all supplied legal basis/source links, or reject/defer the whole set."""
    output=decode_dream_output(raw)
    if influence is not None:
        actions=sequence(output['actions'])
        if output['decision']=='CHANGE':
            if len(actions)!=1:raise InvalidValue()
            action=record(actions[0]);basis=sequence(action['basis_refs'])
            if action['action']!='SET_SCORES' or action['object_id'] not in writable or len(basis)!=1 or record(basis[0])['object_id']!=action['object_id']:raise InvalidValue()
    settings=configuration.candidate.content
    cid=identity('dream-candidate',configuration.database_id,configuration.scope_id,run_id,step_id,handoff_id)
    objects={};links={};roots={};source_bodies={}
    for selected in evidence:
        value=record(selected['object']);oid=cast(str,value['object_id'])
        objects[oid]=value;links[oid]=record(selected['links']);roots[oid]=selected['independent_roots']
        for item in sequence(selected['sources']):
            source=record(item);source_bodies[cast(str,source['source_id'])]=source
    identities={cast(str,s['subject_id']):s for s in subjects};created={};leaves=[];seen=set()
    def resolve(ref):
        value=record(ref)
        if 'existing_id' in value:return cast(str,value['existing_id'])
        return created[cast(int,value['local_ref'])]
    def subject(ref):
        sid=resolve(ref)
        if sid not in identities:raise InvalidValue()
        return sid
    for ordinal,raw_action in enumerate(sequence(output['actions'])):
        action=record(raw_action);kind=cast(str,action['action']);basis=[]
        for raw_basis in sequence(action['basis_refs']):
            ref=record(raw_basis);bid=cast(str,ref['object_id']);value=objects.get(bid)
            if value is None or value['lifecycle']!='ACTIVE' or value['revision']!=ref['expected_revision']:raise InvalidValue()
            basis.append((ref,value))
        old=None
        if kind in ('SET_SCORES','REPLACE_CURRENT'):
            oid=cast(str,action['object_id']);old=objects.get(oid)
            if oid not in writable or old is None or old['revision']!=action['expected_revision']:raise InvalidValue()
        else:oid=identity('dream-goal' if kind=='CREATE_GOAL' else 'dream-object',configuration.database_id,configuration.scope_id,cid,ordinal)
        if oid in seen:raise InvalidValue()
        seen.add(oid)
        if kind=='CREATE_GOAL':
            world=cast(str,action['world_scope'])
            candidates=[created[cast(int,ref)] for ref in sequence(action['basis_action_refs'])]+[cast(str,b['object_id']) for _,b in basis]
            if not candidates:raise InvalidValue()
            for bid in candidates:
                obj=objects.get(bid)
                if obj is None:raise InvalidValue()
                declared=record(record(obj['content'])['world_scope'])
                if world!=('REAL' if declared['kind']=='REAL' else str(declared['kind'])+':'+str(declared['context_id'])):raise InvalidValue()
            if action['route_id'] is not None and action['route_id'] not in routes:raise InvalidValue()
            if action['deadline'] is not None:
                source_ids={cast(str,record(s)['source_id']) for bid in candidates for s in sequence(links[bid]['sources'])}
                check_deadline_source(cast(int,action['deadline']),tuple(source_bodies[sid] for sid in sorted(source_ids)))
            proposed={k:action[k] for k in ('content','world_scope','deadline','reminder_lead_seconds','route_id')}
            proposed.update(subject_ids=tuple(sorted(subject(ref) for ref in sequence(action['subject_refs']))),basis_id=candidates[0])
            leaf=isolate_goal_change({'change_version':1,'action':kind,'target_id':oid,'expected_revision':None,'proposed_value':proposed,'links':None},8192)
        else:
            object_kind=cast(str,old['kind']) if old is not None else 'MEMORY' if kind=='CREATE_MEMORY' else 'RELATION'
            supplied=record(old['content']) if kind=='SET_SCORES' and old is not None else record(action['content']) if kind=='REPLACE_CURRENT' else action
            fields=MEMORY_CONTENT.fields if object_kind=='MEMORY' else RELATION_CONTENT.fields
            content={f.name:supplied[f.name] for f in fields}
            if kind!='SET_SCORES':
                if object_kind=='MEMORY':
                    content['subject_ids']=tuple(subject(r) for r in sequence(supplied['subject_ids']))
                    content['speaker_subject_id']=None if supplied['speaker_subject_id'] is None else subject(supplied['speaker_subject_id'])
                else:
                    for name in ('from_ref','to_ref'):
                        ref=record(supplied[name]);rid=subject(ref['id']) if ref['type']=='SUBJECT' else resolve(ref['id'])
                        target=identities.get(rid) if ref['type']=='SUBJECT' else objects.get(rid)
                        if target is None or target['revision']!=ref['expected_revision']:raise InvalidValue()
                        content[name]=MappingProxyType(dict(ref)|{'id':rid})
            world=record(content['world_scope'])
            if world['context_id'] is not None and (world['context_id'] not in identities or identities[world['context_id']]['kind']!='CONTEXT'):raise InvalidValue()
            if any(record(value['content'])['world_scope']!=world for _,value in basis):raise InvalidValue()
            revision=1 if old is None else cast(int,old['revision'])+1
            retention=settings.integer('memory.initial_retention') if old is None else cast(int,record(old['scores'])['retention'])
            reason='Configured initial retention.' if old is None else record(old['scores'])['retention_reason']
            if kind=='SET_SCORES':
                retention=max(0,min(100,retention+cast(int,action['retention_delta'])))
                reason='retention_delta='+format(cast(int,action['retention_delta']),'+d')+';\n'+cast(str,action['retention_reason'])
            modified=max(now,cast(int,old['modified_at_us'])) if old is not None else now
            lifecycle,since=transition(old,retention,modified,settings.integer('memory.forget_below'),settings.integer('memory.restore_at'))
            basis_links=tuple({'dependent_id':oid,'dependent_revision':revision,'basis_id':ref['object_id'],'basis_revision':ref['expected_revision'],
                'kind':ref['kind'],'evidence_roots':roots[ref['object_id']]} for ref,_ in basis if ref['object_id']!=oid)
            if old is not None and any(ref['object_id']==oid for ref,_ in basis):
                inherited=tuple(dict(record(link))|{'dependent_revision':revision} for link in sequence(links[oid]['bases']))
                by_id={cast(str,link['basis_id']):link for link in (*inherited,*basis_links)}
                basis_links=tuple(by_id[key] for key in sorted(by_id))
            source_links=combine_sources(oid,revision,tuple(links[ref['object_id']] for ref,_ in basis),None if old is None else links[oid])
            value={'object_version':2,'object_id':oid,'instance_id':configuration.scope_id,'kind':object_kind,'revision':revision,
                'created_at_us':now if old is None else old['created_at_us'],'modified_at_us':modified,'lifecycle':lifecycle,'forgotten_since_us':since,
                'retention_policy_ref':'configured_retention:1' if old is None else old['retention_policy_ref'],'content':content,
                'scores':{'belief':action['belief'],'belief_reason':action['belief_reason'],'retention':retention,'retention_reason':reason,
                    'scale_id':'acceptance_100_v1','score_basis':tuple(ref['basis_id'] for ref in basis_links)},
                'origin':{'kind':'DERIVED','candidate_id':cid,'batch_id':None,'actor_ref':'dream_cognition','model_origin':'REMOTE_PROVIDER','candidate_origin':'MODEL_VALIDATED'} if old is None else old['origin']}
            leaf=isolate_change({'change_version':1,'action':kind,'target_id':oid,'expected_revision':None if old is None else old['revision'],
                'proposed_value':value,'links':{'sources':source_links,'bases':basis_links}},8192,text_format=True,daily_format=True)
            from companion_memory.memory.changes import semantic_change
            if old is not None and not semantic_change(old,links[oid],record(leaf['proposed_value']),record(leaf['links'])):continue
            objects[oid]=record(leaf['proposed_value']);links[oid]=record(leaf['links'])
        leaves.append(leaf)
        if kind.startswith('CREATE_'):created[cast(int,action['local_ref'])]=oid
    candidate=DreamCandidate(cid,run_id,step_id,request_id,handoff_id,material_digest,cast(str,output['decision']),cast(str,output['reason']),tuple(leaves))
    candidate.digest
    return candidate


def combine_sources(oid:str,revision:int,links:tuple[Record,...],original:Record|None=None):
    """Never clip a union of authorized source anchors to fit a smaller schema."""
    sources={}
    preserved={} if original is None else {cast(str,record(s)['source_id']):record(s) for s in sequence(original['sources'])}
    for value in links+(() if original is None else (original,)):
        for raw in sequence(value['sources']):
            link=record(raw);sid=cast(str,link['source_id'])
            previous=sources.setdefault(sid,{'anchors':{},'auxiliary':{}})
            for name,field in (('anchors','target_anchors'),('auxiliary','auxiliary_refs')):
                for item in sequence(link[field]):previous[name][encode_content(item,4096)]=item
    if len(sources)>2 or any(len(v['anchors'])>2 or len(v['auxiliary'])>2 for v in sources.values()):raise ValueTooLarge()
    return tuple({'object_id':oid,'object_revision':revision,'source_id':sid,'link_role':preserved[sid]['link_role'] if sid in preserved else 'CONTEXT',
        'target_anchors':tuple(v['anchors'][key] for key in sorted(v['anchors'])),
        'auxiliary_refs':tuple(v['auxiliary'][key] for key in sorted(v['auxiliary']))} for sid,v in sorted(sources.items()))


def check_deadline_source(deadline:int,sources:tuple[Record,...]):
    """A goal time must literally occur in complete native retained event text."""
    from datetime import datetime,timezone
    from re import finditer
    for source in sources:
        for member in sequence(source['members']):
            event=record(record(member)['event'])
            texts=[cast(str,event['body'])]
            texts.extend(cast(str,record(q)['body']) for q in sequence(event['quotation']))
            for text in texts:
                for match in finditer(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})',text):
                    try:stamp=datetime.fromisoformat(match[0]).astimezone(timezone.utc)
                    except ValueError:continue
                    delta=stamp-datetime(1970,1,1,tzinfo=timezone.utc)
                    if (delta.days*86400+delta.seconds)*1000000+delta.microseconds==deadline:return
    raise InvalidValue()
