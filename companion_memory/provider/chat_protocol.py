"""Closed Chat Completions encoding and independent completion/usage evidence.

This module performs no transmission, admission, persistence or business write.
Only a later durable Provider owner may turn this protocol evidence into a
terminal receipt. Invalid output never discards separately available usage.
"""
from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import cast
from .chat_json import decode_wire, encode_wire
from .values import Data, InvalidData, Record, as_record, freeze, is_identifier
from .token_costs import InvalidAmount, add, quantity

_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def _text(value: Data, limit: int, *, nonempty: bool = True) -> str:
    if type(value) is not str or len(value.encode('utf-8')) > limit or nonempty and not value.strip():
        raise InvalidData()
    return value


@dataclass(frozen=True, slots=True)
class ChatBinding:
    """Immutable protocol resources, not a credential or permission to send.

    The caller supplies the configuration-bound resource digest and observed
    model allowlist. Eligibility and routing evidence remain admission concerns.
    """
    requested_model: str
    expected_models: tuple[str, ...]
    resolved_model: str | None
    schema_ref: str
    schema_digest: str
    schema_name: str
    schema_bytes: bytes

    def __post_init__(self) -> None:
        if (self.requested_model not in ('ark-code-latest','MiniMax-M3','deepseek-flash') or type(self.expected_models) is not tuple
                or not 1 <= len(self.expected_models) <= 8
                or any(not is_identifier(v) for v in self.expected_models)
                or len(set(self.expected_models)) != len(self.expected_models)
                or self.resolved_model is not None and not is_identifier(self.resolved_model)
                or not is_identifier(self.schema_ref) or type(self.schema_digest) is not str
                or _DIGEST.fullmatch(self.schema_digest) is None
                or self.schema_name not in ('text_learning', 'initial_persona')
                or type(self.schema_bytes) is not bytes or len(self.schema_bytes) > 12288):
            raise InvalidData()
        if hashlib.sha256(self.schema_bytes).hexdigest() != self.schema_digest:
            raise InvalidData()
        if self.requested_model in ('MiniMax-M3','deepseek-flash') and (self.expected_models!=(self.requested_model,) or self.resolved_model is not None):
            raise InvalidData()
        decode_wire(self.schema_bytes, 12288)


def encode_request(payload: object, binding: ChatBinding) -> bytes:
    """Validate a complete semantic generation payload and build one Chat body."""
    if type(binding) is not ChatBinding:
        raise InvalidData()
    value = as_record(freeze(payload, 131072, owned=True))
    if set(value) != {'format_version', 'messages', 'schema_ref', 'schema_digest', 'output_tokens',
                      'reservation_input_bound', 'context_digest'}:
        raise InvalidData()
    if (type(value['format_version']) is not int or value['format_version'] != 2
            or value['schema_ref'] != binding.schema_ref or value['schema_digest'] != binding.schema_digest
            or type(value['output_tokens']) is not int or value['output_tokens'] != 2048
            or type(value['reservation_input_bound']) is not int or not 1 <= value['reservation_input_bound'] <= 1048576
            or type(value['context_digest']) is not str or _DIGEST.fullmatch(value['context_digest']) is None):
        raise InvalidData()
    messages = value['messages']
    if type(messages) is not tuple or len(messages) != 2:
        raise InvalidData()
    wire: list[dict[str, str]] = []
    for raw, role, limit in zip(messages, ('SYSTEM', 'USER'), (4096, 32768), strict=True):
        message = as_record(raw)
        if set(message) != {'role', 'text'} or message['role'] != role:
            raise InvalidData()
        wire.append({'role': role.lower(), 'content': _text(message['text'], limit)})
    if binding.requested_model == 'deepseek-flash':
        from .deepseek_protocol import encode
        return encode(wire,binding.schema_bytes)
    if binding.requested_model == 'MiniMax-M3':
        from .minimax_protocol import encode
        return encode(wire,binding.schema_bytes)
    result = as_record(freeze({'model': binding.requested_model, 'messages': wire,
        'max_tokens': 2048, 'n': 1, 'stream': False,
        'response_format': {'type': 'json_schema', 'json_schema': {'name': binding.schema_name,
            'schema': decode_wire(binding.schema_bytes, 12288), 'strict': True}}}, 131072, owned=True))
    return encode_wire(result, 131072)


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """Safe reported quantities; unknown or uncovered responsibility is explicit."""
    fields: Record
    raw_usage: Record
    valid: bool
    billing_covered: bool


