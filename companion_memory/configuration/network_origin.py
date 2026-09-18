"""Strict external HTTP origin and authority parsing, independent of bind ports.

Proxy headers never participate in this identity. Host has no scheme and uses
the configured origin's default port; Origin supplies its own scheme and port.
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv6Address
import re
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class NetworkOrigin:
    scheme: str
    host: str
    port: int


def authority(value: str, scheme: str) -> tuple[str, int]:
    """Validate a single ASCII authority without credentials or URL components."""
    if not value or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError('Invalid authority.')
    if any(c in value for c in '/\\?#@%,'):
        raise ValueError('Invalid authority.')
    parsed = urlsplit(scheme + '://' + value)
    host = parsed.hostname
    if not host or value.endswith(':'):
        raise ValueError('Invalid authority.')
    if ':' in host:
        if re.fullmatch(r'\[[0-9A-Fa-f:.]+\](?::[0-9]+)?', value) is None:
            raise ValueError('IPv6 requires brackets.')
        host = str(IPv6Address(host))
    elif re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) is None:
        raise ValueError('Invalid hostname.')
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('Invalid port.')
    return host.lower(), port if port is not None else 443 if scheme == 'https' else 80


def parse_origin(value: str) -> NetworkOrigin:
    """Compare the complete scheme, hostname and effective port tuple."""
    if type(value) is not str or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError('Invalid origin.')
    matched = re.fullmatch(r'(https?)://([^/\\?#]+)', value, re.IGNORECASE)
    if matched is None:
        raise ValueError('An HTTP or HTTPS origin is required.')
    scheme = matched[1].lower()
    host, port = authority(matched[2], scheme)
    return NetworkOrigin(scheme, host, port)
