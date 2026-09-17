"""Complete deterministic daily candidates under frozen native read authority.

The final model order determines identities. Platform reuse is resolved once
while staging the candidate; the application transaction must recheck that
mapping and every observed revision before any formal owner writes.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration
from companion_memory.persistence import Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue,ValueTooLarge
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record,sequence,MEMORY_CONTENT,RELATION_CONTENT
from companion_memory.memory.sources import SourcePayload,check_anchor
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.formats import transition
from .candidates import Candidate,stable_identity,isolate_candidate,manifest_digest
from .daily_resources import TRANSFORM_VERSION
from .daily_output import decode_daily_output
from .goal_proposals import isolate_goal_change

Record=MappingProxyType[str,Value]

@dataclass(frozen=True,slots=True)
class DailyAuthority:
    """Frozen trusted scope, separate from the model's desired object IDs."""
    subject_ids:frozenset[str]
    worlds:tuple[Record,...]
    writable_objects:frozenset[str]
    route_ids:tuple[str,...]

class DailyCandidateTransform:
    """Pure IDs and complete values; native originals are supplied by the collector."""
    def __init__(self,configuration:StoredCognitionConfiguration):
        if (type(configuration) is not StoredDailyConfiguration and type(configuration) is not StoredDreamConfiguration and type(configuration) is not StoredManagedConfiguration):raise InvalidValue()
        self.configuration=configuration

    def build(self,source:Record,work:Record,run:Record,turn:Record,output:Record|None,
              authority:DailyAuthority,observed:tuple[Record,...],payloads:dict[str,SourcePayload],platform_subjects:tuple[Record,...],basis_roots:dict[str,tuple[str,...]]) -> Candidate:
        """Freeze all six actions, or a failed candidate with no partial effects.

        Authority, source payloads and observations come from retained native
        context/step records. The caller independently rechecks those records
        in the staging transaction; malformed proposals produce a failed set.
        """
        if (type(authority) is not DailyAuthority or run['batch_id']!=source['batch_id'] or turn['run_id']!=run['object_id']
                or turn['phase']!='RESULT_STORED' or run['active_turn_id']!=turn['object_id']):raise InvalidValue()
        actual={cast(str,item['object_id']):item for item in observed}
        if len(actual)!=len(observed):raise InvalidValue()
        settings=self.configuration.candidate.content
        terminal='SENSITIVE_DROPPED' if turn['result_kind']=='SENSITIVE' else 'FAILED_DROPPED'
        leaves=();mapping=()
        handoff=cast(str|None,turn['handoff_id'])
        if turn['result_kind']=='FINAL':
            if output is None or handoff is None:raise InvalidValue()
            cid=self.identity('candidate',source,handoff,0,'CANDIDATE')
            try:
                checked=decode_daily_output(encode_content(output,24576),turn=cast(int,turn['ordinal'])+1,completed_tools=cast(int,run['tool_count']))
                if checked['kind']!='FINAL':raise InvalidValue()
                leaves,mapping=self._items(checked,source,cid,handoff,authority,actual,payloads,platform_subjects,basis_roots)
                terminal='SUCCEEDED'
            except ValueTooLarge:raise
            except InvalidValue:leaves=();mapping=()
        retained=handoff if terminal=='SUCCEEDED' else None
        origin=retained or cast(str,turn['provider_request_id'])
        manifest={'candidate_version':4,'candidate_id':self.identity('candidate',source,origin,0,'CANDIDATE'),'batch_id':source['batch_id'],
            'run_id':source['run_id'],'work_generation':work['generation'],'config_snapshot_id':self.configuration.snapshot_id,
            'provider_request_id':turn['provider_request_id'],'handoff_ref':retained,'transform_version':TRANSFORM_VERSION,'source_id':source['source_id'],
            'manifest_digest':'pending','terminal_proposal':terminal,'ordered_change_refs':tuple(MappingProxyType({'ordinal':i,'target_id':leaf['target_id'],
                'action':leaf['action'],'digest':sha256(encode_content(leaf,8192)).hexdigest()}) for i,leaf in enumerate(leaves)),
            'origin':MappingProxyType({'storage_execution':'ACTUAL','model_adapter':'REMOTE_PROVIDER','candidate_origin':'MODEL_VALIDATED','database_id':self.configuration.database_id}),
            'context_id':run['context_id'],'context_digest':run['context_digest'],'output_schema_revision':next(record(r)['schema_ref'] for r in sequence(self.configuration.candidate.text.record('provider.transport')['roles']) if record(r)['role']=='LEARNING'),
            'reasoning_run_id':run['object_id'],'transcript_digest':run['transcript_digest'],'action_mapping':mapping}
        manifest['manifest_digest']=manifest_digest(MappingProxyType(manifest))
        return isolate_candidate(manifest,leaves,item_limit=settings.integer('cognition.candidate_item_limit'),item_bytes=8192,total_bytes=73728,daily_format=True)

    def identity(self,kind,source,handoff,ordinal,object_kind):
        return stable_identity(kind,self.configuration.database_id,cast(str,source['batch_id']),handoff,TRANSFORM_VERSION,ordinal,object_kind)

    def _items(self,output,source,cid,handoff,authority,observed,payloads,platform_subjects,basis_roots):
        members={record(m)['message_id']:record(m) for m in sequence(source['ordered_members'])}
        settings=self.configuration.candidate.content;now=cast(int,source['frozen_at_us'])
        created={};leaves=[];mapping=[];current={};subject_ids=set(authority.subject_ids)
        platforms={(s['platform_id'],s['external_subject_id']):s for s in platform_subjects}
        for ordinal,raw in enumerate(sequence(output['actions'])):
            item=record(raw);action=cast(str,item['action'])
            for anchor_value in sequence(item['target_anchors']):
                anchor=record(anchor_value);mid=cast(str,anchor['message_id'])
                if mid not in members or mid not in payloads:raise InvalidValue()
                check_anchor(anchor,members[mid],payloads[mid])
            for auxiliary in sequence(item['auxiliary_refs']):
                member=members.get(record(auxiliary)['message_id'])
                if member is None or member['role'] not in ('H','R'):raise InvalidValue()
            old=None;reused=False
            if action in ('REPLACE_CURRENT','SET_SCORES'):
                oid=cast(str,item['object_id']);old=observed.get(oid)
                if oid not in authority.writable_objects or old is None:raise InvalidValue()
                if old['revision']!=item['expected_revision']:raise InvalidValue()
            else:
                kind={'REGISTER_SUBJECT':'SUBJECT','CREATE_MEMORY':'MEMORY','CREATE_RELATION':'RELATION','CREATE_GOAL':'GOAL'}[action]
                oid=self.identity('subject' if kind=='SUBJECT' else 'goal' if kind=='GOAL' else 'object',source,handoff,ordinal,kind)
            def resolve(value):
                ref=record(value)
                return cast(str,ref['existing_id']) if 'existing_id' in ref else created[cast(int,ref['local_ref'])]
            def subject(value):
                sid=resolve(value)
                if sid not in subject_ids:raise InvalidValue()
                return sid
            if action=='REGISTER_SUBJECT':
                kind=item['subject_kind']
                if kind=='PLATFORM_PERSON':
                    if item['platform_id']!=source['platform_id'] or not any(members[mid]['role']=='T' and record(payload.event['sender'])['subject_id']==item['external_subject_id'] for mid,payload in payloads.items()):raise InvalidValue()
                    previous=platforms.get((item['platform_id'],item['external_subject_id']))
                    if previous is not None:oid=cast(str,previous['subject_id']);reused=True
                subject_ids.add(oid)
                value={'subject_version':1,'subject_id':oid,'instance_id':self.configuration.scope_id,'kind':kind,'platform_id':item['platform_id'],
                    'external_subject_id':item['external_subject_id'],'label':item['label'],'revision':1}
                if kind=='PLATFORM_PERSON' and not reused:platforms[(item['platform_id'],item['external_subject_id'])]=MappingProxyType(value)
                links={'sources':({'object_id':oid,'object_revision':1,'source_id':source['source_id'],'link_role':'DIRECT','target_anchors':item['target_anchors'],'auxiliary_refs':()},),'bases':()}
            elif action=='CREATE_GOAL':
                world=cast(str,item['world_scope'])
                if world not in tuple('REAL' if w['kind']=='REAL' else cast(str,w['kind'])+':'+cast(str,w['context_id']) for w in authority.worlds):raise InvalidValue()
                if item['route_id'] is not None and item['route_id'] not in authority.route_ids:raise InvalidValue()
                for basis_value in sequence(item['basis_refs']):
                    basis=record(basis_value);basis_object=observed.get(cast(str,basis['object_id']))
                    if basis_object is None or basis_object['revision']!=basis['expected_revision']:raise InvalidValue()
                bases=[created[cast(int,b)] for b in sequence(item['basis_action_refs'])]
                bases.extend(cast(str,record(b)['object_id']) for b in sequence(item['basis_refs']))
                for basis in bases:
                    obj=current.get(basis) or observed.get(basis)
                    if obj is None or obj['kind'] not in ('MEMORY','RELATION'):raise InvalidValue()
                    scope=record(record(obj['content'])['world_scope'])
                    if world!=('REAL' if scope['kind']=='REAL' else cast(str,scope['kind'])+':'+cast(str,scope['context_id'])):raise InvalidValue()
                if not bases:raise InvalidValue()
                if item['deadline'] is not None:
                    if item['route_id'] is None or item['reminder_lead_seconds'] is None:raise InvalidValue()
                    self._deadline(item,payloads)
                value={key:item[key] for key in ('content','world_scope','deadline','reminder_lead_seconds','route_id')}
                value.update(subject_ids=tuple(sorted(subject(ref) for ref in sequence(item['subject_refs']))),basis_id=bases[0])
                links=None
            else:
                source_content=record(item['content']) if action=='REPLACE_CURRENT' else record(cast(Record,old)['content']) if action=='SET_SCORES' else item
                kind=cast(str,old['kind']) if old is not None else 'MEMORY' if action=='CREATE_MEMORY' else 'RELATION'
                fields=MEMORY_CONTENT.fields if kind=='MEMORY' else RELATION_CONTENT.fields
                content={f.name:source_content[f.name] for f in fields}
                if action!='SET_SCORES':
                    if kind=='MEMORY':
                        content['subject_ids']=tuple(subject(ref) for ref in sequence(source_content['subject_ids']))
                        content['speaker_subject_id']=None if source_content['speaker_subject_id'] is None else subject(source_content['speaker_subject_id'])
                    else:
                        for name in ('from_ref','to_ref'):
                            ref=record(source_content[name]);target=subject(ref['id']) if ref['type']=='SUBJECT' else resolve(ref['id'])
                            if ref['type']=='OBJECT' and target not in current and target not in observed:raise InvalidValue()
                            content[name]=MappingProxyType(dict(ref)|{'id':target})
                if content['world_scope'] not in authority.worlds:raise InvalidValue()
                retention=settings.integer('memory.initial_retention') if old is None else cast(int,record(old['scores'])['retention'])
                reason='Configured initial retention.' if old is None else record(old['scores'])['retention_reason']
                if action=='SET_SCORES':
                    retention=max(0,min(100,retention+cast(int,item['retention_delta'])))
                    reason='retention_delta='+format(cast(int,item['retention_delta']),'+d')+';\n'+cast(str,item['retention_reason'])
                modified=max(now,cast(int,old['modified_at_us'])) if old is not None else now
                lifecycle,since=transition(old,retention,modified,settings.integer('memory.forget_below'),settings.integer('memory.restore_at'))
                revision=1 if old is None else cast(int,old['revision'])+1
                bases=[]
                for basis_value in sequence(item['basis_refs']):
                    basis=record(basis_value);bid=cast(str,basis['object_id']);obj=observed.get(bid)
                    if obj is None or obj['revision']!=basis['expected_revision'] or record(obj['content'])['world_scope']!=content['world_scope']:raise InvalidValue()
                    roots=basis_roots.get(bid)
                    if roots is None or not 1<=len(roots)<=8:raise InvalidValue()
                    bases.append({'dependent_id':oid,'dependent_revision':revision,'basis_id':bid,'basis_revision':basis['expected_revision'],'kind':basis['kind'],'evidence_roots':roots})
                value={'object_version':2,'object_id':oid,'instance_id':self.configuration.scope_id,'kind':kind,'revision':revision,'created_at_us':now if old is None else old['created_at_us'],
                    'modified_at_us':modified,'lifecycle':lifecycle,'forgotten_since_us':since,'retention_policy_ref':'configured_retention:1' if old is None else old['retention_policy_ref'],
                    'content':content,'scores':{'belief':item['belief'],'belief_reason':item['belief_reason'],'retention':retention,'retention_reason':reason,'scale_id':'acceptance_100_v1',
                        'score_basis':tuple(b['basis_id'] for b in bases)},'origin':{'kind':'DIRECT_LEARNING','candidate_id':cid,'batch_id':source['batch_id'],'actor_ref':'daily_cognition',
                        'model_origin':'REMOTE_PROVIDER','candidate_origin':'MODEL_VALIDATED'} if old is None else old['origin']}
                links={'sources':({'object_id':oid,'object_revision':revision,'source_id':source['source_id'],'link_role':'DIRECT','target_anchors':item['target_anchors'],'auxiliary_refs':item['auxiliary_refs']},),'bases':tuple(bases)}
            mapping.append(MappingProxyType({'model_ordinal':ordinal,'local_ref':item['local_ref'],'object_id':oid,'effect_ordinal':None if reused else len(leaves),'reused':reused}))
            if not reused:
                leaf={'change_version':1,'action':action,'target_id':oid,'expected_revision':None if old is None else old['revision'],'proposed_value':value,'links':links}
                checked=isolate_goal_change(leaf,8192) if action=='CREATE_GOAL' else isolate_change(leaf,8192,text_format=True,daily_format=True)
                leaves.append(checked)
                if action!='CREATE_GOAL':current[oid]=record(checked['proposed_value'])
            if action in ('REGISTER_SUBJECT','CREATE_MEMORY','CREATE_RELATION','CREATE_GOAL'):created[cast(int,item['local_ref'])]=oid
        return tuple(leaves),tuple(mapping)

    @staticmethod
    def _deadline(item,payloads):
        """Require an explicit original ISO timestamp for a proposed UTC deadline."""
        from datetime import datetime,timezone
        from re import finditer
        value=cast(int,item['deadline']);found=False
        for raw in sequence(item['target_anchors']):
            anchor=record(raw);event=payloads[cast(str,anchor['message_id'])].event
            text=cast(str,event['body']) if anchor['part'] in ('EVENT','BODY') else cast(str,record(sequence(event['quotation'])[cast(int,anchor['item_index'])])['body']) if anchor['part']=='QUOTATION' else ''
            if anchor['start_utf8'] is not None:text=text.encode()[cast(int,anchor['start_utf8']):cast(int,anchor['end_utf8'])].decode()
            for match in finditer(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})',text):
                try:stamp=datetime.fromisoformat(match[0]).astimezone(timezone.utc)
                except ValueError:continue
                delta=stamp-datetime(1970,1,1,tzinfo=timezone.utc)
                if (delta.days*86400+delta.seconds)*1000000+delta.microseconds==value:found=True
        if not found:raise InvalidValue()
