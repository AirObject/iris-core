"""New-interpreter persona terminal cuts; no recovery path sends model work."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import cast
from companion_memory.persistence import Committed
from tests.persistence.support import Hooks
from tests.text_learning.persona_terminal_support import PersonaScenario


async def main():
    root=Path(sys.argv[1]).resolve();port=int(sys.argv[2]);mode,stage,edge=sys.argv[3:6]
    hooks=Hooks();scenario=PersonaScenario(root,port,hooks);armed=False;matched=False
    prefix={'absent_resolution':'INSERT INTO self_model_initial_persona_candidates',
        'absent_retry':'UPDATE self_model_initial_persona_runs','registered_retry':'UPDATE self_model_initial_persona_runs','length_settle':'UPDATE provider_attempts',
        'length_resolution':'INSERT INTO self_model_initial_persona_candidates'}[stage]
    def barrier():
        os.write(1,b'COMMIT_BARRIER\n');sys.stdin.buffer.read(1)
    def before(sql):
        nonlocal matched
        if armed and sql.startswith(prefix):matched=True
        if matched and sql=='COMMIT' and edge=='before':barrier()
    def after(sql):
        if matched and sql=='COMMIT' and edge=='after':barrier()
    hooks.before=before;hooks.after=after
    try:
        await scenario.start('CREATE_NEW' if mode=='create' else 'OPEN_EXISTING')
        if mode=='create':
            if stage.startswith('absent'):
                current=await scenario.associate()
                values=[scenario.run_id,current['revision'],1,current['provider_operation_key'],0]
                (root/'original-resolution.json').write_text(json.dumps(values))
                armed=stage=='absent_resolution'
                saved=await scenario.api.record_initial_persona_resolution('resolve',str(values[0]),int(values[1]),int(values[2]),str(values[3]),int(values[4]),time.monotonic()+10)
                assert type(saved) is Committed,saved
                await scenario.join()
                armed=True
                if stage=='absent_retry':
                    saved=await scenario.retry();assert type(saved) is Committed,saved
            else:
                armed=stage!='registered_retry';current=await scenario.run()
                await scenario.api.generate(scenario.run_id,1,cast(str,current['provider_operation_key']),time.monotonic()+10)
                await scenario.join()
                if stage=='registered_retry':
                    armed=True;saved=await scenario.retry();assert type(saved) is Committed,saved
        if mode=='resume':
            assert stage.startswith('absent')
            values=json.loads((root/'original-resolution.json').read_text())
            saved=await scenario.api.record_initial_persona_resolution('resolve',str(values[0]),int(values[1]),int(values[2]),str(values[3]),int(values[4]),time.monotonic()+10)
            assert type(saved) is Committed,saved
            await scenario.join()
            if (await scenario.run())['generation']==1:
                retried=await scenario.retry();assert type(retried) is Committed,retried
        current=await scenario.run()
        print(json.dumps({'run_state':current['state'],'generation':current['generation'],
            'candidates':scenario.bodies('self_model_initial_persona_candidates'),
            'requests':scenario.bodies('provider_requests'),'attempts':scenario.bodies('provider_attempts'),
            'handoff_count':len(scenario.rows('provider_handoffs'))}),flush=True)
    finally:
        assert await scenario.host.close()


if __name__=='__main__':asyncio.run(main())
