"""Synthetic bounded roster and fresh-interpreter original receipt verification."""
import asyncio
from pathlib import Path
import sys
import time
import hashlib
from companion_memory.persistence import Found,Committed
from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from tests.text_learning.test_text_host import make_host
from tests.text_learning.host_driver import confirm_local


def roster(instance: str='instance',*,maximum: bool=False) -> tuple[dict,...]:
    kinds=('PLATFORM_PERSON','PLATFORM_PERSON','FICTIONAL_CHARACTER','FICTIONAL_CHARACTER','CONTEXT','CONTEXT')
    return tuple({'subject_version':1,'subject_id':('subject-'+str(i)).ljust(128,'x') if maximum else 'subject-'+str(i),
        'instance_id':instance,'kind':kind,'platform_id':'test-platform','external_subject_id':str(i).ljust(320,'e') if maximum else str(i),
        'label':('字'*85+'x') if maximum else 'Synthetic '+str(i),'revision':1} if kind=='PLATFORM_PERSON' else
        {'subject_version':1,'subject_id':('subject-'+str(i)).ljust(128,'x') if maximum else 'subject-'+str(i),
        'instance_id':instance,'kind':kind,'platform_id':None,'external_subject_id':None,'label':('字'*85+'x') if maximum else 'Synthetic '+str(i),'revision':1}
        for i,kind in enumerate(kinds))


async def join(host):
    if host.persona is not None and host.persona._task is not None:await host.persona._task


async def worker(root: Path,mode: str):
    host=make_host(root.resolve(),1)
    try:
        assert type(await confirm_local(lambda:host.initialize(mode))) is Found
        port=host.initialization_port()
        assert type(await confirm_local(lambda:port.register_initial_self('input','PRESET','Synthetic operator input.','ACTUAL_INPUT',time.monotonic()+5))) is Committed
        await join(host)
        value=await confirm_local(lambda:port.register_initial_subjects('roster',roster(),'ACTUAL_INPUT',time.monotonic()+5))
        await join(host);assert type(value) is Committed,value
        print(value.source,hashlib.sha256(encode_value(receipt_value(value.receipt),65536)).hexdigest())
    finally:assert await host.close()

if __name__=='__main__':asyncio.run(worker(Path(sys.argv[1]),sys.argv[2]))
