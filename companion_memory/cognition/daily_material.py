"""Lossless complete daily material split into bounded canonical UTF-8 leaves.

No truncation or success substitution occurs when any root, leaf or aggregate
limit fails. A frozen value is data; only the reasoning owner may authorize its
persistence or issue a request-specific read lease.
"""
from dataclasses import dataclass,replace
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import Field,RecordSchema,SequenceSchema,ScalarSchema,BoundedTextSchema,Value,InvalidValue,ValueTooLarge
from companion_memory.persistence.daily_records import BASE,DailyTable,ID,UINT,REVISION,DIGEST,OPERATION,IndexSpec,daily_catalog,identity,enum
from companion_memory.persistence.text_records import isolate_record
from companion_memory.persistence.content_codec import encode_content,decode_content
from .text_context import MEMBER,BASIS

LEAF_REF=RecordSchema((Field('object_id',ID),Field('ordinal',ScalarSchema('integer',0,47)),Field('digest',DIGEST),Field('byte_count',ScalarSchema('integer',1,6144))))
MANIFEST=RecordSchema(BASE+(Field('context_version',ScalarSchema('integer',2,2)),Field('context_kind',enum('LEARNING','TOOL_RESULT','GOAL_DEDUP','PERSONA','PROVIDER_RESULT')),
    Field('owner_ref',ID),Field('batch_id',ID,nullable=True),Field('run_id',ID,nullable=True),Field('source_id',ID,nullable=True),
    Field('state',enum('STORED','RELEASED')),Field('persona_publication_id',ID,nullable=True),Field('persona_revision',REVISION,nullable=True),
    Field('prompt_ref',ID,nullable=True),Field('schema_ref',ID),Field('transform_ref',ID,nullable=True),Field('model_binding_digest',DIGEST),
    Field('ordered_members',SequenceSchema(MEMBER,0,4)),Field('related_objects',SequenceSchema(BASIS,0,4)),
    Field('leaf_refs',SequenceSchema(LEAF_REF,1,48)),Field('payload_digest',DIGEST),Field('wire_digest',DIGEST,nullable=True),
    Field('byte_count',ScalarSchema('integer',1,262144)),Field('input_token_estimate',UINT,nullable=True),Field('reservation_input_bound',UINT),
    Field('original_operation',OPERATION),Field('terminal_operation',OPERATION,nullable=True)))
LEAF=RecordSchema(BASE+(Field('context_id',ID),Field('ordinal',ScalarSchema('integer',0,47)),Field('text',BoundedTextSchema(6144)),
    Field('text_bytes',ScalarSchema('integer',1,6144)),Field('digest',DIGEST)))
ROOT_TABLE=DailyTable('learning_contexts',(MANIFEST,),16384,True,(IndexSpec('by_owner',('owner_ref',),unique=False,identity_only=True),))
LEAF_TABLE=DailyTable('learning_context_leaves',(LEAF,),8192,False,(IndexSpec('by_context_ordinal',('context_id','ordinal'),point_read=False),))
TABLES=(ROOT_TABLE,LEAF_TABLE)

def context_catalog():
    """Bind complete root and leaf bodies to the new independent static format."""
    from companion_memory.persistence import StatementDefinition,TableDefinition
    from companion_memory.persistence.owned_statements import StatementCatalog
    catalog=daily_catalog('cognition',5,TABLES)
    key=RecordSchema((Field('object_id',ID),Field('context_id',ID)))
    remove=StatementDefinition("DELETE FROM cognition_learning_context_leaves WHERE scope_id=:scope_id AND object_id=:object_id AND json_extract(body,'$.context_id')=:context_id RETURNING object_id",key,RecordSchema((Field('object_id',ID),)),True)
    shared_key=RecordSchema((Field('caller_scope',ID),Field('object_id',ID)))
    shared=[]
    for table,maximum in (('learning_contexts',16384),('learning_context_leaves',8192)):
        shared.append(('provider_read_'+table,StatementDefinition(
            'SELECT object_id,revision,body FROM cognition_'+table+" WHERE :scope_id='provider' AND scope_id=:caller_scope AND object_id=:object_id",
            shared_key,RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('body',BoundedTextSchema(maximum)))),False)))
    statements=catalog.statements+(('daily_context_leaf_delete',remove),)+tuple(shared)
    return StatementCatalog(replace(catalog.definition,statements=tuple(s for _,s in statements)),statements)

@dataclass(frozen=True,slots=True)
class FrozenDailyMaterial:
    """Complete canonical data, with no grant to mutate a business owner."""
    manifest:MappingProxyType[str,Value]
    leaves:tuple[MappingProxyType[str,Value],...]
    body:bytes


