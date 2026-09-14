"""Real loopback HTTP/TLS framing, deadlines and credential ownership checks.

Each fixture accepts one connection and records the complete request count.
Only independent synthetic credentials and temporary self-signed TLS files exist.
"""
from contextlib import contextmanager
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import Available, CredentialLease, CredentialResolver
from companion_memory.provider.resources import CancellationSource
from companion_memory.provider.values import as_record, freeze
from tests.text_learning.configuration_support import inputs


@contextmanager
def server(response: bytes, *, context: ssl.SSLContext | None = None,
           entered: threading.Event | None = None, release: threading.Event | None = None,
           release_timeout: float = 3):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen(2); listener.settimeout(.1)
    requests: list[bytes] = []
    failures: list[Exception] = []
    stopped=threading.Event()
    def run():
        try:
            # Host initialization may precede a send, or legitimately send nothing.
            # Keep admission alive until explicit fixture cleanup, without treating
            # an idle listener as a transport timeout or manufacturing a request.
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept();break
                except socket.timeout:continue
                except OSError:
                    if stopped.is_set():return
                    raise
            else:return
            with connection:
                connection.settimeout(3)
                if context is not None:
                    connection = context.wrap_socket(connection, server_side=True)
                try:
                    request = bytearray()
                    while b'\r\n\r\n' not in request:
                        block = connection.recv(8192)
                        if not block: return
                        request.extend(block)
                    headers, body = bytes(request).split(b'\r\n\r\n', 1)
                    size = int(next(line.split(b':', 1)[1] for line in headers.split(b'\r\n') if line.startswith(b'Content-Length:')))
                    while len(body) < size:
                        block = connection.recv(8192)
                        if not block: return
                        body += block
                    requests.append(headers + b'\r\n\r\n' + body)
                    if entered is not None: entered.set()
                    if release is not None: release.wait(release_timeout)
                    connection.sendall(response)
                finally:
                    connection.close()
        except (OSError, ValueError) as failure:
            failures.append(failure)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        yield listener.getsockname()[1], requests, failures
    finally:
        if release is not None: release.set()
        stopped.set();listener.close(); worker.join(4)
        if worker.is_alive(): raise AssertionError('Loopback server retained a live worker.')


class ChatTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = as_record(freeze(inputs(Path(self.temporary.name))[5]['explicit_values']['provider.transport'], 2048))
        self.leases: list[CredentialLease] = []
        def resolve(secret, revision, account):
            self.assertEqual((secret, revision, account), ('fixture_secret', 'fixture_secret_revision', 'fixture_account'))
            lease = CredentialLease(b'independent-test-credential')
            self.leases.append(lease)
            return Available(lease)
        self.resolver = CredentialResolver(resolve)

    def exchange(self, response, **options):
        with server(response, **options) as (port, requests, failures):
            value = ChatTransport.controlled_loopback(self.settings, self.resolver, time.monotonic, port)
            result = value.exchange(b'{"fixture":true}', time.monotonic() + 2, CancellationSource().token)
        self.assertEqual(len(requests), 1)
        self.assertTrue(self.leases[-1].released)
        self.assertNotIn('independent-test-credential', repr(result))
        return result, requests

    def test_complete_post_framings_are_single_attempt_and_preserve_body(self):
        for response in (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}',
                         b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\n{\r\n1\r\n}\r\n0\r\n\r\n',
                         b'HTTP/1.0 200 OK\r\n\r\n{}'):
            with self.subTest(response=response):
                result, requests = self.exchange(response)
                self.assertEqual((result.state, result.status, result.body), ('RESPONSE', 200, b'{}'))
                self.assertTrue(requests[0].startswith(b'POST /api/coding/v3/chat/completions HTTP/1.1\r\n'))
                self.assertIn(b'Accept-Encoding: identity\r\n', requests[0])

    def test_header_body_and_framing_limits_reject_complete_unsupported_input(self):
        for response in (
            b'HTTP/1.1 200 OK\r\nX-Long: ' + b'a' * 16384 + b'\r\n\r\n{}',
            b'HTTP/1.1 200 OK\r\n' + b''.join(f'X-{i}: a\r\n'.encode() for i in range(101)) + b'\r\n{}',
            b'HTTP/1.1 200 OK\r\nContent-Length: 262145\r\n\r\n',
            b'HTTP/1.1 200 OK\r\n\r\n' + b'x' * 262145,
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n40001\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx',
            b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n{}',
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}',
            b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n{}'):
            with self.subTest(prefix=response[:80]):
                result, _ = self.exchange(response)
                self.assertEqual(result.state, 'REMOTE_RESULT_UNKNOWN')
                self.assertIsNone(result.body)

    def test_redirect_does_not_follow_or_resolve_another_credential(self):
        result, _ = self.exchange(b'HTTP/1.1 307 Redirect\r\nLocation: https://example.invalid/secret\r\n\r\n')
        self.assertEqual((result.status, result.reason), (307, 'REDIRECT_REJECTED'))
        self.assertEqual(len(self.leases), 1)

    def test_deadline_cancellation_and_slot_ownership_follow_actual_socket(self):
        entered, release = threading.Event(), threading.Event()
        cancel = CancellationSource()
        with server(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}', entered=entered, release=release) as (port, requests, failures):
            transport = ChatTransport.controlled_loopback(self.settings, self.resolver, time.monotonic, port)
            results = []
            worker = threading.Thread(target=lambda: results.append(transport.exchange(b'{}', time.monotonic() + .3, cancel.token)))
            worker.start()
            try:
                self.assertTrue(entered.wait(2)); cancel.cancel()
                busy = transport.exchange(b'{}', time.monotonic() + 1, CancellationSource().token)
                self.assertEqual(busy.reason, 'RESOURCE_BUSY')
                self.assertFalse(self.leases[-1].released)
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertTrue(self.leases[-1].released)
                self.assertEqual(results[0].state, 'REMOTE_RESULT_UNKNOWN')
                self.assertEqual(len(requests), 1)
            finally:
                release.set(); worker.join(4)

    def test_expired_or_revoked_credentials_never_send(self):
        expired = CancellationSource(); expired.cancel()
        transport = ChatTransport.controlled_loopback(self.settings, self.resolver, time.monotonic, 1)
        self.assertEqual(transport.exchange(b'{}', time.monotonic() + 1, expired.token).state, 'NOT_SENT')
        self.assertEqual(self.leases, [])
        lease = CredentialLease(b'synthetic'); lease.revoke()
        self.assertIsNone(lease._header()); lease.release(); self.assertTrue(lease.released)
        self.assertNotIn('synthetic', repr(lease))

    def test_real_tls_validates_certificate_and_rejects_untrusted_peer(self):
        root = Path(self.temporary.name)
        key, certificate = root/'synthetic-key.pem', root/'synthetic-cert.pem'
        process = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', str(key), '-out', str(certificate)], capture_output=True)
        self.assertEqual(process.returncode, 0)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(certificate, key)
        trusted = ssl.create_default_context(cafile=str(certificate))
        for client, expected in ((trusted, 'RESPONSE'), (ssl.create_default_context(), 'NOT_SENT')):
            with server(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}', context=context) as (port, requests, failures):
                transport = ChatTransport.controlled_loopback(self.settings, self.resolver, time.monotonic, port, client)
                result = transport.exchange(b'{}', time.monotonic() + 2, CancellationSource().token)
                self.assertEqual(result.state, expected)
                self.assertEqual(len(requests), int(expected == 'RESPONSE'))
                self.assertTrue(self.leases[-1].released)
