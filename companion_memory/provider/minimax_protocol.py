"""Versioned MiniMax JSON prompt transport, with independent usage observations.

Only documented MiniMax envelope extensions are accepted. The complete content
is parsed as JSON by the shared decoder; wrappers and partial text are never
repaired. This module has no credential, transport or persistence capability.
"""
from types import MappingProxyType
from typing import cast
from .values import Data, Record, InvalidData, as_record, freeze
from .chat_json import decode_wire, encode_wire
from .token_costs import InvalidAmount, quantity

PROTOCOL='MINIMAX_CHAT_JSON_V1'
MODEL='MiniMax-M3'
BILLING='USAGE_ONLY_TRIAL'
JSON_INSTRUCTIONS='\nMINIMAX_JSON_PROMPT_V1\nReturn one complete JSON object satisfying this schema. No markdown, explanation, thinking tags or extra keys. All required fields must be present. String byte limits are checked locally.\nJSON Schema:\n'


def prompt_suffix(schema: bytes) -> str:
    """Bind the exact local business schema to the supplier instruction text."""
    if type(schema) is not bytes or len(schema)>12288:raise InvalidData()
    decode_wire(schema,12288)
    value=JSON_INSTRUCTIONS+schema.decode('utf-8')
    if len(value.encode())>12544:raise InvalidData()
    return value


def encode(messages: list[dict[str,str]],schema: bytes) -> bytes:
    """Emit only the fixed nonstreaming M3 fields, with thinking disabled."""
    system=messages[0]['content']+prompt_suffix(schema)
    if len(system.encode())>16640:raise InvalidData()
    wire=[{'role':'system','content':system},messages[1]]
    return encode_wire(as_record(freeze({'model':MODEL,'messages':wire,'max_completion_tokens':2048,
        'stream':False,'thinking':{'type':'disabled'}},131072,owned=True)),131072)


def observe_usage(raw: Data):
    """Require actual input/output/total, retaining absent optional details as null."""
    from .chat_protocol import observe_usage as common, UsageObservation
    if type(raw) is not MappingProxyType:return common(raw)
    accepted={'prompt_tokens','completion_tokens','total_tokens','prompt_tokens_details','completion_tokens_details','total_characters'}
    valid=True
    try:
        if raw.get('total_characters') is not None:quantity(raw['total_characters'])
    except InvalidAmount:valid=False
    clean=MappingProxyType({k:v for k,v in raw.items() if k!='total_characters'})
    result=common(clean)
    details_ok=True
    for name,fields in (('prompt_tokens_details',{'cached_tokens'}),('completion_tokens_details',{'reasoning_tokens'})):
        detail=raw.get(name)
        if detail is not None and (type(detail) is not MappingProxyType or set(detail)-fields):details_ok=False
    complete=(not set(raw)-accepted and details_ok and valid and result.valid
        and all(result.fields[name] is not None for name in ('input_tokens','output_tokens','total_tokens')))
    return UsageObservation(result.fields,result.raw_usage,bool(valid and result.valid),bool(complete))


def decode(raw: bytes,binding):
    """Validate the whole MiniMax envelope before normalizing shared Chat fields."""
    from .chat_protocol import ChatObservation,_decode_response,_text
    usage=observe_usage(None)
    try:
        value=decode_wire(raw,262144);usage=observe_usage(value.get('usage'))
        base=value.get('base_resp')
        if base is not None:
            base=as_record(base)
            if set(base)!={'status_code','status_msg'}:raise InvalidData()
            code=quantity(base['status_code']);_text(base['status_msg'],4096,nonempty=False)
            if code:return ChatObservation('AUTHENTICATION_FAILED' if code==1004 else 'REMOTE_RESULT_UNKNOWN' if code in (1001,1002,1013) else 'INVALID_RESPONSE',None,usage)
        standard={'id','object','created','model','choices','usage','system_fingerprint','service_tier'}
        extra={'base_resp','input_sensitive','output_sensitive','input_sensitive_type','output_sensitive_type','output_sensitive_int'}
        if set(value)-standard-extra:raise InvalidData()
        for name in ('input_sensitive','output_sensitive'):
            if name in value and type(value[name]) is not bool:raise InvalidData()
        for name in ('input_sensitive_type','output_sensitive_type','output_sensitive_int'):
            if name in value:quantity(value[name])
        choices=value.get('choices')
        if type(choices) is not tuple or len(choices)!=1:raise InvalidData()
        choice=as_record(choices[0]);message=as_record(choice.get('message'))
        if message.get('name') is not None:_text(message['name'],128)
        if message.get('audio_content') not in (None,''):raise InvalidData()
        if message.get('reasoning_details') not in (None,()):raise InvalidData()
        if message.get('reasoning_content') not in (None,''):raise InvalidData()
        clean_message={k:v for k,v in message.items() if k not in ('name','audio_content','reasoning_details')}
        clean: dict[str,object]={k:v for k,v in value.items() if k in standard}
        clean['choices']=({'index':choice.get('index'),**choice,'message':clean_message},)
        if value.get('input_sensitive') or value.get('output_sensitive'):
            # A refusal still requires a valid response identity and whole envelope.
            clean['choices']=({**choice,'message':{**clean_message,'refusal':'Supplier content refusal.'}},)
        result=_decode_response(encode_wire(as_record(freeze(clean,262144,owned=True)),262144),binding)
        return ChatObservation(result.outcome,result.result,usage)
    except (InvalidData,InvalidAmount):return ChatObservation('INVALID_RESPONSE',None,usage)
