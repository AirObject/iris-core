"""Domain-safe identifiers with no framework dependency."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ResourceId:
    value: UUID

    @classmethod
    def parse(cls, value: str) -> ResourceId:
        return cls(UUID(value))

    def __str__(self) -> str:
        return str(self.value)
