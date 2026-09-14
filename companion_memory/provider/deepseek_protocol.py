"""Closed DeepSeek Chat JSON Object protocol and independent token observations.

The nonthinking, nonstreaming binding accepts only documented fields. JSON
Object does not attest to the business schema; the complete local validators
still decide whether a proposal can become a candidate. No repair or retry runs.
"""
from types import MappingProxyType
from .values import Data, InvalidData, as_record, freeze, is_identifier
from .chat_json import decode_wire, encode_wire
from .token_costs import InvalidAmount, quantity, add

PROTOCOL = 'DEEPSEEK_CHAT_JSON_V1'
MODEL = 'deepseek-flash'
JSON_INSTRUCTIONS = '\nDEEPSEEK_JSON_OBJECT_V1\nReturn one complete JSON object conforming to this local schema, with every required field and no extra keys, markdown or explanation. UTF-8 limits and source bindings are checked locally.\nJSON Schema:\n'


def prompt_suffix(schema: bytes) -> str:
    """Include the complete immutable business schema within the wire capacity."""
    if type(schema) is not bytes or len(schema) > 12288:
        raise InvalidData()
    decode_wire(schema, 12288)
    result = JSON_INSTRUCTIONS + schema.decode('utf-8')
    if len(result.encode()) > 12544:
        raise InvalidData()
    return result


def encode(messages: list[dict[str, str]], schema: bytes) -> bytes:
    """Encode exactly two messages and the six fixed nonthinking request fields."""
    system = messages[0]['content'] + prompt_suffix(schema)
    if len(system.encode()) > 16640:
        raise InvalidData()
    return encode_wire(as_record(freeze({'model': MODEL,
        'messages': [{'role': 'system', 'content': system}, messages[1]],
        'max_tokens': 2048, 'stream': False, 'thinking': {'type': 'disabled'},
        'response_format': {'type': 'json_object'}}, 131072, owned=True)), 131072)


def observe_usage(raw: Data):
    """Keep cache hit/miss reports and validate both partitions, never fill zero."""
    from .chat_protocol import observe_usage as common, UsageObservation
    source = raw if type(raw) is MappingProxyType else MappingProxyType({})
    allowed = {'prompt_tokens', 'completion_tokens', 'total_tokens',
        'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens',
        'prompt_tokens_details', 'completion_tokens_details'}
    clean = {k: v for k, v in source.items() if k not in ('prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')}
    details = source.get('prompt_tokens_details')
    # Normalize the official hit field only; an absent optional alias stays absent.
    clean['prompt_tokens_details'] = {'cached_tokens': source.get('prompt_cache_hit_tokens')}
    result = common(freeze(clean, 262144, owned=True))
    valid = result.valid and (raw is None or type(raw) is MappingProxyType)
    covered = result.billing_covered and not set(source) - allowed
    observed: dict[str, Data] = dict(result.raw_usage)
    for name in ('prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
        value = source.get(name)
        try:
            if value is not None:
                value = quantity(value)
        except InvalidAmount:
            valid = False
            value = None
        observed[name] = value
    observed['cached_tokens'] = None
    if details is not None:
        if type(details) is not MappingProxyType:
            valid = False
        else:
            covered &= not set(details) - {'cached_tokens'}
            try:
                if details.get('cached_tokens') is not None:
                    observed['cached_tokens'] = quantity(details['cached_tokens'])
                    valid &= observed['cached_tokens'] == observed['prompt_cache_hit_tokens']
            except InvalidAmount:
                valid = False
    output_details = source.get('completion_tokens_details')
    if output_details is not None:
        covered &= type(output_details) is MappingProxyType and not set(output_details) - {'reasoning_tokens'}
    try:
        hit, miss = observed['prompt_cache_hit_tokens'], observed['prompt_cache_miss_tokens']
        if hit is not None and miss is not None and source.get('prompt_tokens') is not None:
            valid &= add(quantity(hit), quantity(miss)) == quantity(source['prompt_tokens'])
    except InvalidAmount:
        valid = False
    covered &= all(source.get(k) is not None for k in
        ('prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'))
    # Thinking is disabled: a nonzero reasoning report contradicts this binding.
    if result.fields['reasoning_tokens'] not in (None, 0):
        valid = False
    return UsageObservation(result.fields, as_record(freeze(observed, 2048)), bool(valid), bool(covered and valid))


def decode(raw: bytes, binding):
    """Validate DeepSeek's envelope before reusing complete content normalization."""
    from .chat_protocol import ChatObservation, _decode_response, _text
    usage = observe_usage(None)
    try:
        response = decode_wire(raw, 262144)
        usage = observe_usage(response.get('usage'))
        required = {'id', 'object', 'created', 'model', 'choices', 'system_fingerprint'}
        if not required <= set(response) or set(response) - required - {'usage'}:
            raise InvalidData()
        if (not is_identifier(response['id']) or response['object'] != 'chat.completion'
                or not is_identifier(response['model'])):
            raise InvalidData()
        quantity(response['created'])
        _text(response['system_fingerprint'], 128)
        if response['model'] not in binding.expected_models:
            return ChatObservation('MODEL_BINDING_MISMATCH', None, usage)
        choices = response['choices']
        if type(choices) is not tuple or len(choices) != 1:
            raise InvalidData()
        choice = as_record(choices[0])
        if (set(choice) != {'index', 'message', 'finish_reason', 'logprobs'}
                or type(choice['index']) is not int or choice['index'] != 0
                or choice['logprobs'] is not None):
            raise InvalidData()
        message = as_record(choice['message'])
        if (not {'role', 'content'} <= set(message) or message['role'] != 'assistant'
                or set(message) - {'role', 'content', 'reasoning_content', 'tool_calls'}
                or message.get('reasoning_content') not in (None, '')
                or message.get('tool_calls') is not None):
            raise InvalidData()
        if message['content'] is not None:
            _text(message['content'], 262144, nonempty=False)
        finish = choice['finish_reason']
        if finish not in ('stop', 'length', 'content_filter', 'tool_calls', 'insufficient_system_resource', 'aborted'):
            raise InvalidData()
        if finish in ('insufficient_system_resource', 'aborted', 'tool_calls'):
            return ChatObservation('INVALID_RESPONSE', None, usage)
        result = _decode_response(raw, binding)
        return ChatObservation(result.outcome, result.result, usage)
    except (InvalidData, InvalidAmount):
        return ChatObservation('INVALID_RESPONSE', None, usage)
