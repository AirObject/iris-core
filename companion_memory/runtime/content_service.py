"""Native bounded entry operations over the complete content owner assembly.

Public callers receive finite entry capabilities. Model execution, immutable
candidate transformation and local finalization have separate retained original
identities; recovery never calls a model. Native gate grants live only as long as
their actual original Provider consumers, including timed-out owners.
"""
from __future__ import annotations
from companion_memory.persistence.completion import CompletionScope, finish_owned, retain_completion, start_owned
import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from weakref import WeakValueDictionary
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.cognition.synthetic_graph import SyntheticGraphInput
from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
from companion_memory.cognition.synthetic_mixed import SyntheticMixedInput
from companion_memory.cognition.goal_proposals import SyntheticGoalInput
from companion_memory.ingress.events import canonical_event
from companion_memory.ingress.media_events import isolate_media_event, InvalidStateCombination
from companion_memory.persistence.schema import ValueTooLarge
from companion_memory.memory.formats import record, sequence
from companion_memory.memory.sources import decode_source
from companion_memory.persistence import Committed, Found, NotCommitted, RecoveryHandle, ResultBoundCommand, Unconfirmed, Value
from companion_memory.persistence.schema import freeze_value, InvalidValue, valid_identifier
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider import ProviderService
from .content_assembly import ContentAssembly, stable
from .content_gate import ContentGate
from .content_media import ContentMedia, MediaPolicy
from .results import RuntimeError, Rejected, NotCommitted as ContentNotCommitted, Unconfirmed as ContentUnconfirmed


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ContentEntryPort:
    """One trusted registered entry; no object/history/configuration authority."""
    _runtime: ContentRuntimeService
    _entry: str

    def __init__(self): raise TypeError('Entry capabilities are issued by trusted runtime setup.')

    async def accept_event(self, key: object, event: object):
        return await _entry_call(self, 'accept_event', key, event)

    async def run_learning(self, key: object):
        """Prepare media, freeze FIFO input and settle the one original learning work."""
        return await _entry_call(self, 'run_learning', key, None)


async def _entry_call(port, operation, key, argument):
    try: runtime = object.__getattribute__(port, '_runtime')
    except AttributeError: runtime = None
    if type(port) is not ContentEntryPort or type(runtime) is not ContentRuntimeService or runtime._entries.get(id(port)) is not port:
        return Rejected(RuntimeError('ACCESS_DENIED', operation, 'capability', 'BINDING_MISMATCH'))
    return await runtime.entry_call(port, operation, key, argument)


