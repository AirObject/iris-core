"""Complete closed declarations for native semantic transaction participants.

The command policy owns payload schemas and participates in the static identity.
It grants no larger input limit and no authority to bypass an owner. Handlers
are bound by trusted assembly before operations can execute.
"""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass,replace
from types import MappingProxyType
from typing import cast
from .definitions import (CommandSpec,ResultBoundCommandDefinition,RepositoryDefinition,AuditFieldBinding,AuditResultBinding)
from .schema import Field,RecordSchema,SequenceSchema,ScalarSchema,BoundedTextSchema,Value,InvalidValue
from .semantic_records import Record,ID,N,P,H,record,integer,enum,isolate
from .content_codec import decode_content,encode_content
from .service import UnitOfWork
from companion_memory.logging_service import AuditRequirement
from companion_memory.information.records import COMMAND_INPUT,TARGETS

_ISSUER=object()
Handler=Callable[[str,UnitOfWork,Record,Record],object]


@dataclass(frozen=True,slots=True,init=False)
class SemanticInputPolicy:
    """Exact native command identity, independent of a supplied kind string."""
    owner: SemanticCommands
    variants: tuple[RecordSchema,...]
    _issuer: object
    def __init__(self):raise TypeError('Semantic input policies are assembly-issued.')


class SemanticCommands:
    """Construct all declared schemas and mandatory writers before storage opens."""
    def __init__(self,repositories: tuple[RepositoryDefinition,...],handler: Handler, *, usage_only:bool=False):
        from companion_memory.retrieval import semantic_commands as retrieval
        from companion_memory.provider import embedding_commands as provider
        from companion_memory.cognition import fixed_memory_commands as cognition
        from companion_memory.runtime.content_assembly import owner_fact
        from companion_memory.provider.ledger import CHANGE
        declared_repositories={repository.owner_module:repository for repository in repositories}
        commands=[]
        for namespace,module in (('retrieval',retrieval),('provider',provider),('cognition',cognition)):
            for kind,variants in module.PAYLOADS.items():
                owners=module.WRITERS[kind];readers=module.READERS[kind]
                facts={owner:owner_fact(owner) for owner in owners}
                if 'memory' in owners:
                    facts['memory']=RecordSchema(facts['memory'].fields+(Field('semantic_root',ID),Field('semantic_from_seq',N),
                        Field('semantic_to_seq',N),Field('semantic_gap_delta',integer(-8,8))))
                if namespace=='provider':
                    facts['provider']=RecordSchema(CHANGE.fields+(Field('billing_mode',enum('USAGE_ONLY_TRIAL') if usage_only else enum('TOKEN_METERED','SIMULATED')),
                        Field('currency',enum('CNY','TEST')),Field('quota_known',N,nullable=True),Field('quota_held',N),Field('config_snapshot_id',ID)))
                result=record(outcome=enum('APPLIED'),targets=TARGETS,items=SequenceSchema(ID,0,16),
                    facts=RecordSchema(tuple(Field(owner,facts[owner]) for owner in owners)))
                requirements=tuple(AuditRequirement(owner,'provider_change' if owner=='provider' else 'object_history' if owner=='logging_service'
                    else owner+'_semantic',kind.upper(),1,('APPLY',),facts[owner]) for owner in owners)
                bindings=tuple(AuditResultBinding(requirement.event_slot,1,(
                    AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),
                    AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),AuditFieldBinding('target_refs','RESULT',('targets',)),
                    AuditFieldBinding('change','RESULT',('facts',requirement.owner_module)))) for requirement in requirements)
                policy=object.__new__(SemanticInputPolicy)
                object.__setattr__(policy,'owner',self);object.__setattr__(policy,'variants',variants);object.__setattr__(policy,'_issuer',_ISSUER)
                def execute(uow: UnitOfWork,values: Record,*,action=kind,schemas=variants):
                    payload=decode_payload(schemas,values['payload'])
                    return handler(action,uow,values,payload)
                command=ResultBoundCommandDefinition(namespace,kind,4 if usage_only and namespace=='provider' else 1,COMMAND_INPUT,1,result,
                    tuple(declared_repositories[owner] for owner in dict.fromkeys((*owners,*readers))),requirements,execute,
                    record(actor=ID),bindings,input_policy=policy)
                commands.append(command)
        self.commands=tuple(commands)
        if len(self.commands)!=26:raise InvalidValue()


