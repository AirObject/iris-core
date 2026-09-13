"""Deterministic memory candidates from original structured Provider evidence.

Only CREATE_MEMORY proposals are materialized. Each anchor is checked against an
actual frozen TARGET event, and subjects/worlds against the retained trusted
roster. Formal current-object and source writes remain memory's transaction.
"""
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.text_persistence import StoredTextConfiguration,stored_text_configuration_issue
from companion_memory.persistence.schema import Value,InvalidValue
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record,sequence,MEMORY_CONTENT
from companion_memory.memory.changes import isolate_change
from companion_memory.memory.sources import isolate_source,check_anchor,SourcePayload
from companion_memory.ingress.events import canonical_event
from companion_memory.provider.terminal_evidence import VerifiedTerminal,issued_terminal
from companion_memory.provider.chat_protocol import validate_structured_result,ChatBinding
from companion_memory.provider.values import as_record
from .text_context import FrozenContext,material_digest,normalized_request_digest,restore_context,admission_request
from .text_output import isolate_text_output
from .text_resources import output_schema,resource_digest,LEARNING_INSTRUCTIONS,LEARNING_TRANSFORM_RESOURCE,learning_authorizations
from .candidates import Candidate,stable_identity,manifest_digest,isolate_candidate


@dataclass(frozen=True,slots=True)
class BasisRoots:
    """Original-revision source roots obtained from the authorized memory owner.

    The formal transaction independently rechecks all roots, authority and current
    revisions. This value cannot grant a read or bypass that final validation.
    """
    object_id: str
    revision: int
    message_ids: tuple[str,...]


