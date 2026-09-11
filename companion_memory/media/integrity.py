"""Generation-specific damage evidence blocks reuse independently of interpretation.

Detection fences current sends immediately. The media owner then persists the
bounded fact with required audit; failed persistence retains the local fence and
all references. No repair, fault reset, or replacement generation is inferred.
"""
from __future__ import annotations
import threading
from typing import TYPE_CHECKING, Callable, cast
from companion_memory.persistence import Committed
from companion_memory.persistence.owned_statements import OwnerFailure
if TYPE_CHECKING:
    from .service import MediaService


class MediaIntegrity:
    def __init__(self, media: MediaService):
        self.media = media; self.lock = threading.RLock()
        self.pending: dict[tuple[str, int], str] = {}
        self.notify: Callable[[], None] = lambda: None

    def detect(self, blob, failure: OwnerFailure | OSError) -> None:
        reason = 'CONTENT_MISSING' if isinstance(failure, FileNotFoundError) else getattr(failure, 'reason', None)
        if reason not in ('CONTENT_MISSING', 'CONTENT_CORRUPT', 'RESOURCE_IDENTITY_MISMATCH'): return
        with self.lock:
            self.pending.setdefault((blob['blob_id'], blob['generation']), reason)
            self.notify()

    def check(self, uow, blob) -> None:
        with self.lock:
            reason = self.pending.get((blob['blob_id'], blob['generation']))
        facts = self.media.rows.stage('integrity_get', uow, {'blob_id': blob['blob_id'], 'generation': blob['generation']})
        if reason or facts:
            raise OwnerFailure('FILE_FAILED', 'media', cast(str, reason or facts[0]['reason']))

    async def verify_metadata(self, blob) -> None:
        if self.media._physical_fault: raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')
        with self.lock: reason = self.pending.get((blob['blob_id'], blob['generation']))
        facts = await self.media.rows.read('integrity_get', {'blob_id': blob['blob_id'], 'generation': blob['generation']})
        if reason or facts: raise OwnerFailure('FILE_FAILED', 'media', cast(str, reason or facts[0]['reason']))

    async def flush(self):
        from .service import identity
        with self.lock: facts = tuple(self.pending.items())
        for (bid, generation), reason in facts:
            result = await self.media._execute('block_media_integrity', identity('integrity', bid, generation),
                {'blob_id': bid, 'generation': generation, 'reason': reason})
            if type(result) is not Committed: return result
            with self.lock: self.pending.pop((bid, generation), None)
        return None

    async def io(self, blob, key: str, work):
        unresolved = await self.flush()
        if unresolved is not None: raise OwnerFailure('STORAGE_FAILED', 'storage', 'COMMIT_UNCONFIRMED', True)
        await self.verify_metadata(blob)
        def verified():
            try: return work()
            except (OwnerFailure, OSError) as failure:
                self.detect(blob, failure)
                raise
        try: return await self.media._io(key, verified)
        finally: await self.flush()
