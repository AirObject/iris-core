"""Advance real media lifecycle against an explicit test-only wall-clock boundary."""
from unittest.mock import patch
from companion_memory.persistence import Found


async def expire_unbound(media):
    """Expire through the lifecycle API; leave GC inside its configured grace period."""
    rows = await media.rows.read('upload_page', {'after': '', 'limit': 16})
    now = max(media.upload_expires_at_us(row) for row in rows) + 1000
    with patch('companion_memory.media.service.time.time_ns', return_value=now * 1000):
        result = await media.recover_media()
    assert type(result) is Found, result
    return now + media.settings.integer('media.gc_unreferenced_grace_ms') * 1000 + 1000
