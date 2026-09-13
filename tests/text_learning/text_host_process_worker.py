"""New-interpreter local recovery of the complete host and original terminals."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from companion_memory.persistence import Found,Committed
from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from companion_memory.memory.formats import record
from tests.text_learning.test_text_host import make_host


async def main():
    host=make_host(Path(sys.argv[1]),int(sys.argv[2]));receipts=[]
    try:
        opened=await host.initialize('OPEN_EXISTING')
        assert type(opened) is Found,opened
        assert host.runtime is not None
        entry=host.runtime.bind_entry('entry')
        for key in sys.argv[3:]:
            original=await entry.run_learning(key)
            assert type(original) is Committed,original
            receipts.append(hashlib.sha256(encode_value(receipt_value(original.receipt),65536)).hexdigest())
        ready=record(opened.value)['learning_ready']
    finally:
        closed=await host.close()
    print(json.dumps({'learning_ready':ready,'receipts':receipts,'closed':closed}),flush=True)


if __name__=='__main__':asyncio.run(main())
