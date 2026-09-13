"""Configuration-owned errors alongside explicit durable commit and cleanup facts.

A successful commit may precede an unavailable full configuration view. That
result remains committed; only a complete validated view can initialize runtime.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING,Literal
from companion_memory.persistence import Receipt,RecoveryHandle
from .text_resolution import TextConfigurationError
if TYPE_CHECKING:
    from .text_persistence import StoredTextConfiguration


@dataclass(frozen=True,slots=True)
class ConfigurationCommitted:
    receipt:Receipt
    source:Literal['NEW','EXISTING']
    configuration:StoredTextConfiguration | None
    error:TextConfigurationError | None=None
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationNotCommitted:
    error:TextConfigurationError
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationRejected:
    error:TextConfigurationError
    cleanup_pending:bool=False


@dataclass(frozen=True,slots=True)
class ConfigurationUnconfirmed:
    reference:RecoveryHandle
    error:TextConfigurationError
    cleanup_pending:bool
