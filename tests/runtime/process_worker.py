"""Child-only crash barriers around real disposable SQLite commit boundaries."""
import asyncio
from dataclasses import replace
import threading
import json
from pathlib import Path
import sqlite3
import sys
from typing import cast
from companion_memory.runtime import IngressPort,WorkCapability,RuntimeObservationGrant
from companion_memory.runtime.results import Committed,Found
from tests.runtime.support import Fixture
from tests.runtime.test_batch_execution import response
from tests.runtime.configuration_support import event
from tests.provider.support import record,records


def barrier():
    print('RUNTIME_BARRIER',flush=True)
    sys.stdin.buffer.readline()


async def main(root:Path,action:str):
    started=threading.Event();release=threading.Event()
    scenario=replace(response(),started=started,release=release) if action=='provider_started' else response()
    fixture=Fixture(root,(scenario,),**({'runtime_changes':{'runtime.read_page_size':int(sys.argv[3])}} if len(sys.argv)>3 else {}))
    if action=='inspect_checkpoints':
        runtime=await fixture.initialize('OPEN_EXISTING')
        from contextlib import closing
        with closing(sqlite3.connect(fixture.path)) as connection:
            count=connection.execute('SELECT count(*) FROM runtime_recovery').fetchone()[0]
        print(json.dumps({'count':count,'calls':len(fixture.adapter.calls),'lifecycle':runtime.get_health()['lifecycle']}),flush=True)
        await fixture.close();return
    armed=False
    writing=False
    class Connection(sqlite3.Connection):
        def execute(self,sql,parameters=(),/):
            nonlocal writing
            if armed:
                if action.startswith('accept') and sql.startswith('INSERT INTO ingress_events'):writing=True
                if action=='provider_registered' and sql.startswith('INSERT INTO provider_requests'):writing=True
                if action=='claim_after' and sql.startswith('UPDATE runtime_work') and type(parameters) is dict and parameters.get('state')=='EXECUTING':writing=True
                if action=='provider_prepared' and sql.startswith('INSERT INTO provider_attempts'):writing=True
                if action=='provider_memory_result' and sql.startswith('UPDATE provider_attempts'):barrier()
                if action=='provider_completed' and sql.startswith('UPDATE provider_requests'):writing=True
                if action.startswith('candidate') and sql.startswith('UPDATE runtime_work') and type(parameters) is dict and parameters.get('state')=='CANDIDATE_STORED':writing=True
                if action.startswith('terminal') and sql.startswith('UPDATE runtime_work') and type(parameters) is dict and parameters.get('state')=='TERMINAL':writing=True
                if action=='configuration_after' and sql.startswith('INSERT INTO active_configuration'):writing=True
                if action=='create_after' and sql.startswith('CREATE TABLE'):writing=True
                if action=='runtime_page_after' and sql.startswith('INSERT INTO runtime_recovery'):writing=True
                if action in ('focus_after','exit_after') and sql.startswith('UPDATE runtime_mode') and type(parameters) is dict and parameters.get('state')==('DREAM_FOCUSED' if action=='focus_after' else 'DRAINING'):writing=True
                if action=='publication_after' and sql.startswith('INSERT INTO synthetic_learning') and type(parameters) is dict and parameters.get('state')=='PUBLISHED':writing=True
                if action=='transfer_after' and sql.startswith('UPDATE buffers_positions'):writing=True
                if sql=='COMMIT' and writing and action.endswith('_before'):barrier()
            result=super().execute(sql,parameters)
            if armed and sql=='COMMIT' and writing:
                writing=False
                if not action.endswith('_before'):barrier()
            return result
    def connect(database,**kwargs):return sqlite3.connect(database,factory=Connection,**kwargs)
    from companion_memory.persistence import DatabaseResources
    fixture.resources=DatabaseResources('runtime-database',lambda identity,path:json.loads(fixture.identity_path.read_text())=={'identity':identity,'path':path},connect=connect)
    if action in ('create_after','configuration_after','runtime_page_after'):armed=True
    try:runtime=await fixture.initialize('CREATE_NEW' if action=='create_after' else 'OPEN_EXISTING')
    except AssertionError:
        if action!='inspect_integrity':raise
        assert fixture.runtime is not None
        recovery=await fixture.runtime.recover_runtime()
        print(json.dumps({'lifecycle':fixture.runtime.get_health()['lifecycle'],'model_calls':len(fixture.adapter.calls),'reason':getattr(recovery,'reason',None)}),flush=True)
        await fixture.close();return
    if action=='inspect_integrity':
        print(json.dumps({'lifecycle':runtime.get_health()['lifecycle'],'model_calls':len(fixture.adapter.calls),'reason':None}),flush=True)
        await fixture.close();return
    if action=='resume_claim':
        before=len(fixture.adapter.calls)
        page=await runtime._transactions.rows.read('work_page',{'after':'','limit':1})
        state=page[0]['state']
        result=await runtime.run_ready_cycle()
        print(json.dumps({'recovery_calls':before,'recovered_work_state':state,'result':type(result).__name__,'model_calls':len(fixture.adapter.calls)}),flush=True)
        await fixture.close();return
    eid,port=await fixture.entry();assert type(port) is IngressPort
    if action=='inspect':
        result=await port.accept_event(event('crash_event'))
        assert type(result) is Committed,result
        observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,),True))
        state=await observer.read_entry_status({'entry_id':eid,'cursor':None,'limit':8});assert type(state) is Found,state
        batches=await observer.read_batch_status({'entry_id':eid,'cursor':None,'limit':8});assert type(batches) is Found,batches
        print(json.dumps({'sequence':record(result.receipt.result)['sequence'],'source':result.source,'pending':records(record(state.value)['rows'])[0]['pending_total'],
            'batches':[{'state':row['state'],'terminal':row['terminal']} for row in records(record(batches.value)['rows'])],
            'mode':runtime.get_health()['mode'],'transferred':records(record(state.value)['rows'])[0]['transferred_count'],'model_calls':len(fixture.adapter.calls),'sqlite':sqlite3.sqlite_version}),flush=True)
        await fixture.close();return
    if action in ('focus_after','publication_after','exit_after','transfer_after'):
        from companion_memory.runtime import FocusGrant,FocusPort
        focus=runtime.bind_focus_coordinator(FocusGrant('instance','dream_run','dream_coordinator'));assert type(focus) is FocusPort
        armed=action=='focus_after'
        entered=await focus.enter_focus('enter',1);assert type(entered) is Committed,entered
        staged=await port.accept_event(event('during'));assert type(staged) is Committed,staged
        armed=action=='publication_after'
        published=await fixture.participant.publish('dream_run','configuration:1')
        from companion_memory.persistence import Committed as StorageCommitted
        assert type(published) is StorageCommitted,published
        armed=action=='exit_after'
        finished=await focus.finish_focus('finish',record(published.receipt.result)['publication_id'],3);assert type(finished) is Committed,finished
        armed=action=='transfer_after'
        await runtime.transfer_dream_page(port,0)
        raise AssertionError('Focus boundary was not reached.')
    if action.startswith('accept'):
        armed=True
        await port.accept_event(event('crash_event'))
    else:
        for key in ('one','two','three'):
            accepted=await port.accept_event(event(key));assert type(accepted) is Committed,accepted
        frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'learn','type':'THRESHOLD'});assert type(frozen) is Committed,frozen
        if action=='claim_after':armed=True
        work=await runtime.claim_work(cast(str,record(frozen.receipt.result)['object_id']),1);assert type(work) is Found,work
        armed=True
        if action=='provider_started':
            task=asyncio.create_task(runtime.run_work(cast(WorkCapability,work.value)))
            if not await asyncio.to_thread(started.wait,5):raise AssertionError('Model worker did not start.')
            barrier()
            release.set();await task
        else:await runtime.run_work(cast(WorkCapability,work.value))
    raise AssertionError('The selected crash boundary did not execute.')


if __name__=='__main__':asyncio.run(main(Path(sys.argv[1]),sys.argv[2]))