def declared(definition: CommandSpec) -> bool:
    policy=definition.input_policy
    if type(policy) is not SemanticInputPolicy:return False
    if (getattr(policy,'_issuer',None) is not _ISSUER or type(getattr(policy,'owner',None)) is not SemanticCommands
            or not any(command is definition for command in policy.owner.commands)):
        raise InvalidValue()
    return True


def decode_payload(variants: tuple[RecordSchema,...],body: Value) -> Record:
    if type(body) is not str:raise InvalidValue()
    raw=decode_content(body.encode('utf-8'),24576)
    for schema in variants:
        try:value=isolate(schema,raw,24576)
        except InvalidValue:continue
        if encode_content(value,24576).decode('utf-8')!=body:raise InvalidValue()
        return value
    raise InvalidValue()


def validate_values(definition: CommandSpec,values: Record) -> None:
    if not declared(definition):return
    policy=cast(SemanticInputPolicy,definition.input_policy)
    payload=decode_payload(policy.variants,values['payload'])
    if definition.operation_kind=='prepare' and payload['kind']=='EMBED':
        document=payload['purpose']=='DOCUMENT'
        if document:
            if payload['object_ref'] is None or payload['change_seq'] is None or payload['change_seq']==0:raise InvalidValue()
        elif payload['object_ref'] is not None or payload['change_seq'] is not None:raise InvalidValue()
        text=payload['rendered_text']
        if type(text) is not str or not 1<=len(text.encode('utf-8'))<=(8192 if document else 512):raise InvalidValue()
    if definition.operation_kind in ('fail','generation_fail') and payload['error']=='NONE':raise InvalidValue()


def descriptor(definition: CommandSpec,ordinary: Record) -> Record:
    """Encode every schema node once, preserving complete types and constraints.

    Integer references address a topologically ordered schema pool. This removes
    repeated result/audit structures, without replacing schemas with only hashes.
    The encoding version and original ordered fields are part of the signature.
    """
    if not declared(definition):raise InvalidValue()
    from companion_memory.logging_service.audit_materialization import binding_value
    policy=cast(SemanticInputPolicy,definition.input_policy)
    nodes: list[Value]=[];indexes: dict[bytes,int]={}
    def node(schema: ScalarSchema | BoundedTextSchema | RecordSchema | SequenceSchema) -> int:
        if type(schema) is ScalarSchema:value=('scalar',schema.kind,schema.minimum,schema.maximum,schema.choices)
        elif type(schema) is BoundedTextSchema:value=('text',schema.max_utf8_bytes)
        elif type(schema) is SequenceSchema:value=('sequence',node(schema.item),schema.minimum,schema.maximum)
        else:
            assert type(schema) is RecordSchema
            value=('record',tuple((field.name,node(field.schema),field.nullable,field.optional) for field in schema.fields))
        key=encode_content(value,32768)
        if key not in indexes:indexes[key]=len(nodes);nodes.append(value)
        return indexes[key]
    data=dict(ordinary)
    data['input']=node(definition.input_schema);data['result']=node(definition.result_schema)
    data['audit_schemas']=tuple(MappingProxyType({'slot':audit.event_slot,'change':node(audit.change_schema),
        'reasons':audit.reason_codes,'targets':audit.target_limit}) for audit in sorted(definition.required_audits,key=lambda audit:audit.event_slot))
    assert type(definition) is ResultBoundCommandDefinition
    data['intent_schema']=node(definition.audit_intent_schema);data['audit_bindings']=binding_value(definition)
    data['payload_variants']=tuple(node(schema) for schema in policy.variants)
    data['schema_nodes']=tuple(nodes);data['semantic_descriptor_version']=1
    result=MappingProxyType(data);encode_content(result,8192)
    return result
