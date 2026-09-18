"""h11 framing with bounded reads and synchronous response serialization.

One request is accepted per connection. Body admission happens before reading
body bytes; the application retains all actual work after transport timeouts.
"""
from __future__ import annotations

import asyncio
import h11

HEADER_LIMIT = 8192


async def read_head(reader: asyncio.StreamReader) -> tuple[h11.Connection, h11.Request]:
    """Bound the complete header block, then let h11 validate HTTP syntax."""
    raw = await reader.readuntil(b'\r\n\r\n')
    if len(raw) > HEADER_LIMIT:
        raise ValueError('Header limit.')
    # h11 combines identical Content-Length fields. The public protocol rejects
    # duplicate fields even when the HTTP parser could normalize them safely.
    names = [line.partition(b':')[0].lower() for line in raw.split(b'\r\n')[1:-2]]
    if len(names) != len(set(names)) or any(line[:1] in (b' ', b'\t') for line in raw.split(b'\r\n')[1:-2]):
        raise ValueError('Duplicate or folded header.')
    protocol = h11.Connection(h11.SERVER, max_incomplete_event_size=HEADER_LIMIT)
    protocol.receive_data(raw)
    event = protocol.next_event()
    if not isinstance(event, h11.Request):
        raise ValueError('Request header required.')
    return protocol, event


async def read_body(reader: asyncio.StreamReader, protocol: h11.Connection, length: int) -> bytes:
    """Read only the admitted byte length; reject trailers and framing mismatch."""
    body = bytearray()
    while True:
        event = protocol.next_event()
        if event is h11.NEED_DATA:
            chunk = await reader.read(min(65536, length - len(body) + 1))
            protocol.receive_data(chunk)
        elif isinstance(event, h11.Data):
            body.extend(event.data)
            if len(body) > length:
                raise ValueError('Body limit.')
        elif isinstance(event, h11.EndOfMessage):
            if event.headers or len(body) != length:
                raise ValueError('Body framing mismatch.')
            return bytes(body)
        else:
            raise ValueError('Incomplete request.')


def response_head(status: int, headers: list[tuple[str, str]]) -> tuple[h11.Connection, bytes]:
    """Serialize a response header without scheduling or starting network I/O."""
    protocol = h11.Connection(h11.SERVER)
    wire = protocol.send(h11.Response(status_code=status, headers=headers))
    assert wire is not None
    return protocol, wire


def response_bytes(status: int, body: bytes, headers: list[tuple[str, str]]) -> bytes:
    """Prepare all framing before the final permission and synchronous write."""
    protocol, head = response_head(status, headers)
    return head + (protocol.send(h11.Data(data=body)) or b'') + (protocol.send(h11.EndOfMessage()) or b'')
