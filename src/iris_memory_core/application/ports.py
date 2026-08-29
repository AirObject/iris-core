"""Framework-neutral ports shared by later application services."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdentifierGenerator(Protocol):
    def new(self) -> UUID: ...
