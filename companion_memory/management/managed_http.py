"""Bounded same-origin management HTTP and independently authenticated host HTTP.

Only compiled static assets are served. Protected volume paths are never URL
paths. Timed-out application tasks remain owned until actual completion; reading
health cannot trigger business recovery, model dispatch, or authentication writes.
"""
from __future__ import annotations
import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import fields, is_dataclass
import hmac
import json
import re
import time
from companion_memory.retrieval.delivery import DeliveryScope
from pathlib import Path
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit

from companion_memory.configuration.deployment import DeploymentSettings
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.request_effects import RequestEffects, observe_effects
from .identity import IdentityAuthority, Principal, digest
from .developer_audit import DeveloperAudit


def result_envelope(result: object) -> tuple[int, dict[str, object]]:
    """Keep durable outcome and real cleanup separate in the managed API format."""
    from companion_memory.persistence import Committed, NotCommitted, Rejected, Unconfirmed, Found, NotFound, Failed
    from companion_memory.configuration.managed_persistent_results import ConfigurationCommitted, ConfigurationNotCommitted, ConfigurationRejected, ConfigurationUnconfirmed
    from companion_memory.information.errors import InformationRejected, InformationNotCommitted, InformationUnconfirmed
    from companion_memory.information.business import BusinessPending
    from companion_memory.retrieval.query_service import RecallPending
    from .information_http import response_value
    if isinstance(result, (InformationRejected, InformationNotCommitted, InformationUnconfirmed, BusinessPending, RecallPending)):
        status, projected = response_value(result)
        data = cast(dict[str, object], public_value(projected))
        state = str(data['status'])
    else:
        if type(result) is ConfigurationCommitted:
            data = cast(dict[str, object], public_value({'receipt': result.receipt, 'source': result.source,
                'configuration_available': result.configuration is not None, 'error': result.error, 'cleanup_pending': result.cleanup_pending}))
        else:
            data = cast(dict[str, object], public_value(result))
        if type(data) is not dict:
            raise ValueError('Managed response requires a closed object projection.')
        state = ('COMMITTED' if isinstance(result, (Committed, ConfigurationCommitted)) else
            'NOT_COMMITTED' if isinstance(result, (NotCommitted, ConfigurationNotCommitted)) else
            'REJECTED' if isinstance(result, (Rejected, ConfigurationRejected)) else
            'UNCONFIRMED' if isinstance(result, (Unconfirmed, ConfigurationUnconfirmed)) or data.get('state') == 'UNCONFIRMED' else
            'FAILED' if type(result) is Failed else 'ABSENT' if type(result) is NotFound else 'OBSERVED')
        if state == 'OBSERVED' and ({'code', 'operation', 'field', 'reason'} <= set(data)
                or type(data.get('error')) is dict and set(data) <= {'error', 'cleanup_pending'}):
            if 'error' not in data:
                data = {'error': data}
            state = 'FAILED'
        status = 202 if state == 'UNCONFIRMED' else 409 if state in ('NOT_COMMITTED', 'REJECTED') else 503 if state == 'FAILED' else 200
    error = data.get('error')
    cleanup = data.get('cleanup_pending') is True or type(error) is dict and error.get('cleanup_pending') is True
    envelope: dict[str, object] = {'version': 1, 'outcome': state, 'cleanup_pending': cleanup, 'data': data}
    if status >= 400:
        safe_error = cast(dict[str, object], error) if type(error) is dict else {'code': 'PRECONDITION_FAILED', 'reason': 'NOT_COMMITTED'}
        envelope['error'] = {key: safe_error.get(key, default) for key, default in
            (('code', 'STORAGE_FAILED'), ('operation', 'http'), ('field', 'result'), ('reason', 'UNAVAILABLE'))}
        status = {'ACCESS_DENIED': 403, 'INVALID_INPUT': 400, 'RESOURCE_BUSY': 429, 'MODE_BLOCKED': 409}.get(str(safe_error.get('code')), status)
    return status, envelope


