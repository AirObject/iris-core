"""Bounded daily Chat wire format and independently checked supplier envelopes.

The business owner validates actions after Provider verifies the whole response.
A malformed or partial output is a known failure, never a sensitive-media cache
entry. Original usage remains observable independently of proposal validity.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from .chat_json import encode_wire,decode_wire
from .chat_protocol import ChatObservation,UsageObservation
from .deepseek_protocol import observe_usage as deepseek_usage
from .minimax_protocol import observe_usage as minimax_usage
from .values import InvalidData,as_record,freeze,is_identifier,Record
from .token_costs import quantity,InvalidAmount

@dataclass(frozen=True,slots=True)
class DailyChatBinding:
    role:str
    requested_model:str
    schema_ref:str
    schema_digest:str
    schema_bytes:bytes
    prompt_digest:str
    prompt_bytes:bytes

    def __post_init__(self):
        if (self.role not in ('LEARNING','GOAL_DEDUP','PERSONA','MEDIA') or self.requested_model not in ('deepseek-flash','MiniMax-M3')
                or self.requested_model=='MiniMax-M3' and self.role!='MEDIA' or not is_identifier(self.schema_ref)
                or type(self.schema_bytes) is not bytes or not 1<=len(self.schema_bytes)<=40960
                or type(self.prompt_bytes) is not bytes or not 1<=len(self.prompt_bytes)<=8192
                or sha256(self.schema_bytes).hexdigest()!=self.schema_digest or sha256(self.prompt_bytes).hexdigest()!=self.prompt_digest):raise InvalidData()
        decode_wire(self.schema_bytes,40960);self.prompt_bytes.decode('utf-8')


def encode_daily_request(binding:DailyChatBinding,user_text:str) -> bytes:
    """One immutable system/user pair; roles never inherit one another's limits."""
    if type(binding) is not DailyChatBinding or binding.role=='MEDIA' or type(user_text) is not str or not 1<=len(user_text.encode())<=262144:raise InvalidData()
    system=binding.prompt_bytes.decode()+'\nReturn JSON conforming to this complete schema. UTF-8 limits are checked locally.\n'+binding.schema_bytes.decode()
    value={'model':binding.requested_model,'messages':({'role':'system','content':system},{'role':'user','content':user_text}),
        'max_tokens':{'LEARNING':4096,'GOAL_DEDUP':512,'PERSONA':2048}[binding.role],'stream':False,
        'thinking':{'type':'disabled'},'response_format':{'type':'json_object'}}
    return encode_wire(as_record(freeze(value,1048576,owned=True)),1048576)


def _string(value:object,maximum:int,*,empty:bool=False) -> str:
    if type(value) is not str or len(value.encode())>maximum or not empty and not value:raise InvalidData()
    return value


def decode_daily_response(raw:bytes,binding:DailyChatBinding) -> ChatObservation:
    """Identity, exact framing fields, refusal and output are checked in that order."""
    if type(binding) is not DailyChatBinding:raise InvalidData()
    return decode_bound_response(raw,model=binding.requested_model,schema_ref=binding.schema_ref,
        role=binding.role,format_version=5,output_limit=24576)


