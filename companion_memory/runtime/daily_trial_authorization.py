"""Trusted, bounded purpose accounting before native Provider registration.

Preparation never issues this capability. Explicit trusted approval binds one
host and immutable resources. Every consumed slot retains its exact original
request; reopening is paused and never refunds, reassigns or sends a slot.
"""
from __future__ import annotations
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import stat
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Receipt
from companion_memory.persistence.content_codec import encode_content,decode_content
from companion_memory.persistence.semantic_records import Record,record,ID,H,T,CONFIG,enum,isolate,string,number
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.deadlines import check_deadline
from companion_memory.persistence.completion import start_owned
from .daily_trial_package import purpose_slots,canonical

BINDING=record(format=enum('DAILY_TRIAL_AUTH_V1'),package_id=ID,config=CONFIG,code_digest=H,configuration_digest=H,
    resources_digest=H,materials_digest=H,decision_ref=ID,account_evidence_digest=H,input_evidence_digest=H,
    execution=enum('REAL','CONTROLLED'),expires_at=T)
REQUEST=record(slot_id=ID,role=enum('MEDIA','LEARNING','GOAL_DEDUP','EMBEDDING_DOCUMENT','EMBEDDING_QUERY'),
    request_id=ID,attempt_id=ID,operation_key=ID,work_id=ID,request_digest=H,wire_digest=H,account_id=ID,profile_id=ID)
ENTRY=record(request=REQUEST,state=enum('RESERVED','REGISTERED'),commit_id=(ID,),previous=H)


@dataclass(frozen=True,slots=True,init=False)
class DailyTrialActivation:
    authority:DailyTrialAuthority
    binding:Record
    digest:str


class DailyTrialAuthority:
    """Trusted setup verifies approval and selects only reviewed native material.

    The selector receives an owner-issued Provider request. It must check the
    frozen display/source/work mapping and return its one original purpose slot.
    Neither configuration, model output nor an HTTP caller receives this object.
    """
    def __init__(self,verify:Callable[[Record],bool],select:Callable[[object],str]):
        self.verify=verify;self.select=select;self.grant:DailyTrialActivation|None=None

    def activate(self,binding:object) -> DailyTrialActivation:
        value=isolate(BINDING,binding)
        if self.grant is not None or not self.verify(value):raise InvalidValue()
        grant=object.__new__(DailyTrialActivation)
        for key,item in (('authority',self),('binding',value),('digest',sha256(encode_content(value,8192)).hexdigest())):object.__setattr__(grant,key,item)
        self.grant=grant;return grant


