"""Configuration-owned initial publication and exact cross-restart reconstruction.

One short transaction installs all immutable domains and the active pointer.
Initialization confirmation uses retained original candidate/intent, not a newly
invented version. No edit, activation, migration or repair operation is exposed.
"""
from __future__ import annotations

from dataclasses import dataclass,replace
import asyncio
import time
from types import MappingProxyType
from typing import cast

from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence.completion import start_owned, CompletionScope
from companion_memory.persistence import (
    AuditFieldBinding,AuditResultBinding,Committed,Field,Found,NotCommitted,NotFound,
    PersistenceService,RecoveryHandle,Unconfirmed,RecordSchema,ResultBoundCommand,ResultBoundCommandDefinition,
    Rejected,Failed,ScalarSchema,SequenceSchema,BoundedTextSchema,Staged,UnitOfWork,Value,
)
from companion_memory.persistence.configuration_repository import create_configuration_repository
from . import create_registry_builder,Ok
from .semantic_resolution import TextMaterialContract as MaterialContract
from .content_codec import decode_content_entry as decode_entry, entries_digest as _digest, platform_domain_id as _platform_domain_id
from .semantic_codec import candidate_values as _candidate_values
from .semantic_resolution import (
    SemanticConfigurationCandidate,SemanticConfigurationErr,SemanticConfigurationOk,
    configuration_failure,resolve_semantic_configuration,semantic_snapshot_issue,
)
from .semantic_persistent_results import ConfigurationCommitted,ConfigurationNotCommitted,ConfigurationRejected,ConfigurationUnconfirmed

ID=ScalarSchema('identifier')
INT=ScalarSchema('integer')
DOMAINS=SequenceSchema(RecordSchema((Field('domain_id',ID),Field('revision',INT))),1,6)
TARGETS=SequenceSchema(RecordSchema((Field('object_id',ID),Field('previous_revision',INT,nullable=True),Field('revision',INT))),1,16)
CHANGE=RecordSchema((Field('snapshot_id',ID),Field('domains',DOMAINS)))
from companion_memory.persistence.semantic_records import N,integer,CONFIG,isolate,string
from companion_memory.persistence.owned_statements import StatementCatalog
from companion_memory.memory.semantic_initialization import SemanticMemoryInitialization
from companion_memory.retrieval.semantic_initialization import SemanticRetrievalInitialization


_STORED_ISSUER=object()


class _IncompleteVersion(ValueError):
    """A referenced immutable configuration version has missing rows."""


class _UnsupportedFormat(ValueError):
    """Stored configuration encoding cannot be interpreted by this reader."""


@dataclass(frozen=True,slots=True,init=False)
class StoredSemanticConfiguration:
    """Configuration-issued durable identity plus reconstructed native domain views."""
    snapshot_id: str
    database_id: str
    revisions: tuple[tuple[str,int],...]
    candidate: SemanticConfigurationCandidate
    _issuer:object

    def __init__(self):
        raise TypeError('Load a fully validated persistent configuration.')


def stored_semantic_configuration_issue(value: object) -> str | None:
    """Check native durable issuance and the complete reconstructed candidate."""
    if type(value) is not StoredSemanticConfiguration or getattr(value, '_issuer', None) is not _STORED_ISSUER:
        return 'DEFINITION_MISMATCH'
    return 'DEFINITION_MISMATCH' if semantic_snapshot_issue(value.candidate) is not None else None


