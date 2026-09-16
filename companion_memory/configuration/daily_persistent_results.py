"""Configuration-owned errors alongside explicit durable commit and cleanup facts.

A successful commit may precede an unavailable full configuration view. That
result remains committed; only a complete validated view can initialize runtime.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING,Literal
from companion_memory.persistence import Receipt,RecoveryHandle
from .daily_resolution import DailyConfigurationError
if TYPE_CHECKING:
    from .daily_persistence import StoredDailyConfiguration


@dataclass(frozen=True,slots=True)
class ConfigurationCommitted:
    receipt:Receipt
    source:Literal['NEW','EXISTING']
    configuration:StoredDailyConfiguration | None
    error:DailyConfigurationError | None=None
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationNotCommitted:
    error:DailyConfigurationError
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationRejected:
    error:DailyConfigurationError
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationUnconfirmed:
    reference:RecoveryHandle
    error:DailyConfigurationError
    cleanup_pending:bool