class DailyTrialAuthorization:
    """One locked append-only outer ledger; native attempts remain authoritative."""
    def __init__(self,root:Path,activation:DailyTrialActivation,provider):
        if type(activation) is not DailyTrialActivation or activation.authority.grant is not activation or not activation.authority.verify(activation.binding):raise InvalidValue()
        expected={'database_id':provider.configuration.database_id,'instance_id':provider.instance,'snapshot_id':provider.configuration.snapshot_id}
        self.dream=activation.binding['format']=='DREAM_TRIAL_AUTH_V1'
        self.request_schema=REQUEST;self.entry_schema=ENTRY
        slots=purpose_slots();filename='daily-trial-authorization.jsonl';self.journal_format='DAILY_TRIAL_JOURNAL_V1'
        if self.dream:
            from .dream_trial_authorization import DreamTrialAuthority,REQUEST as DREAM_REQUEST,ENTRY as DREAM_ENTRY,purpose_slots as dream_slots
            from companion_memory.configuration.dream_persistence import StoredDreamConfiguration
            from companion_memory.configuration.dream_codec import candidate_values as dream_values
            if type(activation.authority) is not DreamTrialAuthority or type(provider.configuration) is not StoredDreamConfiguration:raise InvalidValue()
            configuration_digest=sha256(canonical(dream_values(provider.configuration.candidate))).hexdigest()
            self.request_schema=DREAM_REQUEST;self.entry_schema=DREAM_ENTRY;slots=list(dream_slots())
            filename='dream-trial-authorization.jsonl';self.journal_format='DREAM_TRIAL_JOURNAL_V1'
        else:
            from companion_memory.configuration.daily_codec import candidate_values
            configuration_digest=sha256(canonical(candidate_values(provider.configuration.candidate))).hexdigest()
        if (activation.binding['config']!=expected or activation.binding['configuration_digest']!=configuration_digest
                or root.resolve(strict=True)!=root or any(p.is_symlink() for p in (root,*root.parents))):raise InvalidValue()
        transports=tuple(provider.transports.values())+(provider.transport,)
        if any(t is None or t.execution_kind!=activation.binding['execution'] for t in transports):raise InvalidValue()
        self.activation=activation;self.provider=provider;self.path=root/filename
        self.stop_path=root/'dream-trial-stop.json'
        self.stopped=self.dream and self.stop_path.exists()
        self.slots={s['slot_id']:s['role'] for s in slots};self.entries:dict[str,Record]={}
        self.fd=-1;self.active=False;self.closed=False;self.faulted=False;self.pending:asyncio.Task|None=None;self.digest='0'*64
        if self.path.exists():
            self.fd=os.open(self.path,os.O_RDWR|os.O_APPEND|os.O_NOFOLLOW)
            try:
                self._lock()
                with os.fdopen(os.dup(self.fd),'rb') as stream:
                    stream.seek(0)
                    if stream.readline(8193)!=self.header():raise InvalidValue()
                    for ordinal in range(65):
                        raw=stream.readline(16385)
                        if not raw:break
                        if ordinal>=64 or len(raw)>16384 or not raw.endswith(b'\n'):raise InvalidValue()
                        entry=isolate(self.entry_schema,decode_content(raw[:-1],16384),16384)
                        if encode_content(entry,16384)+b'\n'!=raw or entry['previous']!=self.digest:raise InvalidValue()
                        self._accept(entry);self.digest=sha256(raw).hexdigest()
            except BaseException:
                os.close(self.fd);self.fd=-1;raise

    def header(self) -> bytes:
        return encode_content(MappingProxyType({'format':self.journal_format,'authorization_digest':self.activation.digest}),8192)+b'\n'

    def _lock(self):
        info=os.fstat(self.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise InvalidValue()
        fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def _accept(self,entry:Record):
        request=cast(Record,entry['request']);slot=string(request['slot_id']);old=self.entries.get(slot)
        if self.slots.get(slot)!=request['role']:raise InvalidValue()
        if old is None:
            if entry['state']!='RESERVED' or entry['commit_id'] is not None or any(cast(Record,e['request'])['request_id']==request['request_id'] for e in self.entries.values()):raise InvalidValue()
        elif old['state']!='RESERVED' or entry['state']!='REGISTERED' or entry['commit_id'] is None or old['request']!=request:raise InvalidValue()
        self.entries[slot]=entry

    async def _io(self,operation):
        if self.pending is not None:raise OwnerFailure('RESOURCE_BUSY','authorization','CLEANUP_PENDING',True)
        actual,logical=start_owned(asyncio.to_thread(operation));self.pending=actual
        def ended(job):
            if not job.cancelled():job.exception()
            if self.pending is job:self.pending=None
        actual.add_done_callback(ended)
        try:return await asyncio.shield(logical)
        except OSError:
            self.faulted=True;self.active=False
            raise OwnerFailure('RESULT_UNCONFIRMED','authorization','COMMIT_UNCONFIRMED',not actual.done()) from None
        except BaseException:
            self.faulted=True;self.active=False;raise
        finally:await asyncio.wait((actual,))

    async def resume(self):
        """An explicit trusted resume is separate from construction and recovery."""
        self._admit()
        await self.verify_native()
        if self.fd<0:
            def create():
                self.fd=os.open(self.path,os.O_RDWR|os.O_APPEND|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);self._lock()
                raw=self.header()
                if os.write(self.fd,raw)!=len(raw):raise OSError('Incomplete trial header')
                os.fsync(self.fd);directory=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(directory)
                finally:os.close(directory)
            await self._io(create)
        self._admit();self.active=True

    def _admit(self):
        check_deadline()
        if self.stopped:raise OwnerFailure('ACCESS_DENIED','authorization','PACKAGE_STOPPED')
        if self.faulted:raise OwnerFailure('RESULT_UNCONFIRMED','authorization','COMMIT_UNCONFIRMED')
        if self.closed or not self.provider.ready or not self.activation.authority.verify(self.activation.binding):raise InvalidValue()
        if number(self.activation.binding['expires_at'])<=time.time_ns()//1000:raise OwnerFailure('TIMEOUT','authorization','DEADLINE_EXCEEDED')

    async def verify_native(self):
        try:await self._verify_native()
        except OwnerFailure as failure:
            if self.dream:await self.stop('ORIGINAL_ACCOUNTING_UNCONFIRMED')
            raise failure

    async def _verify_native(self):
        """Compare both ledgers without repairing or sending any request."""
        expected={cast(Record,e['request'])['request_id']:e for e in self.entries.values()};seen=set();after=''
        while page:=await self.provider.ledger.read('requests_page',{'after':after,'limit':8}):
            for request in page:
                self.provider.checkpoint();after=request['object_id'];entry=expected.get(after)
                if entry is None:raise OwnerFailure('STORAGE_FAILED','authorization','INTEGRITY_FAILURE')
                bound=cast(Record,entry['request'])
                if (entry['state']!='REGISTERED' or request['fingerprint']!=bound['request_digest'] or request['task_role']!=bound['role']
                        or request['operation_key']!=bound['operation_key'] or request['account_id']!=bound['account_id']):raise OwnerFailure('RESULT_UNCONFIRMED','authorization','COMMIT_UNCONFIRMED')
                original=await self.provider.registered_request_receipt(request['object_id'])
                if original.commit_id!=entry['commit_id']:raise OwnerFailure('STORAGE_FAILED','authorization','INTEGRITY_FAILURE')
                seen.add(after)
                if request['phase']!='TERMINAL':raise OwnerFailure('RESOURCE_BUSY','authorization','ORIGINAL_RESULT_UNCONFIRMED')
                error=request['first_error']
                if error is not None and cast(Record,error)['reason'] in ('AUTHENTICATION_FAILED','MODEL_BINDING_MISMATCH','INVALID_RESPONSE','INPUT_FORMAT_UNSUPPORTED','COMMIT_UNCONFIRMED'):
                    raise OwnerFailure('ACCESS_DENIED','authorization','OPERATION_NOT_GRANTED')
                if request['outcome']=='SUCCEEDED':
                    handoff=await self.provider.ledger.get('handoffs',request['handoff_id'])
                    if handoff is None or cast(Record,handoff['embedding_cleanup'])['state']!='RETIRED':raise OwnerFailure('RESOURCE_BUSY','authorization','CLEANUP_PENDING',True)
        if seen!=set(expected):raise OwnerFailure('RESULT_UNCONFIRMED','authorization','COMMIT_UNCONFIRMED')

    async def reserve(self,request):
        """Consume one reviewed purpose after native material checks, before SQL."""
        self._admit()
        if not self.active or self.fd<0 or request.owner is not self.provider:raise InvalidValue()
        await self.verify_native()
        slot=self.activation.authority.select(request)
        from companion_memory.provider.daily_execution import DailyRequest
        from companion_memory.provider.embedding_service import EmbeddingRequest
        if type(request) is DailyRequest:role=request.binding.role;account=request.account['account_id']
        elif type(request) is EmbeddingRequest:
            role='EMBEDDING_'+string(cast(Record,request.description['payload'])['purpose']);account=self.provider.account['account_id']
        else:raise InvalidValue()
        if self.slots.get(slot)!=role or slot in self.entries:raise OwnerFailure('ACCESS_DENIED','authorization','OPERATION_NOT_GRANTED')
        value=isolate(self.request_schema,{'slot_id':slot,'role':role,'request_id':request.request_id,'attempt_id':request.attempt_id,
            'operation_key':request.description['original_request_key'],'work_id':request.description['work_id'],'request_digest':request.fingerprint,
            'wire_digest':sha256(request.wire).hexdigest(),'account_id':account,'profile_id':request.description['profile_id']})
        await self._append(value,'RESERVED',None);self._admit()

    async def registered(self,request,receipt:Receipt):
        if await self.provider.trial_registration_receipt(request)!=receipt:raise InvalidValue()
        values=[cast(Record,e['request']) for e in self.entries.values() if cast(Record,e['request'])['request_id']==request.request_id]
        if len(values)!=1 or receipt.identity.operation_kind not in ('register','register_daily_request'):raise InvalidValue()
        await self._append(values[0],'REGISTERED',receipt.commit_id)

    async def _append(self,request:Record,state:str,commit_id:str|None):
        if self.faulted:raise OwnerFailure('RESULT_UNCONFIRMED','authorization','COMMIT_UNCONFIRMED')
        entry=isolate(self.entry_schema,{'request':request,'state':state,'commit_id':commit_id,'previous':self.digest},16384)
        raw=encode_content(entry,16384)+b'\n'
        def write():
            if state=='RESERVED':self._admit()
            if os.write(self.fd,raw)!=len(raw):raise OSError('Incomplete trial entry')
            os.fsync(self.fd);self._accept(entry);self.digest=sha256(raw).hexdigest()
        await self._io(write)

    async def stop(self,reason:str,request_id:str|None=None):
        """Fsync a permanent package stop; no original slot or duty is released."""
        if not self.dream:return
        allowed=('PROVIDER_FAILURE','REMOTE_UNKNOWN','MODEL_PROTOCOL_REJECTED','ORIGINAL_ACCOUNTING_UNCONFIRMED','RESOURCE_LIMIT','CLEANUP_UNFINISHED')
        if reason not in allowed:raise InvalidValue()
        self.active=False;self.stopped=True
        raw=encode_content(MappingProxyType({'format':'DREAM_TRIAL_STOP_V1','authorization_digest':self.activation.digest,
            'reason':reason,'request_id':request_id,'journal_digest':self.digest}),8192)+b'\n'
        def persist():
            if self.stop_path.exists():
                previous=self.stop_path.read_bytes()
                if len(previous)>8192:raise InvalidValue()
                value=decode_content(previous.rstrip(b'\n'),8192)
                if type(value) is not dict or value.get('authorization_digest')!=self.activation.digest:raise InvalidValue()
                return
            fd=os.open(self.stop_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            try:
                if os.write(fd,raw)!=len(raw):raise OSError('Incomplete stop record')
                os.fsync(fd)
            finally:os.close(fd)
            directory=os.open(self.stop_path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        await self._io(persist)

    def close(self) -> bool:
        self.closed=True;self.active=False
        if self.pending is not None:return False
        if self.fd>=0:os.close(self.fd);self.fd=-1
        return True
