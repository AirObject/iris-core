"""Actual generation setup helpers with simulated model inputs and no timer races."""
import asyncio
import time
from companion_memory.information.index_worker import LocalIndexWorker
from companion_memory.information.management import HostIdentity, ManagementPort
from companion_memory.information.records import Record, record, text
from companion_memory.persistence import Committed, Found
from companion_memory.runtime.information_host import InformationHost


def index_identity() -> HostIdentity:
    return HostIdentity('publication', 'principal', 'host', 'entry', frozenset(('index_begin', 'index_claim',
        'index_apply_object', 'index_confirm_page', 'index_publish', 'index_trim_page')), (), time.monotonic() + 300)


async def index_port(h: InformationHost) -> ManagementPort:
    registered = await h.register_entry('register', 'entry', 'host', 'sample_platform', 'conversation')
    if type(registered) is not Committed: raise RuntimeError('Expected actual registered entry.')
    return await h.bind_management(index_identity())


async def build(h: InformationHost, port: ManagementPort, key: str) -> tuple[str, Record]:
    if h.retrieval is None or h.runtime is None: raise RuntimeError('Expected actual index owners.')
    coordinator, _ = await h.retrieval.query_generation()
    begun = await port.execute('index_begin', key, {'expected_generation': coordinator['active_generation']})
    if type(begun) is not Committed: raise RuntimeError('Actual rebuild registration failed: ' + repr(begun))
    gid = text(record(record(record(begun.receipt.result)['facts'])['retrieval'])['object_id'])
    worker = LocalIndexWorker(h.runtime, h.retrieval, port, 'publication')
    for _ in range(130):
        result = await worker.run(gid)
        await asyncio.sleep(0)
        if type(result) is Found: break
        if type(result) is not Committed: raise RuntimeError('Actual page failed: ' + repr(result))
    else: raise RuntimeError('Bounded fixture did not catch up.')
    generation, _ = await h.retrieval.work_page(gid)
    return gid, generation


async def publish(port: ManagementPort, key: str, generation: Record) -> Committed:
    result = await port.execute('index_publish', key, {'generation_id': generation['generation_id'], 'expected_revision': generation['revision']})
    if type(result) is not Committed: raise RuntimeError('Actual publication failed: ' + repr(result))
    return result
