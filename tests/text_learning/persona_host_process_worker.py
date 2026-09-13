"""Real process barriers around first-persona owner transactions and publication."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time
from typing import cast
from unittest.mock import patch
from companion_memory.provider import WorkPort
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.text_records import stable_identity
from companion_memory.memory.formats import record
from companion_memory.self_model.formats import candidate_digest
from companion_memory.self_model.management import SavedResolution
from tests.persistence.support import Hooks
from tests.text_learning.test_text_host import make_host
from tests.text_learning.host_driver import confirm_local,complete_learning
from tests.runtime.configuration_support import event


async def main():
    root=Path(sys.argv[1]);port=int(sys.argv[2]);mode,stage,edge=sys.argv[3:6]
    host=make_host(root,port);hooks=Hooks();matched=False;learning=False
    prefix={'prepare':'INSERT INTO self_model_initial_persona_runs','handoff':'INSERT INTO provider_handoffs',
        'candidate':'INSERT INTO self_model_initial_persona_candidates','publication':'INSERT INTO self_model_persona_publications',
        'learning_context':'INSERT INTO cognition_learning_contexts','learning_handoff':'INSERT INTO provider_handoffs',
        'learning_candidate':'INSERT INTO cognition_candidates','learning_finalize':'INSERT INTO memory_objects',
        'learning_readmission':'INSERT INTO cognition_candidates'}.get(stage,'NO_MATCH')
    def barrier():
        os.write(1,b'COMMIT_BARRIER\n');sys.stdin.buffer.read(1)
    def before(sql):
        nonlocal matched
        if sql.startswith(prefix) and (not stage.startswith('learning_') or learning):matched=True
        if matched and sql=='COMMIT' and edge=='before':barrier()
    def after(sql):
        if matched and sql=='COMMIT' and edge=='after':barrier()
    hooks.before=before;hooks.after=after
    host.resources=replace(host.resources,database=replace(host.resources.database,connect=hooks.connect))
    try:
        opened=await confirm_local(lambda:host.initialize('CREATE_NEW' if mode=='create' else 'OPEN_EXISTING'))
        assert type(opened) is Found,opened
        assert host.stored is not None and host.persona is not None
        owner=host.combination.persona.persona;assert owner is not None
        run_id=stable_identity('persona-run',host.stored.database_id,'instance')
        api=host.initialization_port()
        async def join():
            assert host.persona is not None
            if host.persona._task is not None:await host.persona._task
        async def run():
            value=await owner.read_original('run',run_id,time.monotonic()+5)
            assert value is not None
            return value.value
        if mode=='create':
            value=await api.register_initial_self('input','NO_PRESET','I explicitly provide no external preset.','ACTUAL_INPUT',time.monotonic()+10)
            assert type(value) is Committed,value
            await join()
            value=await api.prepare_initial_persona('prepare',stable_identity('self-input',host.stored.database_id,'instance'),1,1,time.monotonic()+10)
            assert type(value) is Committed,value
            await join()
        if mode in ('create','resume'):
            current=await run()
            value=await api.generate(run_id,cast(int,current['generation']),cast(str,current['provider_operation_key']),time.monotonic()+10)
            assert type(value) is SavedResolution,value
            await join()
        if mode=='create':
            pending=await api.read_pending(run_id,time.monotonic()+5)
            assert type(pending) is Found,pending
            await join();candidate=record(pending.value);current=await run();digest=candidate_digest(candidate)
            value=await api.review_initial_persona('approve',run_id,cast(int,current['revision']),cast(str,candidate['object_id']),1,digest,'APPROVE',time.monotonic()+5)
            assert type(value) is Committed,value
            await join();current=await run()
            value=await api.publish_initial_persona('publish',run_id,cast(int,current['revision']),cast(str,candidate['object_id']),2,digest,host.gate.epoch,time.monotonic()+5)
            assert type(value) is Committed,value
            await join()
        if mode=='create' and stage.startswith('learning_'):
            learning=True
            value=await confirm_local(lambda:host.register_entry('entry','entry','host','sample_platform','conversation'))
            assert type(value) is Committed,value
            runtime=host.runtime;assert runtime is not None and runtime.text_contexts is not None
            entry=runtime.bind_entry('entry');read=runtime.memory.bind_read(('self',),('read_subject','get_current'))
            runtime.text_contexts.bind_entry(entry,read,('self',),(),({'kind':'REAL','context_id':None},))
            for ordinal in range(3):
                supplied=event('event-'+str(ordinal),'A controlled factual claim.');supplied['event_version']=2
                accepted=await confirm_local(lambda:entry.accept_event('accept-'+str(ordinal),supplied))
                assert type(accepted) is Committed,accepted
            if stage=='learning_readmission':
                original_generate=WorkPort.generate
                async def blocked(port,request):
                    host.gate.close_ordinary()
                    try:return await original_generate(port,request)
                    finally:host.gate.resolve_cutoff('NORMAL',host.gate.epoch)
                with patch.object(WorkPort,'generate',blocked):
                    waiting=await confirm_local(lambda:entry.run_learning('learn'))
                assert type(waiting) is Found and record(waiting.value)['state']=='WAITING_ADMISSION',waiting
                learned=await complete_learning(entry,'readmission')
            else:learned=await complete_learning(entry,'learn')
            assert type(learned) is Committed,learned
        current=await owner.read_original('run',run_id,time.monotonic()+5)
        publication=await owner.current_original(time.monotonic()+5)
        work=await host.assembly.rows.read('work_page',{'after':'','limit':10})
        print(json.dumps({'run_state':None if current is None else current.value['state'],'mode':host.gate.state,
            'publication':publication is not None,'learning_ready':record(opened.value)['learning_ready'],
            'learning_phases':[value['phase'] for value in work]}),flush=True)
    finally:
        assert await host.close()


if __name__=='__main__':asyncio.run(main())