class SemanticConfigurationAssembly:
    """Trusted complete storage declarations and single configuration publisher."""
    def __init__(self,memory_catalog: StatementCatalog | None=None,retrieval_catalog: StatementCatalog | None=None):
        from companion_memory.runtime.content_assembly import owner_fact
        memory_fact=RecordSchema(owner_fact('memory').fields+(Field('semantic_root',ID),Field('semantic_from_seq',N),
            Field('semantic_to_seq',N),Field('semantic_gap_delta',integer(-8,8))))
        root_facts=RecordSchema((Field('memory',memory_fact),Field('retrieval',owner_fact('retrieval'))))
        repository=create_configuration_repository()
        self.repository=replace(repository, definition=replace(repository.definition, schema_version=5))
        self._binding: SemanticConfigurationBinding | None = None
        self.memory=SemanticMemoryInitialization(memory_catalog);self.retrieval=SemanticRetrievalInitialization(retrieval_catalog)
        self.repositories=(self.repository.definition,self.memory.catalog.definition,self.retrieval.catalog.definition)
        root_requirements=tuple(AuditRequirement(owner,owner+'_initialized','INITIALIZE_SEMANTIC',1,('APPLY',),schema)
            for owner,schema in (('memory',memory_fact),('retrieval',owner_fact('retrieval'))))
        root_bindings=tuple(AuditResultBinding(r.event_slot,1,(
            AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),
            AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),AuditFieldBinding('target_refs','RESULT',('targets',)),
            AuditFieldBinding('change','RESULT',('facts',r.owner_module)))) for r in root_requirements)
        requirement=AuditRequirement('configuration','configuration_initialized','CONFIGURATION_INITIALIZED',1,('INITIALIZE',),CHANGE)
        definition=ResultBoundCommandDefinition('configuration','initialize_semantic',1,
            RecordSchema((Field('catalog',BoundedTextSchema(8192)),Field('domains',SequenceSchema(RecordSchema((Field('domain_id',ID),Field('digest',ID),Field('entries',SequenceSchema(RecordSchema((Field('parameter_key',BoundedTextSchema(128)),Field('body',BoundedTextSchema(8192)))),1,128)))),1,6)))),
            1,RecordSchema((Field('snapshot_id',ID),Field('domains',DOMAINS),Field('targets',TARGETS),Field('change',CHANGE),Field('facts',root_facts))),
            self.repositories,(requirement,)+root_requirements,self._handle,RecordSchema((Field('actor',ID),)),
            (AuditResultBinding(requirement.event_slot,1,(
                AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),
                AuditFieldBinding('reason_code','CONSTANT',constant='INITIALIZE'),AuditFieldBinding('target_refs','RESULT',('targets',)),
                AuditFieldBinding('change','RESULT',('change',)),)),)+root_bindings)
        from companion_memory.persistence.command_capacity import _issue_semantic_configuration_capacity
        definition=replace(definition,capacity_policy=_issue_semantic_configuration_capacity(self))
        self.commands=(definition,)

    def _handle(self,uow:UnitOfWork,values:MappingProxyType[str,Value]) -> object:
        if self._binding is None:
            raise ValueError('Configuration publisher is not bound.')
        return self._binding.handle_initialization(uow,values)

    def bind(self,storage:PersistenceService,instance_id:str,bootstrap:SemanticConfigurationCandidate) -> SemanticConfigurationBinding:
        if self._binding is not None:
            raise ValueError('Configuration assembly is already bound.')
        if semantic_snapshot_issue(bootstrap) is not None:
            raise ValueError('A complete native text configuration is required.')
        binding=SemanticConfigurationBinding(self,storage,instance_id,bootstrap)
        self._binding=binding
        return binding


type ConfigurationWriteResult = ConfigurationCommitted | ConfigurationNotCommitted | ConfigurationRejected | ConfigurationUnconfirmed | SemanticConfigurationErr
type ConfigurationReadResult = SemanticConfigurationOk[StoredSemanticConfiguration] | SemanticConfigurationErr


