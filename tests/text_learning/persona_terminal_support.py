"""Public-host fixture for known failures and native registration absence.

Every operation uses actual SQLite and the host's local management capability.
Synthetic protocol bytes are independent of native transaction evidence.
"""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import time
from typing import cast
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.text_records import stable_identity
from companion_memory.memory.formats import record
from tests.persistence.support import Hooks
from tests.text_learning.test_text_host import make_host
from tests.text_learning.host_driver import confirm_local
from tests.text_learning.test_provider import response


def length_response(usage: str = 'complete') -> bytes:
    body=json.loads(response('{"partial":').split(b'\r\n\r\n',1)[1])
    body['choices'][0]['finish_reason']='length'
    if usage=='missing':body.pop('usage')
    if usage=='invalid':body['usage']['total_tokens']=0
    encoded=json.dumps(body).encode()
    return f'HTTP/1.1 200 OK\r\nContent-Length: {len(encoded)}\r\n\r\n'.encode()+encoded


class PersonaScenario:
    """Own exactly one public host and its finite real database fixtures."""
    def __init__(self,root: Path,port: int,hooks: Hooks|None=None):
        self.root=root.resolve();self.host=make_host(self.root,port)
        if hooks is not None:
            resources=self.host.resources
            self.host.resources=replace(resources,database=replace(resources.database,connect=hooks.connect))
        self.run_id=stable_identity('persona-run','text-provider-database','instance')
        self.input_id=stable_identity('self-input','text-provider-database','instance')

    @property
    def api(self):return self.host.initialization_port()

    async def join(self):
        manager=self.host.persona;assert manager is not None
        if manager._task is not None:await manager._task

    async def start(self,mode='CREATE_NEW'):
        opened=await confirm_local(lambda:self.host.initialize(mode));assert type(opened) is Found,opened
        if mode=='CREATE_NEW':
            saved=await confirm_local(lambda:self.api.register_initial_self('input','NO_PRESET','No preset was supplied.','ACTUAL_INPUT',time.monotonic()+5))
            assert type(saved) is Committed,saved
            await self.join()
            saved=await confirm_local(lambda:self.api.prepare_initial_persona('prepare',self.input_id,1,1,time.monotonic()+5))
            assert type(saved) is Committed,saved
            await self.join()
        return opened

    async def run(self):
        owner=self.host.combination.persona.persona;assert owner is not None
        found=await owner.read_original('run',self.run_id,time.monotonic()+5);assert found is not None
        return found.value

    async def associate(self,key='associate'):
        current=await self.run()
        saved=await self.api.associate_initial_persona_request(key,self.run_id,cast(int,current['revision']),
            cast(int,current['generation']),self.host.gate.epoch,time.monotonic()+5)
        assert type(saved) is Committed,saved
        await self.join()
        return await self.run()

    async def candidate(self):
        pending=await self.api.read_pending(self.run_id,time.monotonic()+5);assert type(pending) is Found,pending
        await self.join()
        return record(pending.value)

    async def retry(self,key='retry'):
        current=await self.run()
        value=await self.api.retry_initial_persona(key,self.run_id,cast(int,current['revision']),cast(int,current['generation']),
            cast(str,current['resolution_id']),self.host.gate.epoch,time.monotonic()+5)
        await self.join()
        return value

    def rows(self,table: str):
        assert table in ('provider_requests','provider_attempts','provider_handoffs','provider_reservations','provider_budget_windows',
            'self_model_initial_persona_candidates','self_model_initial_persona_runs','audit_records','operation_receipts')
        with closing(sqlite3.connect(self.root/'database'/'runtime.sqlite3')) as connection:
            return connection.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()

    def bodies(self,table: str):
        assert table.startswith(('provider_','self_model_'))
        with closing(sqlite3.connect(self.root/'database'/'runtime.sqlite3')) as connection:
            return tuple(json.loads(row[0]) for row in connection.execute('SELECT body FROM '+table+' ORDER BY object_id'))
