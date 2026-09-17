"""Reviewed fixed sets with atomic source and memory establishment.

The trusted review authority binds complete material bytes before any member can
be established. Each member has its own original receipt; a partially completed
sealed set remains valid across process exits. Creation never writes history.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, stored_cognition_configuration_issue
from collections.abc import Callable
from dataclasses import dataclass
from weakref import WeakValueDictionary
from hashlib import sha256
from types import MappingProxyType
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration,stored_semantic_configuration_issue
from companion_memory.persistence import PersistenceService,UnitOfWork,Value,ResultBoundCommandDefinition
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import StatementCatalog,OwnerFailure
from companion_memory.persistence.semantic_catalog import SemanticRecords
from companion_memory.persistence.semantic_records import Record,ID,H,record,isolate,identity,number,string
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.memory.formats import decode_object,record as object_record,sequence,isolate_links
from companion_memory.memory.transactions import MemoryTransactions,ApplyScope,applied_counts
from companion_memory.ingress.events import isolate_event,plain
from .fixed_memory_repository import TABLES

_REVIEW=record(instance_id=ID,set_id=ID,entry_id=ID,review_ref=ID,review_digest=H,manifest_digest=H,reviewed_by=ID)
_ISSUER=object()


@dataclass(frozen=True,slots=True,weakref_slot=True,init=False)
class FixedReviewGrant:
    """Nonserializable review authority issued only by trusted setup verification."""
    claims: Record
    entry_id: str
    _issuer: object
    _authority: FixedReviewAuthority

    def __init__(self):raise TypeError('Review grants are issued by trusted review resources.')


class FixedReviewAuthority:
    """Trusted setup's review-decision verifier, never an untrusted API parameter."""
    def __init__(self,verify: Callable[[Record],bool]):
        self._verify=verify
        self._grants: WeakValueDictionary[int,FixedReviewGrant]=WeakValueDictionary()

    def grant(self,claims: object) -> FixedReviewGrant:
        value=isolate(_REVIEW,claims)
        if len(string(value['reviewed_by']).encode())>32 or not self._verify(value):
            raise ValueError('The delegated review decision does not authorize these exact materials.')
        result=object.__new__(FixedReviewGrant)
        object.__setattr__(result,'claims',value);object.__setattr__(result,'entry_id',string(value['entry_id']))
        object.__setattr__(result,'_issuer',_ISSUER);object.__setattr__(result,'_authority',self)
        self._grants[id(result)]=result
        return result


