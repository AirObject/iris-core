"""One bounded periodic media lifecycle task; time never grants ownership.

Each tick advances one GC page and one processing observation page. Slow actual
work retains the same task and resources. Closing stops future ticks and joins
already started work without cancelling its physical or database execution.
"""
from __future__ import annotations
import asyncio
import time
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import Found
from companion_memory.persistence.completion import finish_owned
from companion_memory.persistence.owned_statements import OwnerFailure
if TYPE_CHECKING:
    from .service import MediaService


class MediaLifecycle:
    def __init__(self, media: MediaService) -> None:
        self.media = media
        self.task: asyncio.Task[None] | None = None
        self.stop = asyncio.Event()
        self.gc_cursor = ''
        self.work_cursor = ''
        self._suspects = 0
        self.suspects: int | None = None
        self.observed_at_us: int | None = None

    def start(self) -> None:
        """Start at most one local timer after successful media initialization."""
        if self.task is None and not self.stop.is_set():
            self.task = asyncio.create_task(finish_owned(self.run()))

    async def run(self) -> None:
        while not self.stop.is_set():
            try: await asyncio.wait_for(self.stop.wait(), self.media.settings.integer('media.gc_interval_ms') / 1000)
            except TimeoutError: pass
            if self.stop.is_set(): break
            await self.advance()

    async def advance(self) -> None:
        """Advance finite work; errors preserve the cursor and actual protections."""
        result = await self.media.collect_unreferenced(self.gc_cursor)
        if type(result) is Found:
            cursor = cast(str, result.value['after'])
            self.gc_cursor = '' if cursor == self.gc_cursor else cursor
        if self.stop.is_set(): return
        try:
            rows = await self.media.rows.read('work_recovery_page', {'after': self.work_cursor, 'limit': self.media.settings.integer('media.gc_page_size')})
            now = time.time_ns() // 1000
            for row in rows:
                if row['phase'] not in ('RESULT_STORED', 'WAITING_ADMISSION') and now - cast(int, row['last_observed_at_us']) >= self.media.settings.integer('media.processing_suspect_after_ms') * 1000:
                    self._suspects += 1
                self.work_cursor = cast(str, row['work_id'])
            if len(rows) < self.media.settings.integer('media.gc_page_size'):
                self.suspects, self._suspects = self._suspects, 0
                self.observed_at_us = now
                self.work_cursor = ''
        except OwnerFailure:
            self.suspects = None
