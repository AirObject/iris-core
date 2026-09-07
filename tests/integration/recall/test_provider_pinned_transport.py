"""Real isolated HTTP/TLS exercises of the deployment-only outbound boundary."""

from __future__ import annotations

import ipaddress
import json
import ssl
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.domain.vector import EmbeddingProviderError, VectorSpaceConfig
from iris_memory_core.providers.embedding import HttpEmbeddingProvider
from iris_memory_core.providers.transport import PinnedEmbeddingTransport, ProviderOutboundPolicy


@pytest.fixture
def server() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"requests": [], "mode": "ok"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            state["requests"].append((self.headers["Host"], self.path))
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            mode = state["mode"]
            try:
                if mode == "headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    for _ in range(15):
                        self.wfile.write(b"a")
                        self.wfile.flush()
                        time.sleep(0.04)
                    return
                if mode in {"redirect", "rate", "failure"}:
                    self.send_response({"redirect": 302, "rate": 429, "failure": 503}[mode])
                    self.send_header("Location", "http://169.254.169.254/metadata")
                    self.end_headers()
                    return
                if mode == "huge":
                    payload = b"x" * 513
                elif mode == "invalid":
                    payload = b"provider-secret-raw-error-body"
                elif mode == "boolean":
                    payload = b'{"data":[{"embedding":[true,0]}]}'
                elif mode == "wrong-dimension":
                    payload = b'{"data":[{"embedding":[1,0,0]}]}'
                elif mode == "bad-index":
                    payload = b'{"data":[{"index":9,"embedding":[1,0]}]}'
                else:
                    payload = json.dumps(
                        {
                            "data": [
                                {"index": i, "embedding": [1.0, 0.0]}
                                for i in reversed(range(len(body["input"])))
                            ]
                        }
                    ).encode()
                self.send_response(200)
                if mode != "huge":
                    self.send_header("Content-Length", str(len(payload)))
                if mode == "encoding":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                if mode == "body":
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.04)
                else:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http.daemon_threads = True
    state["http"] = http
    state["endpoint"] = f"http://provider.test:{http.server_port}/v1/embeddings"
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def transport(**options: Any) -> PinnedEmbeddingTransport:
    return PinnedEmbeddingTransport(
        ProviderOutboundPolicy(allow_loopback=True, **options),
        resolver=lambda host, port: ("127.0.0.1",),
    )


def invoke(server: dict[str, Any], **options: Any) -> list[Any]:
    return transport(**options)(
        server["endpoint"], "isolated-test-key", "test-model", ["fixed probe"], 2
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "fe80::1",
        "::",
        "0.0.0.0",
        "224.0.0.1",
        "ff02::1",
        "240.0.0.1",
        "::ffff:169.254.169.254",
        "2002:a9fe:a9fe::1",
        "2001:db8::1",
    ],
)
def test_default_policy_rejects_nonpublic_and_transition_addresses(address: str) -> None:
    assert not ProviderOutboundPolicy().address_allowed(
        ipaddress.ip_address(address), scheme="https"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/v1/embeddings",
        "https://user:password@example.com/x",
        "https://example.com/x?key=secret",
        "https://example.com/#secret",
        "file:///etc/passwd",
        "https://example.com:99999/x",
        "https://example.com/\r\nAuthorization:x",
        "https://[::1%25lo0]/x",
    ],
)
def test_endpoint_rejects_untrusted_forms(endpoint: str) -> None:
    with pytest.raises(EmbeddingProviderError, match="outbound_denied"):
        ProviderOutboundPolicy().endpoint(endpoint)


def test_exact_deployment_hosts_and_private_allowlist() -> None:
    policy = ProviderOutboundPolicy(
        allowed_hosts=("allowed.example",), allowed_private_networks=("10.20.0.0/16",)
    )
    with pytest.raises(EmbeddingProviderError):
        policy.endpoint("https://allowed.example.evil/v1/embeddings")
    assert policy.address_allowed(ipaddress.ip_address("10.20.1.2"), scheme="https")
    assert not policy.address_allowed(ipaddress.ip_address("10.21.1.2"), scheme="https")
    assert not policy.address_allowed(ipaddress.ip_address("169.254.169.254"), scheme="https")
    assert not policy.address_allowed(ipaddress.ip_address("10.20.1.2"), scheme="http")


def test_real_http_pins_checked_ip_once_and_preserves_host(server: dict[str, Any]) -> None:
    calls: list[str] = []

    def rebinding(host: str, port: int) -> tuple[str, ...]:
        calls.append(host)
        return ("127.0.0.1",) if len(calls) == 1 else ("169.254.169.254",)

    client = PinnedEmbeddingTransport(
        ProviderOutboundPolicy(allow_loopback=True), resolver=rebinding
    )
    assert client(server["endpoint"], "test", "test", ["one", "two"], 2) == [[1.0, 0.0], [1.0, 0.0]]
    assert calls == ["provider.test"]
    assert server["requests"] == [(f"provider.test:{server['http'].server_port}", "/v1/embeddings")]