class FixedMemorySets:
    """Cognition's native participant; memory remains the sole source writer."""
    def __init__(self,catalog: StatementCatalog,storage: PersistenceService,configuration: StoredSemanticConfiguration | StoredCognitionConfiguration,
                 memory: MemoryTransactions,review: FixedReviewGrant,establishment: ResultBoundCommandDefinition,checkpoint: Callable[[],None]):
        if ((stored_cognition_configuration_issue(configuration,storage=storage) if type(configuration) in (StoredDailyConfiguration,StoredDreamConfiguration) else stored_semantic_configuration_issue(configuration)) is not None or type(memory) is not MemoryTransactions
                or memory.configuration is not configuration or memory.storage is not storage or memory.semantic is None
                or type(review) is not FixedReviewGrant or getattr(review,'_issuer',None) is not _ISSUER
                or type(getattr(review,'_authority',None)) is not FixedReviewAuthority or review._authority._grants.get(id(review)) is not review
                or review.claims['instance_id']!=memory.instance_id or catalog.definition.owner_module!='cognition'):
            raise ValueError('Native fixed-set participants and delegated review authority are required.')
        self.catalog=catalog;self.storage=storage;self.memory=memory;self.review=review;self.checkpoint=checkpoint
        self.establishment=establishment
        if establishment.operation_kind!='fixed_establish':raise ValueError('Original establishment definition is required.')
        self.instance=memory.instance_id;self.rows=SemanticRecords(catalog,TABLES,storage,self.instance)
        self.config=MappingProxyType({'database_id':configuration.database_id,'instance_id':self.instance,'snapshot_id':configuration.snapshot_id})
        self._recovery_root: Record|None=None;self._recovery_ordinal=0;self._recovery_digest=sha256(b'[')
        self.expected=12 if configuration.candidate.text.record('retrieval.embedding')['qualification_profile'] in ('SMALL_REAL_TRIAL','DAILY_INTEGRATION') else 4096

    def _required(self,uow: UnitOfWork,set_id: str) -> Record:
        root=self.rows.get('fixed_memory_set',uow,set_id)
        if root is None or root['config']!=self.config:raise OwnerFailure('PRECONDITION_FAILED','set','SOURCE_CHANGED')
        if any(root[key]!=self.review.claims[key] for key in ('set_id','review_ref','review_digest','manifest_digest','reviewed_by')):
            raise OwnerFailure('ACCESS_DENIED','review','BINDING_MISMATCH')
        return root

    def sealed_member(self,uow: UnitOfWork,set_id: str,ordinal: int) -> tuple[Record,Record]:
        """Supply the actual sealed member only inside the original establishment."""
        root=self._required(uow,set_id)
        member=self.rows.get('fixed_memory_member',uow,identity('fixed-member',set_id,ordinal))
        if root['state']!='SEALED' or member is None or member['state']!='STORED' or ordinal!=root['established_members']:
            raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        return root,member

    def _member(self,payload: Record) -> Record:
        event=isolate_event(decode_content(string(payload['event_json']).encode(),2048),2048)
        memory=decode_object(string(payload['memory_json']).encode(),text_format=True)
        if (event['media'] or memory['instance_id']!=self.instance or memory['kind']!='MEMORY' or memory['revision']!=1
                or memory['object_id']!=identity('fixed-memory',self.instance,self.review.claims['set_id'],payload['member_id'])
                or object_record(memory['origin'])['kind']!='OPERATOR_INPUT'
                or encode_content(event,2048).decode()!=payload['event_json']
                or sha256(encode_content((payload['event_json'],payload['memory_json']),8192)).hexdigest()!=payload['content_digest']):
            raise InvalidValue()
        return memory

    def _subjects(self,uow: UnitOfWork,memory: Record) -> frozenset[str]:
        if not self.memory.rows.stage('subject_identity',uow,{'kind':'SELF','platform_id':None,'external_subject_id':None}):
            raise OwnerFailure('PRECONDITION_FAILED','subject','BASIS_UNAVAILABLE')
        content=object_record(memory['content']);world=object_record(content['world_scope'])
        identities={string(v) for v in sequence(content['subject_ids'])}
        if content['speaker_subject_id'] is not None:identities.add(string(content['speaker_subject_id']))
        if world['context_id'] is not None:identities.add(string(world['context_id']))
        for sid in identities:
            subject=self.memory.subject(uow,sid)
            if subject is None or sid==world['context_id'] and subject['kind']!='CONTEXT':
                raise OwnerFailure('PRECONDITION_FAILED','subject','BASIS_UNAVAILABLE')
        return frozenset(identities)

    def _members(self,uow: UnitOfWork,set_id: str):
        for ordinal in range(self.expected):
            self.checkpoint()
            member=self.rows.get('fixed_memory_member',uow,identity('fixed-member',set_id,ordinal))
            if member is None or member['ordinal']!=ordinal or member['set_id']!=set_id:raise InvalidValue()
            yield member

    def handle(self,kind: str,uow: UnitOfWork,envelope: Record,payload: Record) -> object:
        """Stage one complete native command, with exact owner facts and audits."""
        operation,fingerprint=self.storage.semantic_operation_context(uow,self.catalog.definition)
        if operation.operation_kind!=kind or operation.scope_id!=self.instance or payload['set_id']!=self.review.claims['set_id']:
            raise OwnerFailure('ACCESS_DENIED','binding','BINDING_MISMATCH')
        self.checkpoint()
        targets: list[Record]=[];items: tuple[str,...]=();counts: tuple[Record,...]=()
        def target(value: Record,previous: Value):
            targets.append(MappingProxyType({'object_id':value['row_id'],'previous_revision':previous,'revision':value['revision']}))
        def update(name: str,old: Record,**changes: Value) -> Record:
            value=self.rows.write(name,uow,dict(old)|{'revision':number(old['revision'])+1,**changes},expected_revision=number(old['revision']))
            target(value,old['revision']);return value
        set_id=string(payload['set_id'])
        if kind=='fixed_begin':
            if payload['config']!=self.config or any(payload[k]!=self.review.claims[k] for k in ('manifest_digest','review_ref','review_digest')):
                raise OwnerFailure('ACCESS_DENIED','review','BINDING_MISMATCH')
            root=self.rows.write('fixed_memory_set',uow,{'v':1,'row_id':set_id,'revision':1,'set_id':set_id,'config':self.config,
                'state':'OPEN','expected_members':self.expected,'stored_members':0,'established_members':0,
                'manifest_digest':payload['manifest_digest'],'review_ref':payload['review_ref'],'review_digest':payload['review_digest'],
                'reviewed_by':self.review.claims['reviewed_by'],'created_at':envelope['observed_at']})
            target(root,None)
        else:
            root=self._required(uow,set_id)
            if root['revision']!=payload['expected_revision']:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            if kind=='fixed_add_member':
                if root['state']!='OPEN' or payload['ordinal']!=root['stored_members'] or number(payload['ordinal'])>=self.expected:raise InvalidValue()
                self._subjects(uow,self._member(payload))
                member=self.rows.write('fixed_memory_member',uow,{'v':1,'row_id':identity('fixed-member',set_id,payload['ordinal']),'revision':1,
                    'set_id':set_id,**{key:payload[key] for key in ('ordinal','member_id','event_json','memory_json','content_digest')},
                    'state':'STORED','object_ref':None,'source_id':None,'establishment_ref':None})
                target(member,None);update('fixed_memory_set',root,stored_members=number(root['stored_members'])+1)
            elif kind=='fixed_seal':
                if root['state']!='OPEN' or root['stored_members']!=self.expected:raise InvalidValue()
                digest=sha256();digest.update(b'[')
                for ordinal,member in enumerate(self._members(uow,set_id)):
                    self._member(member)
                    if member['state']!='STORED':raise InvalidValue()
                    if ordinal:digest.update(b',')
                    digest.update(encode_content(member['content_digest'],128))
                digest.update(b']')
                if digest.hexdigest()!=root['manifest_digest']:raise OwnerFailure('ACCESS_DENIED','review','BINDING_MISMATCH')
                update('fixed_memory_set',root,state='SEALED')
            elif kind=='fixed_establish':
                root,member=self.sealed_member(uow,set_id,number(payload['ordinal']))
                if member['revision']!=payload['expected_member_revision']:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
                proof=self.memory.prepare_fixed_source(uow,self,set_id,number(payload['ordinal']))
                value=proof.object_value;oid=string(value['object_id']);source=proof.source;sid=string(source['source_id'])
                event=object_record(source['event'])
                links=isolate_links({'sources':[{'object_id':oid,'object_revision':1,'source_id':sid,'link_role':'DIRECT',
                    'target_anchors':[{'message_id':event['client_event_key'],'part':'EVENT','item_index':None,'start_utf8':None,
                        'end_utf8':None,'occurrence_id':None,'interpretation_id':None}],'auxiliary_refs':[]}],'bases':[]},oid,1)
                scope=ApplyScope(self.instance,None,None,frozenset(),self._subjects(uow,value),frozenset((oid,)),frozenset((sid,)))
                change=MappingProxyType({'change_version':1,'action':'CREATE_MEMORY','target_id':oid,'expected_revision':None,'proposed_value':value,'links':links})
                applied=self.memory.apply_change_set(uow,scope,(change,),proof,None,number(envelope['observed_at']),operation.operation_key,'USER_REVIEWED_FIXED')
                if applied.history or len(applied.objects)!=1 or applied.objects[0]['previous_revision'] is not None:raise InvalidValue()
                counts=applied_counts(applied)
                receipt=MappingProxyType({'kind':kind,'key':operation.operation_key,'fingerprint':fingerprint})
                update('fixed_memory_member',member,state='ESTABLISHED',object_ref=MappingProxyType({'object_id':oid,'revision':1}),source_id=sid,establishment_ref=receipt)
                complete=number(root['established_members'])+1
                if complete==self.expected:
                    for established in self._members(uow,set_id):
                        if established['state']!='ESTABLISHED':raise InvalidValue()
                        # The current final receipt is staged, all preceding receipts must already be durable.
                        if established['ordinal']!=payload['ordinal']:
                            self._verify_establishment(uow,established)
                update('fixed_memory_set',root,established_members=complete,state='ESTABLISHED' if complete==self.expected else 'SEALED')
                targets.extend((MappingProxyType({'object_id':oid,'previous_revision':None,'revision':1}),
                    MappingProxyType({'object_id':sid,'previous_revision':None,'revision':2})))
                items=(oid,sid)
            else:raise InvalidValue()
        actual=self.storage.transaction_row_changes(uow)
        facts: dict[str,object]={'cognition':{'rows_changed':actual.get('cognition',0),'state':'APPLIED','references':[],'counts':[]}}
        if kind=='fixed_establish':
            assert self.memory.semantic is not None
            facts['memory']={'rows_changed':actual.get('memory',0),'state':'APPLIED','references':[],
                'counts':counts,**self.memory.semantic.audit_fact(uow)}
        return {'outcome':'APPLIED','targets':tuple(targets),'items':items,'facts':facts}

    def _verify_establishment(self,uow: UnitOfWork,member: Record) -> None:
        """Verify prior native receipts without issuing writes or model work."""
        receipt=object_record(member['establishment_ref'])
        result=self.storage.confirm_prior_operation(uow,self.establishment,string(receipt['key']))
        if (result is None or result.fingerprint!=receipt['fingerprint'] or receipt['kind']!='fixed_establish'
                or string(object_record(member['object_ref'])['object_id']) not in sequence(object_record(result.result)['items'])
                or member['source_id'] not in sequence(object_record(result.result)['items'])
                or set(object_record(object_record(result.result)['facts']))!={'cognition','memory'}):raise InvalidValue()

    async def recover(self,deadline: float) -> bool:
        """Check a bounded prefix of the frozen set and original two-audit receipts.

        This read-only cursor is kept until the immutable observed set has been
        checked completely. It never establishes missing members or sends work.
        """
        import time
        from companion_memory.persistence import Found
        from companion_memory.persistence.deadlines import DeadlineScope
        with DeadlineScope(deadline):
            root=await self.rows.read('fixed_memory_set',string(self.review.claims['set_id']))
            if root is None:return True
            if (root['config']!=self.config or any(root[k]!=self.review.claims[k] for k in
                    ('set_id','manifest_digest','review_ref','review_digest','reviewed_by'))):raise InvalidValue()
            if self._recovery_root is None:
                self._recovery_root=root;self._recovery_ordinal=0;self._recovery_digest=sha256(b'[')
            elif root!=self._recovery_root:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            count=number(root['stored_members'])
            while self._recovery_ordinal<count:
                if time.monotonic()>=deadline:return False
                ordinal=self._recovery_ordinal
                member=await self.rows.read('fixed_memory_member',identity('fixed-member',root['set_id'],ordinal))
                if member is None or member['set_id']!=root['set_id'] or member['ordinal']!=ordinal:raise InvalidValue()
                self._member(member)
                established=ordinal<number(root['established_members'])
                if (member['state']=='ESTABLISHED')!=established:raise InvalidValue()
                if established:
                    receipt=object_record(member['establishment_ref'])
                    found=await self.storage.bind_operation(self.establishment,self.instance).read_receipt(receipt['key'])
                    if type(found) is not Found or found.value.fingerprint!=receipt['fingerprint'] or receipt['kind']!='fixed_establish':raise InvalidValue()
                    result=object_record(found.value.result);oid=identity('fixed-memory',self.instance,root['set_id'],member['member_id'])
                    sid=identity('fixed-source',self.instance,root['set_id'],member['member_id'])
                    if (member['object_ref']!=MappingProxyType({'object_id':oid,'revision':1}) or member['source_id']!=sid
                            or result['items']!=(oid,sid) or set(object_record(result['facts']))!={'cognition','memory'}):raise InvalidValue()
                if ordinal:self._recovery_digest.update(b',')
                self._recovery_digest.update(encode_content(member['content_digest'],128));self._recovery_ordinal+=1
            digest=self._recovery_digest.copy();digest.update(b']')
            if root['state']!='OPEN' and digest.hexdigest()!=root['manifest_digest']:raise InvalidValue()
            if await self.rows.read('fixed_memory_set',string(root['set_id']))!=root:raise InvalidValue()
            return True
