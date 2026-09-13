"""Complete frozen learning context and deterministic bounded reconstruction.

Every original event, related object and reviewed persona is retained verbatim.
The context digest precedes the normalized request digest; neither is filled in
from an unverified stored hash. Leaves split at UTF-8 boundaries and retain all
text. Capacity failure rejects the entire material, never shortens its content.
"""
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import BoundedTextSchema, Field, RecordSchema, ScalarSchema, SequenceSchema, Value, InvalidValue, ValueTooLarge, freeze_value
from companion_memory.persistence.text_records import BASE, ID, UINT, REVISION, DIGEST, OPERATION, RecordTable, IndexSpec, record_catalog, isolate_record, stable_identity, digest
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.ingress.events import plain, canonical_event
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.memory.formats import record, sequence, isolate_object
from companion_memory.self_model.formats import PROJECTION
from companion_memory.provider.chat_protocol import ChatBinding, encode_request
from companion_memory.provider.values import freeze, as_record

MEMBER = RecordSchema((Field('role', ScalarSchema('enum', choices=('HISTORY','TARGET','RECENT'))), Field('message_id', ID), Field('payload_digest', DIGEST)))
BASIS = RecordSchema((Field('object_id', ID), Field('revision', REVISION), Field('grant_ref', ID), Field('snapshot_digest', DIGEST)))
LEAF_REF = RecordSchema((Field('object_id', ID), Field('ordinal', ScalarSchema('integer',0,7)), Field('digest', DIGEST), Field('byte_count', ScalarSchema('integer',0,7168))))
BINDING = RecordSchema(tuple(Field(name, ID) for name in ('profile_id','config_snapshot_id','profile_revision','price_revision')) + (
    Field('protocol', ScalarSchema('enum', choices=('OPENAI_CHAT_COMPLETIONS',))), Field('model_id',ID),
    Field('capability_evidence_ref',ID), Field('billing_evidence_ref',ID), Field('request_digest',DIGEST)))
IDENTITY = RecordSchema(tuple(Field(name,ID) for name in ('database_id','instance_id','batch_id','run_id','source_id','config_snapshot_id')))
RESOURCES = RecordSchema(tuple(Field(kind+suffix,ID if suffix=='_ref' else DIGEST) for kind in ('prompt','schema','transform') for suffix in ('_ref','_digest')))
MANIFEST = RecordSchema(BASE + (
    Field('context_version',ScalarSchema('integer',1,1)),Field('batch_id',ID),Field('run_id',ID),Field('source_id',ID),
    Field('state',ScalarSchema('enum',choices=('STORED','RELEASED'))),Field('persona_publication_id',ID),Field('persona_revision',REVISION),
    Field('prompt_ref',ID),Field('schema_ref',ID),Field('transform_ref',ID),Field('model_binding_digest',DIGEST),
    Field('ordered_members',SequenceSchema(MEMBER,1,4)),Field('related_objects',SequenceSchema(BASIS,0,2)),
    Field('leaf_refs',SequenceSchema(LEAF_REF,1,8)),Field('payload_digest',DIGEST),Field('wire_digest',DIGEST),
    Field('byte_count',ScalarSchema('integer',0,65536)),Field('input_token_estimate',UINT,nullable=True),Field('reservation_input_bound',UINT),
    Field('original_operation',OPERATION),Field('terminal_operation',OPERATION,nullable=True)))
LEAF = RecordSchema(BASE + (Field('context_id',ID),Field('ordinal',ScalarSchema('integer',0,7)),
    Field('text',BoundedTextSchema(7168)),Field('text_bytes',ScalarSchema('integer',0,7168)),Field('digest',DIGEST)))
REQUEST_IDENTITY = RecordSchema(tuple(Field(name,ID) for name in ('operation_key','run_id','profile_id')) + (
    Field('entry_ids',SequenceSchema(ID,1,1)),) + tuple(Field(name,ID,nullable=True,optional=True)
        for name in ('parent_request_id','trace_id','batch_id','dream_run_id','prompt_revision')))


def context_catalog():
    """Declare exactly the two context tables and four fixed indexes."""
    return record_catalog('cognition',3,(
        RecordTable('learning_contexts',8192,True,(IndexSpec('by_batch',('batch_id',)),IndexSpec('by_run',('run_id',),point_read=False))),
        RecordTable('learning_context_leaves',8192,False,(
            IndexSpec('by_context_ordinal',('context_id','ordinal'),point_read=False),
            IndexSpec('by_context',('context_id',),unique=False,identity_only=True)),delete_by='context_id')))