def test_mixed_dns_answers_reject_before_connect(server: dict[str, Any]) -> None:
    client = PinnedEmbeddingTransport(
        ProviderOutboundPolicy(allow_loopback=True),
        resolver=lambda host, port: ("127.0.0.1", "169.254.169.254"),
    )
    with pytest.raises(EmbeddingProviderError, match="outbound_denied"):
        client(server["endpoint"], "test", "test", ["one"], 1)
    assert not server["requests"]


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("redirect", "redirect_denied"),
        ("rate", "rate_limited"),
        ("failure", "transport_error"),
        ("huge", "response_too_large"),
        ("invalid", "invalid_output"),
        ("boolean", "embedding_not_numeric"),
        ("bad-index", "invalid_output"),
        ("encoding", "invalid_output"),
    ],
)
def test_real_http_rejections_are_bounded_and_never_follow_redirects(
    server: dict[str, Any], mode: str, reason: str
) -> None:
    server["mode"] = mode
    with pytest.raises(EmbeddingProviderError) as caught:
        invoke(server, max_response_bytes=512)
    assert caught.value.reason_code == reason
    assert len(server["requests"]) == 1
    assert "provider-secret" not in str(caught.value)
    assert "isolated-test-key" not in str(caught.value)


def test_request_bytes_are_bounded_before_outbound(server: dict[str, Any]) -> None:
    with pytest.raises(EmbeddingProviderError, match="request_too_large"):
        transport(max_request_bytes=256)(server["endpoint"], "test", "test", ["x" * 300], 1)
    assert not server["requests"]


@pytest.mark.parametrize("mode", ["headers", "body"])
def test_total_deadline_bounds_real_trickling_headers_and_body(
    server: dict[str, Any], mode: str
) -> None:
    server["mode"] = mode
    started = time.monotonic()
    with pytest.raises(EmbeddingProviderError, match="timeout"):
        transport()(server["endpoint"], "test", "test", ["one"], 0.15)
    assert time.monotonic() - started < 0.5


def test_dns_total_deadline() -> None:
    release = threading.Event()
    started = threading.Event()

    def blocked(host: str, port: int) -> tuple[str, ...]:
        started.set()
        release.wait(2)
        return ("93.184.216.34",)

    client = PinnedEmbeddingTransport(ProviderOutboundPolicy(), resolver=blocked)
    before = time.monotonic()
    try:
        with pytest.raises(EmbeddingProviderError, match="timeout"):
            client("https://provider.test/v1/embeddings", "test", "test", ["one"], 0.03)
        assert started.is_set()
        assert time.monotonic() - before < 0.5
    finally:
        release.set()


def test_wrapper_still_enforces_dimension(server: dict[str, Any]) -> None:
    server["mode"] = "wrong-dimension"
    provider = HttpEmbeddingProvider(
        endpoint=server["endpoint"].replace("provider.test", "127.0.0.1"),
        api_key="test",
        space=VectorSpaceConfig(model="test", dimension=2),
        transport=transport(),
    )
    with pytest.raises(EmbeddingProviderError, match="embedding_dimension_mismatch"):
        provider.embed_batch(["fixed probe"])


def test_real_tls_preserves_sni_and_certificate_validation(
    server: dict[str, Any], tmp_path: Path
) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "provider.test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("provider.test")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server["http"].socket = context.wrap_socket(server["http"].socket, server_side=True)
    trusted = ssl.create_default_context(cafile=str(cert_file))
    client = PinnedEmbeddingTransport(
        ProviderOutboundPolicy(allow_loopback=True),
        resolver=lambda host, port: ("127.0.0.1",),
        tls_context=trusted,
    )
    endpoint = server["endpoint"].replace("http:", "https:")
    assert client(endpoint, "test", "test", ["one"], 2) == [[1.0, 0.0]]
    with pytest.raises(EmbeddingProviderError, match="transport_error"):
        client(endpoint.replace("provider.test", "wrong.test"), "test", "test", ["one"], 2)
    assert len(server["requests"]) == 1


def test_dns_workers_have_no_unbounded_submission_queue() -> None:
    from concurrent.futures import ThreadPoolExecutor

    release, all_started = threading.Event(), threading.Event()
    guard = threading.Lock()
    count = 0

    def blocked(host: str, port: int) -> tuple[str, ...]:
        nonlocal count
        with guard:
            count += 1
            if count == 4:
                all_started.set()
        release.wait(2)
        return ("93.184.216.34",)

    client = PinnedEmbeddingTransport(ProviderOutboundPolicy(), resolver=blocked)

    def request() -> str:
        try:
            client("https://provider.test/v1/embeddings", "test", "test", ["one"], 0.15)
        except EmbeddingProviderError as error:
            return error.reason_code
        raise AssertionError("blocked resolver should time out")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(request) for _ in range(4)]
        try:
            assert all_started.wait(1), "four DNS workers did not start"
            assert request() == "rate_limited"
            assert [future.result(timeout=1) for future in futures] == ["timeout"] * 4
            # Timed-out callers do not free still-running resolver slots.
            assert request() == "rate_limited"
            assert count == 4
        finally:
            release.set()