def observe_usage(raw: Data) -> UsageObservation:
    """Extract only known safe fields, without interpreting missing usage as zero."""
    source = raw if type(raw) is MappingProxyType else MappingProxyType({})
    accepted: dict[str, Data] = {}
    valid = raw is None or type(raw) is MappingProxyType
    covered = type(raw) is MappingProxyType
    allowed = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_tokens_details', 'completion_tokens_details'}
    covered &= not (set(source) - allowed)
    def read(record: Record, name: str, target: str) -> None:
        nonlocal valid
        value = record.get(name)
        if value is not None:
            try:
                value = quantity(value)
            except InvalidAmount:
                valid = False
                value = None
        accepted[target] = value
    for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        read(source, name, name)
    for name, count, target, provision in (
            ('prompt_tokens_details', 'cached_tokens', 'cached_tokens', 'provisioned_input_tokens'),
            ('completion_tokens_details', 'reasoning_tokens', 'reasoning_tokens', 'provisioned_output_tokens')):
        details = source.get(name)
        if details is None:
            detail = MappingProxyType({})
        elif type(details) is MappingProxyType:
            detail = details
        else:
            detail = MappingProxyType({})
            valid = False
        read(detail, count, target)
        read(detail, 'provisioned_tokens', provision)
        # Unpriced extensions remain a responsibility gap, including future
        # fields whose meaning cannot be inferred from their spelling.
        covered &= not (set(detail) - {count, 'provisioned_tokens', 'audio_tokens'})
        for extra in ('provisioned_tokens', 'audio_tokens'):
            if extra in detail and (type(detail[extra]) is not int or detail[extra] != 0):
                covered = False
    incoming, outgoing, total = (accepted[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
    try:
        if incoming is not None and outgoing is not None:
            computed = add(cast(int, incoming), cast(int, outgoing))
            if total is not None and total != computed:
                valid = False
        for part, whole in (('cached_tokens', incoming), ('reasoning_tokens', outgoing)):
            if whole is not None and accepted[part] is not None and cast(int, accepted[part]) > cast(int, whole):
                valid = False
    except InvalidAmount:
        valid = False
    covered &= all(accepted[name] is not None for name in ('prompt_tokens', 'completion_tokens', 'cached_tokens'))
    fields: dict[str, Data] = {name: None for name in ('input_tokens', 'output_tokens', 'cache_read_tokens',
        'cache_write_tokens', 'reasoning_tokens', 'input_items', 'embedding_dimensions', 'rerank_candidates',
        'media_bytes', 'media_duration_ms', 'total_tokens')}
    for target, name in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
                         ('total_tokens', 'total_tokens'), ('cache_read_tokens', 'cached_tokens'),
                         ('reasoning_tokens', 'reasoning_tokens')):
        fields[target] = accepted[name]
    return UsageObservation(as_record(freeze(fields, 2048)), as_record(freeze(accepted, 2048)), valid, bool(covered and valid))


@dataclass(frozen=True, slots=True)
class ChatObservation:
    """Protocol completion and usage evidence, without a durable success claim."""
    outcome: str
    result: Record | None
    usage: UsageObservation


def validate_structured_result(value: object, binding: ChatBinding) -> Record:
    """Recheck a recovered normalized handoff against its original model binding."""
    result = as_record(freeze(value, 8192, owned=True))
    if (set(result) != {'format_version', 'output', 'stop_reason', 'provider_response_ref', 'requested_model_id',
            'reported_model_id', 'resolved_model_id', 'output_schema_ref', 'raw_output_digest'}
            or type(result['format_version']) is not int or result['format_version'] != 2 or result['stop_reason'] != 'STOP'
            or result['requested_model_id'] != binding.requested_model or result['reported_model_id'] not in binding.expected_models
            or result['resolved_model_id'] != binding.resolved_model or result['output_schema_ref'] != binding.schema_ref
            or not is_identifier(result['provider_response_ref']) or type(result['raw_output_digest']) is not str
            or _DIGEST.fullmatch(result['raw_output_digest']) is None):
        raise InvalidData()
    encode_wire(as_record(result['output']), 6144)
    return result


def decode_response(raw: bytes, binding: ChatBinding) -> ChatObservation:
    """Dispatch only explicitly bound supplier protocols."""
    if type(binding) is not ChatBinding:raise InvalidData()
    if binding.requested_model=='deepseek-flash':
        from .deepseek_protocol import decode
        return decode(raw,binding)
    if binding.requested_model=='MiniMax-M3':
        from .minimax_protocol import decode
        return decode(raw,binding)
    return _decode_response(raw,binding)


def _decode_response(raw: bytes, binding: ChatBinding) -> ChatObservation:
    """Validate the whole response; refusals and partial text never become output."""
    if type(binding) is not ChatBinding:
        raise InvalidData()
    unavailable = observe_usage(None)
    try:
        response = decode_wire(raw, 262144)
    except InvalidData:
        return ChatObservation('INVALID_RESPONSE', None, unavailable)
    usage = observe_usage(response.get('usage'))
    try:
        required = {'id', 'object', 'created', 'model', 'choices'}
        if not required <= set(response) or set(response) - required - {'usage', 'system_fingerprint', 'service_tier'}:
            raise InvalidData()
        if (not is_identifier(response['id']) or response['object'] != 'chat.completion'
                or not is_identifier(response['model'])):
            raise InvalidData()
        quantity(response['created'])
        for name in ('system_fingerprint', 'service_tier'):
            if response.get(name) is not None:
                _text(response[name], 128)
        if response['model'] not in binding.expected_models:
            return ChatObservation('MODEL_BINDING_MISMATCH', None, usage)
        choices = response['choices']
        if type(choices) is not tuple or len(choices) != 1:
            raise InvalidData()
        choice = as_record(choices[0])
        if (not {'index', 'message', 'finish_reason'} <= set(choice)
                or set(choice) - {'index', 'message', 'finish_reason', 'logprobs', 'moderation_hit_type'}
                or type(choice['index']) is not int or choice['index'] != 0 or choice.get('logprobs') is not None):
            raise InvalidData()
        if choice.get('moderation_hit_type') is not None:
            _text(choice['moderation_hit_type'], 128)
        message = as_record(choice['message'])
        if (not {'role', 'content'} <= set(message) or message['role'] != 'assistant'
                or set(message) - {'role', 'content', 'refusal', 'reasoning_content', 'tool_calls', 'function_call'}
                or message.get('tool_calls') is not None or message.get('function_call') is not None):
            raise InvalidData()
        if choice['finish_reason'] not in ('stop', 'length', 'content_filter'):
            raise InvalidData()
        if message['content'] is not None:
            _text(message['content'], 262144, nonempty=False)
        if message.get('reasoning_content') is not None:
            _text(message['reasoning_content'], 262144, nonempty=False)
        refusal = message.get('refusal')
        if refusal is not None:
            _text(refusal, 262144, nonempty=False)
        if choice['finish_reason'] == 'content_filter' or refusal:
            return ChatObservation('OTHER_REFUSAL', None, usage)
        if choice['finish_reason'] == 'length':
            return ChatObservation('OUTPUT_LIMIT', None, usage)
        text = _text(message['content'], 6144)
        output = decode_wire(text.encode('utf-8'), 6144)
        encode_wire(output, 6144)
        result = as_record(freeze({'format_version': 2, 'output': output, 'stop_reason': 'STOP',
            'provider_response_ref': response['id'], 'requested_model_id': binding.requested_model,
            'reported_model_id': response['model'], 'resolved_model_id': binding.resolved_model,
            'output_schema_ref': binding.schema_ref, 'raw_output_digest': hashlib.sha256(text.encode('utf-8')).hexdigest()}, 8192, owned=True))
        return ChatObservation('SUCCEEDED', result, usage)
    except (InvalidData, InvalidAmount):
        return ChatObservation('INVALID_RESPONSE', None, usage)
