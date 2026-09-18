"""Reopen a populated synthetic instance for independent HTTP result observation."""
import asyncio
import json
from pathlib import Path
import signal
import sys
from companion_memory.configuration.deployment import resolve_deployment
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.managed_http import ManagedHTTP
from companion_memory.persistence import Ready
from .test_business import controlled_resources


async def main():
    root=Path(sys.argv[1]);port=int(sys.argv[2]);stop=asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)
    settings=resolve_deployment({'deployment.data_root':str(root),'deployment.port':port,'deployment.origin':f'http://127.0.0.1:{port}'})
    bootstrap=ManagedBootstrap(settings);assert type(await bootstrap.open()) is Ready
    app=ManagedApplication(bootstrap,resource_factory=controlled_resources(1))
    await app.business.recover()
    if app.business.task is not None:await app.business.task
    http=ManagedHTTP(settings,app.identity,app.dispatch,app.health,root.parent/'communication-static',communication_source=lambda:app.business.communication)
    await http.start();print(json.dumps({'state':'READY'}),flush=True)
    try:await stop.wait()
    finally:
        while not await http.close():await asyncio.sleep(.05)
        while not await app.business.close():await asyncio.sleep(.05)
        await bootstrap.close()
        dispatcher=app.business.communication_dispatcher
        print(json.dumps({'storage':bootstrap.assembly.storage.get_health().lifecycle,'supplier_requests':0,
            'new_writes':dispatcher.writes if dispatcher else 0,'resource_fd':app.resources.fd}),flush=True)


if __name__=='__main__':asyncio.run(main())
