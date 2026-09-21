"""Complete-item information budgeting using the actual canonical wire encoding.

Ordering remains stable. An omitted object, event or goal is never shortened or
partially attributed. Single-record sections fail explicitly when they cannot
fit, and each caller chooses whether partial availability is acceptable.
"""
from types import MappingProxyType
from companion_memory.persistence import Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import ValueTooLarge
from companion_memory.persistence.record_primitives import Record


def bounded_items(items: tuple[Record, ...], budget: int, metadata: dict[str, Value] | None = None) -> tuple[tuple[Record, ...], int]:
    """Return the longest complete prefix fitting its actual section envelope."""
    selected: tuple[Record, ...] = ()
    for item in items:
        candidate = (*selected, item)
        value: Value = candidate if metadata is None else MappingProxyType(metadata | {'items': candidate})
        try: encode_content(value, budget)
        except ValueTooLarge: break
        selected = candidate
    return selected, len(items) - len(selected)


def bounded_single(value: Record, budget: int, observed_at: int) -> tuple[Record, bool]:
    """Preserve the whole section or return an explicit unavailable section."""
    try:
        encode_content(value, budget)
        return value, False
    except ValueTooLarge:
        return MappingProxyType({'availability': 'UNAVAILABLE', 'reason': 'LIMIT_EXCEEDED', 'observed_at': observed_at}), True
