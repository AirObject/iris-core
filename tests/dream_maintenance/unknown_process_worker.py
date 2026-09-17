"""Native dream UNKNOWN survives its process; a new host cannot clear or resend it."""
import asyncio
import json
from pathlib import Path
import sys
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from tests.provider.test_chat_transport import server
from .host_support import make_dream_host
from .seed_support import establish_memory


async def run(root:Path,mode:str,port:int):
    credentials=[];host=make_dream_host(root,port,credentials,with_self=True)
    try:
        assert type(await host.initialize(mode)) is Found
        c=host.combination.dream;provider=host.provider
        assert c is not None and provider is not None
        if mode=='CREATE_NEW':
            assert type(await host.register_entry('register','entry','host','sample_platform','external')) is Committed
            await establish_memory(host,with_self=True)
        admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+90))
        if mode=='CREATE_NEW':
            assert type(await admin.start_dream('start','run',1,1,mode='BACKGROUND')) is Committed
            assert type(await admin.resume_dream('resume','run',1,1)) is Committed
            for _ in range(3):
                value=await host.advance_dream();assert type(value) is Committed,value
        current=await c.inspect('run');assert current is not None
        assert current['state']=='RECOVERY_REQUIRED' and current['remote_result']=='UNKNOWN',current
        requests=await provider.ledger.read('requests_page',{'after':'','limit':8})
        assert len(requests)==1 and requests[0]['phase']=='REMOTE_RESULT_UNKNOWN',requests
        for operation,key in ((admin.resume_dream,'resume-unknown'),(admin.abort_dream,'abort-unknown')):
            try:result=await operation(key+'-'+mode,'run',cast(int,current['revision']),cast(int,current['mode_epoch']))
            except OwnerFailure:pass
            else:
                assert type(result) is Found,result
                value=result.value
                assert type(value) is MappingProxyType and value.get('state') in ('PENDING','RECOVERY_REQUIRED'),result
        after=await c.inspect('run');assert after is not None and after['remote_result']=='UNKNOWN'
        assert not c.dispatch_enabled
        assert await provider.ledger.read('requests_page',{'after':'','limit':8})==requests
        print(json.dumps({'mode':mode,'state':after['state'],'remote_result':after['remote_result'],'request_id':requests[0]['object_id'],
            'request_phase':requests[0]['phase'],'credentials':len(credentials),'attempts':len(requests),'dispatch':c.dispatch_enabled}),flush=True)
        if mode=='OPEN_EXISTING':assert not credentials
    finally:assert await host.close()


if __name__=='__main__':
    root=Path(sys.argv[1]);mode=sys.argv[2]
    if mode=='CREATE_NEW':
        with server(b'') as (port,requests,failures):
            asyncio.run(run(root,mode,port));assert len(requests)==1 and not failures
    else:asyncio.run(run(root,mode,9))
