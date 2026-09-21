"""Read-only notification routing needed by the goals-owned delivery transaction.

Trusted assembly supplies the permission owner's participant and fixed route
projection. Goals cannot mutate routes, authenticate sessions or issue tokens.
"""
from typing import Protocol
from companion_memory.persistence import UnitOfWork
from companion_memory.persistence.daily_records import Record
from companion_memory.persistence.owned_statements import StatementCatalog


class NotificationRouteReader(Protocol):
    @property
    def catalog(self) -> StatementCatalog: ...

    def notification_route(self, uow: UnitOfWork, route_id: str) -> Record | None:
        """Read the original permission projection within the caller's transaction."""
        ...
