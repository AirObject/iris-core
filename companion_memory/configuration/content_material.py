"""Native complete-source material identity and conservative full-window bounds.

The format is reversible and has two messages. Protocol constants never supply
configuration defaults or authorize a learning or Provider operation.
"""
from dataclasses import dataclass
from companion_memory.persistence.schema import valid_identifier

CONTENT_SYSTEM_TEXT = 'Verification records only. Decode full sources. Learn target messages only; context is auxiliary.\n'
SOURCE_HEADER_LIMIT = 1024
MATERIAL_FIXED_LIMIT = 1220
MEMBER_REFERENCE_LIMIT = 180


@dataclass(frozen=True, slots=True, init=False)
class ContentMaterialContract:
    """A fixed format capability issued for a trusted platform binding."""
    platform_id: str
    participant: str
    event_format: str
    material_format: str
    template: str
    bound_rule: str
    version: int

    def __init__(self):
        raise TypeError('Bind a supported complete-source material format.')

    def window_bound(self, count: int, event_bytes: int, occurrences: int, interpretation_bytes: int) -> int:
        return MATERIAL_FIXED_LIMIT + count * (MEMBER_REFERENCE_LIMIT + 4 * (
            (SOURCE_HEADER_LIMIT + event_bytes + occurrences * interpretation_bytes + 2) // 3))


def bind_content_material(platform_id: str) -> ContentMaterialContract:
    """Issue the immutable material format; the platform ID grants no access."""
    if not valid_identifier(platform_id):
        raise ValueError('A safe platform identifier is required.')
    value = object.__new__(ContentMaterialContract)
    for key, item in dict(platform_id=platform_id, participant='bounded_learning',
                          event_format='source_event_json:2', material_format='complete_source_base64:1',
                          template='target_source_records:1', bound_rule='complete_source_bound:1', version=1).items():
        object.__setattr__(value, key, item)
    return value


def valid_content_material(value: object) -> bool:
    if type(value) is not ContentMaterialContract:
        return False
    try:
        if any(type(object.__getattribute__(value, key)) is not str for key in (
            'platform_id', 'participant', 'event_format', 'material_format', 'template', 'bound_rule'
        )) or type(object.__getattribute__(value, 'version')) is not int:
            return False
        return value == bind_content_material(value.platform_id)
    except (AttributeError, ValueError):
        return False
