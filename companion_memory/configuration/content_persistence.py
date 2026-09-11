"""Configuration-owned initial publication and exact cross-restart reconstruction.

One short transaction installs all immutable domains and the active pointer.
Initialization confirmation uses retained original candidate/intent, not a newly
invented version. No edit, activation, migration or repair operation is exposed.
"""
from __future__ import annotations

from dataclasses import dataclass,replace
import asyncio
from types import MappingProxyType
from typing import cast

from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence import (
    AuditFieldBinding,AuditResultBinding,Committed,Field,Found,NotCommitted,NotFound,
    PersistenceService,RecoveryHandle,Unconfirmed,RecordSchema,ResultBoundCommand,ResultBoundCommandDefinition,
    Rejected,Failed,ScalarSchema,SequenceSchema,BoundedTextSchema,Staged,UnitOfWork,Value,
)
from companion_memory.persistence.configuration_repository import create_configuration_repository
from . import create_registry_builder,Ok
from .content_material import ContentMaterialContract as MaterialContract
from .content_codec import decode_content_entry as decode_entry, candidate_values as _candidate_values, entries_digest as _digest, platform_domain_id as _platform_domain_id
from .content_resolution import (
    ContentConfigurationCandidate,ContentConfigurationErr,ContentConfigurationOk,
    configuration_failure,resolve_content_configuration,content_snapshot_issue,
)
from .content_persistent_results import ConfigurationCommitted,ConfigurationNotCommitted,ConfigurationRejected,ConfigurationUnconfirmed

ID=ScalarSchema('identifier')
INT=ScalarSchema('integer')
DOMAINS=SequenceSchema(RecordSchema((Field('domain_id',ID),Field('revision',INT))),1,4)
TARGETS=SequenceSchema(RecordSchema((Field('object_id',ID),Field('previous_revision',INT,nullable=True),Field('revision',INT))),1,16)
CHANGE=RecordSchema((Field('snapshot_id',ID),Field('domains',DOMAINS)))


_STORED_ISSUER=object()


class _IncompleteVersion(ValueError):
    """A referenced immutable configuration version has missing rows."""


class _UnsupportedFormat(ValueError):
    """Stored configuration encoding cannot be interpreted by this reader."""


@dataclass(frozen=True,slots=True,init=False)
class StoredContentConfiguration:
    """Configuration-issued durable identity plus reconstructed native domain views."""
    snapshot_id: str
    database_id: str
    revisions: tuple[tuple[str,int],...]
    candidate: ContentConfigurationCandidate
    _issuer:object

    def __init__(self):
        raise TypeError('Load a fully validated persistent configuration.')


class ContentConfigurationAssembly:
    """Trusted complete storage declarations and single configuration publisher."""
    def __init__(self):
        repository=create_configuration_repository()
        self.repository=replace(repository, definition=replace(repository.definition, schema_version=2))
        self._binding: ContentConfigurationBinding | None = None
        requirement=AuditRequirement('configuration','configuration_initialized','CONFIGURATION_INITIALIZED',1,('INITIALIZE',),CHANGE)
        definition=ResultBoundCommandDefinition('configuration','initialize_content',1,
            RecordSchema((Field('catalog',BoundedTextSchema(8192)),Field('domains',SequenceSchema(RecordSchema((Field('domain_id',ID),Field('digest',ID),Field('entries',SequenceSchema(RecordSchema((Field('parameter_key',BoundedTextSchema(128)),Field('body',BoundedTextSchema(8192)))),1,128)))),1,4)))),
            1,RecordSchema((Field('snapshot_id',ID),Field('domains',DOMAINS),Field('targets',TARGETS),Field('change',CHANGE))),
            (self.repository.definition,),(requirement,),self._handle,RecordSchema((Field('actor',ID),)),
            (AuditResultBinding(requirement.event_slot,1,(
                AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),
                AuditFieldBinding('reason_code','CONSTANT',constant='INITIALIZE'),AuditFieldBinding('target_refs','RESULT',('targets',)),
                AuditFieldBinding('change','RESULT',('change',)),)),))
        self.commands=(definition,)
        self.repositories=(self.repository.definition,)

    def _handle(self,uow:UnitOfWork,values:MappingProxyType[str,Value]) -> object:
        if self._binding is None:
            raise ValueError('Configuration publisher is not bound.')
        return self._binding.handle_initialization(uow,values)

    def bind(self,storage:PersistenceService,instance_id:str) -> ContentConfigurationBinding:
        if self._binding is not None:
            raise ValueError('Configuration assembly is already bound.')
        binding=ContentConfigurationBinding(self,storage,instance_id)
        self._binding=binding
        return binding


