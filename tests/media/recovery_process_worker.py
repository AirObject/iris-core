"""New media interpreter rechecks actual files after a killed recovery scan."""
import asyncio
import json
import os
from pathlib import Path
import sys
from companion_memory.media.service import MediaService
from tests.memory.support import Fixture


async def main(root: Path, interrupt: bool):
    media = MediaService(); fixture = Fixture(root, media)
    inspected = []
    original_io = media._io
    async def inspect(key, work):
        result = await original_io(key, work)
        if key.startswith('recover:'):
            inspected.append(key)
            if interrupt and len(inspected) == 17:
                os.write(1, b'RECOVERY_PAUSED\n'); sys.stdin.buffer.read(1)
        return result
    media._io = inspect
    try:
        await fixture.initialize('OPEN_EXISTING')
        print(json.dumps({'ready': media._ready, 'models': len(fixture.adapter.calls),
            'files': len(tuple(media.published.iterdir())), 'inspected': inspected}), flush=True)
    finally: await fixture.close()


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2] == 'interrupt'))
