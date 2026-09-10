"""Native exclusive module-owner leases issued by one verified storage service.

A lease does not expose SQL or transaction control. It prevents a second service
from recovering records while the first provider still owns executing work.
"""
from __future__ import annotations
from typing import TYPE_CHECKING
import threading

_OWNER_LOCK = threading.RLock()
_OWNERS: dict[tuple[str, str, str], object] = {}
if TYPE_CHECKING:
    from .service import PersistenceService


class ModuleOwnerLease:
    """Opaque lease; only the issuing service can release or validate ownership."""
    __slots__ = ("_service", "_owner", "_token", "_key")

    def __new__(cls):
        raise TypeError("Owner leases are issued by storage assembly.")

    @classmethod
    def _create(cls, service: PersistenceService, owner: str, token: object, key: tuple[str,str,str]) -> ModuleOwnerLease | None:
        with _OWNER_LOCK:
            if key in _OWNERS:
                return None
            _OWNERS[key] = token
        self = object.__new__(cls)
        object.__setattr__(self, "_service", service)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_key", key)
        return self

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Owner leases are immutable.")

    def is_active(self) -> bool:
        """Check native issuance and current resource ownership without I/O."""
        return self._service._owner_valid(self)

    def release(self) -> bool:
        """Trusted owner calls only after all its adapter and local tasks ended."""
        return self._service._release_owner(self)

    @property
    def database_id(self) -> str | None:
        """Return only the prebound database identity, never a resource path."""
        return self._service._owner_database(self)


def _release_global(lease: ModuleOwnerLease) -> None:
    """Keep an old owner reserved even if its borrowed storage was closed first."""
    with _OWNER_LOCK:
        if _OWNERS.get(lease._key) is lease._token:
            del _OWNERS[lease._key]