def _manifest(raw:object):
    value=ROOT_TABLE.isolate(raw)
    learning=value['context_kind']=='LEARNING'
    if learning:
        if any(value[name] is None for name in ('batch_id','run_id','source_id','persona_publication_id','persona_revision','prompt_ref','transform_ref','wire_digest')):raise InvalidValue()
        roles=tuple(item['role'] for item in cast(tuple[MappingProxyType[str,Value],...],value['ordered_members']))
        if not 1<=roles.count('TARGET')<=2 or roles.count('HISTORY')>1 or roles.count('RECENT')>1 or roles!=tuple(sorted(roles,key=('HISTORY','TARGET','RECENT').index)):raise InvalidValue()
        if value['owner_ref']!=value['run_id']:raise InvalidValue()
    elif value['ordered_members'] or value['related_objects']:raise InvalidValue()
    if value['context_kind']=='PERSONA' and (any(value[name] is not None for name in ('batch_id','source_id','persona_publication_id','persona_revision'))
            or value['run_id']!=value['owner_ref'] or any(value[name] is None for name in ('prompt_ref','transform_ref','wire_digest'))):raise InvalidValue()
    if (value['persona_publication_id'] is None)!=(value['persona_revision'] is None):raise InvalidValue()
    if (value['state']=='RELEASED')!=(value['terminal_operation'] is not None):raise InvalidValue()
    refs=cast(tuple[MappingProxyType[str,Value],...],value['leaf_refs'])
    if len({cast(str,ref['object_id']) for ref in refs})!=len(refs) or sum(cast(int,ref['byte_count']) for ref in refs)!=value['byte_count']:raise InvalidValue()
    for ordinal,ref in enumerate(refs):
        if ref['ordinal']!=ordinal or ref['object_id']!=identity('context-leaf',cast(str,value['database_id']),cast(str,value['instance_id']),value['object_id'],ordinal):raise InvalidValue()
    return value


def freeze_material(metadata:dict[str,object],body:bytes) -> FrozenDailyMaterial:
    """Compute each true encoded size and retain the exact complete original bytes."""
    if type(body) is not bytes or not 1<=len(body)<=262144:raise ValueTooLarge()
    try:body.decode('utf-8')
    except UnicodeError:raise InvalidValue() from None
    if 'leaf_refs' in metadata or 'payload_digest' in metadata or 'byte_count' in metadata:raise InvalidValue()
    cid=metadata['object_id'];database=cast(str,metadata['database_id']);instance=cast(str,metadata['instance_id'])
    base={field.name:metadata[field.name] for field in BASE if field.name not in ('object_id','revision')}
    leaves=[];offset=0
    while offset<len(body):
        if len(leaves)==48:raise ValueTooLarge()
        end=min(offset+6144,len(body))
        # JSON escaping counts in the complete 8192-byte leaf as well. Shrink
        # only a split boundary, never the retained material or a source part.
        while True:
            try:text=body[offset:end].decode('utf-8')
            except UnicodeError:end-=1;continue
            leaf={**base,'object_id':identity('context-leaf',database,instance,cast(str,cid),len(leaves)),
                'revision':1,'context_id':cid,'ordinal':len(leaves),'text':text,'text_bytes':end-offset,'digest':sha256(body[offset:end]).hexdigest()}
            try:checked=LEAF_TABLE.isolate(leaf)
            except (ValueTooLarge,InvalidValue):
                if end<=offset+1:raise ValueTooLarge()
                end=offset+max(1,(end-offset)*3//4);continue
            break
        leaves.append(checked);offset=end
    refs=tuple({'object_id':leaf['object_id'],'ordinal':leaf['ordinal'],'digest':leaf['digest'],'byte_count':leaf['text_bytes']} for leaf in leaves)
    manifest=_manifest({**metadata,'leaf_refs':refs,'payload_digest':sha256(body).hexdigest(),'byte_count':len(body)})
    return restore_material(manifest,tuple(leaves))


def restore_material(manifest:object,leaves:object) -> FrozenDailyMaterial:
    """Recompute all bytes, identities, order and aggregate hashes on every restore."""
    root=_manifest(manifest)
    if root['state']!='STORED' or type(leaves) not in (tuple,list):raise InvalidValue()
    checked=tuple(LEAF_TABLE.isolate(leaf) for leaf in cast(tuple,leaves));refs=cast(tuple[MappingProxyType[str,Value],...],root['leaf_refs'])
    if len(checked)!=len(refs):raise InvalidValue()
    parts=[]
    for ordinal,(leaf,ref) in enumerate(zip(checked,refs,strict=True)):
        raw=cast(str,leaf['text']).encode('utf-8')
        if (leaf['context_id']!=root['object_id'] or leaf['ordinal']!=ordinal or leaf['object_id']!=ref['object_id']
                or leaf['text_bytes']!=len(raw) or leaf['digest']!=sha256(raw).hexdigest() or ref['digest']!=leaf['digest'] or ref['byte_count']!=len(raw)
                or leaf['revision']!=1 or any(leaf[name]!=root[name] for name in ('database_id','instance_id','config_snapshot_id','created_at_us'))):raise InvalidValue()
        parts.append(raw)
    body=b''.join(parts)
    if len(body)!=root['byte_count'] or sha256(body).hexdigest()!=root['payload_digest']:raise InvalidValue()
    return FrozenDailyMaterial(root,checked,body)
