"""Actual static descriptor budgets checked before opening content resources."""
import json
from types import MappingProxyType
from companion_memory.persistence._codec import assembly_value, command_descriptor
from companion_memory.persistence.schema import encode_value


def check_content_assembly(repositories, commands):
    """Measure the exact durable encoding, including every Provider declaration."""
    complete = assembly_value(repositories, commands)
    descriptors = sum(len(encode_value(command_descriptor(command), 1048576)) for command in commands)
    raw = json.loads(complete)
    repository_bytes = len(json.dumps(raw['repositories'], ensure_ascii=True, separators=(',', ':'), sort_keys=True).encode())
    envelope = len(complete) - descriptors - repository_bytes
    if descriptors > 655360 or repository_bytes > 131072 or envelope > 8192 or len(complete) > 794624:
        raise ValueError('The complete content storage assembly exceeds its durable format budget.')
    return MappingProxyType({'command_count': len(commands), 'command_descriptors': descriptors,
        'repository_descriptors': repository_bytes, 'envelope': envelope, 'assembly': len(complete)})
