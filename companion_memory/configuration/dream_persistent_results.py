"""Configuration-owned errors alongside explicit durable commit and cleanup facts.

A successful commit may precede an unavailable full configuration view. That
result remains committed; only a complete validated view can initialize runtime.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING,Literal
from companion_memory.persistence import Receipt,RecoveryHandle
from .dream_resolution import DreamConfigurationError
if TYPE_CHECKING:
    from .dream_persistence import StoredDreamConfiguration


@dataclass(frozen=True,slots=True)
class ConfigurationCommitted:
    receipt:Receipt
    source:Literal['NEW','EXISTING']
    configuration:StoredDreamConfiguration | None
    error:DreamConfigurationError | None=None
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationNotCommitted:
    error:DreamConfigurationError
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationRejected:
    error:DreamConfigurationError
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationUnconfirmed:
    reference:RecoveryHandle
    error:DreamConfigurationError
    cleanup_pending:bool
