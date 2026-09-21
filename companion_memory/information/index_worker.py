"""Finite local index page processing with original-step confirmation.

Only retrieval's persisted checkpoints select work. Memory supplies its current
objects and missing generation coverage; all writes retain their fixed native
command and necessary audits. Publication is a separate atomic owner command.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
import time
import secrets
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.retrieval.index import LocalIndex
from companion_memory.retrieval.lexical import normalize_material, object_text
from companion_memory.runtime.content_service import ContentRuntimeService
from .errors import InformationError, InformationRejected, InformationNotCommitted, rejected
from .management import ManagementPort
from companion_memory.persistence.record_primitives import Record, checked, identity, integer, text
from companion_memory.retrieval.index_inputs import SCHEMAS


@dataclass(frozen=True, slots=True)
class PendingIndexStep:
    kind: str
    key: str
    payload: Record


class LocalIndexWorker:
    """One admitted page, at most sixteen object writes and no waiting queue."""
    def __init__(self, runtime: ContentRuntimeService, index: LocalIndex, port: ManagementPort, owner_id: str):
        self.runtime, self.index, self.port, self.owner_id = runtime, index, port, owner_id
        self.jobs: set[asyncio.Task[object]] = set()
        self.pending: PendingIndexStep | None = None
        self.closed = False

    async def _step(self, kind: str, key: str, value: object) -> object:
        payload = checked(SCHEMAS[kind], value, 24576)
        self.pending = PendingIndexStep(kind, key, payload)
        result = await self.port.execute(kind, key, payload)
        if type(result) in (Committed, InformationNotCommitted, InformationRejected): self.pending = None
        return result

    async def run(self, generation_id: str, *, publish_if_ready: bool = False) -> object:
        deadline = bounded_deadline(time.monotonic(), 5)
        if self.closed: return rejected('index_worker', OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED'))
        if self.jobs: return rejected('index_worker', OwnerFailure('RESOURCE_BUSY', 'index', 'ADMISSION_FULL', True))
        token = secrets.token_hex(16)
        try: self.index.retain_work(token, generation_id)
        except OwnerFailure as failure: return rejected('index_worker', failure)
        async def work() -> object:
            with DeadlineScope(deadline):
                try:
                    if self.pending is not None:
                        pending = self.pending
                        result = await self.port.resolve(pending.kind, pending.key, pending.payload)
                        if type(result) in (Committed, InformationNotCommitted): self.pending = None
                        # A confirmed original step is a complete round; never
                        # treat confirmation as permission to start another step.
                        return result
                    reason = self.runtime.gate.information_operation_reason()
                    if reason is not None: raise OwnerFailure('MODE_BLOCKED', 'state', reason)
                    generation, page = await self.index.work_page(generation_id)
                    if page is None:
                        refs = await self.index.memory.index_pending_page(generation_id)
                        if not refs and not generation['pending_count']:
                            if publish_if_ready and generation['status'] == 'BUILDING':
                                return await self._step('index_publish', identity('publish_index_generation', generation_id, generation['revision']),
                                    {'generation_id': generation_id, 'expected_revision': generation['revision']})
                            return Found(MappingProxyType({'status': 'CAUGHT_UP', 'generation_id': generation_id}))
                        result = await self._step('index_claim', identity('claim_index_page', generation_id, generation['revision']),
                            {'generation_id': generation_id, 'expected_revision': generation['revision'], 'owner_id': self.owner_id})
                        if type(result) is not Committed: return result
                        generation, page = await self.index.work_page(generation_id)
                    if page is None or page['owner_id'] != self.owner_id:
                        raise OwnerFailure('ACCESS_DENIED', 'index', 'BINDING_MISMATCH')
                    remaining = 16 - integer(page['count'])
                    refs = await self.index.memory.index_pending_page(generation_id, text(page['cursor']) if page['cursor'] is not None else '')
                    for reference in refs[:remaining]:
                        if self.closed: raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')
                        oid = text(reference['object_id']); current = await self.index.memory.index_current(oid)
                        if (current is None) != (reference['action'] == 'REMOVE') or current is not None and current['revision'] != reference['revision']:
                            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                        material = normalize_material(object_text(current), byte_limit=8192, term_limit=4096) if current is not None else None
                        result = await self._step('index_apply_object', identity('apply_index_object', page['page_id'], page['revision'], oid, reference['revision']),
                            {'generation_id': generation_id, 'page_id': page['page_id'], 'expected_revision': page['revision'],
                                'object_id': oid, 'object_revision': reference['revision'], 'action': reference['action'],
                                'body_digest': sha256(encode_content(current, 4096)).hexdigest() if current is not None else '',
                                'normalized_text': material.text if material is not None else ''})
                        if type(result) is not Committed: return result
                        generation, page = await self.index.work_page(generation_id)
                        if page is None: raise OwnerFailure('STORAGE_FAILED', 'index', 'INTEGRITY_FAILURE')
                    more = await self.index.memory.index_pending_page(generation_id, text(page['cursor']) if page['cursor'] is not None else '')
                    return await self._step('index_confirm_page', identity('confirm_index_page', page['page_id'], page['revision']),
                        {'generation_id': generation_id, 'page_id': page['page_id'], 'expected_revision': page['revision'], 'scan_complete': not more})
                except OwnerFailure as failure: return rejected('index_worker', failure)
        task, outcome = start_owned(work()); self.jobs.add(task); self.runtime.retain_external_work(task)
        def ended(job: asyncio.Task[object]) -> None:
            if not job.cancelled(): job.exception()
            self.jobs.discard(job)
            self.index.release_work(token)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((outcome,), timeout=max(0, deadline - time.monotonic()))
        if done: return outcome.result()
        return InformationRejected(InformationError('TIMEOUT', 'index_worker', 'index', 'DEADLINE_EXCEEDED', True))

    def stop(self) -> None:
        self.closed = True
