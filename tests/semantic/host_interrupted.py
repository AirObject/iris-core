"""Fresh process for interrupted native registration and zero-send recovery."""
from types import MappingProxyType
import asyncio
import json
from pathlib import Path
import sys
from companion_memory.persistence import Found
from companion_memory.persistence.semantic_records import identity
from tests.semantic.test_semantic_host import make_host,opened
from tests.semantic.public_support import establish,activate


async def main():
    root=Path(sys.argv[2])
    if sys.argv[1]=='send':
        host,oid=await establish(root,int(sys.argv[3]));activate(host)
        assert host.semantic is not None
        await host.semantic.resume('resume')
        work=await host.semantic.prepare_document(oid,'interrupted-document','fixture-partition');assert type(work) is str,work
        print(json.dumps({'work_id':work}),flush=True)
        await host.semantic.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
        raise AssertionError('Parent must stop this process while the actual wire is held.')
    host=make_host(root,int(sys.argv[3]))
    try:
        assert type(await opened(host,'OPEN_EXISTING')) is Found
        assert host.embedding is not None and host.semantic is not None
        assert host.embedding.executions==0 and host.authorization is None
        requests=await host.embedding.ledger.read('requests_page',{'after':'','limit':8})
        assert len(requests)==1 and requests[0]['phase']=='REMOTE_RESULT_UNKNOWN',requests
        result=await host.semantic.run_work(sys.argv[4]);assert type(result) is MappingProxyType,result
        assert result['state']=='REMOTE_UNKNOWN' and result['cleanup_pending'] is False,result
        repeat=await host.semantic.run_work(sys.argv[4]);assert type(repeat) is MappingProxyType,repeat;assert repeat==result
        assert host.embedding.executions==0
        print(json.dumps({'request_id':requests[0]['object_id'],'state':result['state'],'sends':0}),flush=True)
    finally:assert await host.close()


if __name__=='__main__':asyncio.run(main())