class TextCandidateInput:
    """Fixed transform resources; no host-supplied synthetic proposal bodies."""
    def __init__(self,configuration: StoredTextConfiguration):
        if stored_text_configuration_issue(configuration) is not None:raise InvalidValue()
        generation=configuration.candidate.text.record('provider.generation')
        if (generation['transform_digest']!=resource_digest(LEARNING_TRANSFORM_RESOURCE) or generation['prompt_digest']!=resource_digest(LEARNING_INSTRUCTIONS.encode())
                or generation['schema_digest']!=resource_digest(output_schema('LEARNING'))):raise InvalidValue()
        self.configuration=configuration
        self.transform_version=cast(str,generation['transform_ref'])
        self.fingerprint=cast(str,generation['transform_digest'])

    def build(self,source: object,generation: int,terminal: VerifiedTerminal,context: FrozenContext,
              basis_roots: tuple[BasisRoots,...] = (),*,admission_generation: int=1) -> Candidate:
        """Recreate stable proposals without current time, new defaults or network I/O."""
        configuration=self.configuration
        if not issued_terminal(terminal) or type(context) is not FrozenContext or terminal.database_id!=configuration.database_id:raise InvalidValue()
        resources=configuration.candidate.text.record('provider.generation')
        binding=ChatBinding(cast(str,resources['model_id']),cast(tuple[str,...],resources['expected_reported_models']),cast(str|None,resources['resolved_model_id']),
            cast(str,resources['schema_ref']),cast(str,resources['schema_digest']),'text_learning',output_schema('LEARNING'))
        # A frozen value is data, not an issued capability. Reconstruct every
        # leaf, request and wire field before trusting its authorization roster.
        if restore_context(context.manifest,context.leaves,context.request,binding)!=context:raise InvalidValue()
        admitted=admission_request(context,admission_generation)
        original=isolate_source(source);manifest=context.manifest
        if (terminal.original_request is None or normalized_request_digest(terminal.original_request)!=normalized_request_digest(admitted)
                or any(manifest[name]!=original[name] for name in ('batch_id','run_id','source_id','config_snapshot_id'))
                or configuration.snapshot_id!=manifest['config_snapshot_id'] or manifest['database_id']!=configuration.database_id):raise InvalidValue()
        request=terminal.request;attribution=as_record(request['attribution'])
        if (request['source']!='REMOTE_PROVIDER' or request['format_version']!=2 or request['result_owner']!='cognition' or request['task_role']!='LEARNING'
                or request['caller_module']!='cognition' or request['caller_scope']!=manifest['instance_id']
                or request['config_snapshot_id']!=configuration.snapshot_id or attribution['entry_ids']!=(original['entry_id'],)
                or request['operation_key']!=admitted['operation_key'] or attribution['batch_id']!=original['batch_id'] or attribution['run_id']!=original['run_id']):raise InvalidValue()
        members=tuple(record(value) for value in sequence(original['ordered_members']))
        frozen_members=tuple(record(value) for value in sequence(manifest['ordered_members']))
        if tuple(({'H':'HISTORY','T':'TARGET','R':'RECENT'}[cast(str,item['role'])],item['message_id'],item['payload_digest']) for item in members)!=tuple((item['role'],item['message_id'],item['payload_digest']) for item in frozen_members):raise InvalidValue()
        settings=configuration.candidate.content
        if any(record(context.context['resources'])[name]!=resources[name] for name in ('prompt_ref','prompt_digest','schema_ref','schema_digest','transform_ref','transform_digest')):raise InvalidValue()
        authorization=learning_authorizations(cast(str,context.context['system_text']))
        rid=cast(str,request['object_id']);handoff=cast(str|None,request['handoff_id'])
        batch=cast(str,original['batch_id']);database=configuration.database_id
        origin_ref=handoff or rid
        cid=stable_identity('candidate',database,batch,origin_ref,self.transform_version,0,'CANDIDATE')
        proposed='FAILED_DROPPED';leaves=()
        if request['outcome']=='SUCCEEDED':
            # Protocol identity is Provider-owned. A corrupted original handoff
            # is an integrity failure, not an ordinary invalid model proposal.
            structured=validate_structured_result(terminal.result,binding)
            try:
                output=isolate_text_output(structured['output'])
                leaves=self._items(output,original,members,context,authorization,basis_roots,cid,origin_ref)
                proposed='SUCCEEDED'
            except InvalidValue:
                leaves=()
        retained_handoff=handoff if proposed=='SUCCEEDED' else None
        candidate: dict[str,Value]={'candidate_version':3,'candidate_id':cid,'batch_id':batch,'run_id':original['run_id'],'work_generation':generation,
            'config_snapshot_id':configuration.snapshot_id,'provider_request_id':rid,'handoff_ref':retained_handoff,'transform_version':self.transform_version,
            'source_id':original['source_id'],'manifest_digest':'pending','terminal_proposal':proposed,
            'ordered_change_refs':tuple(MappingProxyType({'ordinal':i,'target_id':leaf['target_id'],'action':leaf['action'],'digest':hashlib.sha256(encode_content(leaf,8192)).hexdigest()}) for i,leaf in enumerate(leaves)),
            'origin':MappingProxyType({'storage_execution':'ACTUAL','model_adapter':'REMOTE_PROVIDER','candidate_origin':'MODEL_VALIDATED','database_id':database}),
            'context_id':manifest['object_id'],'context_digest':material_digest(context.context),'output_schema_revision':resources['schema_ref']}
        candidate['manifest_digest']=manifest_digest(MappingProxyType(candidate))
        return isolate_candidate(candidate,leaves,item_limit=settings.integer('cognition.candidate_item_limit'),item_bytes=settings.integer('cognition.candidate_item_max_bytes'),
            total_bytes=settings.integer('cognition.candidate_max_bytes'),text_format=True)

    def _items(self,output,source,members,context: FrozenContext,authorization,basis_roots,cid,origin_ref):
        user=record(context.context['user'])
        events={cast(str,record(record(value)['member'])['message_id']):record(record(value)['event']) for value in sequence(user['members'])}
        original={member['message_id']:member for member in members}
        subjects={cast(str,record(item)['subject_id']) for item in sequence(authorization['subjects'])}
        related={cast(str,record(item)['object_id']):record(item) for item in sequence(user['related'])}
        roots={item.object_id:item for item in basis_roots if type(item) is BasisRoots}
        if len(roots)!=len(basis_roots):raise InvalidValue()
        settings=self.configuration.candidate.content;leaves=[]
        for ordinal,raw in enumerate(sequence(output['memories'])):
            item=record(raw)
            if not set(cast(tuple[str,...],sequence(item['subject_ids'])))<=subjects or item['world_scope'] not in sequence(authorization['worlds']):raise InvalidValue()
            anchors=[]
            for raw_anchor in sequence(item['target_anchors']):
                anchor=record(raw_anchor);member=original.get(anchor['message_id']);event=events.get(cast(str,anchor['message_id']))
                if member is None or event is None:raise InvalidValue()
                full=MappingProxyType({**anchor,'occurrence_id':None,'interpretation_id':None})
                check_anchor(full,member,SourcePayload(event,canonical_event(event),()))
                anchors.append(full)
            for raw_auxiliary in sequence(item['auxiliary_refs']):
                auxiliary=record(raw_auxiliary);member=original.get(auxiliary['message_id'])
                if member is None or member['role'] not in ('H','R'):raise InvalidValue()
            oid=stable_identity('object',self.configuration.database_id,cast(str,source['batch_id']),origin_ref,self.transform_version,ordinal,'MEMORY')
            bases=[]
            for raw_basis in sequence(item['basis_refs']):
                basis=record(raw_basis);target=related.get(cast(str,basis['object_id']))
                if target is None or target['revision']!=basis['expected_revision'] or record(target['content'])['world_scope']!=item['world_scope']:raise InvalidValue()
                observed=roots.get(cast(str,basis['object_id']))
                if observed is None or observed.revision!=basis['expected_revision']:raise OwnerFailure('PRECONDITION_FAILED','object','BASIS_UNAVAILABLE')
                bases.append({'dependent_id':oid,'dependent_revision':1,'basis_id':basis['object_id'],'basis_revision':basis['expected_revision'],
                    'kind':basis['kind'],'evidence_roots':observed.message_ids})
            retention=settings.integer('memory.initial_retention');forgotten=retention<settings.integer('memory.forget_below')
            obj={'object_version':2,'object_id':oid,'instance_id':context.manifest['instance_id'],'kind':'MEMORY','revision':1,
                'created_at_us':source['frozen_at_us'],'modified_at_us':source['frozen_at_us'],'lifecycle':'FORGOTTEN' if forgotten else 'ACTIVE',
                'forgotten_since_us':source['frozen_at_us'] if forgotten else None,'retention_policy_ref':'configured_retention:1',
                'content':{field.name:item[field.name] for field in MEMORY_CONTENT.fields},
                'scores':{'belief':item['belief'],'retention':retention,'scale_id':'acceptance_100_v1','belief_reason':item['belief_reason'],'retention_reason':'Configured initial retention.',
                    'score_basis':tuple(b['basis_id'] for b in bases)},
                'origin':{'kind':'DIRECT_LEARNING','candidate_id':cid,'batch_id':source['batch_id'],'actor_ref':'content_scheduler','model_origin':'REMOTE_PROVIDER','candidate_origin':'MODEL_VALIDATED'}}
            links={'sources':({'object_id':oid,'object_revision':1,'source_id':source['source_id'],'link_role':'DIRECT','target_anchors':tuple(anchors),'auxiliary_refs':item['auxiliary_refs']},),'bases':tuple(bases)}
            leaves.append(isolate_change({'change_version':1,'action':'CREATE_MEMORY','target_id':oid,'expected_revision':None,'proposed_value':obj,'links':links},8192,text_format=True))
        return tuple(leaves)
