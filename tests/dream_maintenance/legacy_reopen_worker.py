"""Run against an explicitly selected original or current daily implementation."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from companion_memory.persistence import Found,Committed
from tests.daily_cognition.test_host import make_host


async def run(root:Path,mode:str):
    root.mkdir(mode=0o700,exist_ok=True)
    credentials=[];host=make_host(root,9,credentials)
    try:
        opened=await host.initialize(mode)
        if type(opened) is not Found:raise AssertionError(opened)
        original=await host.register_entry('legacy-register','entry','host','sample_platform','external')
        if type(original) is not Committed:raise AssertionError(original)
        if mode=='OPEN_EXISTING' and original.source!='EXISTING':raise AssertionError('Old key was not confirmed')
        if credentials or host.scheduling():raise AssertionError('Recovery opened remote dispatch')
        with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
            raw=db.execute('SELECT assembly FROM application_metadata').fetchone()[0]
            requests=db.execute('SELECT count(*) FROM provider_requests').fetchone()[0]
        print(json.dumps({'mode':mode,'schema_digest':sha256(raw).hexdigest(),'schema_bytes':len(raw),
            'commit_id':original.receipt.commit_id,'fingerprint':original.receipt.fingerprint,'source':original.source,
            'requests':requests,'credentials':len(credentials),'commands':len(host.combination.commands)}))
    finally:
        if not await host.close():raise AssertionError('Physical resources remain')


if __name__=='__main__':asyncio.run(run(Path(sys.argv[1]),sys.argv[2]))