class SemanticConfigurationBinding:
    """Restricted initialization and point loads through configuration-owned ports."""
    def __init__(self,assembly:SemanticConfigurationAssembly,storage:PersistenceService,instance_id:str,bootstrap:SemanticConfigurationCandidate):
        self._bootstrap=bootstrap
        self._instance_id=instance_id
        self._tasks:set[asyncio.Task[object]]=set()
        self._closed=False
        self._released=False
        self._assembly=assembly
        self._storage=storage
        lease=storage.claim_module_owner(assembly.repository.definition)
        if lease is None:
            raise ValueError('Configuration owner is unavailable.')
        self._lease=lease
        self._ports={name:storage.bind_statement(assembly.repository.definition,definition,instance_id) for name,definition in assembly.repository.statements}
        self._operation=storage.bind_operation(assembly.commands[0],instance_id)
        try:
            assembly.memory.bind(storage,instance_id);assembly.retrieval.bind(storage,instance_id)
        except Exception:
            assembly.memory.close();assembly.retrieval.close();lease.release()
            raise

    def handle_initialization(self,uow:UnitOfWork,values:MappingProxyType[str,Value]) -> object:
        from companion_memory.persistence.schema import freeze_value
        if values != freeze_value(self._assembly.commands[0].input_schema,_candidate_values(self._bootstrap)):
            raise ValueError('Configuration initialization differs from its bound complete candidate.')
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
        config=isolate(CONFIG,{'database_id':self._lease.database_id,'instance_id':self._instance_id,'snapshot_id':snapshot_id})
        space=string(self._bootstrap.text.record('retrieval.semantic')['space_id'])
        memory=self._assembly.memory.initialize(uow,config,space)
        retrieval=self._assembly.retrieval.initialize(uow,config,space)
        facts: dict[str,dict[str,Value]]={owner:{'rows_changed':1,'state':'INITIALIZED','references':(), 'counts':()} for owner in ('memory','retrieval')}
        facts['memory'].update({'semantic_root':memory['row_id'],'semantic_from_seq':0,'semantic_to_seq':0,'semantic_gap_delta':0})
        return {'snapshot_id':snapshot_id,'domains':revisions,
                'targets':[{'object_id':root,'previous_revision':None,'revision':1} for root in (snapshot_id,memory['row_id'],retrieval['row_id'])],
                'change':{'snapshot_id':snapshot_id,'domains':revisions},'facts':facts}

    def _finished(self,task:asyncio.Task[object]) -> None:
        if not task.cancelled():task.exception()
        self._tasks.discard(task)

    def participate_snapshot(self, uow: UnitOfWork, expected: StoredSemanticConfiguration) -> bool:
        """Verify the active durable snapshot inside another owner's transaction."""
        if self._closed or type(expected) is not StoredSemanticConfiguration or expected._issuer is not _STORED_ISSUER:
            return False
        result = self._ports['active'].participate(uow, {})
        return type(result) is Staged and type(result.value) is tuple and len(result.value) == 1 and type(result.value[0]) is MappingProxyType and result.value[0]['snapshot_id'] == expected.snapshot_id

    async def persist_semantic_configuration(self,operation_key:object,candidate:object,*,actor:str,protected_directories:object) -> ConfigurationWriteResult:
        """Bound the complete publication/load while retaining an unfinished owner."""
        operation='persist_semantic_configuration'
        if self._closed or type(candidate) is not SemanticConfigurationCandidate or semantic_snapshot_issue(candidate) is not None:return configuration_failure('ACCESS_DENIED','identity','BINDING_MISMATCH',operation)
        from .semantic_codec import candidate_inputs
        preflight = resolve_semantic_configuration(*candidate_inputs(candidate, protected_directories))
        if type(preflight) is SemanticConfigurationErr:
            return SemanticConfigurationErr(replace(preflight.error, operation=operation))
        try:command=ResultBoundCommand(1,_candidate_values(candidate),{slot:{'actor':actor} for slot in ('configuration_initialized','memory_initialized','retrieval_initialized')})
        except ValueError:return configuration_failure('INVALID_INPUT','value','LIMIT_EXCEEDED',operation)
        handle=self._operation.recovery_handle(operation_key,command)
        if type(handle) is not RecoveryHandle:return self._commit_result(handle)
        if self._tasks:return ConfigurationUnconfirmed(handle,configuration_failure('PERSISTENCE_FAILED','storage','UNCONFIRMED',operation).error,True)
        task,logical=start_owned(self._publish(operation_key,command,handle,candidate,protected_directories))
        self._tasks.add(cast(asyncio.Task[object],task));task.add_done_callback(self._finished)
        done,_=await asyncio.wait((logical,),timeout=candidate.runtime.integer('runtime.operation_timeout_ms')/1000)
        return logical.result() if done else ConfigurationUnconfirmed(handle,configuration_failure('PERSISTENCE_FAILED','storage','UNCONFIRMED',operation).error,True)

    async def _publish(self, operation_key: object, command: ResultBoundCommand, handle: RecoveryHandle,
                       candidate: SemanticConfigurationCandidate, protected_directories: object) -> ConfigurationWriteResult:
        with CompletionScope() as completion:
            confirmed = await self._operation.resolve_operation(handle)
            if type(confirmed) is NotCommitted:
                confirmed = await self._operation.execute(operation_key, command)
            if type(confirmed) is not Committed:
                return self._commit_result(confirmed)
            result = confirmed.receipt.result
            if type(result) is not MappingProxyType:
                failure = configuration_failure('INTEGRITY_FAILURE', 'storage', 'CONTENT_MISMATCH', 'persist_semantic_configuration')
                return ConfigurationCommitted(confirmed.receipt, confirmed.source, None, failure.error, completion.pending)
            loaded = await self._load(result['snapshot_id'], candidate.material_contracts, protected_directories, bootstrap=candidate)
            if isinstance(loaded, SemanticConfigurationOk):
                return ConfigurationCommitted(confirmed.receipt, confirmed.source, loaded.value, cleanup_pending=completion.pending)
            return ConfigurationCommitted(confirmed.receipt, confirmed.source, None, loaded.error, completion.pending)

    def _commit_result(self,result:object) -> ConfigurationWriteResult:
        operation='persist_semantic_configuration'
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
        """Observe the current pointer while retaining this read's actual owner."""
        if self._closed:
            return configuration_failure('ACCESS_DENIED', 'identity', 'BINDING_MISMATCH', 'load_semantic_configuration')
        task, logical = start_owned(self._read_active_snapshot_id())
        self._tasks.add(cast(asyncio.Task[object], task))
        task.add_done_callback(self._finished)
        return await asyncio.shield(logical)

    async def _read_active_snapshot_id(self) -> object:
        result=await self._ports['active'].read_object({})
        if type(result) is Found and type(result.value) is tuple:
            if not result.value:return NotFound()
            row=result.value[0]
            if type(row) is not MappingProxyType:
                return configuration_failure('INTEGRITY_FAILURE', 'storage', 'CONTENT_MISMATCH', 'load_semantic_configuration')
            return Found(row['snapshot_id'])
        return result

    async def load_semantic_configuration(self,snapshot_id:object,material_contracts:tuple[MaterialContract,...],protected_directories:object,*,bootstrap:SemanticConfigurationCandidate) -> ConfigurationReadResult:
        """Bounded whole-domain reconstruction; timeout cannot release a live reader."""
        if self._closed or semantic_snapshot_issue(bootstrap) is not None:return configuration_failure('ACCESS_DENIED','identity','BINDING_MISMATCH','load_semantic_configuration')
        if self._tasks:return configuration_failure('PERSISTENCE_FAILED','storage','READ_FAILED','load_semantic_configuration')
        task,logical=start_owned(self._load(snapshot_id,material_contracts,protected_directories,bootstrap=bootstrap))
        self._tasks.add(cast(asyncio.Task[object],task));task.add_done_callback(self._finished)
        done,_=await asyncio.wait((logical,),timeout=bootstrap.runtime.integer('runtime.operation_timeout_ms')/1000)
        return logical.result() if done else configuration_failure('PERSISTENCE_FAILED','storage','READ_FAILED','load_semantic_configuration')

    async def _load(self,snapshot_id:object,material_contracts:tuple[MaterialContract,...],protected_directories:object,*,bootstrap:SemanticConfigurationCandidate) -> SemanticConfigurationOk[StoredSemanticConfiguration] | SemanticConfigurationErr:
        """Reconstruct complete definitions and values, refusing missing or altered rows."""
        operation='load_semantic_configuration'
        deadline = time.monotonic() + bootstrap.runtime.integer('runtime.operation_timeout_ms') / 1000
        async def read(name: str, args: dict[str, object]) -> tuple[MappingProxyType[str, Value], ...]:
            if time.monotonic() >= deadline:
                raise OSError('Configuration observation deadline has ended.')
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
            if set(catalog)!= {'version','domains','materials'} or type(catalog['version']) is not int or catalog['version']!=5:
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
            resolved=resolve_semantic_configuration(domains['foundation'],domains['runtime'],platforms,domains['content'],domains['information'],domains['semantic_retrieval'],protected_directories,material_contracts)
            if type(resolved) is not SemanticConfigurationOk:
                raise ValueError('Configuration values are no longer supported.')
            # Compare full re-encoded definitions/value provenance, including defaults.
            if _candidate_values(resolved.value)!=original:
                raise ValueError('Stored configuration or bootstrap differs.')
            rebuilt=_candidate_values(resolved.value)
            for d in cast(list[dict[str,object]],rebuilt['domains']):
                entries=cast(list[dict[str,str]],d['entries'])
                if tuple((e['parameter_key'],e['body']) for e in entries)!=encoded_by_domain[d['domain_id']]:
                    raise ValueError('Stored value provenance differs.')
            config=isolate(CONFIG,{'database_id':self._lease.database_id,'instance_id':self._instance_id,'snapshot_id':snapshot_id})
            space=string(bootstrap.text.record('retrieval.semantic')['space_id'])
            if not await self._assembly.memory.verify(config,space) or not await self._assembly.retrieval.verify(config,space):
                raise _IncompleteVersion()
            stored=object.__new__(StoredSemanticConfiguration)
            object.__setattr__(stored,"_issuer",_STORED_ISSUER)
            # Full stored metadata, values and provenance were independently
            # resolved and compared above. Reuse that exact immutable bootstrap
            # identity so borrowed audit bindings still match the opened store.
            for key,value in dict(snapshot_id=snapshot_id,database_id=self._lease.database_id,revisions=tuple(revisions),candidate=bootstrap).items():
                object.__setattr__(stored,key,value)
            return SemanticConfigurationOk(stored)
        except OSError:
            return configuration_failure('PERSISTENCE_FAILED','storage','READ_FAILED',operation)
        except _IncompleteVersion:
            return configuration_failure('INTEGRITY_FAILURE','storage','VERSION_MISSING',operation)
        except _UnsupportedFormat:
            return configuration_failure('INTEGRITY_FAILURE','storage','FORMAT_UNSUPPORTED',operation)
        except (ValueError,KeyError,TypeError,AttributeError,RecursionError,OverflowError):
            return configuration_failure('INTEGRITY_FAILURE','storage','CONTENT_MISMATCH',operation)

    def release_bootstrap_writers(self) -> bool:
        """Transfer root ownership only after the actual bootstrap task ends."""
        if self._tasks:return False
        results=(self._assembly.memory.close(),self._assembly.retrieval.close())
        return all(results)

    def close(self) -> bool:
        """Release only after outstanding storage jobs have finished."""
        self._closed=True
        # Each retained task joins only its own storage completion notifications.
        if self._released:return True
        if self._tasks:return False
        released=(self._assembly.memory.close(),self._assembly.retrieval.close(),self._lease.release())
        self._released=all(released)
        return self._released



def stored_configuration_issue(value:object) -> bool:
    """Pure native view validation; a foreign database still fails at resource binding."""
    if type(value) is not StoredSemanticConfiguration:return True
    try:return object.__getattribute__(value,'_issuer') is not _STORED_ISSUER or semantic_snapshot_issue(value.candidate) is not None
    except AttributeError:return True
