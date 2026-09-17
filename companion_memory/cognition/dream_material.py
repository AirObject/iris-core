"""Complete formal evidence and existing registered authority for one dream step."""
from hashlib import sha256
from typing import cast
from companion_memory.persistence.semantic_records import Record
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue,ValueTooLarge
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.values import freeze,DataLimit
from companion_memory.provider.dream_protocol import encode_dream_request,input_byte_bound
from companion_memory.memory.formats import record,sequence
from companion_memory.memory.dream_view import evidence
from .daily_material import freeze_material


def collect(owner,uow,object_id:str,revision:int,*,influence:bool=False):
    memory=owner.memory
    current=memory.current(uow,object_id)
    if current is None or current['revision']!=revision:raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
    current_links=memory.links(uow,object_id,revision)
    refs=[{'object_id':object_id,'revision':revision}]
    for raw in sequence(current_links['bases']):
        link=record(raw);basis=memory.current(uow,cast(str,link['basis_id'])) if influence else None
        if influence and (basis is None or basis['lifecycle']!='ACTIVE'):continue
        refs.append({'object_id':link['basis_id'],'revision':basis['revision'] if influence and basis is not None else link['basis_revision']})
    selected=evidence(memory,uow,cast(tuple[Record,...],freeze(tuple(refs),8192,owned=True)),self_only=False,allow_changed=influence)
    subjects=set();entries=set()
    for item in selected:
        value=record(item['object']);content=record(value['content'])
        if value['kind']=='MEMORY':subjects.update(cast(tuple[str,...],content['subject_ids']))
        else:
            subjects.update(cast(str,record(content[name])['id']) for name in ('from_ref','to_ref') if record(content[name])['type']=='SUBJECT')
        world=record(content['world_scope'])
        if world['context_id'] is not None:subjects.add(cast(str,world['context_id']))
        for raw in sequence(item['sources']):entries.add(cast(str,record(record(raw)['body'])['entry_id']))
    if len(entries)!=1:raise ValueTooLarge()
    entry_id=next(iter(entries))
    scope=owner.scopes.get(entry_id)
    if scope is None:raise OwnerFailure('ACCESS_DENIED','source','OPERATION_NOT_GRANTED')
    allowed_subjects,worlds,routes=scope
    if not subjects<=set(allowed_subjects) or any(record(record(item['object'])['content'])['world_scope'] not in worlds for item in selected):raise OwnerFailure('ACCESS_DENIED','source','OPERATION_NOT_GRANTED')
    registered=tuple(memory.subject(uow,sid) for sid in sorted(subjects))
    if any(value is None for value in registered):raise OwnerFailure('ACCESS_DENIED','subject','OPERATION_NOT_GRANTED')
    return selected,registered,entry_id,routes


def material(owner,uow,run,object_id:str,revision:int,step_id:str,operation,now:int,*,impact=None):
    evidence,subjects,entry_id,routes=collect(owner,uow,object_id,revision,influence=impact is not None)
    pointer=owner.current.rows.get('current_persona',uow,owner.current.pointer_id)
    if pointer is None:raise InvalidValue()
    current=owner.current.participate_current(uow,pointer['publication_id'],pointer['revision'])
    if current is None:raise InvalidValue()
    try:
        body=cast(Record,freeze({'origin':'DREAM','object_id':object_id,'evidence':evidence,'subjects':subjects,'entry_id':entry_id,
            'routes':routes,'current_persona':current,'created_at_us':now,**({'influence':impact} if impact is not None else {})},262144,owned=True))
    except DataLimit:raise ValueTooLarge() from None
    raw=encode_content(body,262144);binding=owner.provider.dream_bindings['DREAM_REVIEW']
    wire=encode_dream_request(binding,raw.decode());bound=input_byte_bound(binding,raw)
    profile=next(p for p in owner.provider.profiles if p['material_role']=='DREAM_REVIEW')
    if bound>profile['max_input_units']:raise ValueTooLarge()
    setting=next(s for s in cast(tuple[Record,...],owner.configuration.candidate.text.record('provider.transport')['roles']) if s['role']=='DREAM_REVIEW')
    metadata=owner.base(owner.key('dream-material',step_id),now)|{'context_version':3,'context_kind':'DREAM_REVIEW','owner_ref':run['run_id'],
        'batch_id':None,'run_id':run['run_id'],'source_id':None,'state':'STORED','persona_publication_id':pointer['publication_id'],
        'persona_revision':pointer['revision'],'prompt_ref':setting['prompt_ref'],'schema_ref':binding.schema_ref,'transform_ref':'dream_formal_actions_v1',
        'model_binding_digest':sha256(encode_content((owner.configuration.snapshot_id,binding.prompt_digest,binding.schema_digest),8192)).hexdigest(),
        'ordered_members':(),'related_objects':(),'wire_digest':sha256(wire).hexdigest(),'input_token_estimate':None,
        'reservation_input_bound':bound,'original_operation':operation,'terminal_operation':None}
    return freeze_material(metadata,raw,dream_format=True)
