"""Durable background run control with original-key confirmation.

This owner records coordination facts. It does not call models, mutate another
owner or manufacture a mode transition. Focused entry and business effects must
participate through their native runtime and domain transactions.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import RLock
import time
from hashlib import sha256
from collections.abc import Callable,Awaitable
from types import MappingProxyType
from typing import cast

from companion_memory.configuration.dream_persistence import StoredDreamConfiguration, stored_dream_configuration_issue
from companion_memory.persistence import (Committed, Found, NotCommitted, NotFound, PersistenceService,
    ResultBoundCommand, ResultBoundCommandDefinition, UnitOfWork)
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope, current_deadline, check_deadline
from companion_memory.persistence.daily_records import DailyRows, identity
from companion_memory.persistence.owned_statements import OwnerCauses, OwnerFailure
from companion_memory.persistence.schema import Field, RecordSchema, InvalidValue, BoundedTextSchema, freeze_value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.semantic_records import ID, P, N, Record, fields, enum
from .records import TABLES, RUN_STATES, TERMINAL_STATES, dream_catalog, validate_run
from .results import INTENT, audits, result, result_schema, target


@dataclass(frozen=True, slots=True)
class _DispatchState:
    run_id: str
    revision: int
    mode_epoch: int
    generation: int


CONTROL_KINDS=frozenset(('initialize_dream_control','start_background_dream','start_focused_dream','pause_dream',
    'resume_dream','request_dream_abort','abort_background_dream','exit_focused_dream','complete_dream_exit','abort_dream','finish_focused_dream','finish_background_dream'))

@dataclass(frozen=True,slots=True,init=False)
class ControlIntent:
    owner:DreamControl
    kind:str
    key:str
    digest:str
    generation:int
    def __init__(self):raise TypeError('The native control owner issues entry intent.')


class DreamControl:
    """One finite owner, with retained actual tasks and immutable original keys."""

    def __init__(self):
        self.catalog = dream_catalog()
        self.causes = OwnerCauses()
        self.bound = False
        self.closed = False
        self._control_generation = 0
        self._executing_generation = 0
        self._readers:set[asyncio.Task]=set()
        self._dispatch: _DispatchState | None = None
        self._control_lock = RLock()
        self._task: asyncio.Task | None = None
        self._serial = asyncio.Lock()
        self._waiting = 0
        self.checkpoint: Callable[[], None] | None = None
        self.now: Callable[[], int] | None = None
        self.focused_ready: Callable[[UnitOfWork,Record],int] | None = None
        self.run_admission: Callable[[UnitOfWork,Record],None] | None = None
        self.resume_readiness: Callable[[UnitOfWork,Record],None] | None = None
        self.exit_readiness: Callable[[UnitOfWork,Record],None] | None = None
        common = fields(run_id=ID, expected_revision=P, mode_epoch=N)
        layouts = {
            'initialize_dream_control': (),
            'start_background_dream': fields(run_id=ID, expected_revision=P, mode_epoch=N,
                trigger=enum('MANUAL', 'SCHEDULED'), local_date=(BoundedTextSchema(10),)),
            'pause_dream': common,
            'resume_dream': common,
            'abort_background_dream': common,
            'request_dream_abort': common,
        }
        definitions = []
        for name, parameters in layouts.items():
            requirements, bindings = audits(name, ('dream',))

            def handle(uow: UnitOfWork, values: Record, operation: str = name):
                try:
                    return self.handle(operation, uow, values)
                except OwnerFailure as failure:
                    self.causes.record(operation, values, failure)
                    raise

            definitions.append(ResultBoundCommandDefinition('dream', name, 1,
                RecordSchema((Field('operation_id', ID),) + parameters), 1,
                result_schema(('dream',), ('INITIALIZED', *RUN_STATES)),
                (self.catalog.definition,), requirements, handle, INTENT, bindings))
        self.commands = tuple(definitions)

    def bind(self, storage: PersistenceService, configuration: StoredDreamConfiguration,
             *, checkpoint: Callable[[], None], now: Callable[[], int]) -> None:
        """Bind the exact new configuration; reconstructing never enables sends."""
        if (self.bound or self.closed or stored_dream_configuration_issue(configuration) is not None
                or not callable(checkpoint) or not callable(now)):
            raise InvalidValue()
        self.storage = storage
        self.configuration = configuration
        self.checkpoint = checkpoint
        self.now = now
        self.rows = DailyRows(self.catalog, TABLES, storage, configuration.database_id,
                              configuration.scope_id, configuration.snapshot_id)
        self.operations = {d.operation_kind: storage.bind_operation(d, configuration.scope_id) for d in self.commands}
        self.root_id = identity('dream-schedule', configuration.database_id, configuration.scope_id)
        from companion_memory.persistence.owned_statements import BoundStatements
        self.provider_views=BoundStatements(self.catalog,storage,'provider')
        self.bound = True

    def _base(self, object_id: str, now: int) -> dict[str, object]:
        return {'format_version': 1, 'object_id': object_id, 'revision': 1,
            'database_id': self.configuration.database_id, 'instance_id': self.configuration.scope_id,
            'config_snapshot_id': self.configuration.snapshot_id, 'created_at_us': now, 'updated_at_us': now}

    def _operation(self, uow: UnitOfWork) -> dict[str, str]:
        original = self.storage.dream_operation_context(uow, self.catalog.definition)
        return {name: getattr(original, name) for name in
                ('owner_namespace', 'operation_kind', 'scope_id', 'operation_key')}

    def _ready(self, uow: UnitOfWork) -> int:
        if not self.bound or self.closed or self.now is None or self.checkpoint is None:
            raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')
        self.checkpoint()
        uow.require_commit_permission(lambda: not self.closed)
        now = self.now()
        if type(now) is not int or not 0 <= now <= (1 << 63) - 1:
            raise InvalidValue()
        return now

    @property
    def dispatch_enabled(self) -> bool:
        """Observe the last fenced current-state grant, never a historical receipt."""
        with self._control_lock:
            grant = self._dispatch
            return not self.closed and grant is not None and grant.generation == self._control_generation

    def _control_intent(self, *, revoke: bool) -> int:
        with self._control_lock:
            self._control_generation += 1
            if revoke:
                self._dispatch = None
            return self._control_generation

    def begin_intent(self,kind:str,key:str,values:dict[str,object],actor:str) -> ControlIntent:
        """Seal a validated entry generation before lookups or admission can suspend."""
        if kind not in CONTROL_KINDS or not self.bound or self.closed:raise InvalidValue()
        shape_kind='pause_dream' if kind=='abort_dream' else kind
        definition=next(d for d in self.commands if d.operation_kind==shape_kind)
        payload=freeze_value(definition.input_schema,{'operation_id':key,**values})
        intent=freeze_value(INTENT,{'actor':actor})
        digest=sha256(encode_content((kind,payload,intent),16384)).hexdigest()
        generation=self._control_intent(revoke=kind!='resume_dream')
        issued=object.__new__(ControlIntent)
        for name,value in (('owner',self),('kind',kind),('key',key),('digest',digest),('generation',generation)):
            object.__setattr__(issued,name,value)
        return issued

    async def abort_request(self,run_id:str):
        return await self.rows.read('abort_requests',identity('dream-abort',self.configuration.database_id,self.configuration.scope_id,run_id))

    def revoke_dispatch(self) -> None:
        """Fence a validated trusted stopping intent before any asynchronous lookup."""
        self._control_intent(revoke=True)

    def participate_completion(self,uow:UnitOfWork,run_id:str,frozen_revision:int,epoch:int) -> Record:
        """Merge one original request outcome without undoing newer stop intent.

        Only native consumers call this after retaining the exact frozen input;
        they must independently prove the request, work, step and Provider result.
        """
        current=self.rows.get('runs',uow,run_id)
        if (current is None or cast(int,current['revision'])<frozen_revision or current['mode_epoch']!=epoch
                or current['state'] in TERMINAL_STATES or current['active_step_id'] is None):
            raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
        return validate_run(current)

    def require_dispatch(self, uow: UnitOfWork, run_id: str, revision: int, epoch: int) -> Record:
        """Consume only this runtime's current run/revision/epoch-bound permission.

        A queued stopping intent immediately invalidates the generation. The
        same generation is checked again before the short transaction commits.
        Already sent work uses its original confirmation path, not this grant.
        """
        current = self.participate_run(uow, run_id, revision, epoch)
        if self.run_admission is not None:self.run_admission(uow,current)
        with self._control_lock:
            grant = self._dispatch
            if (grant is None or self.closed or grant.generation != self._control_generation
                    or (grant.run_id, grant.revision, grant.mode_epoch) != (run_id, revision, epoch)
                    or current['state'] != 'RUNNING'):
                raise OwnerFailure('ACCESS_DENIED', 'run', 'RUN_NOT_RESUMABLE')
        uow.require_commit_permission(lambda: self._dispatch is grant and self.dispatch_enabled)
        return current

    def require_provider_dispatch(self,uow:UnitOfWork,expected:Record) -> None:
        """Consume the same grant in Provider's separately scoped registration."""
        from companion_memory.persistence.text_records import decode_row
        from .records import RUN
        raw=self.provider_views.stage('provider_read_runs',uow,{'caller_scope':self.configuration.scope_id,'object_id':expected['run_id']})
        if len(raw)!=1:raise InvalidValue()
        current=validate_run(decode_row(raw[0],RUN,8192,self.configuration.database_id,self.configuration.scope_id,self.configuration.snapshot_id))
        with self._control_lock:
            grant=self._dispatch
            if current!=expected or grant is None or not self.dispatch_enabled or (grant.run_id,grant.revision,grant.mode_epoch)!=(current['run_id'],current['revision'],current['mode_epoch']):
                raise OwnerFailure('ACCESS_DENIED','run','RUN_NOT_RESUMABLE')
        uow.require_commit_permission(lambda:self._dispatch is grant and self.dispatch_enabled)

    @staticmethod
    def settled(run: Record) -> bool:
        """A run label never substitutes for evidence of resource completion."""
        return (run['local_confirmation'] == 'CONFIRMED' and run['remote_result'] in ('NONE', 'KNOWN')
                and not run['cleanup_pending'] and run['active_step_id'] is None)

    def participate_run(self, uow: UnitOfWork, run_id: str, revision: int, epoch: int) -> Record:
        """Fence one original run in a native domain transaction."""
        self._ready(uow)
        current = self.rows.get('runs', uow, run_id)
        if current is None or current['revision'] != revision or current['mode_epoch'] != epoch:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        return validate_run(current)

    def handle(self, kind: str, uow: UnitOfWork, values: Record) -> Record:
        now = self._ready(uow)
        original = self._operation(uow)
        if original['operation_key'] != values['operation_id']:
            raise InvalidValue()
        root = self.rows.get('schedule', uow, self.root_id)
        targets: list[dict[str, object]] = []
        if kind == 'initialize_dream_control':
            if root is not None:
                raise OwnerFailure('PRECONDITION_FAILED', 'state', 'STATE_MISMATCH')
            self.rows.write('schedule', uow, self._base(self.root_id, now) | {
                'schedule_revision': 1, 'last_local_date': None, 'active_run_id': None,'self_cursor':'','object_cursor':'','expiry_after_us':0,'expiry_after_id':'','impact_cursor':0,
                'last_run_id': None, 'dispatch': 'PAUSED', 'last_operation': original})
            targets.append(target(self.root_id, 1))
            state = 'INITIALIZED'
        elif kind == 'start_background_dream':
            run,created=self.start_run(uow,values,now,original,root,mode='BACKGROUND',state='RUNNING',epoch=cast(int,values['mode_epoch']))
            targets.extend(created);state='RUNNING'
        else:
            run = self.participate_run(uow, cast(str, values['run_id']), cast(int, values['expected_revision']), cast(int, values['mode_epoch']))
            if root is None or root['active_run_id'] != run['run_id'] or run['state'] in TERMINAL_STATES:
                raise OwnerFailure('PRECONDITION_FAILED', 'run', 'RUN_NOT_RESUMABLE')
            changes: dict[str, object] = {}
            if kind == 'request_dream_abort':
                abort_id=identity('dream-abort',self.configuration.database_id,self.configuration.scope_id,run['run_id'])
                if self.rows.get('abort_requests',uow,abort_id) is not None:raise OwnerFailure('PRECONDITION_FAILED','run','ALREADY_COMMITTED')
                self.rows.write('abort_requests',uow,self._base(abort_id,now)|{'run_id':run['run_id'],'requested_revision':run['revision'],
                    'mode_epoch':run['mode_epoch'],'original_operation':original})
                targets.append(target(abort_id,1))
                state='RECOVERY_REQUIRED' if run['state']=='RECOVERY_REQUIRED' else 'PAUSED' if self.settled(run) else 'PAUSING'
                changes['end_reason']='ABORTED_SAFELY'
            elif kind == 'pause_dream':
                if run['state'] not in ('PREPARING', 'RUNNING', 'FINALIZING', 'PAUSING'):
                    raise OwnerFailure('PRECONDITION_FAILED', 'run', 'RUN_NOT_RESUMABLE')
                state = 'PAUSED' if self.settled(run) else 'PAUSING'
            elif kind == 'resume_dream':
                if self.rows.get('abort_requests',uow,identity('dream-abort',self.configuration.database_id,self.configuration.scope_id,run['run_id'])) is not None:raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
                generation=self._executing_generation
                with self._control_lock:
                    if generation!=self._control_generation:raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
                uow.require_commit_permission(lambda:generation==self._control_generation and not self.closed)
                if run['state'] not in ('PREPARING','RUNNING','PAUSING','PAUSED'):
                    raise OwnerFailure('PRECONDITION_FAILED', 'run', 'RUN_NOT_RESUMABLE')
                if self.resume_readiness is not None:self.resume_readiness(uow,run)
                elif not self.settled(run):raise OwnerFailure('PRECONDITION_FAILED','run','RUN_NOT_RESUMABLE')
                if now >= cast(int, run['deadline_at_us']):
                    raise OwnerFailure('TIMEOUT', 'run', 'DEADLINE_EXCEEDED')
                if now < cast(int, run['updated_at_us']):
                    raise OwnerFailure('PRECONDITION_FAILED', 'clock', 'CLOCK_REGRESSED')
                if run['mode']=='FOCUSED':
                    if self.focused_ready is None:raise InvalidValue()
                    changes['mode_epoch']=self.focused_ready(uow,run)
                state = 'RUNNING'
            elif kind == 'abort_background_dream':
                if run['mode'] != 'BACKGROUND' or not self.settled(run):
                    raise OwnerFailure('PRECONDITION_FAILED', 'run', 'RUN_NOT_RESUMABLE')
                if self.exit_readiness is not None:self.exit_readiness(uow,run)
                state = 'ABORTED'
                exit_id = identity('dream-exit', self.configuration.database_id, self.configuration.scope_id, run['run_id'])
                self.rows.write('exits', uow, self._base(exit_id, now) | {
                    'run_id': run['run_id'], 'mode_epoch': run['mode_epoch'], 'outcome': 'ABORTED_SAFELY',
                    'publication_id': None, 'publication_revision': None, 'original_operation': original})
                targets.append(target(exit_id, 1))
                changes.update(exit_result='ABORTED_SAFELY', end_reason='ABORTED_SAFELY')
                root_revision = cast(int, root['revision'])
                self.rows.write('schedule', uow, dict(root) | {'revision': root_revision + 1,
                    'updated_at_us': max(now, cast(int, root['updated_at_us'])), 'active_run_id': None,
                    **{k:run[k] for k in ('self_cursor','object_cursor','expiry_after_us','expiry_after_id','impact_cursor')},'last_operation': original}, root_revision)
                targets.append(target(self.root_id, root_revision + 1, root_revision))
            else:
                raise InvalidValue()
            revision = cast(int, run['revision'])
            changed = validate_run(dict(run) | changes | {'state': state, 'revision': revision + 1,
                'updated_at_us': max(now, cast(int, run['updated_at_us'])), 'last_operation': original})
            self.rows.write('runs', uow, changed, revision)
            targets.append(target(cast(str, run['run_id']), revision + 1, revision))
        return result(cast(str, values['operation_id']), state, {'dream': {
            'rows_changed': self.storage.transaction_row_changes(uow)['dream'], 'targets': targets}})


    def start_run(self,uow:UnitOfWork,values:Record,now:int,original,root,*,mode:str,state:str,epoch:int):
        targets=[]
        if root is None or root['revision'] != values['expected_revision']:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        if root['active_run_id'] is not None:
            raise OwnerFailure('RESOURCE_BUSY', 'run', 'RUN_ACTIVE')
        if values['trigger'] == 'SCHEDULED':
            from zoneinfo import ZoneInfo
            from companion_memory.runtime.dream_clock import due_date
            schedule=self.configuration.candidate.text.record('dream.schedule')
            due=due_date(now,ZoneInfo(cast(str,self.configuration.candidate.text.value('runtime.timezone'))),
                cast(str,schedule['local_time']),cast(str|None,root['last_local_date']))
            if not schedule['enabled'] or due is None or values['local_date']!=due.local_date:
                raise OwnerFailure('PRECONDITION_FAILED', 'date', 'STATE_MISMATCH')
        limits = self.configuration.candidate.text.record('dream.resources')
        run = validate_run(self._base(cast(str, values['run_id']), now) | {
            'run_id': values['run_id'], 'scope': self.configuration.scope_id,
            'schedule_revision': root['schedule_revision'], 'local_date': values['local_date'],
            'mode': mode, 'trigger': values['trigger'], 'state': state,
            'mode_epoch': epoch, 'deadline_at_us': now + cast(int, limits['run_timeout_ms']) * 1000,
            'step_deadline_at_us': None, 'object_cursor': root['object_cursor'], 'expiry_after_us':root['expiry_after_us'],'expiry_after_id':root['expiry_after_id'], 'impact_cursor': root['impact_cursor'], 'self_cursor': root['self_cursor'],
            'objects_used': 0, 'edges_used': 0, 'model_calls_used': 0, 'steps_completed': 0,
            'steps_deferred': 0, 'active_step_id': None, 'remaining_work': True, 'coverage': 'NOT_SCANNED',
            'local_confirmation': 'CONFIRMED', 'remote_result': 'NONE', 'cleanup_pending': False,
            'exit_result': None, 'persona_publication_id': None, 'end_reason': 'NONE',
            'original_operation': original, 'last_operation': original})
        self.rows.write('runs', uow, run)
        root_revision = cast(int, root['revision'])
        self.rows.write('schedule', uow, dict(root) | {'revision': root_revision + 1,
            'updated_at_us': max(now, cast(int, root['updated_at_us'])),
            'active_run_id': run['run_id'], 'last_run_id': run['run_id'], 'last_operation': original,
            'last_local_date': run['local_date'] if run['trigger'] == 'SCHEDULED' else root['last_local_date']}, root_revision)
        targets.extend((target(cast(str, run['run_id']), 1), target(self.root_id, root_revision + 1, root_revision)))
        return run,targets

    async def execute(self, kind: str, key: str, values: dict[str, object], *, actor: str, intent:ControlIntent|None=None):
        """Trusted host invocation; a timed-out actual job retains the only slot."""
        if not self.bound or self.closed or kind not in self.operations:
            raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        resources = self.configuration.candidate.text.record('dream.resources')
        deadline = current_deadline(cast(int, resources['operation_timeout_ms']) / 1000)
        definition = next(d for d in self.commands if d.operation_kind == kind)
        command = ResultBoundCommand(1, {'operation_id': key, **values},
            {audit.event_slot: {'actor': actor} for audit in definition.required_audits})
        # Validate the exact immutable operation before it can revoke authority.
        self.operations[kind].recovery_handle(key, command)
        if intent is None and kind in CONTROL_KINDS:intent=self.begin_intent(kind,key,values,actor)
        if intent is not None:
            if (type(intent) is not ControlIntent or intent.owner is not self or intent.key!=key
                    or intent.kind!=kind and not (intent.kind=='abort_dream' and kind in ('request_dream_abort','abort_background_dream','exit_focused_dream'))):raise InvalidValue()
            payload=freeze_value(definition.input_schema,{'operation_id':key,**values})
            if intent.digest!=sha256(encode_content((intent.kind,payload,freeze_value(INTENT,{'actor':actor})),16384)).hexdigest():raise InvalidValue()
            generation=intent.generation
        else:generation=self._control_generation
        if self._waiting >= cast(int, resources['completed_observations']):
            raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL')
        self._waiting += 1
        acquired = False
        try:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                await asyncio.wait_for(self._serial.acquire(), remaining)
            except TimeoutError:
                raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED') from None
            acquired = True
            if time.monotonic() >= deadline:
                raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED')
            if self.closed or self._task is not None:
                raise OwnerFailure('RESOURCE_BUSY', 'state', 'CLEANUP_PENDING', self._task is not None)
            async def apply():
                with DeadlineScope(deadline):
                    check_deadline()
                    self._executing_generation=generation
                    grant_before=self._dispatch
                    outcome, cause = await self.causes.execute_original(self.operations[kind], definition, key, command)
                    if type(outcome) is NotCommitted and cause is not None:
                        raise cause
                    if type(outcome) is Committed and outcome.source=='NEW' and 'run_id' in values and kind not in ('plan_dream_candidate','retire_dream_material','retire_periodic_material'):
                        with self._control_lock:
                            if generation==self._control_generation:self._dispatch=None
                    if type(outcome) is Committed and outcome.source == 'NEW' and (kind.startswith(('apply_dream_candidate','expire_dream_memory_')) or kind in ('settle_obsolete_dream_influence','scan_dream_influence','prepare_dream_influence','defer_dream_influence','finish_dream_influence','discard_dream_influence','discard_dream_review','defer_dream_review','prepare_dream_review','store_dream_review_result','finish_dream_review','resume_dream','observe_dream_retention','account_dream_time','decay_dream_memory','prepare_periodic_persona','store_periodic_generation','store_periodic_review','publish_periodic_persona','keep_periodic_persona')):
                        # This receipt can already be stale. A bounded fresh
                        # read must match its run/revision/epoch before granting
                        # dispatch, and no await follows the generation check.
                        try:
                            check_deadline()
                            run = await self.inspect(cast(str, values['run_id']))
                            check_deadline()
                        except OwnerFailure:
                            run = None
                        with self._control_lock:
                            if (not self.closed and generation == self._control_generation and run is not None
                                    and run['state'] == 'RUNNING' and (self.settled(run) or kind in ('settle_obsolete_dream_influence','scan_dream_influence','prepare_dream_influence','defer_dream_influence','finish_dream_influence','discard_dream_influence','discard_dream_review','defer_dream_review','prepare_dream_review','store_dream_review_result','resume_dream','prepare_periodic_persona','store_periodic_generation','store_periodic_review'))
                                    and (run['revision'] == cast(int, values['expected_revision']) + 1 or
                                        kind in ('store_dream_review_result','store_periodic_generation','store_periodic_review')
                                        and grant_before is not None and grant_before.generation==generation
                                        and (grant_before.run_id,grant_before.revision+1,grant_before.mode_epoch)==(run['run_id'],run['revision'],run['mode_epoch'])
                                        and any(type(t) is MappingProxyType and t['object_id']==run['run_id'] and t['revision']==run['revision'] for t in cast(tuple,cast(Record,outcome.receipt.result)['targets'])))
                                    and (run['mode_epoch'] == values['mode_epoch'] or kind=='resume_dream' and run['mode']=='FOCUSED' and run['mode_epoch']==cast(int,values['mode_epoch'])+1)):
                                self._dispatch = _DispatchState(cast(str, run['run_id']), cast(int, run['revision']),
                                    cast(int, run['mode_epoch']), generation)
                    return outcome

            actual, logical = start_owned(apply())
            self._task = actual

            def ended(job: asyncio.Task):
                if not job.cancelled():
                    job.exception()
                if self._task is job:
                    self._task = None

            actual.add_done_callback(ended)
            done, _ = await asyncio.wait((logical,), timeout=max(0, deadline - time.monotonic()))
            if not done:
                raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED', True)
            return logical.result()
        finally:
            if acquired:
                self._serial.release()
            self._waiting -= 1

    async def confirm(self, kind: str, key: str):
        """Original receipt observation cannot resume volatile admission."""
        if not self.bound or self.closed or kind not in self.operations:
            raise InvalidValue()
        original = await self._read(lambda:self.operations[kind].read_receipt(key))
        if type(original) is Found:
            return Committed(original.value, 'EXISTING')
        return None if type(original) is NotFound else original

    async def inspect(self, run_id: str) -> Record | None:
        """Read current durable facts without creating an idle checkpoint."""
        if not self.bound or self.closed:
            raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')
        value = await self._read(lambda:self.rows.read('runs', run_id))
        return validate_run(value) if value is not None else None

    async def schedule(self) -> Record | None:
        if not self.bound or self.closed:
            raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')
        return await self._read(lambda:self.rows.read('schedule', self.root_id))

    async def _read[T](self,operation:Callable[[],Awaitable[T]]) -> T:
        """Bound logical observation while retaining actual late I/O ownership."""
        deadline=current_deadline(cast(int,self.configuration.candidate.text.record('dream.resources')['operation_timeout_ms'])/1000)
        if len(self._readers)>=cast(int,self.configuration.candidate.text.record('dream.resources')['completed_observations']):
            raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
        async def read():
            with DeadlineScope(deadline):
                check_deadline();value=await operation();check_deadline();return value
        actual,logical=start_owned(read());self._readers.add(actual)
        def ended(task):
            if not task.cancelled():task.exception()
            self._readers.discard(task)
        actual.add_done_callback(ended)
        done,_=await asyncio.wait((logical,),timeout=max(0,deadline-time.monotonic()))
        if not done:raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
        return logical.result()

    def close(self) -> bool:
        """Revoke dispatch immediately; retain actual work until its own callback."""
        self.closed = True
        self._control_intent(revoke=True)
        return self._task is None and self._waiting == 0 and not self._readers
