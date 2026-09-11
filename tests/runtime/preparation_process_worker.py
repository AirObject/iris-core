"""Parent-controlled death at original public preparation and media transactions."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import MappingProxyType
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from companion_memory.provider import SimulationAdapter, Scenario
from companion_memory.runtime.content_assembly import stable
from tests.memory.support import Fixture
from tests.runtime.test_preparation_lifecycle import upload_window
from tests.provider.support import success


def plain(value):
    if type(value) in (dict, MappingProxyType): return {k: plain(v) for k, v in value.items()}
    if type(value) in (tuple, list): return [plain(v) for v in value]
    return value


async def main(root, mode, kind, when):
    fixture = Fixture(root, MediaService(), SyntheticCandidateInput('preparation_recovery:1', ('fact',), 50))
    fixture.adapter = SimulationAdapter((Scenario('SUCCEEDED', {'text': 'description', 'modality': 'IMAGE', 'task': 'DESCRIBE',
        'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
        {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success()))
    await fixture.initialize(mode)
    runtime = fixture.runtime; assert runtime is not None
    active = False
    def stop():
        os.write(1, b'READY\n'); sys.stdin.buffer.read(1)
    def before(sql):
        if active and sql == 'COMMIT' and when == 'before': stop()
    def after(sql):
        if active and sql == 'COMMIT' and when == 'after': stop()
    original = runtime.execute; selected_kind = kind
    async def execute(kind, key, values):
        nonlocal active
        if kind == selected_kind:
            (root / 'original-command.json').write_text(json.dumps({'kind': kind, 'key': key, 'values': plain(values)}))
            active = True
        try: return await original(kind, key, values)
        finally: active = False
    try:
        if mode == 'CREATE_NEW':
            entry, _ = await upload_window(fixture)
            fixture.hooks.before, fixture.hooks.after = before, after
            runtime.execute = execute
            result = await entry.run_learning('original')
            assert type(result) is Committed, result
            raise AssertionError('Requested boundary was not reached.')
        intent = json.loads((root / 'original-command.json').read_text())
        receipt = await fixture.assembly.operations[intent['kind']].read_receipt(intent['key'])
        prep = stable('preparation', fixture.expected_id, 'entry', 'original')
        rows = await fixture.assembly.rows.read('preparations_get', {'preparation_id': prep})
        batches = await fixture.assembly.rows.read('batches_page', {'after': '', 'limit': 16})
        assert fixture.media is not None
        work = await fixture.media.rows.read('work_recovery_page', {'after': '', 'limit': 16})
        print(json.dumps({'models': len(fixture.adapter.calls), 'runtime': runtime.state,
            'preparation': [{'phase': row['phase'], 'parked_from_phase': row['parked_from_phase']} for row in rows],
            'batches': plain(batches), 'media_work': plain(work),
            'receipt': encode_value(receipt_value(receipt.value), 65536).decode() if type(receipt) is Found else None}), flush=True)
    finally: await fixture.close()


if __name__ == '__main__': asyncio.run(main(Path(sys.argv[1]), *sys.argv[2:]))
