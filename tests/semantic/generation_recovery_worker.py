"""Fresh interpreter confirmation of existing generation and original receipt.

The fixture has no Provider adapter or credential resolver. Opening and reading
must use committed data and files without re-entering a write handler.
"""
import asyncio
import json
from pathlib import Path
import sys
from companion_memory.persistence import Found
from tests.semantic.generation_support import GenerationFixture


async def main(root: Path,generation: str) -> None:
    fixture=await GenerationFixture(root).open('OPEN_EXISTING')
    try:
        definition=next(d for d in fixture.semantic_commands.commands if d.operation_kind=='publish_generation')
        result=await fixture.storage.bind_operation(definition,'instance').read_receipt('publish:'+generation)
        assert type(result) is Found,result
        sealed=fixture.files.verify(fixture.coverage.space_id,generation,lambda:None)
        print(json.dumps({'generation':(await fixture.control())['current_generation'],'file_digest':sealed.file_digest,
            'receipt_commit_id':result.value.commit_id,'real_provider_calls':0}))
    finally:await fixture.close()


if __name__=='__main__':asyncio.run(main(Path(sys.argv[1]),sys.argv[2]))
