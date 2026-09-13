"""Real interpreter cuts around late-evidence COMMIT; recovery is local only."""
import asyncio,json,os,sys,time
from pathlib import Path
from typing import cast
from companion_memory.provider import ResultGrant
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.completion_evidence import ConfirmedCompletion
from companion_memory.self_model.management import RemoteUnknown
from tests.persistence.support import Hooks
from tests.text_learning.persona_terminal_support import PersonaScenario
from tests.text_learning.late_terminal_support import LateTerminalBarrier


async def main():
    root=Path(sys.argv[1]).resolve();port=int(sys.argv[2]);mode,edge=sys.argv[3:5]
    hooks=Hooks();scenario=PersonaScenario(root,port,hooks)
    if mode=='create':
        with LateTerminalBarrier(scenario) as barrier:
            try:
                await scenario.start();run=await scenario.run();key=cast(str,run['provider_operation_key'])
                task=asyncio.create_task(scenario.api.generate(scenario.run_id,1,key,time.monotonic()+15))
                await barrier.reach_unknown()
                unknown=scenario.bodies('provider_attempts')[0]
                assert unknown['state']=='REMOTE_RESULT_UNKNOWN' and unknown['first_error']['code']=='TIMEOUT' and unknown['terminal_error'] is None
                assert type(await task) is RemoteUnknown
                (root/'unknown-evidence.json').write_text(json.dumps(unknown))
                armed=False
                def cut():
                    os.write(1,b'LATE_COMMIT_BARRIER\n');sys.stdin.buffer.read(1)
                def before(sql):
                    nonlocal armed
                    if sql.startswith('UPDATE provider_attempts'):armed=True
                    if armed and sql=='COMMIT' and edge=='before':cut()
                def after(sql):
                    if armed and sql=='COMMIT' and edge=='after':cut()
                hooks.before=before;hooks.after=after
                barrier.release.set();await scenario.join()
            finally:
                hooks.before=lambda sql:None;hooks.after=lambda sql:None
                barrier.release.set();await scenario.join();assert await scenario.host.close()
    else:
        try:
            await scenario.start('OPEN_EXISTING')
            request=scenario.bodies('provider_requests')[0];attempt=scenario.bodies('provider_attempts')[0]
            completion_key=None
            if request['phase']=='TERMINAL':
                owner=scenario.host.provider.bind_result_owner(ResultGrant('self_model',(request['object_id'],)))
                proof=await owner.verify_terminal(request['object_id']);assert type(proof) is TerminalVerified
                completion=await owner.confirm_completion(proof.value);assert type(completion) is ConfirmedCompletion
                assert proof.value.terminal_reason=='OUTPUT_LIMIT'
                completion_key=dict(completion.operation)
                scenario.host.provider.revoke(owner)
                del completion,proof
                run=await scenario.run()
                await scenario.api.generate(scenario.run_id,1,cast(str,run['provider_operation_key']),time.monotonic()+5);await scenario.join()
            print(json.dumps({'request':request,'attempt':attempt,'candidates':scenario.bodies('self_model_initial_persona_candidates'),
                'handoffs':len(scenario.rows('provider_handoffs')),'completion':completion_key}),flush=True)
        finally:assert await scenario.host.close()


if __name__=='__main__':asyncio.run(main())
