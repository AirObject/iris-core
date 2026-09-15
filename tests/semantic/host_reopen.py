"""Fresh-interpreter public semantic recovery; no activation or send authority."""
import asyncio
import json
from pathlib import Path
import sys
import time
from companion_memory.persistence import Found
from companion_memory.memory.formats import record
from companion_memory.information.management import HostIdentity
from tests.semantic.test_semantic_host import make_host,opened
from tests.information.test_queries import query


async def main():
    host=make_host(Path(sys.argv[1]),int(sys.argv[2]))
    try:
        result=await opened(host,'OPEN_EXISTING');assert type(result) is Found,result
        assert host.embedding is not None and host.semantic is not None
        assert host.embedding.executions==0 and not host._scheduling and host.authorization is None
        work=await host.semantic.work(sys.argv[3]);assert work['state']=='APPLIED' and work['cleanup_pending'] is False
        port=await host.bind_query(HostIdentity('fixture-query','principal','host','entry',frozenset(('search_memory','resolve_recall')),(),time.monotonic()+300))
        original=await port.resolve_recall(query('hybrid',query_text='query 0',retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False))
        assert type(original) is Found and record(original.value)['availability']=='CONFIRMED_ONLY',original
        response=await port.search_memory(query('reopened',query_text='query 0',retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False))
        assert type(response) is Found,response
        value=record(response.value);assert value['response_version']==2 and value['actual_mode']=='HYBRID',value
        assert host.embedding.executions==0
        print(json.dumps({'state':'RECOVERED','sends':0,'query':'HYBRID','original':'CONFIRMED_ONLY'}))
    finally:
        assert await host.close()


if __name__=='__main__':asyncio.run(main())
