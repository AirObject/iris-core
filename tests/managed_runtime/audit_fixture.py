"""Explicit synthetic participant producing deleted history for real browser reads.

Only a disposable, already reviewed instance is accepted. Model transport is
loopback-only; all object changes and history reads use the native domain ports.
The process retains the stopped-volume lease until actual cleanup completes.
"""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from companion_memory.configuration.deployment import environment_settings
from companion_memory.persistence import Ready
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_business import ManagedBusiness
from tests.daily_cognition.test_reasoning import responses
from .browser_fixture import deny_external_connections
from .memory_support import exercise_memory, shared_formal_response
from .test_business import controlled_resources


async def produce(port: int, target: Path) -> None:
    bootstrap = ManagedBootstrap(environment_settings())
    business = None
    try:
        assert type(await bootstrap.open()) is Ready
        identity = bootstrap.assembly.identity; assert identity is not None
        business = ManagedBusiness(bootstrap, identity, resource_factory=controlled_resources(port))
        recovered = await business.recover()
        assert recovered['state'] == 'READY'
        permission = await identity.rows.read('model_dispatch', 'model-dispatch')
        assert permission is not None
        revision = permission['revision']; assert type(revision) is int
        from tests.runtime.configuration_support import event
        from uuid import uuid4
        host = business.host; assert host is not None
        value = event('audit-history-target', '合成历史审核目标。'); value['event_version'] = 2
        from companion_memory.persistence import Committed
        assert type(await host.bind_entry('entry').accept_event('audit-history-target', value)) is Committed
        scope = await exercise_memory(unittest.TestCase(), business, revision, 'fixture-dispatch-' + uuid4().hex, 'fixture-learning-' + uuid4().hex)
        resources = bootstrap.resources; assert resources is not None
        with target.open('x') as stream:
            json.dump(scope, stream)
        target.chmod(0o600)
    finally:
        if business is not None:
            while not await business.close(): await asyncio.sleep(0.1)
        while not await bootstrap.close(): await asyncio.sleep(0.1)


def main() -> None:
    sys.addaudithook(deny_external_connections)
    with responses((shared_formal_response,)) as (port, requests, failures):
        asyncio.run(produce(port, Path(sys.argv[1])))
        assert len(requests) == 1 and not failures
        print(json.dumps({'participant': 'SYNTHETIC_BROWSER_REVIEWER', 'transport': 'CONTROLLED_LOOPBACK',
            'model_requests': len(requests), 'external_requests': 0, 'history': 'DELETED_OBJECT_REMAINS_ABSENT'}))


if __name__ == '__main__': main()
