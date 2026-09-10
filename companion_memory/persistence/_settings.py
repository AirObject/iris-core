"""Select verified persistence values via configuration's public snapshot API.

Configuration owns metadata matching and unique constraints. This module reads
only selected values and has no defaults, private constructors or re-resolution.
"""

from dataclasses import dataclass
from typing import cast

from companion_memory.configuration import EffectiveSnapshot, PresentValue, persistence_snapshot_issue
from .results import PersistenceError


@dataclass(frozen=True, slots=True)
class Settings:
    database_file: str
    operation_timeout_ms: int
    lock_wait_ms: int
    close_timeout_ms: int
    read_capacity: int
    command_max_bytes: int
    receipt_max_bytes: int
    wal_checkpoint_pages: int
    event_max_bytes: int
    events_per_operation: int
    logging_directory: str | None


def read_settings(snapshot: object) -> Settings | PersistenceError:
    issue = persistence_snapshot_issue(snapshot)
    if issue is not None:
        return PersistenceError("CONFIGURATION_UNSUPPORTED", "initialize", "configuration", issue)
    assert type(snapshot) is EffectiveSnapshot
    values = {entry.definition.key: entry.state.value for entry in snapshot.list_entries()
              if type(entry.state) is PresentValue}
    return Settings(
        cast(str, values["storage.database_file"]), cast(int, values["storage.operation_timeout_ms"]),
        cast(int, values["storage.lock_wait_ms"]), cast(int, values["storage.close_timeout_ms"]),
        cast(int, values["storage.read_capacity"]), cast(int, values["storage.command_max_bytes"]),
        cast(int, values["storage.receipt_max_bytes"]), cast(int, values["storage.wal_checkpoint_pages"]),
        cast(int, values["audit.event_max_bytes"]), cast(int, values["audit.events_per_operation"]),
        cast(str | None, values.get("logging.file_directory")),
    )
