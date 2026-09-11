"""Real isolated media process with parent-controlled publication/cleanup stops.

All files and SQLite writes belong to the supplied temporary root. The parent
confirms this interpreter's death before launching local recovery with no model.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import cast
from unittest.mock import patch
from companion_memory.media.service import MediaService, identity
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed
from tests.memory.support import Fixture
from tests.media.lifecycle_support import expire_unbound


def stop(point: str):
    os.write(1, (point + '\n').encode()); sys.stdin.buffer.read(1)


async def main(root: Path, mode: str, point: str):
    media = MediaService(); fixture = Fixture(root, media)
    expected = json.loads((root / 'expected.json').read_text())
    assert expected == {'database_id': fixture.expected_id, 'database_path': str(fixture.path), 'media_root': str(root / 'media')}
    await fixture.initialize(mode)
    original_execute = media._execute; original_sync = media._sync_directory
    async def execute(name, key, values):
        result = await original_execute(name, key, values)
        if type(result) is Committed and {'begin_media_upload': 'intent', 'seal_media_upload': 'sealed', 'publish_media_upload': 'ready', 'retire_media_blob': 'retired', 'delete_media_blob': 'deleted'}.get(name) == point:
            stop(point)
        return result
    deleting = False
    def sync(directory):
        if directory == media.published and point == ('unlinked' if deleting else 'published'): stop(point)
        original_sync(directory)
        if directory == media.published and point == ('gc_synced' if deleting else 'publication_synced'): stop(point)
    if mode == 'CREATE_NEW':
        media._execute = execute; media._sync_directory = sync
    try:
        if mode == 'CREATE_NEW':
            port = media.bind_upload('entry')
            begun = await port.begin_upload('original_upload', 'IMAGE'); assert type(begun) is Committed
            uid = cast(str, record(begun.receipt.result)['upload_id'])
            await port.append_upload(uid, 0, b'owned original bytes for local recovery')
            if point == 'partial': stop(point)
            ready = await port.finish_upload(uid); assert type(ready) is Committed
            if point in ('retired', 'unlinked', 'gc_synced', 'deleted'):
                future = await expire_unbound(media)
                deleting = True
                with patch('companion_memory.media.service.time.time_ns', return_value=future * 1000):
                    await media.collect_unreferenced()
        uploads = await media.rows.read('upload_page', {'after': '', 'limit': 16})
        blobs = await media.rows.read('recovery_page', {'after': '', 'limit': 16})
        print(json.dumps({'uploads': [r['state'] for r in uploads], 'blobs': [r['state'] for r in blobs],
            'files': [p.read_bytes().decode() for p in media.published.iterdir()], 'models': len(fixture.adapter.calls)}), flush=True)
    finally: await fixture.close()


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
