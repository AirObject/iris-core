"""Fresh interpreter confirmation of one publicly established fixed member."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import time
from companion_memory.persistence import Committed
from tests.semantic.fixed_support import FixedFixture


async def main():
    fixture=await FixedFixture(Path(sys.argv[1])).open(sys.argv[2])
    try:
        envelope=fixture.establishment(0)
        if sys.argv[2]=='CREATE_NEW':
            await fixture.prepare()
            result=await fixture.port.establish_one(envelope,time.monotonic()+5)
        else:result=await fixture.port.resolve('fixed_establish',envelope,time.monotonic()+5)
        assert type(result) is Committed,result
        assert fixture.fixed is not None
        assert await fixture.fixed.recover(time.monotonic()+5)
        with sqlite3.connect(fixture.root/'database/runtime.sqlite3') as connection:
            count=connection.execute('SELECT count(*) FROM memory_objects').fetchone()[0]
            state=connection.execute("SELECT json_extract(body,'$.state') FROM cognition_fixed_memory_set").fetchone()[0]
        print(json.dumps({'fingerprint':result.receipt.fingerprint,'objects':count,'state':state,'provider_calls':0}))
    finally:await fixture.close()


if __name__=='__main__':asyncio.run(main())
