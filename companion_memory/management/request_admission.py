"""Concurrent ordinary requests and exclusive, bounded maintenance admission.

Slots follow actual descendant completion, even when a public result is already
available. Independent audit requests do not enter this gate.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Awaitable
from companion_memory.persistence.completion import start_owned, retain_completion
from companion_memory.persistence.owned_statements import OwnerFailure


class RequestAdmission:
    def __init__(self, limit: int, drain_seconds: float):
        self.limit, self.drain_seconds = limit, drain_seconds
        self.tasks: set[asyncio.Task[object]] = set()
        self.exclusive = False

    async def run(self, operation: Callable[[], Awaitable[object]], *, exclusive: bool) -> object:
        """Fence new admission before waiting; never cancel existing owner work."""
        if self.exclusive or len(self.tasks) >= self.limit:
            raise OwnerFailure('RESOURCE_BUSY', 'maintenance', 'ADMISSION_FULL', bool(self.tasks))
        if exclusive:
            self.exclusive = True
            if self.tasks:
                try:
                    _, pending = await asyncio.wait(tuple(self.tasks), timeout=self.drain_seconds)
                except BaseException:
                    self.exclusive = False
                    raise
                if pending:
                    self.exclusive = False
                    raise OwnerFailure('RESOURCE_BUSY', 'maintenance', 'CLEANUP_PENDING', True)
        task, outcome = start_owned(operation())
        self.tasks.add(task)
        retain_completion(task)
        def ended(task: asyncio.Task[object]) -> None:
            self.tasks.discard(task)
            if exclusive:
                self.exclusive = False
            if not task.cancelled():
                task.exception()
        task.add_done_callback(ended)
        return await asyncio.shield(outcome)
