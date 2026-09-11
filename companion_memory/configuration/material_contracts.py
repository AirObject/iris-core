"""Trusted finite synthetic material protocol and its complete byte bounds.

The constant text and wire versions define one reversible test protocol. Limits
are explicit configuration values; these protocol constants are not tunables.
"""
from dataclasses import dataclass

SYSTEM_TEXT = 'Synthetic records only. Decode each event. Use H and R as context. Return T references only.\n'
HEADER_KEYS = ('instance','host','platform','entry','batch','run','config')
FORMAT_HEADER = 'material=1\n'
FORMAT_TAIL = 'end\n'
FIXED_BYTES = len(SYSTEM_TEXT.encode()) + len(FORMAT_HEADER.encode()) + sum(len(k)+1+128+1 for k in HEADER_KEYS) + len(FORMAT_TAIL.encode())
REFERENCE_BYTES = 1+4+128+19+27+1


@dataclass(frozen=True, slots=True, init=False)
class MaterialContract:
    """Native immutable version binding, issued only by static supported assembly."""
    platform_id: str
    participant: str
    event_format: str
    material_format: str
    template: str
    bound_rule: str
    version: int

    def __init__(self):
        raise TypeError('Use the supported synthetic material binding.')

    def window_bound(self, count: int, event_bytes: int) -> int:
        """Full-window UTF-8 bytes and simulated input units, including Base64."""
        return FIXED_BYTES + count*(REFERENCE_BYTES+4*((event_bytes+2)//3))


def bind_synthetic_material(platform_id: str) -> MaterialContract:
    """Bind the fixed participant/template format to one trusted platform ID."""
    from companion_memory.persistence.schema import valid_identifier
    if not valid_identifier(platform_id):
        raise ValueError('A safe platform identifier is required.')
    result = object.__new__(MaterialContract)
    for k,v in dict(platform_id=platform_id,participant='synthetic_learning',event_format='synthetic_event_json',material_format='synthetic_window_base64',template='synthetic_target_refs',bound_rule='synthetic_window_bound',version=1).items():
        object.__setattr__(result,k,v)
    return result


def valid_material(value: object) -> bool:
    if type(value) is not MaterialContract:
        return False
    try:
        names=('platform_id','participant','event_format','material_format','template','bound_rule')
        if any(type(object.__getattribute__(value,key)) is not str for key in names) or type(object.__getattribute__(value,'version')) is not int:return False
        return value == bind_synthetic_material(value.platform_id)
    except (AttributeError, ValueError):
        return False
