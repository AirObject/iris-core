"""Same-transaction verification of retained text context and formal candidates.

Only the explicit text assembly installs this participant. Original context,
request, actual Provider result and current basis revisions must agree before
candidate publication; terminal work releases every context leaf in its UoW.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.configuration import PresentValue
from companion_memory.configuration.resolution_results import ResolutionOk
from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.cognition.candidates import Candidate
from companion_memory.cognition.context_storage import ContextStorage
from companion_memory.cognition.text_context import FrozenContext,restore_context,material_digest,admission_request,learning_request_key,normalized_request_digest
from companion_memory.cognition.text_candidates import TextCandidateInput,BasisRoots
from companion_memory.cognition.text_resources import output_schema,learning_authorizations
from companion_memory.memory.formats import record,sequence
from companion_memory.memory.sources import decode_source
from companion_memory.persistence import UnitOfWork,Value,RecordSchema,Field
from companion_memory.persistence.content_codec import decode_content,encode_content
from companion_memory.persistence.text_records import ID,DIGEST,isolate_record
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.chat_protocol import ChatBinding
from companion_memory.provider.terminal_evidence import VerifiedTerminal
from companion_memory.provider.service import derived_id
if TYPE_CHECKING:
    from .content_assembly import ContentAssembly

WORK_BINDING=RecordSchema(tuple(Field(name,DIGEST if name in ('candidate_source_fingerprint','request_digest','context_digest') else ID)
    for name in ('profile_id','prompt_revision','transform_version','candidate_source_fingerprint','request_digest',
        'context_id','context_digest','output_schema_revision','config_snapshot_id')))


class TextContentTransactions:
    """Explicit cognition participant sharing the existing native owner lease."""
    def __init__(self,assembly: ContentAssembly):
        if not assembly.text_format or type(assembly.configuration) is not StoredTextConfiguration:raise InvalidValue()
        self.assembly=assembly;self.configuration=assembly.configuration
        catalog=next(c for c in assembly.catalogs if c.definition.owner_module=='cognition')
        self.contexts=ContextStorage(catalog,assembly.storage,self.configuration,assembly.instance_id)
        self.candidates=TextCandidateInput(self.configuration)
        generation=self.configuration.candidate.text.record('provider.generation')
        entry=self.configuration.candidate.foundation.get_entry('provider.profiles')
        if type(entry) is not ResolutionOk or type(entry.value.state) is not PresentValue:raise InvalidValue()
        profiles=cast(tuple[Value,...],entry.value.state.value)
        if len(profiles)!=1:raise InvalidValue()
        self.profile=record(profiles[0])
        accounts=self.configuration.candidate.foundation.get_entry('provider.accounts')
        if type(accounts) is not ResolutionOk or type(accounts.value.state) is not PresentValue:raise InvalidValue()
        configured_accounts=cast(tuple[Value,...],accounts.value.state.value)
        if len(configured_accounts)!=1:raise InvalidValue()
        self.account=record(configured_accounts[0])
        self.chat=ChatBinding(cast(str,generation['model_id']),cast(tuple[str,...],generation['expected_reported_models']),
            cast(str|None,generation['resolved_model_id']),cast(str,generation['schema_ref']),cast(str,generation['schema_digest']),
            'text_learning',output_schema('LEARNING'))

    def request_identity(self,source: MappingProxyType[str,Value],work: MappingProxyType[str,Value]):
        """Derive the first frozen identity, which never changes on readmission."""
        batch=cast(str,source['batch_id']);key=learning_request_key(batch,1)
        settings=self.configuration.candidate.text.record('provider.generation')
        return {'operation_key':key,'run_id':source['run_id'],'profile_id':self.profile['profile_id'],
            'batch_id':batch,'entry_ids':(source['entry_id'],),'prompt_revision':settings['prompt_ref']}

    def work_binding(self,context: FrozenContext,admission_generation: int=1) -> MappingProxyType[str,Value]:
        """Encode precisely the retained runtime association fields."""
        settings=self.configuration.candidate.text.record('provider.generation')
        binding=record(context.context['model_binding'])
        expected={'profile_id':self.profile['profile_id'],'config_snapshot_id':self.configuration.snapshot_id,
            'profile_revision':derived_id('profile',self.configuration.snapshot_id,cast(str,self.profile['profile_id'])),
            'price_revision':record(self.account['price'])['revision_ref'],'protocol':'OPENAI_CHAT_COMPLETIONS','model_id':self.profile['model_id'],
            'capability_evidence_ref':settings['capability_evidence_ref'],'billing_evidence_ref':settings['billing_evidence_ref']}
        if any(binding[name]!=value for name,value in expected.items()) or any(record(context.context['resources'])[name]!=settings[name]
                for name in ('prompt_ref','prompt_digest','schema_ref','schema_digest','transform_ref','transform_digest')):raise InvalidValue()
        return isolate_record(WORK_BINDING,{'profile_id':self.profile['profile_id'],'prompt_revision':settings['prompt_ref'],
            'transform_version':self.candidates.transform_version,'candidate_source_fingerprint':self.candidates.fingerprint,
            'request_digest':normalized_request_digest(admission_request(context,admission_generation)),'context_id':context.manifest['object_id'],
            'context_digest':material_digest(context.context),'output_schema_revision':settings['schema_ref'],
            'config_snapshot_id':self.configuration.snapshot_id},8192)

    def original(self,uow: UnitOfWork,source: MappingProxyType[str,Value],work: MappingProxyType[str,Value],binding: object) -> FrozenContext:
        """Reconstruct complete material without refreshing persona or configuration."""
        value=isolate_record(WORK_BINDING,binding,8192)
        stored=self.contexts.read_stored(uow,cast(str,value['context_id']))
        context=restore_context(stored.manifest,stored.leaves,self.request_identity(source,work),self.chat)
        if (self.work_binding(context,cast(int,work['admission_generation']))!=value or context.manifest['created_at_us']!=source['frozen_at_us']
                or any(context.manifest[name]!=source[name] for name in ('batch_id','run_id','source_id','config_snapshot_id'))):raise InvalidValue()
        if tuple(({'H':'HISTORY','T':'TARGET','R':'RECENT'}[cast(str,record(item)['role'])],record(item)['message_id'],record(item)['payload_digest'])
                for item in sequence(source['ordered_members']))!=tuple((record(item)['role'],record(item)['message_id'],record(item)['payload_digest'])
                for item in sequence(context.manifest['ordered_members'])):raise InvalidValue()
        return context

    def verify_association(self,uow: UnitOfWork,values: MappingProxyType[str,Value],work: MappingProxyType[str,Value],batch: MappingProxyType[str,Value]) -> None:
        source=decode_source(cast(str,batch['manifest']))
        if work['model_binding'] is None or values['model_binding']!=work['model_binding']:raise InvalidValue()
        context=self.original(uow,source,work,decode_content(cast(str,values['model_binding']).encode(),8192))
        if values['provider_operation_key']!=admission_request(context,cast(int,work['admission_generation']))['operation_key']:raise InvalidValue()

    def verify_candidate(self,uow: UnitOfWork,work: MappingProxyType[str,Value],batch: MappingProxyType[str,Value],candidate: Candidate,evidence: VerifiedTerminal) -> None:
        source=decode_source(cast(str,batch['manifest']))
        context=self.original(uow,source,work,decode_content(cast(str,work['model_binding']).encode(),8192))
        roots=[]
        for raw in sequence(record(context.context['user'])['related']):
            basis=record(raw);oid=cast(str,basis['object_id']);revision=cast(int,basis['revision'])
            if self.assembly.memory.current(uow,oid)!=basis:raise OwnerFailure('PRECONDITION_FAILED','object','BASIS_UNAVAILABLE')
            links=self.assembly.memory.links(uow,oid,revision)
            identities={cast(str,record(anchor)['message_id']) for source_link in sequence(links['sources'])
                for anchor in sequence(record(source_link)['target_anchors'])}
            identities.update(cast(str,item) for link in sequence(links['bases']) for item in sequence(record(link)['evidence_roots']))
            roots.append(BasisRoots(oid,revision,tuple(sorted(identities))))
        expected=self.candidates.build(source,cast(int,work['generation']),evidence,context,tuple(roots),admission_generation=cast(int,work['admission_generation']))
        if expected!=candidate:raise OwnerFailure('ACCESS_DENIED','candidate','BINDING_MISMATCH')

    async def rebuild_candidate(self,source: MappingProxyType[str,Value],work: MappingProxyType[str,Value],context: FrozenContext,evidence: VerifiedTerminal,deadline: float) -> Candidate:
        """Rebuild from retained material and the basis owner's current root identities."""
        if self.work_binding(context,cast(int,work['admission_generation']))!=isolate_record(WORK_BINDING,decode_content(cast(str,work['model_binding']).encode(),8192),8192):raise InvalidValue()
        roots=[]
        for raw in sequence(record(context.context['user'])['related']):
            basis=record(raw)
            identities=await self.assembly.memory.retained_basis_roots(basis,deadline)
            roots.append(BasisRoots(cast(str,basis['object_id']),cast(int,basis['revision']),identities))
        return self.candidates.build(source,cast(int,work['generation']),evidence,context,tuple(roots),admission_generation=cast(int,work['admission_generation']))

    def apply_scope(self,uow: UnitOfWork,values: MappingProxyType[str,Value],work: MappingProxyType[str,Value],batch: MappingProxyType[str,Value],candidate: Candidate):
        """Use the durably verified context scope, never caller-supplied read IDs.

        Empty command read lists remain reconstructible after context and
        candidate leaves are released. The original final receipt therefore
        keeps the same fingerprint on a later interpreter.
        """
        from companion_memory.memory.transactions import ApplyScope
        if values['readable_objects'] or values['readable_subjects']:raise InvalidValue()
        source=decode_source(cast(str,batch['manifest']))
        context=self.original(uow,source,work,decode_content(cast(str,work['model_binding']).encode(),8192))
        authorization=learning_authorizations(cast(str,context.context['system_text']))
        return ApplyScope(self.assembly.instance_id,cast(str,candidate.manifest['candidate_id']),cast(str,batch['batch_id']),
            frozenset(cast(str,record(raw)['object_id']) for raw in sequence(record(context.context['user'])['related'])),
            frozenset(cast(str,record(raw)['subject_id']) for raw in sequence(authorization['subjects'])),
            frozenset(cast(str,leaf['target_id']) for leaf in candidate.leaves),frozenset())

    def audit_targets(self,uow: UnitOfWork,values: MappingProxyType[str,Value],changes: dict[str,Value],state: str) -> tuple[MappingProxyType[str,Value],...]:
        """Use actual work/candidate/protection roots instead of an operation key."""
        work=self.assembly._get('work',uow,'batch_id',values['batch_id'])
        targets=[MappingProxyType({'object_id':values['batch_id'],'previous_revision':values['expected_revision'],'revision':work['revision']})]
        if state=='CANDIDATE_STORED':
            targets.append(MappingProxyType({'object_id':changes['candidate_id'],'previous_revision':None,'revision':1}))
            batch=self.assembly._get('batches',uow,'batch_id',values['batch_id'])
            source=decode_source(cast(str,batch['manifest']))
            for raw in sequence(source['ordered_members']):
                mid=record(raw)['message_id'];event=self.assembly.ingress.event(uow,cast(str,mid))
                revision=cast(int,event['references_revision'])
                targets.append(MappingProxyType({'object_id':mid,'previous_revision':revision-1,'revision':revision}))
        return tuple(targets)

    def release(self,uow: UnitOfWork,values: MappingProxyType[str,Value],candidate: Candidate) -> MappingProxyType[str,Value]:
        """Stage release under the actual same terminal command, before its receipt."""
        kind='commit_content_published' if candidate.leaves else 'commit_content_without_objects'
        definition=self.assembly.command_definition(kind)
        revision,_=self.contexts.release(uow,cast(str,candidate.manifest['context_id']),{'owner_namespace':definition.owner_namespace,
            'operation_kind':definition.operation_kind,'scope_id':self.assembly.instance_id,'operation_key':values['operation_id']})
        return MappingProxyType({'object_id':candidate.manifest['context_id'],'previous_revision':revision-1,'revision':revision})
