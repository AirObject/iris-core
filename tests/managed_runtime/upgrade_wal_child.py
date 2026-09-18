"""Native predecessor writer deliberately exits after a durable WAL-only commit."""
import asyncio
import json
import os
from pathlib import Path
import sys
import sqlite3
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.persistence import Ready, Committed
from tests.runtime.configuration_support import event
from .test_business import controlled_resources


async def main() -> None:
    root=Path(sys.argv[1])
    # A legal concurrent reader pins the old snapshot so native PASSIVE
    # checkpoints cannot copy the later commits into the main file.
    reader=sqlite3.connect((root/'db/memory.sqlite3').as_uri()+'?mode=ro',uri=True)
    reader.execute('BEGIN');reader.execute('SELECT count(*) FROM application_metadata').fetchone()
    bootstrap=ManagedBootstrap(resolve_deployment({'deployment.data_root':str(root)}),communication_format=False)
    assert type(await bootstrap.open()) is Ready
    app=ManagedApplication(bootstrap,resource_factory=controlled_resources(1))
    await app.business.recover()
    if app.business.task is not None: await app.business.task
    host=app.business.host;assert host is not None
    payload=event('wal-only-event','崩溃前已提交且必须保留的输入')|{'event_version':2}
    result=await host.bind_entry('entry').accept_event('wal-only-key',payload)
    assert type(result) is Committed,result
    print(json.dumps({'commit_id':result.receipt.commit_id,'supplier_requests':0}),flush=True)
    os._exit(86)


if __name__=='__main__':asyncio.run(main())
