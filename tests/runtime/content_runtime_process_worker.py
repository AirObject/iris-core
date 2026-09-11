"""Original public runtime requests survive death before any business response."""
import asyncio
import json
import os
from pathlib import Path
import sys
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.persistence import Committed
from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from companion_memory.runtime.content_assembly import stable
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event


async def main(root, mode, boundary):
    fixture = Fixture(root, candidate_input=SyntheticCandidateInput('persistent_input:1', ('Explicit proposal.',), 50))
    expected = json.loads((root / 'expected.json').read_text())
    assert expected == {'database_id': fixture.expected_id, 'path': str(fixture.path)}
    await fixture.initialize(mode)
    runtime = fixture.runtime; assert runtime is not None
    table, when = boundary.split(':') if boundary != 'none' else ('none', 'none')
    changed = False
    def before(sql):
        nonlocal changed
        prefix = {'candidate': 'INSERT INTO cognition_candidates ', 'object': 'INSERT INTO memory_objects '}.get(table)
        if prefix and sql.startswith(prefix): changed = True
        if changed and sql == 'COMMIT' and when == 'before':
            os.write(1, b'READY\n'); sys.stdin.buffer.read(1)
    def after(sql):
        if changed and sql == 'COMMIT' and when == 'after':
            os.write(1, b'READY\n'); sys.stdin.buffer.read(1)
    fixture.hooks.before, fixture.hooks.after = before, after
    try:
        assert runtime.state == 'READY', runtime.state
        entry = runtime.bind_entry('entry')
        if mode == 'CREATE_NEW':
            for i in range(3):
                value = event('event:' + str(i)); value['event_version'] = 2
                assert type(await entry.accept_event('accept:' + str(i), value)) is Committed
        result = await entry.run_learning('original-trigger')
        assert type(result) is Committed, result
        print(json.dumps({'calls': len(fixture.adapter.calls), 'state': runtime.state,
            'receipt': encode_value(receipt_value(result.receipt), 65536).decode()}), flush=True)
    finally: await fixture.close()


if __name__ == '__main__': asyncio.run(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
