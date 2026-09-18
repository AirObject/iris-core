"""Closed host media adapters; all files and durable facts stay with media."""
from __future__ import annotations

from companion_memory.media.service import MediaUploadPort, MediaUploadResult
from companion_memory.persistence.owned_statements import OwnerFailure


async def dispatch_media(port: MediaUploadPort, action: str, value: dict[str, object]) -> MediaUploadResult:
    """Dispatch only the declared small commands or one admitted binary block."""
    from .managed_application import fields
    if action in ('begin', 'resolve', 'inspect'):
        fields(value, {'key', 'modality'})
        if action == 'begin':
            return await port.begin_upload(value['key'], value['modality'])
        if action == 'resolve':
            return await port.resolve_upload(value['key'], value['modality'])
        return await port.inspect_upload(value['key'], value['modality'])
    if action == 'finish':
        fields(value, {'upload_id'})
        return await port.finish_upload(value['upload_id'])
    if action == 'chunk':
        fields(value, {'upload_id', 'offset', 'data'})
        return await port.append_upload(value['upload_id'], value['offset'], value['data'])
    raise OwnerFailure('ACCESS_DENIED', 'scope', 'OPERATION_NOT_GRANTED')
