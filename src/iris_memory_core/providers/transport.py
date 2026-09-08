"""Deployment-governed, IP-pinned embedding HTTP transport (ADR-0049).

No redirects, environment proxies, response logging, or provider-selected
headers. DNS workers and active requests are bounded independently. A total
request deadline also interrupts trickling TLS/HTTP headers and response bodies.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import ipaddress
import json
import math
import socket
import ssl
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from iris_memory_core.domain.vector import EmbeddingProviderError

_DNS_WORKERS = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="provider-dns"
)
_DNS_SLOTS = threading.BoundedSemaphore(4)
_REQUEST_SLOTS = threading.BoundedSemaphore(32)
Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Sequence[str]]


def _deny(reason: str) -> EmbeddingProviderError:
    return EmbeddingProviderError(reason, retryable=False)


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise EmbeddingProviderError("timeout", retryable=True)
    return left


def _system_addresses(host: str, port: int) -> Sequence[str]:
    return tuple(
        {str(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    )


@dataclass(frozen=True, slots=True)
class ProviderOutboundPolicy:
    """Trusted deployment input, never accepted from a Console request."""

    allowed_hosts: tuple[str, ...] = ()
    allowed_private_networks: tuple[str, ...] = ()
    allow_loopback: bool = False
    max_request_bytes: int = 1_048_576
    max_response_bytes: int = 8_388_608
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 2.0
    max_total_seconds: float = 10.0

    def __post_init__(self) -> None:
        for host in self.allowed_hosts:
            if not host or any(character in host for character in "/@*?#"):
                raise ValueError("provider allowed hosts must be exact hostnames")
        for network in self.allowed_private_networks:
            ipaddress.ip_network(network, strict=True)
        if not 256 <= self.max_request_bytes <= 16_777_216:
            raise ValueError("provider request bound must be within 256..16777216")
        if not 256 <= self.max_response_bytes <= 67_108_864:
            raise ValueError("provider response bound must be within 256..67108864")
        for timeout in (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.max_total_seconds,
        ):
            if not math.isfinite(timeout) or not 0 < timeout <= 120:
                raise ValueError("provider timeout must be finite and within 0..120 seconds")

    def endpoint(self, endpoint: str) -> tuple[SplitResult, str, int]:
        try:
            parsed = urlsplit(endpoint)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except (ValueError, UnicodeError):
            raise _deny("outbound_denied") from None
        if (
            len(endpoint) > 2048
            or any(ord(character) < 33 or ord(character) == 127 for character in endpoint)
            or parsed.scheme not in {"https", "http"}
            or not host
            or "%" in host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not 1 <= port <= 65535
        ):
            raise _deny("outbound_denied")
        if self.allowed_hosts and host not in {
            allowed.encode("idna").decode("ascii").lower() for allowed in self.allowed_hosts
        }:
            raise _deny("outbound_denied")
        # Plain HTTP is only usable when BOTH deployment and resolved addresses
        # identify development loopback; a DNS name alone cannot grant this.
        if parsed.scheme == "http" and not self.allow_loopback:
            raise _deny("outbound_denied")
        return parsed, host, port

    def address_allowed(self, address: Address, *, scheme: str) -> bool:
        if isinstance(address, ipaddress.IPv6Address) and (
            address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
        ):
            return False
        if address.is_loopback:
            return self.allow_loopback
        if scheme != "https":
            return False
        if (
            address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or getattr(address, "scope_id", None) is not None
        ):
            return False
        if address.is_global:
            return True
        return any(
            address in ipaddress.ip_network(network) for network in self.allowed_private_networks
        )


class PinnedEmbeddingTransport:
    """HttpEmbeddingProvider transport signature with one checked DNS answer."""

    def __init__(
        self,
        policy: ProviderOutboundPolicy,
        *,
        resolver: Resolver | None = None,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self.policy = policy
        self._resolver = resolver or _system_addresses
        self._tls = tls_context or ssl.create_default_context()
        if not self._tls.check_hostname or self._tls.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("provider TLS must verify hostname and certificate")

    def _addresses(self, host: str, port: int, deadline: float) -> tuple[Address, ...]:
        try:
            return (ipaddress.ip_address(host),)
        except ValueError:
            pass
        if not _DNS_SLOTS.acquire(blocking=False):
            raise EmbeddingProviderError("rate_limited", retryable=True)
        try:
            future = _DNS_WORKERS.submit(self._resolver, host, port)
        except Exception:
            _DNS_SLOTS.release()
            raise EmbeddingProviderError("transport_error", retryable=True) from None
        future.add_done_callback(lambda _: _DNS_SLOTS.release())
        try:
            resolved = future.result(timeout=_remaining(deadline))
            if not resolved or len(resolved) > 16:
                raise _deny("outbound_denied")
            return tuple(sorted({ipaddress.ip_address(item) for item in resolved}, key=str))
        except (concurrent.futures.TimeoutError, TimeoutError):
            raise EmbeddingProviderError("timeout", retryable=True) from None
        except (ValueError, OSError):
            raise _deny("outbound_denied") from None

    def __call__(
        self, endpoint: str, api_key: str, model: str, texts: list[str], timeout_s: float
    ) -> list[Sequence[float]]:
        return _vectors(
            self.request_json(endpoint, api_key, {"model": model, "input": texts}, timeout_s),
            len(texts),
        )

    def request_json(
        self, endpoint: str, api_key: str, document: dict[str, object], timeout_s: float
    ) -> object:
        """Send one bounded JSON request through the same checked socket boundary."""
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise EmbeddingProviderError("timeout", retryable=True)
        if any(character in api_key for character in "\r\n") or len(api_key) > 4096:
            raise _deny("secret_unavailable")
        if not _REQUEST_SLOTS.acquire(blocking=False):
            raise EmbeddingProviderError("rate_limited", retryable=True)
        deadline = time.monotonic() + min(timeout_s, self.policy.max_total_seconds)
        connected: list[socket.socket] = []
        lock = threading.Lock()

        def expire() -> None:
            with lock:
                for item in connected:
                    with suppress(OSError):
                        item.shutdown(socket.SHUT_RDWR)

        timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        connection: http.client.HTTPConnection | None = None
        try:
            parsed, host, port = self.policy.endpoint(endpoint)
            payload = json.dumps(document, allow_nan=False).encode("utf-8")
            if len(payload) > self.policy.max_request_bytes:
                raise _deny("request_too_large")
            addresses = self._addresses(host, port, deadline)
            if not all(
                self.policy.address_allowed(address, scheme=parsed.scheme) for address in addresses
            ):
                raise _deny("outbound_denied")
            address = addresses[0]
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            wire = socket.socket(family, socket.SOCK_STREAM)
            with lock:
                connected.append(wire)
            wire.settimeout(min(self.policy.connect_timeout_seconds, _remaining(deadline)))
            # A numeric address goes directly to connect, with no second DNS.
            target = (
                (str(address), port, 0, 0) if family == socket.AF_INET6 else (str(address), port)
            )
            wire.connect(target)
            if parsed.scheme == "https":
                wire.settimeout(min(self.policy.connect_timeout_seconds, _remaining(deadline)))
                wire = self._tls.wrap_socket(wire, server_hostname=host)
                with lock:
                    connected.append(wire)
            wire.settimeout(min(self.policy.read_timeout_seconds, _remaining(deadline)))
            connection = http.client.HTTPConnection(host, port)
            connection.sock = wire
            path = parsed.path or "/"
            connection.request(
                "POST",
                path,
                body=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            _remaining(deadline)
            if 300 <= response.status < 400:
                raise _deny("redirect_denied")
            if response.status == 429:
                raise EmbeddingProviderError("rate_limited", retryable=True)
            if response.status != 200:
                raise EmbeddingProviderError("transport_error", retryable=response.status >= 500)
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise _deny("invalid_output")
            length = response.getheader("Content-Length")
            if length is not None and (
                not length.isdecimal() or int(length) > self.policy.max_response_bytes
            ):
                raise _deny("response_too_large")
            body = bytearray()
            while not response.isclosed():
                wire.settimeout(min(self.policy.read_timeout_seconds, _remaining(deadline)))
                chunk = response.read1(min(65_536, self.policy.max_response_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > self.policy.max_response_bytes:
                    raise _deny("response_too_large")
            _remaining(deadline)
            try:
                decoded = json.loads(body)
            except (ValueError, UnicodeError):
                raise _deny("invalid_output") from None
            return decoded
        except EmbeddingProviderError:
            raise
        except TimeoutError:
            raise EmbeddingProviderError("timeout", retryable=True) from None
        except (OSError, http.client.HTTPException):
            reason = "timeout" if time.monotonic() >= deadline else "transport_error"
            raise EmbeddingProviderError(reason, retryable=True) from None
        finally:
            timer.cancel()
            if connection is not None:
                connection.close()
            with lock:
                for item in connected:
                    item.close()
            _REQUEST_SLOTS.release()


def _vectors(body: object, expected: int) -> list[Sequence[float]]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise _deny("invalid_output")
    rows = body["data"]
    if len(rows) != expected:
        raise _deny("embedding_count_mismatch")
    vectors: dict[int, list[float]] = {}
    for position, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
            raise _deny("invalid_output")
        index = row.get("index", position)
        if type(index) is not int or not 0 <= index < expected or index in vectors:
            raise _deny("invalid_output")
        vector = row["embedding"]
        if any(
            type(component) not in (int, float) or not math.isfinite(component)
            for component in vector
        ):
            raise _deny("embedding_not_numeric")
        vectors[index] = vector
    return [vectors[index] for index in range(expected)]
