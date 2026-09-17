"""Interrupt after an actual SQLite exit commit; reopen in a new interpreter.

CREATE_NEW intentionally exits without graceful shutdown only after the native
commit is confirmed. OPEN_EXISTING completes local FIFO with dispatch revoked.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from companion_memory.persistence import Found,Committed
from .host_support import make_dream_host
from .exit_support import initialize,accept,ready_exit,exit_receipt,await_completed


async def run(root,mode,point,abort):
    credentials=[];host=make_dream_host(root,9,credentials)
    try:
        if mode=='CREATE_NEW':
            admin=await initialize(host);await ready_exit(host,admin,abort=abort);await accept(host,1,3)
            c=host.combination.dream
            if c is None:raise AssertionError('Missing control')
            execute=c.execute
            async def interrupt(kind,*args,**kwargs):
                if point=='NORMAL' and kind=='complete_dream_exit':await crash()
                result=await execute(kind,*args,**kwargs)
                if point=='DRAINING' and kind in ('finish_focused_dream','exit_focused_dream') and type(result) is Committed:await crash()
                return result
            async def crash():
                receipt=await exit_receipt(host)
                current=(await host.assembly.rows.read('mode_get',{'mode_id':'instance_mode'}))[0]
                if current['state']!=point or credentials:raise AssertionError('Wrong interruption state')
                print(json.dumps({'exit_commit':receipt.commit_id,'mode':current['state'],'new_sends':0}),flush=True)
                os._exit(0)
            with patch.object(c,'execute',interrupt):await host.advance_dream()
            raise AssertionError('Interruption was not reached')
        opened=await host.initialize('OPEN_EXISTING')
        if type(opened) is not Found:raise AssertionError(opened)
        c=host.combination.dream
        if c is None or c.dispatch_enabled or host.dream_scheduler.enabled:raise AssertionError('Startup enabled dispatch')
        original=await exit_receipt(host)
        ended=await await_completed(host,terminal='ABORTED' if abort else 'COMPLETED')
        if c.dispatch_enabled or credentials:raise AssertionError('Recovery enabled model dispatch')
        root_state=await c.schedule()
        if root_state is None:raise AssertionError('Missing schedule')
        fifo=await host.assembly.buffers.rows.read('fifo',{'entry_id':'entry','state':'NORMAL','limit':16})
        if await exit_receipt(host)!=original:raise AssertionError('Exit receipt changed')
        op=ended['last_operation'];completed=await c.confirm(op['operation_kind'],op['operation_key'])
        if type(completed) is not Committed:raise AssertionError('Completion not confirmed')
        if type(await host.advance_dream()) is not Found:raise AssertionError('Duplicate completion')
        if await c.confirm(op['operation_kind'],op['operation_key'])!=completed:raise AssertionError('Completion receipt changed')
        print(json.dumps({'state':ended['state'],'exit_commit':original.commit_id,'complete_commit':completed.receipt.commit_id,
            'new_sends':len(credentials),'fifo':[row['entry_seq'] for row in fifo],'active_run_id':root_state['active_run_id']}))
    finally:
        if not await host.close():raise AssertionError('Actual resources still owned')


if __name__=='__main__':asyncio.run(run(Path(sys.argv[1]),sys.argv[2],sys.argv[3],bool(int(sys.argv[4]))))
