"""Native attestations of audited Provider terminals for actual result owners.

The issuer retains only weak identity registration. A caller cannot manufacture
an attestation from a status string or request ID, and inspection never sends a
model request. The immutable terminal remains in the Provider-owned ledger.
"""
from __future__ import annotations
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING
from .values import Data
if TYPE_CHECKING:
    from .service import ProviderService


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class VerifiedTerminal:
    """Provider-issued audited request identity, terminal and original owner result."""
    _provider: ProviderService
    database_id: str
    request: MappingProxyType[str, Data]
    result: Data
    original_request: MappingProxyType[str, Data] | None
    terminal_reason: str | None
    confirmed_sent: bool

    def __init__(self):
        raise TypeError('Terminal evidence is issued by the original Provider owner.')


def issued_terminal(value: object) -> bool:
    """Verify exact native issuer registration, including uninitialized-carrier denial."""
    from .service import ProviderService
    if type(value) is not VerifiedTerminal: return False
    try: provider = object.__getattribute__(value, '_provider')
    except AttributeError: return False
    return type(provider) is ProviderService and provider._terminal_evidence.get(id(value)) is value


@dataclass(frozen=True, slots=True)
class TerminalVerified:
    """A native terminal capability, kept separate from serializable result data."""
    value: VerifiedTerminal
