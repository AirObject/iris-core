"""Runnable standard-library HTTP/media and receipt-only WS client.

Example: IRIS_HOST_TOKEN=... python -m clients.iris_client --origin https://core.example
         --entry entry --route reminders
The token is read from the named environment variable, never a URL or log.
Notifications are acknowledged only when requested; reconnect never replays a
business command or a delivery. TLS certificate verification is always enabled.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import struct
from typing import Any
from urllib.parse import urlsplit


class IrisClient:
    """Synchronous independent HTTP implementation with exact original inputs."""
    def __init__(self, origin: str, token: str, *, timeout: float = 15, ca_file: str | None = None):
        parsed = urlsplit(origin)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('An HTTP(S) origin without credentials or path is required.')
        self.origin, self.token, self.timeout = origin, token, timeout
        self.host, self.port, self.secure = parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), parsed.scheme == 'https'
        self.authority = parsed.netloc
        self.context = ssl.create_default_context(cafile=ca_file)

    def request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(path, json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode(), {'Content-Type': 'application/json'})

    def _request(self, path: str, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        connection = (http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.context)
            if self.secure else http.client.HTTPConnection(self.host, self.port, timeout=self.timeout))
        try:
            connection.request('POST', path, body, {'Host': self.authority, 'Authorization': 'Bearer ' + self.token, **headers})
            response = connection.getresponse()
            encoded = response.read(1048577)
            if len(encoded) > 1048576: raise ValueError('Response limit.')
            value = json.loads(encoded)
            if type(value) is not dict or value.get('version') != 1: raise ValueError('Unsupported response.')
            return value
        finally:
            connection.close()

    def media(self, action: str, entry_id: str, original: dict[str, Any]) -> dict[str, Any]:
        if action not in ('begin', 'finish', 'resolve', 'inspect'): raise ValueError('Unsupported media command.')
        return self.request('/api/host/media/' + action, {'entry_id': entry_id, 'input': original})

    def chunk(self, entry_id: str, upload_id: str, offset: int, data: bytes) -> dict[str, Any]:
        if not 1 <= len(data) <= 65536 or not 0 <= offset <= 1048576 - len(data): raise ValueError('Chunk limit.')
        return self._request('/api/host/media/chunk', data, {'Content-Type': 'application/octet-stream',
            'X-Iris-Entry': entry_id, 'X-Iris-Upload': upload_id, 'X-Iris-Offset': str(offset)})

    def upload(self, entry_id: str, original_key: str, path: Path, modality: str = 'IMAGE') -> dict[str, Any]:
        """Upload bounded bytes; callers retain the key and inspect after interruption."""
        if not 1 <= path.stat().st_size <= 1048576: raise ValueError('Blob limit.')
        original = {'key': original_key, 'modality': modality}
        begun = self.media('begin', entry_id, original)
        if begun['outcome'] != 'COMMITTED': return begun
        upload_id = begun['data']['receipt']['result']['upload_id']
        offset = 0
        with path.open('rb') as stream:
            while block := stream.read(65536):
                appended = self.chunk(entry_id, upload_id, offset, block)
                if appended['outcome'] != 'OBSERVED' or appended['data'].get('state') != 'VOLATILE_PROGRESS': return appended
                offset += len(block)
        return self.media('finish', entry_id, {'upload_id': upload_id})

    def websocket(self) -> WebSocketClient:
        return WebSocketClient(self)


class WebSocketClient:
    """Independent masked RFC6455 client, bounded to this closed Core protocol."""
    def __init__(self, http: IrisClient):
        self.socket = socket.create_connection((http.host, http.port), http.timeout)
        try:
            if http.secure: self.socket = http.context.wrap_socket(self.socket, server_hostname=http.host)
            key = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
            headers = (f'GET /api/host/ws HTTP/1.1\r\nHost: {http.authority}\r\nUpgrade: websocket\r\n'
                f'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n'
                f'Sec-WebSocket-Protocol: iris.communication.v1\r\nAuthorization: Bearer {http.token}\r\n\r\n')
            self.socket.sendall(headers.encode('ascii'))
            head = bytearray()
            while not head.endswith(b'\r\n\r\n'):
                piece = self.socket.recv(1)
                if not piece: raise ConnectionError('Upgrade closed.')
                head.extend(piece)
                if len(head) > 8192: raise ValueError('Header limit.')
            lines = bytes(head).decode('ascii').split('\r\n')
            result = dict(line.lower().split(': ', 1) for line in lines[1:] if ': ' in line)
            # The accept proof is case-sensitive even though header names are not.
            proof = next((line.partition(':')[2].strip() for line in lines if line.lower().startswith('sec-websocket-accept:')), '')
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
            if not lines[0].startswith('HTTP/1.1 101 ') or proof != expected or result.get('sec-websocket-protocol') != 'iris.communication.v1':
                raise ConnectionError('Upgrade rejected.')
        except BaseException:
            self.socket.close()
            raise
        self.socket.settimeout(max(35, http.timeout))
        self.closed = False

    def send_frame(self, payload: bytes, opcode: int = 1, *, final: bool = True) -> None:
        if self.closed or len(payload) > 8192: raise ValueError('Frame unavailable.')
        mask = secrets.token_bytes(4)
        size = bytes([0x80 | len(payload)]) if len(payload) < 126 else b'\xfe' + struct.pack('!H', len(payload))
        encoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes([(0x80 if final else 0) | opcode]) + size + mask + encoded)

    def send(self, value: dict[str, Any]) -> None:
        self.send_frame(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())

    def _read(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            block = self.socket.recv(size - len(data))
            if not block: raise ConnectionError('WebSocket closed.')
            data.extend(block)
        return bytes(data)

    def receive(self) -> dict[str, Any]:
        assembled = bytearray()
        while True:
            first, second = self._read(2)
            opcode, length = first & 15, second & 127
            if first & 0x70 or second & 0x80: raise ValueError('Unsupported server frame.')
            if length == 126: length = struct.unpack('!H', self._read(2))[0]
            elif length == 127: length = struct.unpack('!Q', self._read(8))[0]
            if length > 8192 or len(assembled) + length > 8192: raise ValueError('Message limit.')
            data = self._read(length)
            if opcode == 9:
                self.send_frame(data, 10)
                continue
            if opcode == 10: continue
            if opcode == 8: raise ConnectionError('Server closed WebSocket.')
            if opcode not in (0, 1): raise ValueError('Unsupported application frame.')
            assembled.extend(data)
            if first & 0x80:
                result = json.loads(assembled)
                if type(result) is not dict or result.get('version') != 1: raise ValueError('Unsupported message.')
                return result

    def close(self) -> None:
        if not self.closed:
            try: self.send_frame(struct.pack('!H', 1000), 8)
            except OSError: pass
            finally:
                self.closed = True
                try: self.socket.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                self.socket.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='Receive Core notifications without executing goal actions.')
    parser.add_argument('--origin', required=True)
    parser.add_argument('--entry', required=True)
    parser.add_argument('--route', required=True)
    parser.add_argument('--ack', action='store_true', help='Explicitly acknowledge receipt of each goal notification.')
    parser.add_argument('--ca-file')
    parser.add_argument('--takeover', action='store_true', help='Explicitly replace only this route consumer.')
    parser.add_argument('--reconnect-attempts', type=int, choices=range(9), default=5, help='Finite jittered reconnect budget; never replays writes.')
    options = parser.parse_args()
    client = IrisClient(options.origin, os.environ['IRIS_HOST_TOKEN'], ca_file=options.ca_file)
    if __package__:
        from .reconnect import listen
    else:
        from reconnect import listen
    listen(client, options.route, options.entry, acknowledge=options.ack, takeover=options.takeover,
        reconnect_attempts=options.reconnect_attempts)


if __name__ == '__main__':
    main()
