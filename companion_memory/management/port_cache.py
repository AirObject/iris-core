"""Bounded native port leases that outlive logical timeout responses.

Binding is serialized; business execution is concurrent. Eviction requires
zero active callers and zero retained descendants for that exact port.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import time
from companion_memory.information.business import InformationPort
from companion_memory.persistence.completion import CompletionScope
from companion_memory.persistence.owned_statements import OwnerFailure


@dataclass(slots=True)
class CachedPort:
    port: InformationPort
    expires: float
    users: int = 0


class PortCache:
    def __init__(self, limit: int = 16):
        self.limit = limit
        self.entries: dict[tuple[object, ...], CachedPort] = {}
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def lease(self, key: tuple[object, ...], bind: Callable[[float], Awaitable[InformationPort]],
                    revoke: Callable[[InformationPort], None]) -> AsyncIterator[InformationPort]:
        """Keep this lease until every owner-started descendant has actually ended."""
        async with self.lock:
            entry = self.entries.get(key)
            now = time.monotonic()
            if entry is not None and entry.expires <= now:
                if entry.users:
                    raise OwnerFailure('RESOURCE_BUSY', 'resource', 'CLEANUP_PENDING', True)
                revoke(entry.port)
                del self.entries[key]
                entry = None
            if entry is None:
                if any(previous_key[:-1] == key[:-1] and previous.users
                       for previous_key, previous in self.entries.items()):
                    raise OwnerFailure('RESOURCE_BUSY', 'resource', 'CLEANUP_PENDING', True)
                # Native bindings also have a fixed shared capacity. Reclaim
                # idle cache entries before issuing a new handle.
                for previous_key, previous in tuple(self.entries.items()):
                    if not previous.users:
                        revoke(previous.port)
                        del self.entries[previous_key]
                if len(self.entries) >= self.limit:
                    raise OwnerFailure('RESOURCE_BUSY', 'resource', 'ADMISSION_FULL', True)
                expires = now + 30
                entry = CachedPort(await bind(expires), expires)
                self.entries[key] = entry
            entry.users += 1
        completion = CompletionScope()
        try:
            with completion:
                yield entry.port
        finally:
            def ended() -> None:
                entry.users -= 1
            completion.when_ended(ended)
