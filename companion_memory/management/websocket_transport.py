"""Bounded wsproto transport with an explicit synchronous first-write callback.

The transport owns only framing, finite buffers and socket completion. Domain
owners register and settle deliveries. A queued item is not a send receipt.
Compression and binary application frames are deliberately unsupported.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import time
from types import MappingProxyType
from typing import cast
from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, BytesMessage, CloseConnection, Ping, Pong, Request, TextMessage
from wsproto.utilities import LocalProtocolError, RemoteProtocolError
from companion_memory.persistence.schema import Value, freeze_value
from .communication_protocol import LIMITS, SCHEMAS, SUBPROTOCOL
from .managed_http import object_pairs, public_value

Message = MappingProxyType[str, Value]
Handler = Callable[[Message], Awaitable[None]]


@dataclass(slots=True)
class Outbound:
    text: str
    byte_count: int
    first_write: Callable[[bytes], bool]
    complete: asyncio.Future[bool]


def decode_message(encoded: str) -> Message:
    """Reject duplicate fields, excess nesting, versions and full-message limits."""
    value = json.loads(encoded, object_pairs_hook=object_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite value.')))
    if type(value) is not dict or value.get('type') not in ('authenticate', 'subscribe', 'unsubscribe', 'ack'):
        raise ValueError('Unsupported message.')
    kind = cast(str, value['type'])
    if len(encoded.encode('utf-8')) > LIMITS[kind]:
        raise ValueError('Message limit.')
    return cast(Message, freeze_value(SCHEMAS[kind], value))


class WebSocketTransport:
    """One bounded connection; queued and draining items share the same budget."""
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 handler: Handler, *, queue_items: int = 16, queue_bytes: int = 32768):
        self.reader, self.writer, self.handler = reader, writer, handler
        self.protocol = WSConnection(ConnectionType.SERVER)
        self.queue: asyncio.Queue[Outbound] = asyncio.Queue(queue_items)
        self.queue_limit, self.byte_limit = queue_items, queue_bytes
        self.pending: set[asyncio.Future[bool]] = set()
        self.buffered_bytes = 0
        self.closed = False
        self.last_ping = time.monotonic()
        self.ping_payload: bytes | None = None
        self.ping_started = 0.0
        self.send_task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.messages_written = 0
        self.messages_received = 0
        self.peak_queue_bytes = 0
        self.peak_write_buffer_bytes = 0
        self.control_frames_written = 0
        self.backpressure_closed = False
        self.writer.transport.set_write_buffer_limits(high=queue_bytes, low=0)

    def accept(self, headers: dict[str, str], path: str) -> bytes:
        """Prepare the validated upgrade; caller authorizes the actual first write."""
        # Browsers advertise compression by default. Accept no extensions;
        # their offer is not permission to compress this connection.
        self.protocol.initiate_upgrade_connection([(k.encode('ascii'), v.encode('ascii')) for k, v in headers.items()], path)
        events = tuple(self.protocol.events())
        if len(events) != 1 or type(events[0]) is not Request or SUBPROTOCOL not in events[0].subprotocols:
            raise ValueError('Subprotocol required.')
        return self.protocol.send(AcceptConnection(subprotocol=SUBPROTOCOL))

    def has_capacity(self, byte_count: int = 2048) -> bool:
        return not self.closed and len(self.pending) < self.queue_limit and self.buffered_bytes + byte_count <= self.byte_limit

    def enqueue(self, kind: str, value: object, first_write: Callable[[bytes], bool] | None = None) -> asyncio.Future[bool]:
        """Admit a whole encoded message or reject without touching the socket."""
        frozen = freeze_value(SCHEMAS[kind], value)
        text = json.dumps(public_value(frozen), ensure_ascii=False, separators=(',', ':'))
        size = len(text.encode('utf-8'))
        if size > LIMITS[kind]:
            raise ValueError('Encoded message limit.')
        done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        if self.closed or len(self.pending) >= self.queue_limit or self.buffered_bytes + size > self.byte_limit:
            done.set_result(False)
            if not self.closed:
                self.closed = True
                self.writer.close()
            return done
        self.pending.add(done)
        self.buffered_bytes += size
        self.peak_queue_bytes = max(self.peak_queue_bytes, self.buffered_bytes)
        self.queue.put_nowait(Outbound(text, size, first_write or self.write, done))
        return done

    def write(self, data: bytes) -> bool:
        """Bound the actual transport buffer before the synchronous handoff.

        Application queues and control frames share this last I/O budget. Abort
        an overloaded socket rather than retaining an undrained close buffer.
        """
        if self.closed or self.writer.is_closing():
            return False
        if self.writer.transport.get_write_buffer_size() + len(data) > self.byte_limit:
            self.backpressure_closed = self.closed = True
            self.writer.transport.abort()
            return False
        self.writer.write(data)
        self.peak_write_buffer_bytes = max(self.peak_write_buffer_bytes, self.writer.transport.get_write_buffer_size())
        return True

    async def _control(self, event: Ping | Pong | CloseConnection) -> None:
        """One bounded control frame per reader/heartbeat worker, with I/O drain."""
        if self.write(self.protocol.send(event)):
            self.control_frames_written += 1
            await asyncio.wait_for(self.writer.drain(), 10)

    async def _send(self) -> None:
        while not self.closed:
            item = await self.queue.get()
            sent = False
            try:
                if not self.closed:
                    wire = self.protocol.send(TextMessage(data=item.text))
                    sent = item.first_write(wire)
                    if sent:
                        self.messages_written += 1
                        await asyncio.wait_for(self.writer.drain(), 12)
            finally:
                self.buffered_bytes -= item.byte_count
                self.pending.discard(item.complete)
                if not item.complete.done(): item.complete.set_result(sent)
                self.queue.task_done()

    async def _heartbeat(self) -> None:
        while not self.closed:
            await asyncio.sleep(1)
            now = time.monotonic()
            if self.ping_payload is not None:
                if now - self.ping_started >= 10:
                    self.closed = True
                    self.writer.close()
                    return
            elif now - self.last_ping >= 20:
                self.ping_payload = str(now).encode('ascii')
                self.ping_started = now
                await self._control(Ping(payload=self.ping_payload))

    async def run(self) -> None:
        """Bound reassembly before dispatch and retain workers through socket close."""
        self.send_task = asyncio.create_task(self._send())
        self.heartbeat_task = asyncio.create_task(self._heartbeat())
        def failed(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                self.closed = True
                self.writer.close()
        self.send_task.add_done_callback(failed)
        self.heartbeat_task.add_done_callback(failed)
        fragments: list[str] = []
        byte_count = 0
        try:
            while not self.closed:
                data = await self.reader.read(4096)
                if not data: break
                self.protocol.receive_data(data)
                for event in self.protocol.events():
                    if isinstance(event, TextMessage):
                        byte_count += len(event.data.encode('utf-8'))
                        if byte_count > 8192 or len(fragments) >= 8192:
                            raise ValueError('Reassembly limit.')
                        fragments.append(event.data)
                        if event.message_finished:
                            message = decode_message(''.join(fragments))
                            fragments.clear(); byte_count = 0
                            self.messages_received += 1
                            await self.handler(message)
                    elif isinstance(event, BytesMessage):
                        raise ValueError('Binary application frames unavailable.')
                    elif isinstance(event, Ping):
                        await self._control(event.response())
                    elif isinstance(event, Pong):
                        if self.ping_payload is not None and event.payload == self.ping_payload:
                            self.ping_payload = None
                            self.last_ping = time.monotonic()
                    elif isinstance(event, CloseConnection):
                        await self._control(event.response())
                        return
        except (ValueError, RecursionError, UnicodeError, RemoteProtocolError, LocalProtocolError):
            if not self.closed:
                try: await self._control(CloseConnection(code=1002, reason='PROTOCOL_ERROR'))
                except (LocalProtocolError, OSError, TimeoutError): pass
        except (OSError, TimeoutError):
            self.writer.transport.abort()
        finally:
            await self.close()

    async def close(self) -> None:
        """End actual network workers before releasing their connection resources."""
        self.closed = True
        self.writer.close()
        workers = tuple(task for task in (self.send_task, self.heartbeat_task) if task is not None)
        for task in workers:
            if not task.done(): task.cancel()
        if workers: await asyncio.gather(*workers, return_exceptions=True)
        while not self.queue.empty():
            item = self.queue.get_nowait()
            self.buffered_bytes -= item.byte_count
            self.pending.discard(item.complete)
            if not item.complete.done(): item.complete.set_result(False)
            self.queue.task_done()
        try: await asyncio.wait_for(self.writer.wait_closed(), 12)
        except (OSError, TimeoutError):
            self.writer.transport.abort()