def normalized_request_digest(request: object) -> str:
    """Hash the complete original generation descriptor without delivery controls."""
    from companion_memory.provider.service import OPTIONALS
    value=as_record(freeze(request,131072,owned=True))
    body={name:value.get(name) for name in OPTIONALS}
    body.update({name:value[name] for name in ('operation_key','run_id','profile_id','payload','entry_ids')})
    return digest(cast(Value,freeze(body,131072,owned=True)))


def isolate_context(source: object, *, complete: bool = True) -> MappingProxyType[str,Value]:
    """Validate complete material; only the digest-construction call omits one hash."""
    value=cast(MappingProxyType[str,Value],freeze(source,57344,owned=True))
    if set(value)!={'context_version','system_text','user','resources','model_binding'} or type(value['context_version']) is not int or value['context_version']!=1:raise InvalidValue()
    freeze_value(BoundedTextSchema(4096),value['system_text'])
    if type(value['system_text']) is not str or not value['system_text'].strip():raise InvalidValue()
    resources=isolate_record(RESOURCES,value['resources'],2048)
    binding_schema=BINDING if complete else RecordSchema(BINDING.fields[:-1])
    binding=isolate_record(binding_schema,value['model_binding'],2048)
    user=record(value['user'])
    if set(user)!={'members','persona','related','identity'}:raise InvalidValue()
    identity=isolate_record(IDENTITY,user['identity'],1024)
    if binding['config_snapshot_id']!=identity['config_snapshot_id']:raise InvalidValue()
    isolate_record(PROJECTION,user['persona'],2048)
    members=sequence(user['members']); roles=[]; seen=set()
    if not 1<=len(members)<=4:raise InvalidValue()
    for raw in members:
        item=record(raw)
        if set(item)!={'member','event'}:raise InvalidValue()
        member=isolate_record(MEMBER,item['member'],512)
        event=isolate_media_event(plain(item['event']),2048,occurrence_limit=2,text_limit=512)
        if event['media'] or member['payload_digest']!=hashlib.sha256(canonical_event(event)).hexdigest() or member['message_id'] in seen:raise InvalidValue()
        seen.add(member['message_id']);roles.append(member['role'])
    if not 1<=roles.count('TARGET')<=2 or roles.count('HISTORY')>1 or roles.count('RECENT')>1 or roles!=sorted(roles,key=('HISTORY','TARGET','RECENT').index):raise InvalidValue()
    related=sequence(user['related']);seen.clear()
    if len(related)>2:raise InvalidValue()
    for raw in related:
        obj=isolate_object(raw,4096,text_format=True)
        if obj['kind']!='MEMORY' or obj['lifecycle']!='ACTIVE' or obj['instance_id']!=identity['instance_id'] or obj['object_id'] in seen:raise InvalidValue()
        seen.add(obj['object_id'])
    user_bytes=encode_content(user,32768)
    if len(cast(str,value['system_text']).encode())+len(user_bytes)>49152:raise ValueTooLarge()
    encode_content(value,57344)
    return value


def material_digest(context: MappingProxyType[str,Value]) -> str:
    """Hash material before the final request digest exists."""
    binding=record(context['model_binding'])
    value={**context,'model_binding':{key:child for key,child in binding.items() if key!='request_digest'}}
    return digest(isolate_context(value,complete=False))


def generation_payload(context: MappingProxyType[str,Value], reservation_input_bound: int) -> MappingProxyType[str,Value]:
    """Build the exact semantic payload using the full frozen user record."""
    resources=record(context['resources'])
    return cast(MappingProxyType[str,Value],freeze({'format_version':2,'messages':(
        {'role':'SYSTEM','text':context['system_text']},{'role':'USER','text':encode_content(context['user'],32768).decode()}),
        'schema_ref':resources['schema_ref'],'schema_digest':resources['schema_digest'],'output_tokens':2048,
        'reservation_input_bound':reservation_input_bound,'context_digest':material_digest(context)},131072,owned=True))


@dataclass(frozen=True,slots=True)
class FrozenContext:
    """Owned complete records; this pure value grants no persistence or sending."""
    manifest: MappingProxyType[str,Value]
    leaves: tuple[MappingProxyType[str,Value],...]
    context: MappingProxyType[str,Value]
    request: MappingProxyType[str,Value]
    wire: bytes