class ContentConfigurationBinding:
    """Restricted initialization and point loads through configuration-owned ports."""
    def __init__(self,assembly:ContentConfigurationAssembly,storage:PersistenceService,instance_id:str):
        self._tasks:set[asyncio.Task]=set()
        self._closed=False
        self._assembly=assembly
        self._storage=storage
        lease=storage.claim_module_owner(assembly.repository.definition)
        if lease is None:
            raise ValueError('Configuration owner is unavailable.')
        self._lease=lease
        self._ports={name:storage.bind_statement(assembly.repository.definition,definition,instance_id) for name,definition in assembly.repository.statements}
        self._operation=storage.bind_operation(assembly.commands[0],instance_id)

    def handle_initialization(self,uow:UnitOfWork,values:MappingProxyType[str,Value]) -> object:
        def apply(name:str,args:dict[str,object]):
            result=self._ports[name].participate(uow,args)
            if type(result) is not Staged or type(result.value) is not tuple:
                raise ValueError('Configuration transaction could not be completed.')
            return cast(tuple[MappingProxyType[str,Value],...],result.value)
        if apply('active',{}):
            raise ValueError('Active configuration cannot be overwritten.')
        domains=cast(tuple[MappingProxyType[str,Value],...],values['domains'])
        revisions=[]
        for domain in domains:
            entries=cast(tuple[MappingProxyType[str,Value],...],domain['entries'])
            revision=apply('domain_insert',{'domain_id':domain['domain_id'],'item_count':len(entries),'digest':domain['digest']})[0]['revision']
            for entry in entries:
                apply('entry_insert',{'domain_id':domain['domain_id'],'parameter_key':entry['parameter_key'],'body':entry['body']})
            revisions.append({'domain_id':domain['domain_id'],'revision':revision})
        snapshot_id=apply('allocate',{'body':values['catalog']})[0]['snapshot_id']
        apply('activate',{'snapshot_id':snapshot_id})
        return {'snapshot_id':snapshot_id,'domains':revisions,'targets':[{'object_id':snapshot_id,'previous_revision':None,'revision':1}],
                'change':{'snapshot_id':snapshot_id,'domains':revisions}}

    def _finished(self,task:asyncio.Task):
        if not task.cancelled():task.exception()
        self._tasks.discard(task)

    async def persist_content_configuration(self,operation_key:object,candidate:object,*,actor:str,protected_directories:object) -> object:
        """Bound the complete publication/load while retaining an unfinished owner."""
        operation='persist_content_configuration'
        if self._closed or content_snapshot_issue(candidate) is not None:return configuration_failure('ACCESS_DENIED','identity','BINDING_MISMATCH',operation)
        assert type(candidate) is ContentConfigurationCandidate
        from .content_codec import candidate_inputs
        preflight = resolve_content_configuration(*candidate_inputs(candidate, protected_directories))
        if type(preflight) is ContentConfigurationErr:
            return ContentConfigurationErr(replace(preflight.error, operation=operation))
        try:command=ResultBoundCommand(1,_candidate_values(candidate),{'configuration_initialized':{'actor':actor}})
        except ValueError:return configuration_failure('INVALID_INPUT','value','LIMIT_EXCEEDED',operation)
        handle=self._operation.recovery_handle(operation_key,command)
        if type(handle) is not RecoveryHandle:return self._commit_result(handle)
        if self._tasks:return ConfigurationUnconfirmed(handle,configuration_failure('PERSISTENCE_FAILED','storage','UNCONFIRMED',operation).error,True)
        task=asyncio.create_task(self._publish(operation_key,command,handle,candidate,protected_directories))
        self._tasks.add(task);task.add_done_callback(self._finished)
        done,_=await asyncio.wait((task,),timeout=candidate.runtime.integer('runtime.operation_timeout_ms')/1000)
        return task.result() if done else ConfigurationUnconfirmed(handle,configuration_failure('PERSISTENCE_FAILED','storage','UNCONFIRMED',operation).error,True)

    async def _publish(self,operation_key,command,handle,candidate,protected_directories):
        confirmed=await self._operation.resolve_operation(handle)
        if type(confirmed) is NotCommitted:confirmed=await self._operation.execute(operation_key,command)
        if type(confirmed) is not Committed:return self._commit_result(confirmed)
        result=confirmed.receipt.result
        assert type(result) is MappingProxyType
        loaded=await self._load(result['snapshot_id'],candidate.material_contracts,protected_directories,bootstrap=candidate)
        if type(loaded) is ContentConfigurationErr:return ConfigurationCommitted(confirmed.receipt,confirmed.source,None,loaded.error,self._storage.get_health().cleanup_pending)
        assert type(loaded) is ContentConfigurationOk
        return ConfigurationCommitted(confirmed.receipt,confirmed.source,loaded.value)

    def _commit_result(self,result:object):
        operation='persist_content_configuration'
        if type(result) is Unconfirmed:
            return ConfigurationUnconfirmed(result.recovery_handle,configuration_failure('PERSISTENCE_FAILED','storage','UNCONFIRMED',operation).error,result.error.cleanup_pending)
        lower=result.error if type(result) is Rejected or type(result) is NotCommitted or type(result) is Failed else None
        pending=bool(lower and lower.cleanup_pending)
        reason=lower.reason if lower else None
        if reason=='CONTENT_MISMATCH':failure=configuration_failure('INTEGRITY_FAILURE','identity','CONTENT_MISMATCH',operation).error
        elif reason=='CAPABILITY_MISMATCH':failure=configuration_failure('ACCESS_DENIED','identity','BINDING_MISMATCH',operation).error
        elif reason in ('INVALID_SHAPE','LIMIT_EXCEEDED','UNSUPPORTED_COMMAND'):failure=configuration_failure('INVALID_INPUT','value','LIMIT_EXCEEDED' if reason=='LIMIT_EXCEEDED' else 'INVALID_SHAPE',operation).error
        else:failure=configuration_failure('PERSISTENCE_FAILED','storage','NOT_COMMITTED',operation).error
        if type(result) is NotCommitted:return ConfigurationNotCommitted(failure,pending)
        return ConfigurationRejected(failure,pending)

    async def active_snapshot_id(self) -> object:
        result=await self._ports['active'].read_object({})
        if type(result) is Found and type(result.value) is tuple:
            if not result.value:return NotFound()
            row=result.value[0]
            assert type(row) is MappingProxyType
            return Found(row['snapshot_id'])
        return result

    async def load_content_configuration(self,snapshot_id:object,material_contracts:tuple[MaterialContract,...],protected_directories:object,*,bootstrap:ContentConfigurationCandidate):
        """Bounded whole-domain reconstruction; timeout cannot release a live reader."""
        if self._closed or content_snapshot_issue(bootstrap) is not None:return configuration_failure('ACCESS_DENIED','identity','BINDING_MISMATCH','load_content_configuration')
        if self._tasks:return configuration_failure('PERSISTENCE_FAILED','storage','READ_FAILED','load_content_configuration')
        task=asyncio.create_task(self._load(snapshot_id,material_contracts,protected_directories,bootstrap=bootstrap))
        self._tasks.add(task);task.add_done_callback(self._finished)
        done,_=await asyncio.wait((task,),timeout=bootstrap.runtime.integer('runtime.operation_timeout_ms')/1000)
        return task.result() if done else configuration_failure('PERSISTENCE_FAILED','storage','READ_FAILED','load_content_configuration')

    async def _load(self,snapshot_id:object,material_contracts:tuple[MaterialContract,...],protected_directories:object,*,bootstrap:ContentConfigurationCandidate) -> ContentConfigurationOk[StoredContentConfiguration] | ContentConfigurationErr:
        """Reconstruct complete definitions and values, refusing missing or altered rows."""
        operation='load_content_configuration'
        async def read(name,args):
            result=await self._ports[name].read_object(args)
            if type(result) is NotFound:return ()
            if type(result) is not Found or type(result.value) is not tuple:
                raise OSError('Configuration read is unavailable.')
            return cast(tuple[MappingProxyType[str,Value],...],result.value)
        try:
            rows=await read('snapshot',{'snapshot_id':snapshot_id})
            if len(rows)!=1:raise _IncompleteVersion()
            from .persistent_codec import decode_json
            catalog=decode_json(cast(str,rows[0]['body']))
            if type(catalog) is not dict:raise _UnsupportedFormat()
            if set(catalog)!= {'version','domains','materials'} or type(catalog['version']) is not int or catalog['version']!=2:
                raise _UnsupportedFormat()
            original=_candidate_values(bootstrap)
            if original['catalog'] != rows[0]['body']:
                raise ValueError('Configuration bootstrap or material binding differs.')
            domains={}
            revisions=[]
            encoded_by_domain={}
            for item in catalog['domains']:
                identity,revision=item['domain_id'],item['revision']
                metadata=await read('domain',{'domain_id':identity,'revision':revision})
                keys=await read('entry_keys',{'domain_id':identity,'revision':revision})
                if not metadata:raise _IncompleteVersion()
                if len(metadata)!=1 or len(keys)!=metadata[0]['item_count'] or len(keys)>128 or identity in domains:
                    raise ValueError('Configuration domain is incomplete.')
                builder=create_registry_builder(); explicit={};encoded_entries=[]
                for row in keys:
                    found=await read('entry',{'domain_id':identity,'revision':revision,'parameter_key':row['parameter_key']})
                    if len(found)!=1:raise _IncompleteVersion()
                    encoded=cast(str,found[0]['body'])
                    definition,present,value,source=decode_entry(encoded)
                    if definition['key']!=row['parameter_key'] or type(builder.register(definition)) is not Ok:
                        raise ValueError('Configuration definition is invalid.')
                    if present and source=='EXPLICIT':explicit[definition['key']]=value
                    encoded_entries.append((definition['key'],encoded))
                if _digest(tuple(encoded_entries))!=metadata[0]['digest']:
                    raise ValueError('Configuration content differs.')
                frozen=builder.freeze()
                if type(frozen) is not Ok:raise ValueError('Configuration registry cannot be reconstructed.')
                domains[identity]={'registry':frozen.value,'explicit_values':explicit}
                encoded_by_domain[identity]=tuple(encoded_entries)
                revisions.append((identity,revision))
            platform_ids={_platform_domain_id(m.platform_id):m.platform_id for m in bootstrap.material_contracts}
            platforms=[{'platform_id':platform_ids[identity],**d} for identity,d in domains.items() if identity.startswith('platform:')]
            resolved=resolve_content_configuration(domains['foundation'],domains['runtime'],platforms,domains['content'],protected_directories,material_contracts)
            if type(resolved) is not ContentConfigurationOk:
                raise ValueError('Configuration values are no longer supported.')
            # Compare full re-encoded definitions/value provenance, including defaults.
            if _candidate_values(resolved.value)!=original:
                raise ValueError('Stored configuration or bootstrap differs.')
            rebuilt=_candidate_values(resolved.value)
            for d in cast(list[dict[str,object]],rebuilt['domains']):
                entries=cast(list[dict[str,str]],d['entries'])
                if tuple((e['parameter_key'],e['body']) for e in entries)!=encoded_by_domain[d['domain_id']]:
                    raise ValueError('Stored value provenance differs.')
            stored=object.__new__(StoredContentConfiguration)
            object.__setattr__(stored,"_issuer",_STORED_ISSUER)
            for key,value in dict(snapshot_id=snapshot_id,database_id=self._lease.database_id,revisions=tuple(revisions),candidate=resolved.value).items():
                object.__setattr__(stored,key,value)
            return ContentConfigurationOk(stored)
        except OSError:
            return configuration_failure('PERSISTENCE_FAILED','storage','READ_FAILED',operation)
        except _IncompleteVersion:
            return configuration_failure('INTEGRITY_FAILURE','storage','VERSION_MISSING',operation)
        except _UnsupportedFormat:
            return configuration_failure('INTEGRITY_FAILURE','storage','FORMAT_UNSUPPORTED',operation)
        except (ValueError,KeyError,TypeError,AttributeError,RecursionError,OverflowError):
            return configuration_failure('INTEGRITY_FAILURE','storage','CONTENT_MISMATCH',operation)

    def close(self) -> bool:
        """Release only after outstanding storage jobs have finished."""
        self._closed=True
        health=self._storage.get_health()
        # A storage deadline ends its await, not the underlying SQLite thread.
        # Without per-module job accounting, retain conservatively until all
        # borrowed storage I/O and cleanup have ended.
        return not (self._tasks or health.reads_in_flight or health.writes_in_flight or health.cleanup_pending) and self._lease.release()


def stored_configuration_issue(value:object) -> bool:
    """Pure native view validation; a foreign database still fails at resource binding."""
    if type(value) is not StoredContentConfiguration:return True
    try:return object.__getattribute__(value,'_issuer') is not _STORED_ISSUER or content_snapshot_issue(value.candidate) is not None
    except AttributeError:return True
