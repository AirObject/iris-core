"""Frozen trusted activation and append-only original purpose-slot ownership.

Configuration and reviewed material do not issue this capability. Trusted setup
verifies the complete nonsecret binding, including external account facts for a
real transport. Restart reads the same journal and never activates or reserves.
"""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
import os
import stat
import time
from pathlib import Path
from hashlib import sha256
from types import MappingProxyType
from companion_memory.persistence.semantic_records import Record,ID,H,T,record,isolate,string,number,identity,INTENT,enum
from companion_memory.persistence.schema import SequenceSchema,BoundedTextSchema,InvalidValue,RecordSchema,Field
from dataclasses import replace
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.semantic_admission import SemanticAdmission
from companion_memory.persistence.deadlines import check_deadline
from companion_memory.persistence.owned_statements import OwnerFailure

QUERY=record(query_id=ID,text=BoundedTextSchema(512))
BINDING=record(format=enum('SEMANTIC_TRIAL_AUTH_V1'),package_id=ID,set_id=ID,instance_id=ID,database_id=ID,config_snapshot_id=ID,
    code_digest=H,material_digest=H,review_digest=H,protocol_digest=H,sdk_digest=H,decision_ref=ID,
    execution=enum('REAL','CONTROLLED','SIMULATED'),account_evidence_ref=ID,input_evidence_ref=ID,server_evidence_ref=ID,
    document_ids=SequenceSchema(ID,12,4096),queries=SequenceSchema(QUERY,6,6),expires_at=T)
USAGE_BINDING=RecordSchema(tuple(replace(f,schema=enum('SEMANTIC_TRIAL_AUTH_V2')) if f.name=='format' else
    replace(f,schema=SequenceSchema(ID,12,12)) if f.name=='document_ids' else f for f in BINDING.fields)
    +(Field('verification_mode',enum('USER_ALLOCATED_USAGE_TRIAL')),))
DAILY_DOCUMENT=record(object_id=ID,revision=T,material_digest=H,partition_id=ID)
DAILY_QUERY=record(query_id=ID,text=BoundedTextSchema(512),partition_id=ID)
DAILY_BINDING=RecordSchema(tuple(replace(f,schema=enum('DAILY_SEMANTIC_AUTH_V1')) if f.name=='format' else
    replace(f,schema=SequenceSchema(ID,0,12)) if f.name=='document_ids' else
    replace(f,schema=SequenceSchema(DAILY_QUERY,2,2)) if f.name=='queries' else f for f in BINDING.fields)
    +(Field('verification_mode',enum('USER_ALLOCATED_USAGE_TRIAL')),Field('documents',SequenceSchema(DAILY_DOCUMENT,0,12))))


@dataclass(frozen=True,slots=True,init=False)
class SemanticActivation:
    """Native trusted verification of one complete activation package."""
    authority: SemanticActivationAuthority
    binding: Record
    digest: str


class SemanticActivationAuthority:
    """Trusted assembly verifies factual bindings; the executor cannot self-sign."""
    def __init__(self,verify: Callable[[Record],bool]):
        self._verify=verify;self._grant: SemanticActivation|None=None

    def grant(self,binding: object) -> SemanticActivation:
        if type(binding) is dict or type(binding) is MappingProxyType:
            shape=DAILY_BINDING if binding.get('format')=='DAILY_SEMANTIC_AUTH_V1' else USAGE_BINDING if binding.get('format')=='SEMANTIC_TRIAL_AUTH_V2' else BINDING
            value=isolate(shape,binding,1048576)
        else:raise InvalidValue()
        if self._grant is not None or not self._verify(value):raise InvalidValue()
        documents=value['document_ids'];queries=value['queries'];assert type(documents) is tuple and type(queries) is tuple
        if len(set(documents))!=len(documents) or len({string(q['query_id']) for q in queries if type(q) is MappingProxyType})!=len(queries):raise InvalidValue()
        if value['format']=='DAILY_SEMANTIC_AUTH_V1':
            originals=value['documents']
            if type(originals) is not tuple or tuple(_record(d)['object_id'] for d in originals)!=documents or any(number(_record(d)['revision'])<1 for d in originals):raise InvalidValue()
        grant=object.__new__(SemanticActivation)
        object.__setattr__(grant,'authority',self);object.__setattr__(grant,'binding',value)
        object.__setattr__(grant,'digest',sha256(encode_content(value,1048576)).hexdigest());self._grant=grant
        return grant


