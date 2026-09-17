"""Persistent mode transitions and native focus authority share the dispatch gate.

Closing admission precedes the original mode transaction. Pending remote or local
owners prevent focused readiness; only verified publication permits draining.
There is no fault-reset or operator resolution of an unknown request.
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, cast
from weakref import WeakValueDictionary
from companion_memory.persistence import (AuditFieldBinding, AuditResultBinding, Field, RecordSchema,
    ResultBoundCommandDefinition, SequenceSchema, Committed, Found, Value, RepositoryDefinition, Unconfirmed as StorageUnconfirmed,
    PersistenceService, UnitOfWork, NotFound)
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.deadlines import current_deadline,check_deadline
from companion_memory.persistence.schema import valid_identifier
from companion_memory.logging_service import AuditRequirement
from companion_memory.memory.formats import ID, INT, REVISION, enum, record
from .results import Rejected, RuntimeError, NotCommitted
from companion_memory.persistence import NotCommitted as StorageNotCommitted, Rejected as StorageRejected


class ContentPublication(Protocol):
    """Explicit separately owned publication evidence, including its static assembly."""
    repositories: tuple[RepositoryDefinition, ...]
    commands: tuple[ResultBoundCommandDefinition, ...]
    def bind(self, storage: PersistenceService, instance_id: str) -> None: ...
    def verify(self, uow: UnitOfWork, run_id: str, publication_id: str, configuration_id: str) -> bool: ...


class ContentModeCommands:
    """A fixed runtime audit covers every validated mode and preparation transition."""
    def __init__(self, assembly):
        self.assembly = assembly
        change = RecordSchema((Field('object_id', ID), Field('run_id', ID, nullable=True), Field('previous_state', ID),
            Field('state', ID), Field('previous_revision', INT), Field('revision', REVISION), Field('configuration_id', ID),
            Field('publication_id', ID, nullable=True)))
        targets = SequenceSchema(RecordSchema((Field('object_id', ID), Field('previous_revision', INT, nullable=True), Field('revision', INT))), 1, 1)
        result = RecordSchema((Field('change', change), Field('targets', targets)))
        layouts = (
            ('change_content_mode', (Field('action', enum('ENTER', 'READY', 'FINISH', 'FAULT', 'DRAINED')), Field('expected_epoch', REVISION), Field('run_id', ID), Field('publication_id', ID, nullable=True))),
            ('park_content_preparation', (Field('preparation_id', ID), Field('expected_revision', REVISION))),
            ('resume_content_preparation', (Field('preparation_id', ID), Field('expected_revision', REVISION))),
        )
        commands = []
        for name, fields in layouts:
            audit = AuditRequirement('runtime', 'runtime_mode', name.upper(), 1, ('APPLY',), change)
            binding = AuditResultBinding(audit.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
                AuditFieldBinding('change', 'RESULT', ('change',))))
            def handler(uow, value, kind=name): return assembly.run_handler(kind, uow, value, self.handle)
            commands.append(ResultBoundCommandDefinition('runtime', name, 1, RecordSchema((Field('operation_id', ID),) + fields), 1,
                result, assembly.repositories, (audit,), handler, RecordSchema((Field('actor', ID),)), (binding,)))
        self.commands = tuple(commands)

    def handle(self, name: str, uow: UnitOfWork, v: MappingProxyType[str, Value]):
        a = self.assembly; now = a.with_transaction_time(v)['now_us']
        mode = a._get('mode', uow, 'mode_id', 'instance_mode')
        if name != 'change_content_mode':
            prep = a._get('preparations', uow, 'preparation_id', v['preparation_id'])
            if prep['revision'] != v['expected_revision']: raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
            if name == 'park_content_preparation':
                if prep['phase'] not in ('SELECTED', 'CLAIMED', 'MEDIA_READY'):
                    raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
                state = 'PARKED'; generation = prep['owner_generation']
            else:
                if mode['state'] not in ('NORMAL', 'DRAINING') or prep['phase'] != 'PARKED' or now >= prep['deadline_at_us'] or now < prep['last_observed_at_us']:
                    raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
                state = 'MEDIA_READY' if prep['parked_from_phase'] == 'MEDIA_READY' else 'CLAIMED'
                generation = max(1, cast(int, prep['owner_generation']) + 1)
            a.rows.stage('preparations_update', uow, {**prep, 'phase': state, 'revision': cast(int, prep['revision']) + 1,
                'owner_generation': generation, 'mode_epoch': mode['epoch'], 'parked_from_phase': prep['phase'] if state == 'PARKED' else None, 'last_observed_at_us': max(cast(int, prep['last_observed_at_us']), cast(int, now)),
                'spent_ms': max(cast(int, prep['spent_ms']), max(0, cast(int, now) - cast(int, prep['started_at_us'])) // 1000)})
            return self.report(prep['preparation_id'], prep['run_id'], prep['phase'], state, prep['revision'], None)
        if mode['epoch'] != v['expected_epoch']: raise OwnerFailure('PRECONDITION_FAILED', 'state', 'REVISION_CONFLICT')
        action = cast(str, v['action']); previous = mode['state']
        required = {'ENTER': 'NORMAL', 'READY': 'DREAM_PREPARING', 'FINISH': 'DREAM_FOCUSED', 'FAULT': 'DREAM_PREPARING', 'DRAINED': 'DRAINING'}
        if required.get(action) != previous or action != 'ENTER' and mode['run_id'] != v['run_id']:
            raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
        if action == 'ENTER' and a.publication is None: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'state', 'PUBLICATION_MISSING')
        if action == 'READY':
            counts = a.rows.stage('focus_blockers', uow, {})[0]
            if counts['work'] or counts['preparations']: raise OwnerFailure('RESOURCE_BUSY', 'state', 'OWNER_ACTIVE')
            if a.media is not None and a.media.rows.stage('processing_count', uow, {})[0]['count']:
                raise OwnerFailure('RESOURCE_BUSY', 'state', 'OWNER_ACTIVE')
        if action == 'FINISH' and (a.publication is None or v['publication_id'] is None or not a.publication.verify(uow, v['run_id'], v['publication_id'], a.configuration.snapshot_id)):
            raise OwnerFailure('PRECONDITION_FAILED', 'state', 'PUBLICATION_MISSING')
        if action == 'DRAINED' and a.buffers.rows.stage('all_staged_count', uow, {})[0]['count']:
            raise OwnerFailure('RESOURCE_BUSY', 'state', 'OWNER_ACTIVE')
        state = {'ENTER': 'DREAM_PREPARING', 'READY': 'DREAM_FOCUSED', 'FINISH': 'DRAINING', 'FAULT': 'FAULTED', 'DRAINED': 'NORMAL'}[action]
        a.rows.stage('mode_update', uow, {**mode, 'state': state, 'epoch': cast(int, mode['epoch']) + 1,
            'run_id': v['run_id'], 'publication_id': v['publication_id'] if action == 'FINISH' else mode['publication_id'],
            'deadline_at_us': cast(int, now) + a.configuration.candidate.runtime.integer('runtime.focus_drain_timeout_ms') * 1000 if action == 'ENTER' else mode['deadline_at_us']})
        return self.report('instance_mode', v['run_id'], previous, state, mode['epoch'], v['publication_id'])

    def report(self, oid, run, previous, state, revision, publication):
        change = {'object_id': oid, 'run_id': run, 'previous_state': previous, 'state': state,
            'previous_revision': revision, 'revision': revision + 1, 'configuration_id': self.assembly.configuration.snapshot_id,
            'publication_id': publication}
        return {'change': change, 'targets': [{'object_id': oid, 'previous_revision': revision, 'revision': revision + 1}]}


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ContentFocusPort:
    """Trusted per-run mode authority; entry and HTTP roles cannot manufacture it."""
    _owner: ContentFocus
    _run_id: str
    def __init__(self): raise TypeError('Focus authority is issued by trusted setup.')
    async def enter_focus(self, key: str, expected_epoch: int): return await _call(self, 'ENTER', key, expected_epoch, None)
    async def finish_focus(self, key: str, expected_epoch: int, publication_id: str): return await _call(self, 'FINISH', key, expected_epoch, publication_id)


async def _call(port, action, key, epoch, publication):
    try: owner = object.__getattribute__(port, '_owner')
    except AttributeError: owner = None
    if type(port) is not ContentFocusPort or type(owner) is not ContentFocus or owner.ports.get(id(port)) is not port:
        return Rejected(RuntimeError('ACCESS_DENIED', 'focus', 'capability', 'BINDING_MISMATCH'))
    return await owner.call(port, action, key, epoch, publication)


class ContentFocus:
    """Finite retained mode jobs independently wait for the actual dispatch cutoff."""
    def __init__(self, runtime):
        self.runtime = runtime; self.ports: WeakValueDictionary[int, ContentFocusPort] = WeakValueDictionary()
        self.jobs: set[asyncio.Task] = set(); self.closed = False
    def bind(self, run_id: str):
        if self.closed or not valid_identifier(run_id) or len(self.ports) >= self.runtime.settings.integer('runtime.max_active_entries'):
            raise ValueError('Finite native focus scope required.')
        port = object.__new__(ContentFocusPort); object.__setattr__(port, '_owner', self); object.__setattr__(port, '_run_id', run_id)
        self.ports[id(port)] = port
        return port
    async def publish(self, result):
        if type(result) is Committed:
            change = record(record(result.receipt.result)['change'])
            self.runtime.gate.publish_mode(cast(str, change['state']), cast(int, change['revision']))
        return result
    async def call(self, port, action, key, epoch, publication):
        r = self.runtime
        if self.closed or r.state != 'READY': return Rejected(RuntimeError('INVALID_STATE', 'focus', 'state', 'NOT_READY'))
        if not valid_identifier(key) or type(epoch) is not int or epoch < 1 or publication is not None and not valid_identifier(publication):
            return Rejected(RuntimeError('INVALID_INPUT', 'focus', 'input', 'INVALID_SHAPE'))
        if self.jobs: return Rejected(RuntimeError('RESOURCE_BUSY', 'focus', 'state', 'OWNER_ACTIVE', True))
        task = asyncio.create_task(self.perform(port._run_id, action, key, epoch, publication)); self.jobs.add(task)
        def ended(job):
            if not job.cancelled(): job.exception()
            self.jobs.discard(job)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((task,), timeout=r.settings.integer('runtime.operation_timeout_ms') / 1000)
        if not done: return Rejected(RuntimeError('TIMEOUT', 'focus', 'state', 'DEADLINE_EXCEEDED', True))
        return task.result()
    async def perform(self, run, action, key, epoch, publication):
        r = self.runtime
        values = {'action': action, 'expected_epoch': epoch, 'run_id': run, 'publication_id': publication}
        if action == 'ENTER':
            if r.assembly.publication is None: return Rejected(RuntimeError('CAPABILITY_UNAVAILABLE', 'focus', 'state', 'PUBLICATION_MISSING'))
            original = await r.confirm_command('change_content_mode', key, values)
            if type(original) is Committed:
                from .content_assembly import stable
                # Confirm the original completed public result before any current
                # mode read or temporary cutoff. This is a historical read only.
                ready = await r.assembly.operations['change_content_mode'].read_receipt(stable('focus_ready', run))
                if type(ready) is Found: return Committed(ready.value, 'EXISTING')
                if type(ready) is not NotFound: return ready
                r.gate.close_ordinary()
                result = original
            elif type(original) is NotFound:
                r.gate.close_ordinary()
                result = await r.execute('change_content_mode', key, values)
            else:
                if type(original) is StorageUnconfirmed: r.gate.close_ordinary()
                return original
        else:
            result = await r.execute('change_content_mode', key, values)
        if type(result) is not Committed:
            if type(result) in (NotCommitted, StorageNotCommitted, Rejected, StorageRejected) and not getattr(getattr(result, 'error', None), 'cleanup_pending', False) and r.assembly.storage.get_health().lifecycle == 'READY':
                try: mode = (await r.assembly.rows.read('mode_get', {'mode_id': 'instance_mode'}))[0]
                except OwnerFailure: return result
                r.gate.resolve_cutoff(mode['state'], mode['epoch'])
            return result
        # An original receipt is a historical confirmation. It must never
        # republish its old mode over a later completed run.
        try: mode = (await r.assembly.rows.read('mode_get', {'mode_id': 'instance_mode'}))[0]
        except OwnerFailure: return result
        r.gate.resolve_cutoff(mode['state'], mode['epoch'])
        if action == 'ENTER' and (mode['run_id'] != run or mode['state'] not in ('DREAM_PREPARING', 'DREAM_FOCUSED')):
            from .content_assembly import stable
            original = await r.assembly.operations['change_content_mode'].read_receipt(stable('focus_ready', run))
            return Committed(original.value, 'EXISTING') if type(original) is Found else result
        if action == 'ENTER':
            from .content_assembly import stable
            original = await r.assembly.operations['change_content_mode'].read_receipt(stable('focus_ready', run))
            if type(original) is Found: return Committed(original.value, 'EXISTING')
            return await self.drain(run)
        try: await self.transfer_staged(run)
        except OwnerFailure:
            # Publication and mode confirmation survive a later FIFO read failure.
            # Durable staged rows retain the work for the next lifecycle trigger.
            pass
        return result
    async def drain(self, run):
        from .content_assembly import stable
        r = self.runtime; deadline = time.monotonic() + r.settings.integer('runtime.focus_drain_timeout_ms') / 1000
        while True:
            mode = (await r.assembly.rows.read('mode_get', {'mode_id': 'instance_mode'}))[0]
            if mode['state'] != 'DREAM_PREPARING' or mode['run_id'] != run:
                return Found(MappingProxyType({'state': mode['state']}))
            cursor = ''; parked_all = True
            while True:
                rows = await r.assembly.rows.read('preparations_page', {'after': cursor, 'limit': r.settings.integer('runtime.read_page_size')})
                if not rows: break
                for metadata in rows:
                    cursor = cast(str, metadata['preparation_id'])
                    prep = (await r.assembly.rows.read('preparations_get', {'preparation_id': cursor}))[0]
                    if prep['phase'] in ('SELECTED', 'CLAIMED', 'MEDIA_READY'):
                        parked = await r.execute('park_content_preparation', stable('park_preparation', run, cursor), {'preparation_id': cursor, 'expected_revision': prep['revision']})
                        if type(parked) is not Committed:
                            if type(parked) is StorageUnconfirmed or getattr(getattr(parked, 'error', None), 'cleanup_pending', False):
                                parked_all = False; break
                            return parked
                if not parked_all: break
            pending = not parked_all or bool(r.external_work_pending or r._jobs or r.provider.get_health().in_flight or r.provider.get_health().cleanup_pending or r.assembly.storage.get_health().writes_in_flight)
            if not pending:
                await self.settle_media(deadline)
                await self.settle_learning(deadline)
                ready = await r.execute('change_content_mode', stable('focus_ready', run), {'action': 'READY', 'expected_epoch': mode['epoch'], 'run_id': run, 'publication_id': None})
                if type(ready) is Committed: return await self.publish(ready)
            if time.monotonic() >= deadline or r.assembly.utc_now_us() >= mode['deadline_at_us']:
                return await self.publish(await r.execute('change_content_mode', stable('focus_fault', run), {'action': 'FAULT', 'expected_epoch': mode['epoch'], 'run_id': run, 'publication_id': None}))
            await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))
    async def settle_media(self, deadline):
        """Original local confirmation can release unsent or ended cutoff consumers."""
        r = self.runtime
        if r.media is None: return
        cursor = ''
        while time.monotonic() < deadline:
            rows = await r.media.media.rows.read('work_active_page', {'after': cursor, 'limit': r.settings.integer('runtime.read_page_size')})
            if not rows: return
            for metadata in rows:
                if time.monotonic() >= deadline: return
                cursor = metadata['work_id']
                work = (await r.media.media.rows.read('work_get', {'work_id': cursor}))[0]
                await r.media.drive(work, fresh=False)

    async def settle_learning(self, deadline):
        """Only original-query closure or local terminal work can clear the cutoff."""
        from .content_learning import learn_batch
        from companion_memory.memory.sources import decode_source
        r = self.runtime; cursor = ''
        while time.monotonic() < deadline:
            rows = await r.assembly.rows.read('work_page', {'after': cursor, 'limit': r.settings.integer('runtime.read_page_size')})
            if not rows: return
            for metadata in rows:
                if time.monotonic() >= deadline: return
                cursor = metadata['batch_id']
                if metadata['phase'] not in ('REQUEST_ASSOCIATED', 'CANDIDATE_STORED'): continue
                batch = (await r.assembly.rows.read('batches_get', {'batch_id': cursor}))[0]
                await learn_batch(r, decode_source(cast(str, batch['manifest'])), fresh=False)

    async def transfer_staged(self, run):
        """Advance finite local FIFO pages; an occupied normal queue keeps DRAINING."""
        from .content_assembly import stable
        from .content_transfer import transfer_entry
        r = self.runtime; deadline = current_deadline(r.settings.integer('runtime.operation_timeout_ms') / 1000)
        cursor = ''
        while time.monotonic() < deadline:
            entries = await r.assembly.buffers.rows.read('page', {'after': cursor, 'limit': r.assembly.configuration.candidate.content.integer('memory.read_page_size')})
            if not entries: break
            for entry in entries:
                if time.monotonic() >= deadline: return
                cursor = entry['entry_id']
                result = await transfer_entry(r, cursor)
                if type(result) not in (Committed, Found): return
        check_deadline()
        if (await r.assembly.buffers.rows.read('all_staged_count', {}))[0]['count']: return
        mode = (await r.assembly.rows.read('mode_get', {'mode_id': 'instance_mode'}))[0]
        if mode['state'] == 'DRAINING' and mode['run_id'] == run:
            await self.publish(await r.execute('change_content_mode', stable('content_drained', run),
                {'action': 'DRAINED', 'expected_epoch': mode['epoch'], 'run_id': run, 'publication_id': None}))

    def close(self):
        self.closed = True; self.ports.clear()