class ContentRuntimeService:
    """Trusted orchestration with bounded actual jobs and independent read services."""
    def __init__(self, assembly: ContentAssembly, provider: ProviderService,
                 candidates: SyntheticCandidateInput | SyntheticGraphInput | SyntheticMutationInput | SyntheticMixedInput | SyntheticGoalInput, learning_profile: str, media_policy: MediaPolicy | None):
        if type(assembly) is not ContentAssembly or not assembly._bound or type(provider) is not ProviderService or type(candidates) not in (SyntheticCandidateInput, SyntheticGraphInput, SyntheticMutationInput, SyntheticMixedInput, SyntheticGoalInput) or not valid_identifier(learning_profile) or type(candidates) is SyntheticGoalInput and not assembly.information_format:
            raise ValueError('Bound actual owners and explicit model/candidate inputs are required.')
        self.assembly = assembly; self.provider = provider; self.candidates = candidates; self.learning_profile = learning_profile
        self.settings = assembly.configuration.candidate.runtime
        self.provider._registration_limit = assembly.configuration.candidate.content.integer('memory.read_page_size')
        self.gate = ContentGate(self.settings.integer('runtime.max_active_entries') + assembly.configuration.candidate.content.integer('media.processing_concurrency'))
        from .content_modes import ContentFocus
        self.focus = ContentFocus(self)
        self.media_policy = media_policy; self.media: ContentMedia | None = None
        self._entries: WeakValueDictionary[int, ContentEntryPort] = WeakValueDictionary()
        self._jobs: dict[tuple[str, str, str], asyncio.Task] = {}; self._commands: set[asyncio.Task] = set()
        self._external_jobs: set[asyncio.Task[object]] = set()
        self._evidence_jobs: dict[asyncio.Task, tuple[str, str]] = {}
        self._learning_held = {}
        self._request_deadline: ContextVar[float | None] = ContextVar('content_request_deadline', default=None)
        self.state = 'RECOVERING'
        self._recovery_cursor: tuple[str, str] = ('media', '')
        self._recovery_job: asyncio.Task | None = None
        from .content_observation import ContentObservations
        self.observations = ContentObservations(self)
        from companion_memory.memory.maintenance_access import MemoryMaintenance
        from companion_memory.memory.service import MemoryService
        self.maintenance = MemoryMaintenance(self)
        from companion_memory.memory.source_access import SourceAccess
        from companion_memory.media.service import MediaService
        from companion_memory.memory.recovery import MemoryRecovery
        self._memory_recovery = MemoryRecovery(assembly.memory, assembly.media)
        self.sources = SourceAccess(assembly.memory, cast(MediaService, assembly.media) if type(assembly.media) is MediaService else None)
        self.memory = MemoryService(assembly.memory, lambda: None if self.state == 'READY' and self.gate.state in ('NORMAL', 'DRAINING') else 'DREAMING')

    def retain_external_work(self, task: asyncio.Task[object]) -> None:
        """Include admitted local business work in mode and shutdown cutoffs.

        The caller supplies its own actual completion task. Only its completion
        releases this registry; a logical result or global I/O count cannot.
        """
        self._external_jobs.add(task)
        task.add_done_callback(self._external_jobs.discard)

    @property
    def external_work_pending(self) -> bool:
        return bool(self._external_jobs)

    async def initialize(self):
        """Recover original local work before exposing admission; repeated calls join."""
        from .content_recovery import recover_original_work
        if self.state == 'READY': return Found(MappingProxyType({'state': 'READY'}))
        if self.state != 'RECOVERING' or self.provider.get_health().lifecycle != 'READY':
            return Rejected(RuntimeError('INVALID_STATE', 'initialize', 'state', 'NOT_READY'))
        if self.provider._resources is None or self.provider._resources.gate is not self.gate.binding:
            return Rejected(RuntimeError('ACCESS_DENIED', 'initialize', 'capability', 'BINDING_MISMATCH'))
        if self.media_policy is not None and self.media is None: self.media = ContentMedia(self, self.media_policy)
        async def recover():
            try:
                verified = await self._memory_recovery.advance()
                if type(verified) is not Found or record(verified.value)['state'] != 'MEMORY_VERIFIED': return verified
                candidates_verified = await self.assembly.cognition.verify_stored(self.assembly.configuration.candidate.content.integer('memory.read_page_size'),
                    time.monotonic() + self.settings.integer('runtime.recovery_timeout_ms') / 1000)
                if not candidates_verified: return Found(MappingProxyType({'state': 'RECOVERY_PENDING', 'owner': 'cognition'}))
                result = await recover_original_work(self)
                if type(result) is not Found or record(result.value)['state'] != 'ORIGINAL_WORK_VERIFIED': return result
                modes = await self.assembly.rows.read('mode_get', {'mode_id': 'instance_mode'})
                if not modes: return Rejected(RuntimeError('INVALID_STATE', 'initialize', 'state', 'NOT_READY'))
                if self.state != 'RECOVERING': return Rejected(RuntimeError('INVALID_STATE', 'initialize', 'state', 'SERVICE_CLOSED'))
                self.gate.publish_mode(cast(str, modes[0]['state']), cast(int, modes[0]['epoch']))
                if modes[0]['state'] == 'DREAM_PREPARING':
                    settled = await self.focus.drain(modes[0]['run_id'])
                    if type(settled) not in (Committed, Found): return settled
                elif modes[0]['state'] == 'DRAINING':
                    await self.focus.transfer_staged(modes[0]['run_id'])
                self.state = 'READY'
                return Found(MappingProxyType({'state': 'READY', 'storage_execution': 'ACTUAL', 'model_adapter': 'SIMULATED', 'candidate_origin': 'SYNTHETIC'}))
            except OwnerFailure as failure:
                return Rejected(RuntimeError(failure.code, 'initialize', failure.field, failure.reason, failure.cleanup_pending))
            except (InvalidValue, KeyError, IndexError, UnicodeError):
                return Rejected(RuntimeError('STORAGE_FAILED', 'initialize', 'storage', 'INTEGRITY_FAILURE'))
        if self._recovery_job is None or self._recovery_job.done(): self._recovery_job = asyncio.create_task(recover())
        done, _ = await asyncio.wait((self._recovery_job,), timeout=self.settings.integer('runtime.recovery_timeout_ms') / 1000)
        if not done: return Rejected(RuntimeError('TIMEOUT', 'initialize', 'state', 'DEADLINE_EXCEEDED', True))
        return self._recovery_job.result()

    def bind_entry(self, entry_id: str) -> ContentEntryPort:
        """Trusted host setup limits the capability to one registered entry ID."""
        if self.state != 'READY' or len(self._entries) >= self.settings.integer('runtime.max_active_entries') or not valid_identifier(entry_id): raise ValueError('Ready registered entry scope required.')
        port = object.__new__(ContentEntryPort)
        object.__setattr__(port, '_runtime', self); object.__setattr__(port, '_entry', entry_id)
        self._entries[id(port)] = port
        return port

    async def execute(self, kind: str, key: str, values: dict[str, object]) -> object:
        """Run a typed original command, retaining only its actual completion."""
        return await self._command(kind, key, values, confirm_only=False)

    async def reply_entry_status(self, entry_id: str, host_id: str) -> MappingProxyType[str, Value]:
        """Bounded current-entry terminal counts; never returns failed/history bodies."""
        if not await self.assembly.ingress.verify_host_entry(entry_id, host_id):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        rows = await self.assembly.rows.read('observe_entry', {'entry_id': entry_id})
        if len(rows) != 1: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return rows[0]

    async def confirm_command(self, kind: str, key: str, values: dict[str, object]) -> object:
        """Confirm original identity and receipt without entering a business handler."""
        return await self._command(kind, key, values, confirm_only=True)

    async def _command(self, kind: str, key: str, values: dict[str, object], *, confirm_only: bool) -> object:
        """Execute or resolve exactly one original local command with retained ownership."""
        evidence_owner = ('terminal', cast(str, values.get('request_id'))) if kind == 'store_occurrence_result' else ('unsent', cast(str, values.get('work_id'))) if kind == 'change_occurrence_admission' else None
        def on_ended(job=None):
            if job is not None: self._evidence_jobs.pop(job, None)
            if evidence_owner is None or evidence_owner in self._evidence_jobs.values(): return
            media = self.media.media if self.media else None
            if media is not None:
                registry = media.work.evidence if evidence_owner[0] == 'terminal' else media.work.unsent_evidence
                registry.pop(evidence_owner[1], None)
        definition = self.assembly.command_definition(kind)
        try: owned = freeze_value(definition.input_schema, {'operation_id': key, **values})
        except InvalidValue:
            if on_ended is not None: on_ended()
            return Rejected(RuntimeError('INVALID_INPUT', kind, 'input', 'INVALID_SHAPE'))
        command = ResultBoundCommand(1, {'operation_id': key, **values}, {r.event_slot: {'actor': 'content_scheduler'} for r in definition.required_audits})
        port = self.assembly.operations[kind]; handle = port.recovery_handle(key, command)
        if type(handle) is not RecoveryHandle:
            if on_ended is not None: on_ended()
            return handle
        if len(self._commands) >= self.settings.integer('runtime.max_active_entries'):
            if on_ended is not None: on_ended()
            return Rejected(RuntimeError('RESOURCE_BUSY', kind, 'state', 'ADMISSION_FULL'))
        cause_key, slot = self.assembly.causes.watch(kind, owned)
        information_change = any(a.owner_module in ('memory', 'buffers') for a in definition.required_audits) and self.assembly.memory.information is not None
        resolved_change = False
        change_key = stable('information_mutation', kind, key)
        async def execute():
            nonlocal resolved_change
            if confirm_only:
                original_receipt = await port.read_receipt(key)
                if type(original_receipt) is Found:
                    receipt = original_receipt.value
                    if receipt.fingerprint != handle.fingerprint or receipt.command_version != handle.command_version:
                        return Rejected(RuntimeError('IDEMPOTENCY_CONFLICT', kind, 'input', 'CONTENT_MISMATCH'))
                    return Committed(receipt, 'EXISTING')
                return original_receipt
            original = await port.resolve_operation(handle)
            if type(original) is NotCommitted and not self.remaining_request():
                return Rejected(RuntimeError('TIMEOUT', kind, 'state', 'DEADLINE_EXCEEDED'))
            if type(original) is NotCommitted and information_change: self.gate.begin_information_change(change_key)
            result = await port.execute(key, command) if type(original) is NotCommitted else original
            resolved_change = type(result) in (Committed, NotCommitted)
            return result
        task, outcome = start_owned(execute()); self._commands.add(task)
        retain_completion(task)
        if evidence_owner is not None: self._evidence_jobs[task] = evidence_owner
        def ended(job):
            if not job.cancelled(): job.exception()
            self._commands.discard(job); self.assembly.causes.release(cause_key)
            if information_change and resolved_change: self.gate.finish_information_change(change_key)
            on_ended(job)
            if not self._commands: self.assembly._verified_terminals.clear()
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((outcome,), timeout=self.remaining_request())
        if not done: return ContentUnconfirmed(handle, RuntimeError('TIMEOUT', kind, 'storage', 'DEADLINE_EXCEEDED', True))
        result = outcome.result()
        if type(result) is NotCommitted and slot.issue is not None:
            issue = slot.issue
            return ContentNotCommitted(RuntimeError(issue.code, kind, issue.field, issue.reason, issue.cleanup_pending))
        return result

    async def entry_call(self, port: ContentEntryPort, operation: str, key: object, argument: object):
        if self.state != 'READY': return Rejected(RuntimeError('INVALID_STATE', operation, 'state', 'NOT_READY'))
        if not valid_identifier(key): return Rejected(RuntimeError('INVALID_INPUT', operation, 'input', 'INVALID_IDENTIFIER'))
        identity = port._entry, operation, cast(str, key)
        if identity in self._jobs: return Rejected(RuntimeError('RESOURCE_BUSY', operation, 'state', 'OWNER_ACTIVE', True))
        if len(self._jobs) >= self.settings.integer('runtime.max_active_entries'):
            return Rejected(RuntimeError('RESOURCE_BUSY', operation, 'state', 'ADMISSION_FULL'))
        async def run():
            deadline = self._request_deadline.set(time.monotonic() + self.settings.integer('runtime.operation_timeout_ms') / 1000)
            try:
                if operation == 'accept_event': return await self.accept(port._entry, cast(str, key), argument)
                result = await self.learn_entry(port._entry, cast(str, key))
                self.observations.record_work(port._entry, result)
                return result
            except OwnerFailure as failure:
                return Rejected(RuntimeError(failure.code, operation, failure.field, failure.reason, failure.cleanup_pending))
            except ValueTooLarge:
                return Rejected(RuntimeError('INVALID_INPUT', operation, 'input', 'LIMIT_EXCEEDED'))
            except InvalidStateCombination:
                return Rejected(RuntimeError('INVALID_INPUT', operation, 'input', 'INVALID_STATE_COMBINATION'))
            except InvalidValue:
                return Rejected(RuntimeError('INVALID_INPUT', operation, 'input', 'INVALID_SHAPE'))
            finally: self._request_deadline.reset(deadline)
        task, outcome = start_owned(run()); self._jobs[identity] = task
        def ended(job):
            if not job.cancelled(): job.exception()
            self._jobs.pop(identity, None)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((outcome,), timeout=self.settings.integer('runtime.operation_timeout_ms') / 1000)
        if not done: return Rejected(RuntimeError('TIMEOUT', operation, 'state', 'DEADLINE_EXCEEDED', True))
        return outcome.result()

    def remaining_request(self) -> float:
        deadline = self._request_deadline.get()
        return self.settings.integer('runtime.operation_timeout_ms') / 1000 if deadline is None else max(0.0, deadline - time.monotonic())

    async def accept(self, entry_id: str, key: str, event: object):
        value = isolate_media_event(event, self.settings.integer('ingress.event_max_bytes'),
            occurrence_limit=self.assembly.configuration.candidate.content.integer('media.event_occurrence_limit'),
            text_limit=self.assembly.configuration.candidate.content.integer('media.interpretation_text_max_bytes'))
        if sequence(value['media']) and self.assembly.media is not None:
            from companion_memory.media.service import MediaService
            from companion_memory.ingress.events import event_identity
            media = self.assembly.media
            if type(media) is MediaService:
                entry = (await self.assembly.ingress.rows.read('entry', {'entry_id': entry_id}))[0]
                message_id, _, _ = event_identity((self.assembly.instance_id, cast(str, entry['host_id']), entry_id), value)
                await cast(MediaService, media).validate_event_reports(entry_id, message_id, value)
        kind = 'accept_media_event_with_media' if sequence(value['media']) else 'accept_media_event'
        return await self.execute(kind, stable('acceptance', entry_id, key), {'entry_id': entry_id, 'event': canonical_event(value).decode()})

    async def window_has_media(self, entry_id: str) -> bool:
        """Observe the fixed FIFO membership only to choose a predeclared audit mask."""
        from companion_memory.media.service import MediaService
        media = self.assembly.media
        if type(media) is not MediaService: return False
        media = cast(MediaService, media)
        entry = (await self.assembly.ingress.rows.read('entry', {'entry_id': entry_id}))[0]
        platform = self.assembly.configuration.candidate.platform(cast(str, entry['platform_id']))
        state = (await self.assembly.buffers.rows.read('get', {'entry_id': entry_id}))[0]
        positions = await self.assembly.buffers.rows.read('fifo', {'entry_id': entry_id, 'state': 'NORMAL',
            'limit': platform.count('target_count') + platform.count('recent_context_count')})
        messages = [position['message_id'] for position in positions]
        if state['history_id'] is not None: messages.append(state['history_id'])
        for mid in messages:
            if await media.rows.read('event_occurrences', {'message_id': mid}): return True
        return False

    async def learn_entry(self, entry_id: str, key: str) -> object:
        from .content_learning import learn_batch
        preparation_id = stable('preparation', self.assembly.configuration.database_id, entry_id, key)
        batch_id = stable('batch', preparation_id); run_id = stable('run', preparation_id)
        linked = await self.assembly.rows.read('learning_triggers_get', {'trigger_key': stable('select', preparation_id)})
        if linked: batch_id = cast(str, linked[0]['batch_id'])
        rows = await self.assembly.rows.read('batches_get', {'batch_id': batch_id})
        if rows: return await learn_batch(self, decode_source(cast(str, rows[0]['manifest'])), fresh=True, admission_event=stable('select', preparation_id))
        if self.gate.state not in ('NORMAL', 'DRAINING'):
            return Rejected(RuntimeError('MODE_BLOCKED', 'run_learning', 'state', 'DREAMING'))
        from .content_transfer import transfer_entry
        transferred = await transfer_entry(self, entry_id)
        if type(transferred) not in (Committed, Found) and not (type(transferred) is ContentNotCommitted and transferred.error is not None and transferred.error.reason == 'BUFFER_FULL'):
            return transferred
        rows = await self.assembly.rows.read('preparations_get', {'preparation_id': preparation_id})
        if not rows:
            state = (await self.assembly.buffers.rows.read('get', {'entry_id': entry_id}))[0]
            if state['reservation_id'] is not None:
                if state['reservation_kind'] == 'BATCH':
                    batch = (await self.assembly.rows.read('batches_get', {'batch_id': state['reservation_id']}))[0]
                    return await learn_batch(self, decode_source(cast(str, batch['manifest'])), fresh=True, admission_event=stable('select', preparation_id))
                return Found(MappingProxyType({'state': 'WAITING', 'reason': 'EXISTING_PREPARATION', 'preparation_id': state['reservation_id']}))
            kind = 'select_content_preparation_with_media' if await self.window_has_media(entry_id) else 'select_content_preparation'
            selected = await self.execute(kind, stable('select', preparation_id), {'entry_id': entry_id, 'preparation_id': preparation_id, 'batch_id': batch_id, 'run_id': run_id})
            if type(selected) is not Committed: return selected
            rows = await self.assembly.rows.read('preparations_get', {'preparation_id': preparation_id})
        prep = rows[0]
        if prep['phase'] in ('EXPIRED', 'INVALIDATED'):
            return Found(MappingProxyType({'state': prep['phase'], 'preparation_id': preparation_id}))
        now = self.assembly.utc_now_us()
        if now >= cast(int, prep['deadline_at_us']) or now < cast(int, prep['last_observed_at_us']):
            from .preparation_disposal import dispose_preparation
            return await dispose_preparation(self, preparation_id)
        if prep['phase'] == 'PARKED':
            resumed = await self.execute('resume_content_preparation', stable('resume_preparation', preparation_id, self.gate.epoch), {'preparation_id': preparation_id, 'expected_revision': prep['revision']})
            if type(resumed) is not Committed: return resumed
            prep = (await self.assembly.rows.read('preparations_get', {'preparation_id': preparation_id}))[0]
        if prep['phase'] == 'SELECTED':
            claimed = await self.execute('claim_content_preparation', stable('claim', preparation_id),
                {'preparation_id': preparation_id, 'expected_revision': prep['revision'], 'owner_generation': 1})
            if type(claimed) is not Committed: return claimed
        if self.media:
            prepared = await self.media.prepare(preparation_id)
            if type(prepared) is not Found or record(prepared.value)['state'] != 'MEDIA_READY': return prepared
        from .content_media import complete_preparation
        completed = await complete_preparation(self.assembly, self.execute, preparation_id)
        if type(completed) not in (Committed, Found): return completed
        prep = (await self.assembly.rows.read('preparations_get', {'preparation_id': preparation_id}))[0]
        from .preparation_format import decode_preparation
        manifest = decode_preparation(cast(str, prep['manifest']))
        has_media = any(sequence(record(m)['media']) for m in sequence(manifest['ordered_members']))
        frozen = await self.execute('freeze_content_batch_with_media' if has_media else 'freeze_content_batch', stable('freeze', preparation_id, prep['revision']),
            {'preparation_id': preparation_id, 'expected_revision': prep['revision'], 'owner_generation': prep['owner_generation']})
        if type(frozen) is not Committed:
            if type(frozen) is ContentNotCommitted and frozen.error is not None and frozen.error.reason == 'WINDOW_CHANGED':
                from .preparation_disposal import dispose_preparation
                return await dispose_preparation(self, preparation_id)
            return frozen
        batch = (await self.assembly.rows.read('batches_get', {'batch_id': batch_id}))[0]
        return await learn_batch(self, decode_source(cast(str, batch['manifest'])), fresh=frozen.source == 'NEW')

    def release_ended_capabilities(self) -> None:
        """Discard finished local grants without changing durable unknown work."""
        if not self._commands: self.assembly._verified_terminals.clear()
        for bid, (grant, port) in tuple(self._learning_held.items()):
            if port.consumers_ended():
                self.provider.revoke(port); self.gate.revoke(grant); self._learning_held.pop(bid)
        if self.media is not None: self.media.release_ended_capabilities()

    async def close(self) -> bool:
        """Revoke admission and keep the runtime alive until its actual jobs end."""
        self.state = 'CLOSING'; self.gate.close(); self._entries.clear(); self.observations.close(); self.maintenance.close(); self.sources.close(); self.focus.close()
        tasks = tuple(self._external_jobs) + tuple(self.focus.jobs) + tuple(self._jobs.values()) + tuple(self.maintenance.jobs.values()) + tuple(self._commands) + ((self._recovery_job,) if self._recovery_job is not None and not self._recovery_job.done() else ())
        if tasks: await asyncio.wait(tasks, timeout=self.settings.integer('runtime.close_timeout_ms') / 1000)
        if any(not task.done() for task in tasks) or self._external_jobs or self._jobs or self._commands or self.maintenance.jobs or self.sources._active or self.memory._active or self.observations.jobs or self.provider.get_health().cleanup_pending: return False
        self.release_ended_capabilities()
        if not self.memory.close(): return False
        self.state = 'CLOSED'
        return True
