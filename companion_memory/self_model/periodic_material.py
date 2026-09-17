"""Exact complete generation/review material and its configured wire envelope."""
from hashlib import sha256
from typing import cast
from companion_memory.persistence.semantic_records import Record
from companion_memory.persistence.content_codec import encode_content
from companion_memory.provider.values import freeze,DataLimit
from companion_memory.persistence.schema import ValueTooLarge
from companion_memory.provider.dream_protocol import encode_dream_request,input_byte_bound
from companion_memory.cognition.daily_material import freeze_material


def freeze_periodic(owner,role:str,run:Record,body:object,operation:Record,now:int):
    provider=owner.provider;config=owner.configuration;binding=provider.dream_bindings[role]
    try:body=cast(Record,freeze(body,262144,owned=True))
    except DataLimit:raise ValueTooLarge() from None
    raw=encode_content(body,262144)
    if role=='PERSONA_DREAM':reserve_review_envelope(owner,raw)
    wire=encode_dream_request(binding,raw.decode())
    bound=input_byte_bound(binding,raw)
    profile=next(p for p in provider.profiles if p['material_role']==role)
    if bound>profile['max_input_units']:
        from companion_memory.persistence.owned_statements import OwnerFailure
        raise OwnerFailure('RESOURCE_BUSY','material','CAPACITY_REACHED')
    setting=next(r for r in cast(tuple[Record,...],config.candidate.text.record('provider.transport')['roles']) if r['role']==role)
    policy=config.candidate.text.record('self_model.initial_persona')
    metadata=owner.base(owner.key('periodic-material',run['run_id'],role),now)|{
        'context_version':3,'context_kind':role,'owner_ref':run['run_id'],'batch_id':None,'run_id':run['run_id'],'source_id':None,
        'state':'STORED','persona_publication_id':cast(Record,body['previous_persona'])['publication_id'],
        'persona_revision':cast(Record,body['previous_persona'])['revision'],'prompt_ref':setting['prompt_ref'],'schema_ref':binding.schema_ref,
        'transform_ref':policy['transform_ref'],'model_binding_digest':sha256(encode_content(cast(Record,freeze({
            'config':config.snapshot_id,'role':role,'prompt':binding.prompt_digest,'schema':binding.schema_digest},8192,owned=True)),8192)).hexdigest(),
        'ordered_members':(),'related_objects':(),'wire_digest':sha256(wire).hexdigest(),
        'input_token_estimate':None,'reservation_input_bound':bound,'original_operation':operation,'terminal_operation':None}
    return freeze_material(metadata,raw,dream_format=True)


def reserve_review_envelope(owner,raw:bytes):
    """Reserve the complete allowed output in the later independent review.

    No synthetic candidate is stored or presented as a model result. The bound
    uses the strict 16384-byte candidate codec, exact current material, exact
    regulatory SYSTEM content and JSON's worst outer escaping of canonical JSON.
    """
    from companion_memory.persistence.schema import ValueTooLarge
    from companion_memory.provider.dream_protocol import system_message
    binding=owner.provider.dream_bindings['PERSONA_REVIEW']
    extra=len(b',"candidate":')+16384
    maximum_material=len(raw)+extra
    profile=next(p for p in owner.provider.profiles if p['material_role']=='PERSONA_REVIEW')
    maximum_input=len(system_message(binding).encode())+maximum_material
    maximum_wire=len(encode_dream_request(binding,raw.decode()))+2*extra
    if maximum_material>262144 or maximum_input>profile['max_input_units'] or maximum_wire>1048576:raise ValueTooLarge()
    return {'material_bytes':maximum_material,'input_units_bound':maximum_input,'wire_bytes_bound':maximum_wire}
