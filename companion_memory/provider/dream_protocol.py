"""Independent dream roles over the strictly checked shared supplier framing."""
from dataclasses import dataclass
from hashlib import sha256

from companion_memory.cognition.dream_resources import prompt_resource, output_schema
from .chat_json import encode_wire, decode_wire
from .chat_protocol import ChatObservation
from .daily_protocol import decode_bound_response
from .values import InvalidData, as_record, freeze, is_identifier

ROLES = ('DREAM_REVIEW', 'PERSONA_DREAM', 'PERSONA_REVIEW')


@dataclass(frozen=True, slots=True)
class DreamChatBinding:
    """An exact resource binding; no daily PERSONA role can stand in for review."""
    role: str
    requested_model: str
    schema_ref: str
    schema_digest: str
    schema_bytes: bytes
    prompt_digest: str
    prompt_bytes: bytes

    def __post_init__(self):
        if (self.role not in ROLES or self.requested_model != 'deepseek-flash'
                or not is_identifier(self.schema_ref) or type(self.schema_bytes) is not bytes
                or not 1 <= len(self.schema_bytes) <= 40960 or type(self.prompt_bytes) is not bytes
                or not 1 <= len(self.prompt_bytes) <= 8192
                or self.schema_bytes != output_schema(self.role) or self.prompt_bytes != prompt_resource(self.role)
                or sha256(self.schema_bytes).hexdigest() != self.schema_digest
                or sha256(self.prompt_bytes).hexdigest() != self.prompt_digest):
            raise InvalidData()
        decode_wire(self.schema_bytes, 40960)


def encode_dream_request(binding: DreamChatBinding, user_text: str) -> bytes:
    """Encode all complete material and instructions, enforcing the actual body."""
    if (type(binding) is not DreamChatBinding or type(user_text) is not str
            or not 1 <= len(user_text.encode()) <= 262144):
        raise InvalidData()
    system = system_message(binding)
    return encode_wire(as_record(freeze({'model': binding.requested_model,
        'messages': ({'role': 'system', 'content': system}, {'role': 'user', 'content': user_text}),
        'max_tokens': 4096, 'stream': False, 'thinking': {'type': 'disabled'},
        'response_format': {'type': 'json_object'}}, 1048576, owned=True)), 1048576)


def system_message(binding: DreamChatBinding) -> str:
    """The exact SYSTEM content shared by wire encoding and input accounting."""
    if type(binding) is not DreamChatBinding:raise InvalidData()
    return binding.prompt_bytes.decode()+'\nReturn JSON conforming to this complete schema. UTF-8 limits are checked locally.\n'+binding.schema_bytes.decode()


def input_byte_bound(binding: DreamChatBinding, material: bytes) -> int:
    """Conservative input units count complete instructions and material bytes."""
    if type(material) is not bytes:raise InvalidData()
    return len(system_message(binding).encode())+len(material)


def decode_dream_response(raw: bytes, binding: DreamChatBinding) -> ChatObservation:
    """Keep original usage even when framing or whole-output parsing fails.

    The cognition/self-model owner still validates domain output, supplied basis
    revisions and authority after Provider has persisted the result handoff.
    """
    if type(binding) is not DreamChatBinding:
        raise InvalidData()
    return decode_bound_response(raw, model=binding.requested_model, schema_ref=binding.schema_ref,
        role=binding.role, format_version=6, output_limit=24576 if binding.role == 'DREAM_REVIEW' else 16384)


def wire_capacity(role: str) -> dict[str, int]:
    """Encode a full normal material and worst JSON-escaped body independently.

    Profile input units use the complete SYSTEM plus user bytes. A byte-safe
    reservation can use that conservative bound without guessing token counts.
    The wire transport separately measures JSON escaping and its full envelope.
    """
    prompt = prompt_resource(role)
    schema = output_schema(role)
    binding = DreamChatBinding(role, 'deepseek-flash', 's' * 128, sha256(schema).hexdigest(),
        schema, sha256(prompt).hexdigest(), prompt)
    material = 'x' * 262144
    normal = encode_dream_request(binding, material)
    # NUL is one input byte and six JSON wire bytes. It is deliberately reported
    # as non-fitting, never represented as a truncated accepted material.
    system_bytes = len(prompt) + len(schema) + len('\nReturn JSON conforming to this complete schema. UTF-8 limits are checked locally.\n'.encode())
    return {'prompt_bytes': len(prompt), 'schema_bytes': len(schema), 'material_bytes': 262144,
        'input_byte_bound': system_bytes + 262144, 'normal_wire_bytes': len(normal),
        'escaped_material_wire_bytes': 6 * 262144, 'wire_limit_bytes': 1048576}
