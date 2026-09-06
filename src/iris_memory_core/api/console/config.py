"""Deployment-only browser boundary settings, never domain runtime settings."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_bind(value: str) -> tuple[str, int]:
    parsed = urlsplit("//" + value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("console bind must be HOST:PORT") from None
    if not parsed.hostname or port is None or not 1 <= port <= 65535 or parsed.path:
        raise ValueError("console bind must be HOST:PORT")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("console bind must be HOST:PORT")
    return parsed.hostname, port


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    origin: str = "https://localhost"
    dev_http: bool = False
    bind_host: str = "127.0.0.1"
    allowed_hosts: tuple[str, ...] = ("localhost",)
    trusted_proxy_ips: tuple[str, ...] = ()
    assets: Path | None = None

    def validate(self) -> None:
        parsed = urlsplit(self.origin)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("console origin must be an explicit HTTP(S) origin")
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError("console origin has an invalid port") from None
        if not self.allowed_hosts or parsed.hostname not in self.allowed_hosts:
            raise ValueError("console origin host must be explicitly allowed")
        if any("*" in host or "/" in host for host in self.allowed_hosts):
            raise ValueError("console hosts must be exact host names")
        if self.dev_http:
            if (
                parsed.scheme != "http"
                or not is_loopback(self.bind_host)
                or not all(is_loopback(host) for host in self.allowed_hosts)
                or self.trusted_proxy_ips
            ):
                raise ValueError("console_dev_http requires loopback, with no reverse proxy")
        elif parsed.scheme != "https":
            raise ValueError("production Console requires HTTPS")
        for address in self.trusted_proxy_ips:
            ipaddress.ip_address(address)
        if self.assets is not None and not self.assets.is_dir():
            raise ValueError("console assets must be an existing directory")
