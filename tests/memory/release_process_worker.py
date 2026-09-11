"""Real source-release roots and executions interrupted before response delivery."""
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed
from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from companion_memory.provider import SimulationAdapter, Scenario
from tests.memory.support import Fixture
from tests.runtime.test_preparation_lifecycle import upload_window
from tests.provider.support import success


async def main(root, mode, selected_kind, when):
    fixture = Fixture(root, MediaService(), SyntheticCandidateInput('release_process:1', ('original',), 50))
    fixture.adapter = SimulationAdapter((Scenario('SUCCEEDED', {'text': 'description', 'modality': 'IMAGE', 'task': 'DESCRIBE',
        'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
        {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success()))
    await fixture.initialize(mode)
    runtime = fixture.runtime; assert runtime is not None
    active = False
    def stop(): os.write(1, b'READY\n'); sys.stdin.buffer.read(1)
    def before(sql):
        if active and sql == 'COMMIT' and when == 'before': stop()
    def after(sql):
        if active and sql == 'COMMIT' and when == 'after': stop()
    original = runtime.execute
    async def execute(kind, key, values):
        nonlocal active
        active = kind == selected_kind
        try: return await original(kind, key, values)
        finally: active = False
    try:
        if mode == 'CREATE_NEW':
            entry, _ = await upload_window(fixture)
            learned = await entry.run_learning('original'); assert type(learned) is Committed, learned
            value = record(learned.receipt.result)
            target = {'object_id': record(sequence(value['object_refs'])[0])['object_id'], 'source_id': value['source_id']}
            (root / 'original-target.json').write_text(json.dumps(target))
            fixture.hooks.before, fixture.hooks.after = before, after
            runtime.execute = execute
        else: target = json.loads((root / 'original-target.json').read_text())
        result = await runtime.maintenance.bind((cast(str, target['object_id']),)).delete_object('original_delete', target['object_id'], 1)
        assert type(result) is Committed, result
        source = (await fixture.assembly.memory.rows.read('sources_get', {'source_id': target['source_id']}))[0]
        assert fixture.media is not None
        blobs = await fixture.media.rows.read('recovery_page', {'after': '', 'limit': 16})
        print(json.dumps({'models': len(fixture.adapter.calls), 'source_state': source['state'],
            'media_states': [blob['state'] for blob in blobs], 'receipt': encode_value(receipt_value(result.receipt), 65536).decode()}), flush=True)
    finally: await fixture.close()


if __name__ == '__main__': asyncio.run(main(Path(sys.argv[1]), *sys.argv[2:]))
