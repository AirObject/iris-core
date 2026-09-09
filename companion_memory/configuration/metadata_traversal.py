"""Private stack types for copying configuration metadata without recursion.

Visit frames carry untrusted nodes; finish frames carry an ancestor identity and
owned child storage. Destinations are internal lists or dictionaries, and their
slots are chosen by the traversal, never by caller-defined indexing operations.
These helpers do not admit input types or decide validation errors.
"""

from typing import Literal, cast

from .definitions import FrozenMetadataValue

type _MetadataDestination = list[FrozenMetadataValue] | dict[str, FrozenMetadataValue]
type _MetadataVisit = tuple[
    Literal["visit"], object, tuple[str | int, ...], _MetadataDestination, str | int,
]
type _MetadataFinish = tuple[
    Literal["finish"], int, _MetadataDestination, _MetadataDestination, str | int,
]
type _MetadataFrame = _MetadataVisit | _MetadataFinish


def _store_metadata(
    destination: _MetadataDestination, slot: str | int, value: FrozenMetadataValue,
) -> None:
    """Write an owned value using the slot type paired with its internal container."""
    if isinstance(destination, list):
        destination[cast(int, slot)] = value
    else:
        destination[cast(str, slot)] = value