def learning_request_key(batch_id: str,generation: int) -> str:
    """Keep the original deterministic runtime key and its admission generations."""
    freeze_value(ID,batch_id);freeze_value(REVISION,generation)
    parts=(batch_id,) if generation==1 else (batch_id,generation)
    return 'learning_request:'+hashlib.sha256(encode_content(parts,8192)).hexdigest()


def admission_request(context: FrozenContext,generation: int) -> MappingProxyType[str,Value]:
    """Derive one admission from frozen material without changing any saved leaf.

    The frozen model binding always describes the first request. A later
    admission differs only in its deterministically derived operation key; the
    runtime work stores that request's full normalized digest separately.
    """
    freeze_value(REVISION,generation)
    if generation==1:return context.request
    batch=cast(str,context.manifest['batch_id'])
    if context.request['operation_key']!=learning_request_key(batch,1):raise InvalidValue()
    return cast(MappingProxyType[str,Value],freeze({**context.request,'operation_key':learning_request_key(batch,generation)},131072,owned=True))


def freeze_context(source: object, request_identity: object, related_basis: object, operation: object,
                   created_at_us: int, reservation_input_bound: int, binding: ChatBinding) -> FrozenContext:
    """Freeze all material and original request identity before any Provider work.

    Caller supplies an incomplete binding with no request_digest. It must obtain
    and verify the original owner reads and resources before this pure operation.
    """
    context=isolate_context(source,complete=False)
    original_identity=isolate_record(REQUEST_IDENTITY,request_identity,8192)
    request=as_record(freeze({**original_identity,
        'payload':generation_payload(context,reservation_input_bound)},131072,owned=True))
    model_binding={**record(context['model_binding']),'request_digest':normalized_request_digest(request)}
    context=isolate_context({**context,'model_binding':model_binding})
    identity=record(record(context['user'])['identity'])
    if request['run_id']!=identity['run_id'] or request['profile_id']!=model_binding['profile_id']:raise InvalidValue()
    if model_binding['model_id']!=binding.requested_model:raise InvalidValue()
    wire=encode_request(request['payload'],binding)
    raw=encode_content(context,57344)
    context_id=stable_identity('learning-context',cast(str,identity['database_id']),cast(str,identity['instance_id']),identity['batch_id'])
    base={'format_version':1,'revision':1,'database_id':identity['database_id'],'instance_id':identity['instance_id'],
        'config_snapshot_id':identity['config_snapshot_id'],'created_at_us':created_at_us}
    leaves=[];remaining=raw.decode()
    while remaining:
        ordinal=len(leaves)
        if ordinal>=8:raise ValueTooLarge()
        header={**base,'object_id':stable_identity('context-leaf',cast(str,identity['database_id']),cast(str,identity['instance_id']),context_id,ordinal),
            'context_id':context_id,'ordinal':ordinal}
        # Canonical JSON text can expand again inside a leaf body. Bound the
        # complete encoded row, not only the raw text chunk, without losing text.
        def leaf(count: int):
            text=remaining[:count];encoded=text.encode()
            return isolate_record(LEAF,{**header,'text':text,'text_bytes':len(encoded),'digest':hashlib.sha256(encoded).hexdigest()},8192)
        lower=0;upper=min(len(remaining),7168)
        while lower<upper:
            middle=(lower+upper+1)//2
            try:leaf(middle)
            except (InvalidValue,ValueTooLarge):upper=middle-1
            else:lower=middle
        if lower==0:raise ValueTooLarge()
        leaves.append(leaf(lower));remaining=remaining[lower:]
    basis=cast(tuple[Value,...],freeze_value(SequenceSchema(BASIS,0,2),related_basis,owned=True))
    related=sequence(record(context['user'])['related'])
    if len(basis)!=len(related):raise InvalidValue()
    for reference,obj in zip(basis,related,strict=True):
        ref=record(reference);target=record(obj)
        if (ref['object_id'],ref['revision'],ref['snapshot_digest'])!=(target['object_id'],target['revision'],digest(target)):raise InvalidValue()
    persona=record(record(context['user'])['persona']);resources=record(context['resources'])
    manifest=isolate_record(MANIFEST,{**base,'object_id':context_id,'context_version':1,
        **{name:identity[name] for name in ('batch_id','run_id','source_id')},'state':'STORED',
        'persona_publication_id':persona['publication_id'],'persona_revision':persona['revision'],
        **{name:resources[name] for name in ('prompt_ref','schema_ref','transform_ref')},'model_binding_digest':digest(record(context['model_binding'])),
        'ordered_members':tuple(record(v)['member'] for v in sequence(record(context['user'])['members'])),'related_objects':basis,
        'leaf_refs':tuple({'object_id':v['object_id'],'ordinal':v['ordinal'],'digest':v['digest'],'byte_count':v['text_bytes']} for v in leaves),
        'payload_digest':hashlib.sha256(raw).hexdigest(),'wire_digest':hashlib.sha256(wire).hexdigest(),'byte_count':len(raw),
        'input_token_estimate':None,'reservation_input_bound':reservation_input_bound,'original_operation':operation,'terminal_operation':None},8192)
    if sum(len(encode_content(v,8192)) for v in (manifest,*leaves))>73728:raise ValueTooLarge()
    return FrozenContext(manifest,tuple(leaves),context,cast(MappingProxyType[str,Value],request),wire)


