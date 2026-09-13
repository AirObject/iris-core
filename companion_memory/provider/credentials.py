"""Provider-private credential leases with explicit revocation and ownership.

Configuration carries references only. A trusted resolver lends one opaque
lease per actual transport, which releases it after all socket work ends.
Representations and failures contain neither credential text nor its digest.
"""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
import threading
import socket
import ssl
from typing import Literal
from .values import is_identifier


class CredentialLease:
    """Owned bounded bytes; revocation prevents access before a send begins."""
    __slots__ = ('_value', '_lock', '_revoked', '_released')

    def __init__(self, value: bytes):
        if (type(value) is not bytes or not 1 <= len(value) <= 4096
                or any(character < 33 or character > 126 for character in value)):
            raise ValueError('Invalid credential lease.')
        self._value = bytearray(value)
        self._lock = threading.Lock()
        self._revoked = self._released = False

    def __repr__(self) -> str:
        return 'CredentialLease(<opaque>)'

    def revoke(self) -> None:
        """Irreversibly withdraw the lease without implying remote rollback."""
        with self._lock:
            self._revoked = True

    def _header(self) -> bytes | None:
        with self._lock:
            return None if self._revoked or self._released else bytes(self._value)

    def _send_header(self, connection: socket.socket, prefix: bytes) -> bool:
        """Linearize revocation against the actual first write, retaining ownership.

        Revocation completed before this lock is acquired forbids transmission.
        Once the socket write begins, later revocation cannot imply remote undo.
        The borrowed credential never escapes as a public header value.
        """
        if type(connection) not in (socket.socket, ssl.SSLSocket) or type(prefix) is not bytes:
            raise ValueError('A native transport connection is required.')
        with self._lock:
            if self._revoked or self._released:
                return False
            connection.sendall(prefix + bytes(self._value) + b'\r\n\r\n')
            return True

    def release(self) -> None:
        """Erase the owned mutable buffer after the transport consumer ends."""
        with self._lock:
            self._value[:] = b'\x00' * len(self._value)
            self._released = True

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released


@dataclass(frozen=True, slots=True)
class Available:
    """A newly lent native credential, never a configuration value."""
    lease: CredentialLease


@dataclass(frozen=True, slots=True)
class CredentialUnavailable:
    """Closed safe outcomes without an underlying resolver exception."""
    reason: Literal['UNAVAILABLE', 'REVOKED', 'FAILED']


CredentialResult = Available | CredentialUnavailable


class CredentialResolver:
    """Borrowed reference resolver; Provider does not close its external owner."""
    def __init__(self, resolve: Callable[[str, str, str], CredentialResult]):
        if not callable(resolve):
            raise TypeError('An explicit credential resolver is required.')
        self._resolve = resolve

    def resolve(self, secret_ref: str, secret_revision: str, account_ref: str) -> CredentialResult:
        """Resolve safe IDs; malformed results and private exceptions become FAILED."""
        if not all(is_identifier(value) for value in (secret_ref, secret_revision, account_ref)):
            return CredentialUnavailable('FAILED')
        try:
            result = self._resolve(secret_ref, secret_revision, account_ref)
            if type(result) is Available and type(result.lease) is CredentialLease:
                return result
            if type(result) is CredentialUnavailable and result.reason in ('UNAVAILABLE', 'REVOKED', 'FAILED'):
                return result
        except MemoryError:
            raise
        except Exception:
            # The resource boundary cannot expose an arbitrary resolver's error.
            return CredentialUnavailable('FAILED')
        return CredentialUnavailable('FAILED')
