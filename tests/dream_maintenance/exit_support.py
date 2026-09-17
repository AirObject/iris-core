"""Native SQLite exit fixtures using original commands and no model requests."""
import asyncio
import time
from typing import cast
from companion_memory.persistence import Committed,Found
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from companion_memory.memory.formats import record
from tests.runtime.configuration_support import event


async def initialize(host):
    if type(await host.initialize('CREATE_NEW')) is not Found:raise AssertionError('Initialize failed')
    if type(await host.register_entry('register','entry','host','sample_platform','external')) is not Committed:raise AssertionError('Register failed')
    return await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+300))


async def accept(host,first,count):
    entry=host.bind_entry('entry')
    for number in range(first,first+count):
        key='event-'+str(number);raw=event(key,'合成 FIFO 输入 '+str(number));raw['event_version']=2
        result=await entry.accept_event(key,raw)
        if type(result) is not Committed:raise AssertionError(result)


async def ready_exit(host,admin,*,abort=False):
    """Freeze safe KEEP_PREVIOUS disposition, or durable abort, without a model."""
    c=host.combination.dream
    if c is None:raise AssertionError('Missing control')
    if type(await admin.start_dream('start','run',1,1)) is not Committed:raise AssertionError('Start failed')
    run=await c.inspect('run')
    if run is None:raise AssertionError('Missing run')
    if abort:
        values={'run_id':'run','expected_revision':run['revision'],'mode_epoch':run['mode_epoch']}
        if type(await c.execute('request_dream_abort','abort',values,actor='supervisor')) is not Committed:raise AssertionError('Abort intent failed')
    else:
        if type(await admin.resume_dream('resume','run',run['revision'],run['mode_epoch'])) is not Committed:raise AssertionError('Resume failed')
        run=await c.inspect('run')
        if run is None:raise AssertionError('Missing run')
        c.now=lambda:cast(int,run['deadline_at_us'])+1
        if type(await host.advance_dream()) is not Committed:raise AssertionError('Deferral failed')


async def exit_receipt(host):
    p=host.combination.periodic;c=host.combination.dream
    if p is None or c is None:raise AssertionError('Missing owners')
    exit=await c.rows.read('exits',p.key('dream-exit','run'))
    if exit is None:raise AssertionError('Missing persisted exit')
    original=record(exit['original_operation'])
    result=await c.confirm(cast(str,original['operation_kind']),cast(str,original['operation_key']))
    if type(result) is not Committed:raise AssertionError(result)
    return result.receipt


async def await_completed(host,*,terminal='COMPLETED'):
    """Observe automatic local closure; dispatch is deliberately kept revoked."""
    c=host.combination.dream
    if c is None:raise AssertionError('Missing control')
    host.enable_dream_schedule()
    try:
        async with asyncio.timeout(15):
            while True:
                if not host.dream_scheduler.enabled:raise AssertionError(host.dream_scheduler.failure)
                run=await c.inspect('run')
                if run is None:raise AssertionError('Missing run')
                if run['state']==terminal:return run
                await asyncio.sleep(.02)
    finally:
        host.pause_dream_schedule()
        if host.dream_scheduler.task is not None:await host.dream_scheduler.task