def public_value(value: object) -> object:
    """Encode only a caller-selected public projection, never arbitrary attributes."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: public_value(getattr(value, field.name)) for field in fields(value)}
    if type(value) in (dict, MappingProxyType):
        return {key: public_value(item) for key, item in cast(dict, value).items()}
    if type(value) in (list, tuple):
        return [public_value(item) for item in cast(tuple, value)]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError('An explicit public projection is required.')


def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate request field.')
        value[key] = item
    return value


class ManagedHTTP:
    """One listener with bounded connections and no ambient filesystem access."""
    def __init__(self, settings: DeploymentSettings, identity: IdentityAuthority,
                 dispatch: Callable[[Principal | None, str, str, dict[str, object]], Coroutine[object, object, object]],
                 health: Callable[[], dict[str, object]], static_root: Path, *, identity_source: Callable[[], IdentityAuthority] | None = None,
                 audit_source: Callable[[], DeveloperAudit] | None = None):
        self.settings, self.dispatch, self.health = settings, dispatch, health
        self.audit_source = audit_source
        self.identity_source = identity_source or (lambda: identity)
        self.assets = {path: (static_root / name).read_bytes() for path, name in
                       (('/', 'index.html'), ('/app.js', 'app.js'), ('/style.css', 'style.css'))}
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task] = set()
        self.work: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.closed = False
        self.origin = settings.text('deployment.origin')
        self.authority = urlsplit(self.origin).netloc

    async def start(self) -> None:
        """Listen only after the application has acquired its persistent resource lease."""
        if self.closed or self.server is not None:
            raise ValueError('HTTP service already started.')
        self.server = await asyncio.start_server(self.handle, self.settings.text('deployment.bind'),
            self.settings.integer('deployment.port'), limit=8192)

    async def send(self, writer: asyncio.StreamWriter, status: int, body: object,
                   *, content_type: str = 'application/json; charset=utf-8', cookies: tuple[str, ...] = (),
                   authorize: Callable[[], object] | None = None) -> None:
        encoded = body if type(body) is bytes else json.dumps(public_value(body), ensure_ascii=False, separators=(',', ':')).encode()
        if len(encoded) > self.settings.integer('management.body_max_bytes'):
            status, encoded = 503, b'{"version":1,"outcome":"UNCONFIRMED","cleanup_pending":false,"error":{"code":"RESOURCE_BUSY","operation":"http","field":"response","reason":"RESPONSE_LIMIT"}}'
        lines = [f'HTTP/1.1 {status} Response', f'Content-Type: {content_type}', f'Content-Length: {len(encoded)}',
            'Connection: close', 'Cache-Control: no-store', 'X-Content-Type-Options: nosniff',
            "Content-Security-Policy: default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            'Referrer-Policy: no-referrer', 'Cross-Origin-Resource-Policy: same-origin']
        lines += ['Set-Cookie: ' + cookie for cookie in cookies]
        if authorize is not None:
            authorize()
        writer.write(('\r\n'.join(lines) + '\r\n\r\n').encode() + encoded)
        await asyncio.wait_for(writer.drain(), self.settings.integer('management.http_timeout_seconds'))

    async def read_request(self, reader: asyncio.StreamReader, timeout: int) -> tuple[str, str, dict[str, str], dict[str, object]]:
        """Parse one bounded closed request before authentication or dispatch."""
        raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout)
        if len(raw) > 8192:raise ValueError('Header limit.')
        lines = raw.decode('ascii').split('\r\n')
        method, path, version = lines[0].split(' ')
        if method not in ('GET', 'POST') or version != 'HTTP/1.1' or not path.startswith('/') or '?' in path or '#' in path:
            raise ValueError('Unsupported request target.')
        headers: dict[str, str] = {}
        for line in lines[1:-2]:
            if ':' not in line or line[0].isspace():raise ValueError('Invalid header.')
            name, value = line.split(':', 1);name = name.lower()
            if (name in headers or re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name) is None
                    or any(ord(char) < 32 and char != '\t' or ord(char) == 127 for char in value)):
                raise ValueError('Invalid header name or value.')
            headers[name] = value.strip()
        if headers.get('host') != self.authority or 'transfer-encoding' in headers:
            raise OwnerFailure('ACCESS_DENIED', 'origin', 'ORIGIN_REJECTED')
        host_request = path.startswith('/api/host/')
        audit_request = path.startswith('/api/audit/')
        if audit_request and self.audit_source is None:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'AUDIT_ACCESS_DENIED')
        if not host_request and method == 'POST' and headers.get('origin') != self.origin:
            raise OwnerFailure('ACCESS_DENIED', 'origin', 'ORIGIN_REJECTED')
        if 'origin' in headers and headers['origin'] != self.origin:
            raise OwnerFailure('ACCESS_DENIED', 'origin', 'ORIGIN_REJECTED')
        if not headers.get('content-length', '0').isascii() or not headers.get('content-length', '0').isdigit():
            raise ValueError('Invalid content length.')
        length = int(headers.get('content-length', '0'))
        if not 0 <= length <= self.settings.integer('management.body_max_bytes') or method == 'GET' and length:
            raise ValueError('Body limit.')
        payload: dict[str, object] = {}
        if method == 'POST':
            if headers.get('content-type') != 'application/json' or not length:
                raise ValueError('JSON body required.')
            body = await asyncio.wait_for(reader.readexactly(length), timeout)
            parsed = json.loads(body, object_pairs_hook=object_pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            if type(parsed) is not dict:raise ValueError('Object required.')
            payload = parsed
        return method, path, headers, payload

    async def authenticate_request(self, method: str, path: str, headers: dict[str, str]) -> Principal | None:
        """Resolve current authority and CSRF only after the origin boundary."""
        host_request = path.startswith('/api/host/')
        audit_request = path.startswith('/api/audit/')
        if audit_request and self.audit_source is None:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'AUDIT_ACCESS_DENIED')
        principal = None
        public_auth = method == 'POST' and path in ('/api/bootstrap/administrator', '/api/login', '/api/audit/login')
        if not public_auth:
            if host_request:
                bearer = headers.get('authorization', '')
                if not bearer.startswith('Bearer '):raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
                credential = bearer[7:]
            else:
                cookie_pairs: list[tuple[str, object]] = []
                for pair in headers.get('cookie', '').split(';'):
                    if '=' in pair:
                        name, value = pair.strip().split('=', 1)
                        cookie_pairs.append((name, value))
                cookies = object_pairs(cookie_pairs)
                credential = cast(str, cookies.get('iris_audit' if audit_request else 'iris_session', ''))
            if audit_request:
                assert self.audit_source is not None
                principal = self.audit_source().authenticate(credential)
            else:
                principal = await self.identity_source().authenticate(credential, host=host_request)
            if method == 'POST' and not host_request and not hmac.compare_digest(digest(headers.get('x-csrf-token', '')), principal.csrf_digest or ''):
                raise OwnerFailure('ACCESS_DENIED', 'csrf', 'CSRF_REJECTED')
        return principal

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None:return
        if self.closed or len(self.tasks) + len(self.work) >= self.settings.integer('management.http_connections'):
            writer.close();return
        self.tasks.add(task);self.writers.add(writer)
        timeout = self.settings.integer('management.http_timeout_seconds')
        started = False
        dispatched = False
        effects = RequestEffects()
        path = ''
        try:
            method, path, headers, payload = await self.read_request(reader, timeout)
            if method == 'GET' and path in self.assets:
                kind = 'text/html; charset=utf-8' if path == '/' else 'text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8'
                await self.send(writer, 200, self.assets[path], content_type=kind);return
            if method == 'GET' and path == '/health':
                await self.send(writer, 200, {'version': 1, 'health': self.health()});return
            if self.health()['state'] in ('MAINTENANCE', 'RECOVERING'):
                raise OwnerFailure('RESOURCE_BUSY', 'maintenance', 'MAINTENANCE_ACTIVE', True)
            principal = await self.authenticate_request(method, path, headers)
            def start_query(wire: bytes) -> None:
                nonlocal started
                if started or writer.is_closing():
                    raise OwnerFailure('ACCESS_DENIED', 'delivery', 'RESPONSE_CLOSED')
                writer.write(wire)
                started = True
            def encode_query(value) -> bytes:
                encoded = json.dumps({'version': 1, 'outcome': 'OBSERVED', 'data': {'status': 'FOUND', 'value': public_value(value)}},
                    ensure_ascii=False, separators=(',', ':')).encode()
                if len(encoded) > self.settings.integer('management.body_max_bytes'):
                    raise OwnerFailure('RESOURCE_BUSY', 'delivery', 'RESPONSE_LIMIT')
                return (f'HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {len(encoded)}\r\n'
                    'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n').encode() + encoded
            async def dispatch_bound():
                # The application serializes identity-changing HTTP commands
                # until the native owner performs its final first-write barrier.
                with observe_effects(effects), DeliveryScope(encode_query, lambda: not self.closed and not writer.is_closing()
                        and principal is not None and principal.expires_at_us > time.time_ns() // 1000, start_query):
                    return await self.dispatch(principal, method, path, payload)
            dispatched = True
            work = asyncio.create_task(dispatch_bound())
            self.work.add(work)
            work.add_done_callback(self._completed)
            done, _ = await asyncio.wait((work,), timeout=timeout)
            if started:
                await asyncio.wait_for(writer.drain(), timeout)
                return
            if not done:
                nested = payload.get('input')
                original = nested if type(nested) is dict else payload
                await self.send(writer, 202, {'version': 1, 'outcome': 'UNCONFIRMED', 'state': 'UNCONFIRMED',
                    'operation_key': original.get('key', original.get('operation_key')), 'cleanup_pending': True});return
            result = work.result()
            from .backup_download import BackupDownload
            if type(result) is BackupDownload:
                def write_archive(chunk: bytes):
                    nonlocal started
                    started = True
                    writer.write(chunk)
                async def drain_archive():
                    await asyncio.wait_for(writer.drain(), timeout)
                await result.stream(write_archive, drain_archive)
                return
            cookies_out: tuple[str, ...] = ()
            if path == '/api/login' and type(result) is dict and result.get('session') is not None:
                session = cast(dict[str, str], result['session'])
                secure = '; Secure' if self.origin.startswith('https:') else ''
                lifetime = self.settings.integer('management.session_seconds')
                cookies_out = (f'iris_session={session["session"]}; Path=/; HttpOnly; SameSite=Strict; Max-Age={lifetime}{secure}',
                    f'iris_csrf={session["csrf"]}; Path=/; SameSite=Strict; Max-Age={lifetime}{secure}')
                result = {'authenticated': True}
            if path == '/api/audit/login' and type(result) is dict and 'audit_session' in result:
                session = cast(dict[str, str], result['audit_session'])
                assert self.audit_source is not None
                principal = self.audit_source().authenticate(session['credential'])
                secure = '; Secure' if self.origin.startswith('https:') else ''
                lifetime = max(0, (principal.expires_at_us - time.time_ns() // 1000) // 1000000)
                cookies_out = (f'iris_audit={session["credential"]}; Path=/api/audit; HttpOnly; SameSite=Strict; Max-Age={lifetime}{secure}',
                    f'iris_audit_csrf={session["csrf"]}; Path=/; SameSite=Strict; Max-Age={lifetime}{secure}')
                result = {'authenticated': True}
            if path == '/api/audit/logout':
                cookies_out = ('iris_audit=; Path=/api/audit; HttpOnly; SameSite=Strict; Max-Age=0',
                    'iris_audit_csrf=; Path=/; SameSite=Strict; Max-Age=0')
            from companion_memory.persistence import Committed
            if path in ('/api/logout', '/api/password') and type(result) is Committed:
                cookies_out = ('iris_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0', 'iris_csrf=; Path=/; SameSite=Strict; Max-Age=0')
            status, envelope = result_envelope(result)
            audit = self.audit_source() if self.audit_source is not None else None
            await self.send(writer, status, envelope, cookies=cookies_out,
                authorize=(lambda: audit.authorize_delivery(principal, path))
                if principal is not None and principal.kind == 'developer_audit' and audit is not None else None)
        except OwnerFailure as error:
            if started:return
            status = 401 if error.reason == 'AUTHENTICATION_REQUIRED' else 403 if error.code == 'ACCESS_DENIED' else 429 if error.code == 'RESOURCE_BUSY' else 409
            # Failed login may durably record a rate-limit attempt, but never
            # claims a business write. Other post-commit denials must retain
            # original operation confirmation without disclosing its result.
            uncertain = effects.possible_commit and path != '/api/login'
            try:await self.send(writer, status, {'version': 1, 'outcome': 'UNCONFIRMED' if uncertain else 'REJECTED', 'cleanup_pending': error.cleanup_pending,
                'error': {'code': error.code, 'operation': 'http', 'field': error.field, 'reason': error.reason}})
            except (ConnectionError, OSError, asyncio.TimeoutError):pass
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, RecursionError):
            if started:return
            try:await self.send(writer, 202 if dispatched else 400, {'version': 1,
                'outcome': 'UNCONFIRMED' if dispatched else 'REJECTED', 'cleanup_pending': dispatched,
                'error': {'code': 'INTERNAL_FAILURE' if dispatched else 'INVALID_INPUT', 'operation': 'http',
                    'field': 'result' if dispatched else 'request', 'reason': 'CONFIRM_ORIGINAL' if dispatched else 'INVALID_SHAPE'}})
            except (ConnectionError, OSError, asyncio.TimeoutError):pass
        except (ConnectionError, OSError):
            pass
        except Exception:
            if started:return
            try:await self.send(writer, 202 if dispatched else 503, {'version': 1,
                'outcome': 'UNCONFIRMED' if dispatched else 'REJECTED', 'cleanup_pending': dispatched,
                'error': {'code': 'INTERNAL_FAILURE', 'operation': 'http', 'field': 'result',
                    'reason': 'CONFIRM_ORIGINAL' if dispatched else 'UNAVAILABLE'}})
            except (ConnectionError, OSError, asyncio.TimeoutError):pass
        finally:
            writer.close()
            try:await asyncio.wait_for(writer.wait_closed(), timeout)
            except (ConnectionError, OSError, asyncio.TimeoutError):pass
            self.writers.discard(writer);self.tasks.discard(task)

    def _completed(self, task: asyncio.Task) -> None:
        self.work.discard(task)
        if not task.cancelled():task.exception()

    async def close(self) -> bool:
        """Stop admission and bound waiting without cancelling owned business work."""
        self.closed = True
        if self.server is not None:
            self.server.close();await self.server.wait_closed()
        pending = tuple(self.tasks | self.work)
        if pending:
            await asyncio.wait(pending, timeout=self.settings.integer('management.maintenance_timeout_seconds'))
        return not self.tasks and not self.work
