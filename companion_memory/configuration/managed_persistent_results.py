"""Configuration-owned errors alongside explicit durable commit and cleanup facts.

A successful commit may precede an unavailable full configuration view. That
result remains committed; only a complete validated view can initialize runtime.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING,Literal
from companion_memory.persistence import Receipt,RecoveryHandle
from .managed_resolution import ManagedConfigurationError
if TYPE_CHECKING:
    from .managed_persistence import StoredManagedConfiguration


@dataclass(frozen=True,slots=True)
class ConfigurationCommitted:
    receipt:Receipt
    source:Literal['NEW','EXISTING']
    configuration:StoredManagedConfiguration | None
    error:ManagedConfigurationError | None=None
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationNotCommitted:
    error:ManagedConfigurationError
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationRejected:
    error:ManagedConfigurationError
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationUnconfirmed:
    reference:RecoveryHandle
    error:ManagedConfigurationError
    cleanup_pending:bool
