"""Independent interpreter for actual generation commit and restart boundaries.

The parent kills only after a pipe barrier or its controlled HTTP server has
received the request. Inspect uses original local lookup and never sends work.
"""
import asyncio
import json
from pathlib import Path
import sys
from companion_memory.provider import Ready, Found, ResultGrant
from companion_memory.provider.values import as_record
from tests.text_learning.provider_support import Fixture


def barrier():
    print('PROVIDER_BARRIER', flush=True)
    sys.stdin.buffer.read(1)


async def main(root: Path, port: int, action: str):
    fixture = Fixture(root, port)
    try:
        ready = await fixture.initialize('OPEN_EXISTING')
        assert type(ready) is Ready, ready
        if action == 'inspect':
            result = await fixture.work.lookup_request('generate', fixture.request())
            assert type(result) is Found, result
            value = as_record(result.value); request = as_record(value['request'])
            original_result = None
            completion_operation = None
            if request['phase'] == 'TERMINAL':
                assert type(request['object_id']) is str
                owner = fixture.service.bind_result_owner(ResultGrant('cognition', (request['object_id'],)))
                recovered = await owner.recover_result(request['object_id'])
                assert type(recovered) is Found, recovered
                original_result = as_record(recovered.value)['result']
                from companion_memory.provider.terminal_evidence import TerminalVerified
                from companion_memory.provider.completion_evidence import ConfirmedCompletion
                terminal = await owner.verify_terminal(request['object_id'])
                assert type(terminal) is TerminalVerified, terminal
                completion = await owner.confirm_completion(terminal.value)
                assert type(completion) is ConfirmedCompletion, completion
                completion_operation = dict(completion.operation)
            print(json.dumps({'phase': request['phase'], 'outcome': request['outcome'], 'attempts': request['attempt_count'],
                'request_id':request['object_id'], 'has_result':original_result is not None,
                'completion_operation':completion_operation,'credentials':len(fixture.leases), 'ended':len(fixture.ended)}), flush=True)
            return
        commits = 0; writing = False
        def after(sql):
            nonlocal commits, writing
            if sql.startswith(('INSERT INTO provider_requests', 'UPDATE provider_requests')): writing = True
            if sql == 'COMMIT' and writing:
                commits += 1; writing = False
                if action == 'prepared_commit' and commits == 1 or action == 'completed_commit' and commits == 2:
                    barrier()
        def before(sql):
            if action == 'result_received' and sql.startswith('UPDATE provider_requests'):
                barrier()
        fixture.hooks.before = before; fixture.hooks.after = after
        if action == 'in_flight': print('PROVIDER_ACTIVE', flush=True)
        await fixture.work.generate(fixture.request())
        raise AssertionError('The expected process boundary was not reached.')
    finally:
        await fixture.close()


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]))
