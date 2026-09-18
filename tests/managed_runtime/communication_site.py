"""Disposable real browser site with a reviewed synthetic model participant.

The normal goal scheduler remains active. No production provider is constructed
or reachable from this controlled resource factory; startup performs one local
synthetic persona generation and then pauses model dispatch.
"""
import argparse
import asyncio
import json
from pathlib import Path
import signal
import unittest
from companion_memory.persistence import Committed
from companion_memory.runtime.managed_logging import ManagedLogging
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from .communication_live_support import ready_application


async def serve(root: Path, evidence: Path):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    with responses((persona,)) as (port, requests, failures):
        test = unittest.TestCase()
        app, http = await ready_application(test, root, port, port=18080,
            static_root=Path('/workspace/web/dist'), origin='https://localhost')
        logging = ManagedLogging()
        try:
            logging.open(app.bootstrap)
            app.logging = logging
            await logging.attach(app)
            manager = app.business.configuration
            assert manager is not None
            consumer = manager.coordinator.consumers['media']
            publish = consumer.publish
            async def controlled_publish(version_id, resource):
                if (evidence / 'fail-consumer-publish').exists():
                    raise OSError('Synthetic consumer publication failure')
                await publish(version_id, resource)
            consumer.publish = controlled_publish
            result = await app.identity.establish('browser-admin', app.resources.read_secret('bootstrap').decode(), 'Synthetic-Communication-Only-123!')
            test.assertIs(type(result), Committed, result)
            (evidence / 'site-ready.json').write_text(json.dumps({'state': 'READY', 'pid': __import__('os').getpid(),
                'origin': 'https://localhost', 'models': 'LOCAL_SYNTHETIC_ONLY', 'supplier_requests': 0}))
            await stop.wait()
        finally:
            while not await http.close(): await asyncio.sleep(.1)
            while not await app.business.close(): await asyncio.sleep(.1)
            while not await logging.close(): await asyncio.sleep(.1)
            while not await app.bootstrap.close(): await asyncio.sleep(.1)
            (evidence / 'site-cleanup.json').write_text(json.dumps({'http_closed': http.closed,
                'storage': app.bootstrap.assembly.storage.get_health().lifecycle,
                'resource_fd': app.resources.fd, 'logging_closed': not logging.started,
                'mock_requests': len(requests), 'failures': failures, 'supplier_requests': 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.root, args.evidence))
