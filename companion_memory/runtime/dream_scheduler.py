"""Explicit process activation of the native local-date dream scheduler.

Opening never creates a task. A stopped control generation, an existing paused
run, or an unresolved original operation cannot be resumed by a periodic tick.
"""
import asyncio
import time
from typing import TYPE_CHECKING,cast
from zoneinfo import ZoneInfo
from companion_memory.persistence import Committed
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.deadlines import DeadlineScope,check_deadline
from companion_memory.persistence.daily_records import identity
from .dream_clock import due_date
if TYPE_CHECKING:
    from .daily_host import DailyCognitionHost


class DreamScheduler:
    def __init__(self,host:'DailyCognitionHost'):
        self.host=host;self.enabled=False;self.task:asyncio.Task|None=None
        self.wake=asyncio.Event();self.failure:str|None=None;self.busy=False

    def enable(self):
        self.host.checkpoint()
        if self.host.state!='READY' or self.host.combination.dream is None:raise InvalidValue()
        if self.task is not None and not self.task.done():raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        if not self.host.execution_configuration.text.record('dream.schedule')['enabled']:raise OwnerFailure('ACCESS_DENIED','configuration','OPERATION_NOT_GRANTED')
        self.enabled=True;self.failure=None;self.wake.clear()
        self.task=asyncio.create_task(self.run())

    def stop(self):
        self.enabled=False;self.wake.set()
        control=self.host.combination.dream
        if control is not None:control.revoke_dispatch()
        return self.task is None or self.task.done()

    async def run(self):
        settings=self.host.execution_configuration.text.record('dream.schedule')
        while self.enabled:
            try:await self.tick()
            except OwnerFailure as failure:
                # Another sender can acquire the physical slot after the
                # worker's readiness observation. reserve rejects this before
                # registration; it is still ordinary admission backpressure.
                if (failure.code,failure.field,failure.reason)!=('RESOURCE_BUSY','resource','ADMISSION_FULL'):
                    self.failure=failure.reason;self.stop()
            except Exception:
                self.failure='STORAGE_FAILED';self.stop()
            if not self.enabled:break
            self.wake.clear()
            try:await asyncio.wait_for(self.wake.wait(),cast(int,settings['tick_ms'])/1000)
            except TimeoutError:pass

    async def tick(self):
        if not self.enabled:return
        if self.busy:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING',True)
        self.busy=True
        try:
            with DeadlineScope(time.monotonic()+cast(int,self.host.execution_configuration.text.record('dream.resources')['operation_timeout_ms'])/1000):
                await self.advance()
        finally:self.busy=False

    async def advance(self):
        host=self.host;control=host.combination.dream;mode=host.combination.dream_mode
        if control is None or mode is None or host.runtime is None or control.now is None:raise InvalidValue()
        host.checkpoint();root=await control.schedule();check_deadline()
        if root is None:raise InvalidValue()
        if root['active_run_id'] is not None:
            # A retained actual worker owns its resources until it ends. A tick
            # must neither replace it nor revoke admission merely for being busy.
            periodic=host.combination.periodic
            if periodic is not None and periodic.job is not None:return
            # The worker checks dispatch at each new step/send/publication. Its
            # original confirmations and committed exit can progress locally
            # even while a pause, abort or process restart has revoked dispatch.
            if self.enabled:await host.advance_dream()
            return
        settings=host.execution_configuration.text.record('dream.schedule')
        due=due_date(control.now(),ZoneInfo(cast(str,host.execution_configuration.text.value('runtime.timezone'))),
            cast(str,settings['local_time']),cast(str|None,root['last_local_date']))
        if due is None:return
        current=await mode.synchronize();check_deadline()
        if current['state']!='NORMAL' or not self.enabled:return
        def key(kind):return identity(kind,control.configuration.database_id,control.configuration.scope_id,root['schedule_revision'],due.local_date)
        run_id=key('scheduled-dream');kind='start_focused_dream' if settings['focus_default'] else 'start_background_dream'
        values={'run_id':run_id,'expected_revision':root['revision'],'mode_epoch':current['epoch'],'trigger':'SCHEDULED','local_date':due.local_date}
        intent=control.begin_intent(kind,key('schedule-start'),values,'dream_scheduler')
        if settings['focus_default']:host.runtime.gate.close_ordinary()
        outcome=await control.execute(kind,key('schedule-start'),values,actor='dream_scheduler',intent=intent);check_deadline()
        if type(outcome) is not Committed:return
        await mode.synchronize();check_deadline()
        run=await control.inspect(run_id);check_deadline()
        if run is None:raise InvalidValue()
        # No suspension is allowed between this fence and issuing resume intent.
        if not self.enabled or control._control_generation!=intent.generation:return
        await control.execute('resume_dream',key('schedule-resume'),{'run_id':run_id,
            'expected_revision':run['revision'],'mode_epoch':run['mode_epoch']},actor='dream_scheduler')