class SemanticAuthorization:
    """Original slots survive process exit; a consumed slot is never refunded."""
    def __init__(self,root: Path,grant: SemanticActivation,admission: SemanticAdmission):
        if type(grant) is not SemanticActivation or grant.authority._grant is not grant or not grant.authority._verify(grant.binding):raise InvalidValue()
        self.grant=grant;self.admission=admission;self.path=root/'semantic-authorization.jsonl'
        self._fd=-1;self._entries: dict[str,Record]={};self._activated=False
        if self.path.exists():
            self._fd=os.open(self.path,os.O_RDWR|os.O_APPEND|os.O_NOFOLLOW)
            info=os.fstat(self._fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                os.close(self._fd);self._fd=-1;raise InvalidValue()
            try:
                with os.fdopen(os.dup(self._fd),'rb') as stream:
                    stream.seek(0)
                    header=stream.readline(8193)
                    if header!=self._header():raise InvalidValue()
                    self._activated=True
                    while raw:=stream.readline(8193):
                        if len(raw)>8192 or not raw.endswith(b'\n'):raise InvalidValue()
                        entry=isolate(record(work_id=ID,intent=INTENT,state=enum('RESERVED','COMPLETED')),decode_content(raw[:-1],8192))
                        if encode_content(entry,8192)+b'\n'!=raw:raise InvalidValue()
                        key=string(entry['work_id']);old=self._entries.get(key)
                        if old is not None and (old['state']!='RESERVED' or entry['state']!='COMPLETED' or old['intent']!=entry['intent']):raise InvalidValue()
                        if old is None and entry['state']!='RESERVED':raise InvalidValue()
                        intent=_record(entry['intent'])
                        if (intent['package_id']!=grant.binding['package_id'] or intent['authorization_digest']!=grant.digest
                                or number(intent['expires_at'])>number(grant.binding['expires_at'])):raise InvalidValue()
                        documents=grant.binding['document_ids'];queries=grant.binding['queries'];assert type(documents) is tuple and type(queries) is tuple
                        allowed={identity('semantic-slot',grant.binding['package_id'],'DOCUMENT',oid) for oid in documents}
                        allowed.update(identity('semantic-slot',grant.binding['package_id'],'QUERY',_record(q)['query_id']) for q in queries)
                        if intent['slot_id'] not in allowed or old is None and any(_record(e['intent'])['slot_id']==intent['slot_id'] for e in self._entries.values()):raise InvalidValue()
                        if old is None and len(self._entries)>=len(documents)+len(queries):raise InvalidValue()
                        self._entries[key]=entry
            except BaseException:
                os.close(self._fd);self._fd=-1;raise

    def _header(self) -> bytes:
        return encode_content(MappingProxyType({'format':self.grant.binding['format'],'package_id':self.grant.binding['package_id'],
            'authorization_digest':self.grant.digest}),8192)+b'\n'

    @property
    def activated(self) -> bool:
        return self._activated

    def original(self,work_id:str) -> Record|None:
        """Read only the exact work's immutable original slot reservation."""
        return self._entries.get(work_id)

    def activate(self) -> None:
        """Explicit resume activates only this verified original package."""
        if self._activated:return
        self.admission.admit_external('activate_package',string(self.grant.binding['instance_id']),string(self.grant.binding['package_id']),self.grant.digest)
        self._fd=os.open(self.path,os.O_RDWR|os.O_APPEND|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        raw=self._header()
        if os.write(self._fd,raw)!=len(raw):raise OSError('Authorization header incomplete.')
        os.fsync(self._fd)
        directory=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        self._activated=True

    def reserve(self,work: Record,request_digest: str,deadline_at: int,slot_id: str) -> Record:
        """Retain the exact first intention before Provider registration."""
        if not self._activated or work['kind']!='EMBED' or deadline_at>number(self.grant.binding['expires_at']):raise InvalidValue()
        key=string(work['work_id']);old=self._entries.get(key)
        if old is not None:
            intent=old['intent'];assert type(intent) is MappingProxyType
            if old['state']!='RESERVED' or intent['request_digest']!=request_digest or intent['expires_at']!=deadline_at or intent['slot_id']!=slot_id:raise InvalidValue()
            return intent
        if any(_record(e['intent'])['slot_id']==slot_id for e in self._entries.values()):raise InvalidValue()
        documents=self.grant.binding['document_ids'];queries=self.grant.binding['queries']
        assert type(documents) is tuple and type(queries) is tuple
        if len(self._entries)>=len(documents)+len(queries):raise InvalidValue()
        if work['purpose']=='DOCUMENT':
            oid=_record(work['object_ref'])['object_id']
            allowed=_record(work['object_ref'])['revision']==1 and oid in documents and slot_id==identity('semantic-slot',self.grant.binding['package_id'],'DOCUMENT',oid)
            if self.grant.binding['format']=='DAILY_SEMANTIC_AUTH_V1':
                originals=self.grant.binding['documents']
                if type(originals) is not tuple:raise InvalidValue()
                allowed=any(_record(d)['object_id']==oid and _record(d)['revision']==_record(work['object_ref'])['revision']
                    and _record(d)['material_digest']==work['material_digest'] and _record(d)['partition_id']==work['partition_id'] for d in originals)
                allowed=allowed and slot_id==identity('semantic-slot',self.grant.binding['package_id'],'DOCUMENT',oid)
        else:
            allowed=any(type(q) is MappingProxyType and sha256(string(q['text']).encode()).hexdigest()==work['material_digest']
                and slot_id==identity('semantic-slot',self.grant.binding['package_id'],'QUERY',q['query_id'])
                and (self.grant.binding['format']!='DAILY_SEMANTIC_AUTH_V1' or q['partition_id']==work['partition_id']) for q in queries)
        if not allowed:raise InvalidValue()
        intent=isolate(INTENT,{'package_id':self.grant.binding['package_id'],'slot_id':slot_id,'authorization_digest':self.grant.digest,
            'request_digest':request_digest,'expires_at':deadline_at})
        check_deadline()
        if deadline_at<=time.time_ns()//1000:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        self._append(key,intent,'RESERVED');return intent

    def _append(self,key: str,intent: Record,state: str) -> None:
        entry=MappingProxyType({'work_id':key,'intent':intent,'state':state});raw=encode_content(entry,8192)+b'\n'
        self.admission.admit_external('reserve_slot' if state=='RESERVED' else 'complete_slot',string(self.grant.binding['instance_id']),key,sha256(raw).hexdigest())
        if state=='RESERVED':
            check_deadline()
            if number(intent['expires_at'])<=time.time_ns()//1000:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED')
        if os.write(self._fd,raw)!=len(raw):raise OSError('Authorization append incomplete.')
        os.fsync(self._fd);self._entries[key]=entry

    def permits(self,description: Record,intent: Record) -> bool:
        old=self._entries.get(string(description['work_id']))
        return self._activated and old is not None and old['state']=='RESERVED' and old['intent']==intent and intent['authorization_digest']==self.grant.digest

    def complete(self,work_id: str) -> None:
        entry=self._entries[work_id]
        if entry['state']=='COMPLETED':return
        self._append(work_id,_record(entry['intent']),'COMPLETED')

    def close(self) -> None:
        if self._fd>=0:os.close(self._fd);self._fd=-1


def _record(value: object) -> Record:
    """Narrow an already validated immutable nested record."""
    if type(value) is not MappingProxyType:raise InvalidValue()
    return value
