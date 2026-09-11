"""Independent configuration publisher with explicit real-COMMIT barriers.

The supervising process retains database identity before launching this worker.
A barrier is reached only by the transaction that wrote the new snapshot row.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

from companion_memory.configuration.content_persistence import ContentConfigurationAssembly
from companion_memory.configuration.content_persistent_results import ConfigurationCommitted
from companion_memory.persistence import DatabaseResources, PersistenceService, Ready
from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from tests.configuration.content_support import candidate
from tests.persistence.support import Hooks


async def main(root: Path, mode: str, barrier: str) -> None:
    value, supplied = candidate(root)
    retained = root / 'expected-identity.json'
    expected = json.loads(retained.read_text())
    hooks = Hooks()
    has_snapshot = False

    def before(sql: str) -> None:
        nonlocal has_snapshot
        if sql.startswith('INSERT INTO configuration_snapshots'):
            has_snapshot = True
        if sql == 'COMMIT' and has_snapshot and barrier == 'before':
            os.write(1, b'BEFORE_COMMIT\n')
            sys.stdin.buffer.read(1)

    def after(sql: str) -> None:
        if sql == 'COMMIT' and has_snapshot and barrier == 'after':
            os.write(1, b'AFTER_COMMIT\n')
            sys.stdin.buffer.read(1)

    hooks.before, hooks.after = before, after
    assembly = ContentConfigurationAssembly()
    storage = PersistenceService(assembly.repositories, assembly.commands)
    resource = DatabaseResources(expected['identity'], lambda identity, path: expected == {'identity': identity, 'path': path}, connect=hooks.connect)
    ready = await storage.initialize(value.foundation, resource, mode)
    assert type(ready) is Ready, ready
    binding = assembly.bind(storage, 'instance')
    try:
        result = await binding.persist_content_configuration('original-config', value, actor='bootstrap', protected_directories=supplied[4])
        assert type(result) is ConfigurationCommitted and result.configuration is not None, result
        print(json.dumps({'source': result.source, 'snapshot_id': result.configuration.snapshot_id,
                          'receipt': encode_value(receipt_value(result.receipt), 65536).decode('ascii')}), flush=True)
    finally:
        binding.close()
        await storage.close()
        binding.close()


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
