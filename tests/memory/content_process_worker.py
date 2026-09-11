"""Independent real content runtime process, stopped around formal commit.

The parent supplies retained expected identity and confirms process death before
reopening. Recovery executes only the original local candidate finalization.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import cast
from companion_memory.ingress.events import canonical_event
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.memory.formats import record
from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from tests.runtime.configuration_support import event
from tests.memory.support import Fixture


async def main(root: Path, mode: str, barrier: str):
    expected = json.loads((root / 'expected-identity.json').read_text())
    fixture = Fixture(root)
    assert expected == {'identity': fixture.expected_id, 'path': str(fixture.path)}
    await fixture.initialize(mode)
    changed = False
    def before(sql):
        nonlocal changed
        if sql.startswith('INSERT INTO memory_objects '): changed = True
        if changed and sql == 'COMMIT' and barrier == 'before':
            os.write(1, b'BEFORE_COMMIT\n'); sys.stdin.buffer.read(1)
    def after(sql):
        if changed and sql == 'COMMIT' and barrier == 'after':
            os.write(1, b'AFTER_COMMIT\n'); sys.stdin.buffer.read(1)
    fixture.hooks.before, fixture.hooks.after = before, after
    try:
        if mode == 'CREATE_NEW':
            for i in range(3):
                raw = event('event' + str(i)); raw['event_version'] = 2
                encoded = canonical_event(isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))
                await fixture.execute('accept_media_event', 'accept' + str(i), {'entry_id': 'entry', 'event': encoded.decode()})
            source = await fixture.freeze(); result = await fixture.learn(source, 2)
        else:
            work = (await fixture.assembly.rows.read('work_get', {'batch_id': 'batch'}))[0]
            result = await fixture.execute('commit_content_published', 'finish:batch', {'batch_id': 'batch', 'candidate_id': work['candidate_id'],
                'expected_revision': 3, 'generation': 1, 'readable_objects': [], 'readable_subjects': []})
        print(json.dumps({'source': result.source, 'adapter_calls': len(fixture.adapter.calls),
            'receipt': encode_value(receipt_value(result.receipt), 65536).decode()}), flush=True)
    finally:
        await fixture.close()


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