def decode_retained_context(manifest: object, leaves: tuple[object,...]):
    """Verify complete stored material before use or irreversible leaf release.

    Original-request and wire bindings are additionally checked by restoration.
    This common check derives hashes from actual text, not stored hash labels.
    """
    value=isolate_record(MANIFEST,manifest,8192)
    if value['state']!='STORED' or value['revision']!=1 or value['terminal_operation'] is not None:raise InvalidValue()
    database=cast(str,value['database_id']);instance=cast(str,value['instance_id'])
    if value['object_id']!=stable_identity('learning-context',database,instance,value['batch_id']):raise InvalidValue()
    refs=sequence(value['leaf_refs'])
    if type(leaves) is not tuple or len(leaves)!=len(refs):raise InvalidValue()
    owned=tuple(isolate_record(LEAF,leaf,8192) for leaf in leaves)
    for ordinal,(leaf,raw_ref) in enumerate(zip(owned,refs,strict=True)):
        ref=record(raw_ref);text=cast(str,leaf['text']).encode()
        if (any(leaf[name]!=value[name] for name in ('database_id','instance_id','config_snapshot_id','created_at_us','revision'))
                or leaf['object_id']!=stable_identity('context-leaf',database,instance,value['object_id'],ordinal)
                or leaf['ordinal']!=ordinal or ref['ordinal']!=ordinal or leaf['context_id']!=value['object_id']
                or leaf['object_id']!=ref['object_id'] or leaf['text_bytes']!=len(text) or ref['byte_count']!=len(text)
                or leaf['digest']!=hashlib.sha256(text).hexdigest() or ref['digest']!=leaf['digest']):raise InvalidValue()
    if sum(len(encode_content(item,8192)) for item in (value,*owned))>73728:raise ValueTooLarge()
    raw=''.join(cast(str,leaf['text']) for leaf in owned).encode()
    if len(raw)!=value['byte_count'] or hashlib.sha256(raw).hexdigest()!=value['payload_digest']:raise InvalidValue()
    context=isolate_context(decode_content(raw,57344))
    user=record(context['user']);resources=record(context['resources']);persona=record(user['persona'])
    if (any(record(user['identity'])[name]!=value[name] for name in ('database_id','instance_id','batch_id','run_id','source_id','config_snapshot_id'))
            or any(resources[name]!=value[name] for name in ('prompt_ref','schema_ref','transform_ref'))
            or persona['publication_id']!=value['persona_publication_id'] or persona['revision']!=value['persona_revision']
            or digest(record(context['model_binding']))!=value['model_binding_digest']
            or tuple(record(member)['member'] for member in sequence(user['members']))!=value['ordered_members']):raise InvalidValue()
    return value,owned,context


def restore_context(manifest: object, leaves: tuple[object,...], request_identity: object, binding: ChatBinding) -> FrozenContext:
    """Rebuild and compare every original record and digest without refreshing reads."""
    # Recovery may receive the full retained request. Its payload belongs to
    # the request limit, while only the semantic identity uses the smaller
    # identity limit. A supplied payload must equal the reconstructed one.
    original_request=as_record(freeze(request_identity,131072,owned=True))
    identity={name:item for name,item in original_request.items() if name!='payload'}
    value,owned,context=decode_retained_context(manifest,leaves)
    partial={**context,'model_binding':{name:item for name,item in record(context['model_binding']).items() if name!='request_digest'}}
    rebuilt=freeze_context(partial,identity,value['related_objects'],value['original_operation'],cast(int,value['created_at_us']),cast(int,value['reservation_input_bound']),binding)
    if 'payload' in original_request and rebuilt.request!=original_request:raise InvalidValue()
    if rebuilt.manifest!=value or rebuilt.leaves!=owned or rebuilt.context!=context:raise InvalidValue()
    return rebuilt
