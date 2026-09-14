"""Single-attempt bounded HTTP/TLS transport owned by the Provider worker.

There are no proxies, redirects, decompression, background retries or replacement
workers. DNS and socket work share an absolute deadline. An outer cancellation
does not release this transport's slot or credential before actual completion.
"""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import math
import socket
import ssl
import threading
from typing import Literal, cast
from urllib.parse import urlsplit
from .credentials import Available, CredentialLease, CredentialResolver
from .resources import CancellationToken, native_issued
from .values import Record, InvalidData
from .wire_evidence import WireEvidence


class WireFailure(Exception):
    """Safe protocol failure; neither response bytes nor credentials escape."""


@dataclass(frozen=True, slots=True)
class WireObservation:
    """HTTP facts do not themselves prove a durable or remote business terminal."""
    state: Literal['NOT_SENT', 'RESPONSE', 'REMOTE_RESULT_UNKNOWN']
    status: int | None
    body: bytes | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class _Endpoint:
    host: str
    port: int
    path: str
    tls: bool
    context: ssl.SSLContext | None


class _Reader:
    """Cumulative body/header bounds apply to every framing branch and trailer."""
    def __init__(self, connection: socket.socket, settings: Record, deadline: float,
                 monotonic: Callable[[], float], cancellation: CancellationToken):
        self.connection, self.settings, self.deadline = connection, settings, deadline
        self.monotonic, self.cancellation = monotonic, cancellation
        self.buffer = bytearray()
        self.header_bytes = self.header_count = 0

    def check(self) -> None:
        remaining = self.deadline - self.monotonic()
        if remaining <= 0 or self.cancellation.cancelled:
            raise TimeoutError()
        self.connection.settimeout(min(remaining, cast(int, self.settings['read_timeout_ms']) / 1000))

    def receive(self) -> bytes:
        self.check()
        return self.connection.recv(cast(int, self.settings['chunk_bytes']))

    def line(self) -> bytes:
        limit = cast(int, self.settings['headers_max_bytes'])
        while True:
            position = self.buffer.find(b'\r\n')
            if position >= 0:
                size = position + 2
                self.header_bytes += size
                if self.header_bytes > limit:
                    raise WireFailure()
                result = bytes(self.buffer[:position])
                del self.buffer[:size]
                return result
            if self.header_bytes + len(self.buffer) > limit:
                raise WireFailure()
            block = self.receive()
            if not block:
                raise WireFailure()
            self.buffer.extend(block)

    def headers(self) -> dict[bytes, bytes]:
        result: dict[bytes, bytes] = {}
        while line := self.line():
            self.header_count += 1
            if self.header_count > cast(int, self.settings['header_count']):
                raise WireFailure()
            name, separator, value = line.partition(b':')
            if (not separator or not name or any(c not in b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in name)
                    or any(c < 32 and c != 9 or c == 127 for c in value)):
                raise WireFailure()
            name = name.lower()
            if name in result:
                raise WireFailure()
            result[name] = value.strip()
        return result

    def exact(self, count: int) -> bytes:
        output = bytearray()
        while len(output) < count:
            if not self.buffer:
                self.buffer.extend(self.receive())
                if not self.buffer:
                    raise WireFailure()
            size = min(count - len(output), len(self.buffer))
            output.extend(self.buffer[:size])
            del self.buffer[:size]
        return bytes(output)

    def body(self, headers: dict[bytes, bytes]) -> bytes:
        maximum = cast(int, self.settings['response_max_bytes'])
        if headers.get(b'content-encoding', b'identity').lower() != b'identity':
            raise WireFailure()
        length, encoding = headers.get(b'content-length'), headers.get(b'transfer-encoding')
        if length is not None and encoding is not None:
            raise WireFailure()
        if encoding is not None:
            if encoding.lower() != b'chunked':
                raise WireFailure()
            result = bytearray()
            while True:
                line = self.line()
                if not line or len(line) > 16 or any(c not in b'0123456789abcdefABCDEF' for c in line):
                    raise WireFailure()
                count = int(line, 16)
                if count > maximum - len(result):
                    raise WireFailure()
                if count == 0:
                    trailers = self.headers()
                    if any(key in trailers for key in (b'content-length', b'transfer-encoding', b'content-encoding')):
                        raise WireFailure()
                    return bytes(result)
                result.extend(self.exact(count))
                if self.exact(2) != b'\r\n':
                    raise WireFailure()
        if length is not None:
            if not length or len(length) > 10 or not length.isdigit() or int(length) > maximum:
                raise WireFailure()
            return self.exact(int(length))
        result = bytearray(self.buffer)
        self.buffer.clear()
        while len(result) <= maximum:
            block = self.receive()
            if not block:
                return bytes(result)
            result.extend(block)
        raise WireFailure()