def decode_bound_response(raw:bytes,*,model:str,schema_ref:str,role:str,format_version:int,output_limit:int) -> ChatObservation:
    """Shared strict supplier envelope decoder; caller validates its native binding.

    This pure parser grants no request, result-owner or publication authority.
    Daily and dream bindings retain independent exact role/identity validation.
    """
    if ((format_version,output_limit) not in ((5,24576),(6,24576),(6,16384))
            or model not in ('deepseek-flash','MiniMax-M3') or not is_identifier(schema_ref)):
        raise InvalidData()
    observer=minimax_usage if model=='MiniMax-M3' else deepseek_usage
    usage=observer(None)
    try:
        response=decode_wire(raw,262144);usage=observer(response.get('usage'))
        minimax=model=='MiniMax-M3'
        required={'id','object','created','model','choices'}|({'system_fingerprint'} if not minimax else set())
        extras={'usage','system_fingerprint','service_tier'}|({'base_resp','input_sensitive','output_sensitive','input_sensitive_type','output_sensitive_type','output_sensitive_int'} if minimax else set())
        if not required<=set(response) or set(response)-required-extras:raise InvalidData()
        if not is_identifier(response['id']) or response['object']!='chat.completion' or not is_identifier(response['model']):raise InvalidData()
        quantity(response['created'])
        if response['model']!=model:return ChatObservation('MODEL_BINDING_MISMATCH',None,usage)
        for name in ('system_fingerprint','service_tier'):
            if response.get(name) is not None:_string(response[name],128)
        if not minimax and response['system_fingerprint'] is None:raise InvalidData()
        if minimax:
            base=response.get('base_resp')
            if base is not None:
                base=as_record(base)
                if set(base)!={'status_code','status_msg'}:raise InvalidData()
                code=quantity(base['status_code']);_string(base['status_msg'],4096,empty=True)
                if code:return ChatObservation('AUTHENTICATION_FAILED' if code==1004 else 'REMOTE_RESULT_UNKNOWN' if code in (1001,1002,1013) else 'INVALID_RESPONSE',None,usage)
            for name in ('input_sensitive','output_sensitive'):
                if name in response and type(response[name]) is not bool:raise InvalidData()
            for name in ('input_sensitive_type','output_sensitive_type','output_sensitive_int'):
                if name in response:quantity(response[name])
        choices=response['choices']
        if type(choices) is not tuple or len(choices)!=1:raise InvalidData()
        choice=as_record(choices[0]);required_choice={'index','message','finish_reason'}|({'logprobs'} if not minimax else set())
        if (not required_choice<=set(choice) or set(choice)-required_choice-{'logprobs','moderation_hit_type'}
                or type(choice['index']) is not int or choice['index']!=0 or choice.get('logprobs') is not None):raise InvalidData()
        if choice.get('moderation_hit_type') is not None:_string(choice['moderation_hit_type'],128)
        message=as_record(choice['message']);allowed={'role','content','reasoning_content','tool_calls'}|({'refusal','name','audio_content','reasoning_details'} if minimax else set())
        if not {'role','content'}<=set(message) or set(message)-allowed or message['role']!='assistant' or message.get('reasoning_content') not in (None,'') or message.get('tool_calls') is not None:raise InvalidData()
        if minimax:
            if message.get('name') is not None:_string(message['name'],128)
            if message.get('audio_content') not in (None,'') or message.get('reasoning_details') not in (None,()):raise InvalidData()
        if message.get('refusal') is not None:_string(message['refusal'],262144,empty=True)
        if message['content'] is not None:_string(message['content'],262144,empty=True)
        finish=choice['finish_reason']
        if finish not in ('stop','length','content_filter','tool_calls','insufficient_system_resource','aborted'):raise InvalidData()
        if minimax and (response.get('input_sensitive') is True or response.get('output_sensitive') is True):return ChatObservation('SENSITIVE_REFUSAL',None,usage)
        if finish=='content_filter' or message.get('refusal'):return ChatObservation('OTHER_REFUSAL',None,usage)
        if finish=='length':return ChatObservation('OUTPUT_LIMIT',None,usage)
        if finish!='stop':return ChatObservation('INVALID_RESPONSE',None,usage)
        content=_string(message['content'],output_limit)
        output=decode_wire(content.encode(),output_limit);encode_wire(output,output_limit)
        if role=='MEDIA':
            from .image_protocol import decode_image_output
            decode_image_output(content.encode())
        normalized=as_record(freeze({'format_version':format_version,'output':output,'stop_reason':'STOP','provider_response_ref':response['id'],
            'requested_model_id':model,'reported_model_id':response['model'],'resolved_model_id':None,
            'output_schema_ref':schema_ref,'raw_output_digest':sha256(content.encode()).hexdigest()},40960,owned=True))
        return ChatObservation('SUCCEEDED',normalized,usage)
    except (InvalidData,InvalidAmount,UnicodeError):return ChatObservation('INVALID_RESPONSE',None,usage)
