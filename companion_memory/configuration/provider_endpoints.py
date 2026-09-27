"""Validate administrator-selected HTTPS Provider bases without opening a socket."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from companion_memory.persistence.schema import valid_identifier


_HOST = re.compile(r'(?=.{1,253}$)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z')
_PATH = re.compile(r'(?:/[A-Za-z0-9._~-]+)*\Z')


def provider_base(value: object, operation: str) -> tuple[str, str, str]:
    """Return an origin, base path and fixed operation path for public HTTPS.

    The saved URL is a base, such as ``https://example.com/api/v3``. DNS is
    checked again at connection time so a later private-address resolution
    cannot turn this setting into access to a local service.
    """
    if operation not in ('embeddings', 'chat/completions') or type(value) is not str:
        raise ValueError('Invalid Provider endpoint')
    if not 1 <= len(value.encode('utf-8')) <= 256 or value != value.strip() or '\\' in value:
        raise ValueError('Invalid Provider endpoint')
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError('Invalid Provider endpoint') from None
    if (parsed.scheme != 'https' or host is None or port not in (None, 443)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.netloc != host and parsed.netloc != host + ':443'
            or not _HOST.fullmatch(host) or host.lower() != host):
        raise ValueError('Invalid Provider endpoint')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('Invalid Provider endpoint')
    if host.endswith(('.local', '.localhost', '.internal', '.invalid', '.test', '.example')):
        raise ValueError('Invalid Provider endpoint')
    path = parsed.path.rstrip('/')
    if not _PATH.fullmatch(path) or any(part in ('.', '..') for part in path.split('/')):
        raise ValueError('Invalid Provider endpoint')
    suffix = '/' + operation
    if path.endswith(suffix):
        path = path[:-len(suffix)]
    if len((path + suffix).encode()) > 256:
        raise ValueError('Invalid Provider endpoint')
    return 'https://' + host, path, suffix


def provider_model(value: object) -> str:
    """Accept one bounded model identifier without interpreting its supplier."""
    if type(value) is not str or not valid_identifier(value):
        raise ValueError('Invalid Provider model')
    return value