class ChatTransport:
    """Fixed endpoint and reference-bound transport; one actual consumer at a time."""
    def __init__(self, settings: Record, resolver: CredentialResolver, monotonic: Callable[[], float], *, evidence: WireEvidence|None=None):
        from companion_memory.configuration.text_validation import validate_transport
        validate_transport(settings)
        if type(resolver) is not CredentialResolver or not callable(monotonic) or evidence is not None and type(evidence) is not WireEvidence:
            raise InvalidData()
        parsed = urlsplit(cast(str, settings['origin']))
        self._endpoint = _Endpoint(cast(str, parsed.hostname), 443,
            cast(str, settings['base_path']) + cast(str, settings['endpoint_path']), True, ssl.create_default_context())
        self._settings, self._resolver, self._monotonic = settings, resolver, monotonic
        self._slot = threading.Lock()
        self._evidence=evidence

    @classmethod
    def controlled_loopback(cls, settings: Record, resolver: CredentialResolver, monotonic: Callable[[], float],
                            port: int, context: ssl.SSLContext | None = None) -> ChatTransport:
        """Explicit test resource restricted to a literal loopback address.

        This binding cannot redirect to a supplier or accept a caller-selected URL.
        Custom TLS trust is permitted only here and still verifies certificates.
        """
        if type(port) is not int or not 1 <= port <= 65535:
            raise InvalidData()
        if context is not None and (type(context) is not ssl.SSLContext or not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED):
            raise InvalidData()
        value = cls(settings, resolver, monotonic)
        value._endpoint = _Endpoint(str(ipaddress.ip_address('127.0.0.1')), port,
            value._endpoint.path, context is not None, context)
        return value

    def exchange(self, body: bytes, deadline: float, cancellation: CancellationToken) -> WireObservation:
        """Perform one POST; only full framed responses return bounded raw bytes."""
        if self._evidence is not None:self._evidence.request(body)
        result=self._exchange(body,deadline,cancellation)
        if self._evidence is not None:self._evidence.response(result.state,result.status,result.body,result.reason)
        return result

    def _exchange(self, body: bytes, deadline: float, cancellation: CancellationToken) -> WireObservation:
        if (type(body) is not bytes or len(body) > 131072 or not body
                or type(deadline) not in (float, int) or not math.isfinite(deadline)
                or not native_issued(cancellation, CancellationToken)):
            return WireObservation('NOT_SENT', None, None, 'INVALID_REQUEST')
        if not self._slot.acquire(blocking=False):
            return WireObservation('NOT_SENT', None, None, 'RESOURCE_BUSY')
        lease: CredentialLease | None = None
        connection: socket.socket | None = None
        sent = False
        status: int | None = None
        try:
            if cancellation.cancelled or self._monotonic() >= deadline:
                return WireObservation('NOT_SENT', None, None, 'CANCELLED')
            resolved = self._resolver.resolve(*(cast(str, self._settings[key]) for key in ('secret_ref', 'secret_revision', 'account_ref')))
            if type(resolved) is not Available:
                return WireObservation('NOT_SENT', None, None, 'CREDENTIAL_UNAVAILABLE')
            lease = resolved.lease
            endpoint = self._endpoint
            addresses = socket.getaddrinfo(endpoint.host, endpoint.port, type=socket.SOCK_STREAM)
            if not addresses:
                raise OSError()
            family, kind, protocol, _, address = addresses[0]
            connection = socket.socket(family, kind, protocol)
            remaining = deadline - self._monotonic()
            if remaining <= 0 or cancellation.cancelled:
                raise TimeoutError()
            connect_deadline = min(deadline, self._monotonic() + cast(int, self._settings['connect_timeout_ms']) / 1000)
            connection.settimeout(max(0.0, connect_deadline - self._monotonic()))
            connection.connect(address)
            if endpoint.tls:
                assert endpoint.context is not None
                remaining = connect_deadline - self._monotonic()
                if remaining <= 0 or cancellation.cancelled:
                    raise TimeoutError()
                connection.settimeout(remaining)
                connection = endpoint.context.wrap_socket(connection, server_hostname=endpoint.host)
            reader = _Reader(connection, self._settings, deadline, self._monotonic, cancellation)
            reader.check()
            host = endpoint.host if endpoint.port == 443 else endpoint.host + ':' + str(endpoint.port)
            header = (f'POST {endpoint.path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n'
                      f'Accept-Encoding: identity\r\nConnection: close\r\nContent-Length: {len(body)}\r\nAuthorization: Bearer ').encode('ascii')
            # From the first send onward a partial write is remote uncertainty.
            sent = True
            if not lease._send_header(connection, header):
                sent = False
                return WireObservation('NOT_SENT', None, None, 'CREDENTIAL_UNAVAILABLE')
            del header
            reader.check()
            connection.sendall(body)
            line = reader.line().split(b' ', 2)
            if len(line) != 3 or line[0] not in (b'HTTP/1.0', b'HTTP/1.1') or len(line[1]) != 3 or not line[1].isdigit():
                raise WireFailure()
            status = int(line[1])
            if not 200 <= status <= 599:
                raise WireFailure()
            headers = reader.headers()
            if 300 <= status <= 399:
                return WireObservation('RESPONSE', status, None, 'REDIRECT_REJECTED')
            return WireObservation('RESPONSE', status, reader.body(headers), None)
        except (OSError, ValueError, WireFailure):
            return WireObservation('REMOTE_RESULT_UNKNOWN' if sent else 'NOT_SENT', status, None, 'TRANSPORT_FAILED')
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                try:
                    if lease is not None:
                        lease.release()
                finally:
                    self._slot.release()
