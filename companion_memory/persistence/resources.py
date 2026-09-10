"""Explicit resource capabilities for a local single-database service.

Expected identity and its target binding must be retained before initialization.
The verifier is trusted assembly code checking that durable recovery input; this
module supplies no production identity store, path discovery or configuration.
Connections and borrowed clocks are used only by the service's bounded owners.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
import time
from typing import Protocol
from uuid import uuid4


class ConnectionFactory(Protocol):
    """Create a SQLite connection owned exclusively by the requesting worker."""

    def __call__(self, database: str, *, uri: bool, timeout: float, isolation_level: None,
                 check_same_thread: bool) -> sqlite3.Connection: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_identity() -> str:
    return str(uuid4())


@dataclass(frozen=True, slots=True)
class DatabaseResources:
    """Trusted binding and mechanisms, with no duplicate path or tuning values.

    retained_identity_check(expected_database_id, configured_path) must confirm
    a recovery record available across the creation process's exit. Returning
    True without that fact violates this trusted assembly capability. Tests can
    keep the record in a surviving parent or write explicit synthetic metadata.
    Physical file and directory safety is independently checked by storage.
    """

    expected_database_id: str
    retained_identity_check: Callable[[str, str], bool]
    connect: ConnectionFactory = sqlite3.connect
    monotonic: Callable[[], float] = time.monotonic
    utc_now: Callable[[], object] = _utc_now
    new_id: Callable[[], object] = _new_identity
