"""A new interpreter verifies an existing periodic pointer without sending."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import cast
from companion_memory.persistence import Found
from companion_memory.self_model.current import Available
from .host_support import make_dream_host


async def reopen(root:Path,*,with_self:bool=False,other:bool=False):
    credentials=[];host=make_dream_host(root,9,credentials,with_self=with_self)
    if other:host.configure_entry('other','other-partition',('self',),({'kind':'REAL','context_id':None},))
    try:
        opened=await host.initialize('OPEN_EXISTING')
        if type(opened) is not Found or host.current_persona is None:raise AssertionError(opened)
        value=await host.current_persona.port.read_current(time.monotonic()+5)
        if type(value) is not Available:raise AssertionError(value)
        if credentials:raise AssertionError('Recovery dispatched a request')
        print(json.dumps({'revision':value.value['revision'],'publication_id':value.value['publication_id'],
            'text_digest':hashlib.sha256(cast(str,value.value['text']).encode()).hexdigest(),'new_credentials':len(credentials)}))
    finally:
        if not await host.close():raise AssertionError('Actual resource cleanup incomplete')


if __name__=='__main__':asyncio.run(reopen(Path(sys.argv[1]),with_self='--self' in sys.argv,other='--other' in sys.argv))
