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
import h11
import time
from companion_memory.retrieval.delivery import DeliveryScope
from pathlib import Path
from types import MappingProxyType
from typing import cast, TYPE_CHECKING
if TYPE_CHECKING:
    from .communication_sessions import CommunicationSessions
from companion_memory.configuration.network_origin import parse_origin, authority
from .http_protocol import read_head, read_body, response_bytes

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
    from companion_memory.media.service import MediaUnconfirmed
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
            'UNCONFIRMED' if isinstance(result, (Unconfirmed, ConfigurationUnconfirmed, MediaUnconfirmed)) or data.get('state') == 'UNCONFIRMED' else
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
                 audit_source: Callable[[], DeveloperAudit] | None = None,
                 communication_source: Callable[[], CommunicationSessions] | None = None):
        self.settings, self.dispatch, self.health = settings, dispatch, health
        self.audit_source = audit_source
        self.communication_source = communication_source
        self.identity_source = identity_source or (lambda: identity)
        self.assets = {path: (static_root / name).read_bytes() for path, name in
                       (('/', 'index.html'), ('/app.js', 'app.js'), ('/style.css', 'style.css'))}
        for name in ('external_connections.js', 'configuration_operation.js', 'protocol/http.json', 'protocol/ws.json', 'protocol/iris_client.py',
                     'protocol/http_client.py', 'protocol/reconnect.py', 'protocol/event.json', 'protocol/query.json'):
            if (static_root / name).is_file(): self.assets['/' + name] = (static_root / name).read_bytes()
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task] = set()
        self.work: set[asyncio.Task] = set()
        self.connections: set[object] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.closed = False
        self.origin = settings.text('deployment.origin')
        self.external_origin = parse_origin(self.origin)
        self.authority = self.origin.split('://', 1)[1]

    async def start(self) -> None:
        """Listen only after the application has acquired its persistent resource lease."""
        if self.closed or self.server is not None:
            raise ValueError('HTTP service already started.')
        self.server = await asyncio.start_server(self.handle, self.settings.text('deployment.bind'),
            self.settings.integer('deployment.port'), limit=8192)

    async def send(self, writer: asyncio.StreamWriter, status: int, body: object,
                   *, content_type: str = 'application/json; charset=utf-8', cookies: tuple[str, ...] = (),
                   authorize: Callable[[], object] | None = None,
                   first_write: Callable[[bytes], None] | None = None) -> None:
        encoded = body if type(body) is bytes else json.dumps(public_value(body), ensure_ascii=False, separators=(',', ':')).encode()
        if len(encoded) > self.settings.integer('management.body_max_bytes'):
            status, encoded = 503, b'{"version":1,"outcome":"UNCONFIRMED","cleanup_pending":false,"error":{"code":"RESOURCE_BUSY","operation":"http","field":"response","reason":"RESPONSE_LIMIT"}}'
        headers = [('Content-Type', content_type), ('Content-Length', str(len(encoded))),
            ('Connection', 'close'), ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
            ('Content-Security-Policy', "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
            ('Referrer-Policy', 'no-referrer'), ('Cross-Origin-Resource-Policy', 'same-origin')]
        headers += [('Set-Cookie', cookie) for cookie in cookies]
        wire = response_bytes(status, encoded, headers)
        if authorize is not None:
            authorize()
        (first_write or writer.write)(wire)
        await asyncio.wait_for(writer.drain(), self.settings.integer('management.http_timeout_seconds'))

    async def read_request(self, reader: asyncio.StreamReader, timeout: int) -> tuple[str, str, dict[str, str], dict[str, object]]:
        """Parse one bounded closed request before authentication or dispatch."""
        protocol, event = await read_head(reader)
        method, path = event.method.decode('ascii'), event.target.decode('ascii')
        if method not in ('GET', 'POST') or event.http_version != b'1.1' or not path.startswith('/') or '?' in path or '#' in path:
            raise ValueError('Unsupported request target.')
        headers = {name.decode('ascii'): value.decode('ascii') for name, value in event.headers}
        origin = self.external_origin
        if authority(headers.get('host', ''), origin.scheme) != (origin.host, origin.port) or 'transfer-encoding' in headers:
            raise OwnerFailure('ACCESS_DENIED', 'origin', 'ORIGIN_REJECTED')
        host_request = path.startswith('/api/host/')
        audit_request = path.startswith('/api/audit/')
        if audit_request and self.audit_source is None:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'AUDIT_ACCESS_DENIED')
        if not host_request and method == 'POST' and 'origin' not in headers:
            raise OwnerFailure('ACCESS_DENIED', 'origin', 'ORIGIN_REJECTED')
        if 'origin' in headers and parse_origin(headers['origin']) != self.external_origin:
            raise OwnerFailure('ACCESS_DENIED', 'origin', 'ORIGIN_REJECTED')
        if not headers.get('content-length', '0').isascii() or not headers.get('content-length', '0').isdigit():
            raise ValueError('Invalid content length.')
        length = int(headers.get('content-length', '0'))
        if not 0 <= length <= self.settings.integer('management.body_max_bytes') or method == 'GET' and length:
            raise ValueError('Body limit.')
        from .communication_http_schema import BY_PATH
        if path in BY_PATH and length > BY_PATH[path].body_bytes: raise ValueError('Route body limit.')
        payload: dict[str, object] = {}
        if method == 'POST':
            if path == '/api/host/media/chunk':
                if headers.get('content-type') != 'application/octet-stream' or not 1 <= length <= 65536:
                    raise ValueError('Binary block limit.')
                offset = headers.get('x-iris-offset', '')
                if not offset.isascii() or not offset.isdigit() or str(int(offset)) != offset or int(offset) > 1048576:
                    raise ValueError('Canonical offset required.')
                from companion_memory.persistence.schema import valid_identifier
                entry, upload = headers.get('x-iris-entry', ''), headers.get('x-iris-upload', '')
                if not valid_identifier(entry) or not valid_identifier(upload):
                    raise ValueError('Upload binding required.')
                body = await read_body(reader, protocol, length)
                return method, path, headers, {'entry_id': entry,
                    'input': {'upload_id': upload, 'offset': int(offset), 'data': body}}
            if headers.get('content-type') != 'application/json' or not length:
                raise ValueError('JSON body required.')
            body = await read_body(reader, protocol, length)
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
        if self.closed or len(self.connections) >= self.settings.integer('management.http_connections'):
            writer.close();return
        slot = object()
        self.connections.add(slot)
        owned_work: asyncio.Task | None = None
        self.tasks.add(task);self.writers.add(writer)
        timeout = self.settings.integer('management.http_timeout_seconds')
        started = False
        dispatched = False
        effects = RequestEffects()
        path = ''
        def write_response(wire: bytes) -> None:
            nonlocal started
            if started or writer.is_closing():
                raise OwnerFailure('ACCESS_DENIED', 'delivery', 'RESPONSE_CLOSED')
            writer.write(wire)
            started = True
        try:
            method, path, headers, payload = await asyncio.wait_for(self.read_request(reader, timeout), timeout)
            peers = self.settings.text('deployment.trusted_proxy_peers')
            peer = writer.get_extra_info('peername')
            if peers and (peer is None or peer[0] not in peers.split(',')) and not (path == '/health' and peer is not None and peer[0] in ('127.0.0.1', '::1')):
                raise OwnerFailure('ACCESS_DENIED', 'origin', 'PROXY_PEER_REJECTED')
            if path == '/api/host/ws':
                if method != 'GET' or headers.get('upgrade', '').lower() != 'websocket' or self.communication_source is None:
                    raise OwnerFailure('ACCESS_DENIED', 'connection', 'NOT_READY')
                # Authenticated WS capacity is owned separately, leaving HTTP
                # admission available even when all sixteen consumers are live.
                self.connections.discard(slot)
                await self.communication_source().serve(reader, writer, headers, write_response)
                return
            if method == 'GET' and path in self.assets:
                kind = 'text/html; charset=utf-8' if path == '/' else 'text/javascript; charset=utf-8' if path.endswith('.js') else 'application/json; charset=utf-8' if path.endswith('.json') else 'text/plain; charset=utf-8' if path.endswith('.py') else 'text/css; charset=utf-8'
                await self.send(writer, 200, self.assets[path], content_type=kind, first_write=write_response);return
            if method == 'GET' and path == '/health':
                await self.send(writer, 200, {'version': 1, 'health': self.health()}, first_write=write_response);return
            if not path.startswith('/api/audit/') and self.health()['state'] in ('MAINTENANCE', 'RECOVERING'):
                raise OwnerFailure('RESOURCE_BUSY', 'maintenance', 'MAINTENANCE_ACTIVE', True)
            deadline = time.monotonic() + timeout
            principal = await asyncio.wait_for(self.authenticate_request(method, path, headers), timeout)
            def start_query(wire: bytes) -> None:
                nonlocal started
                if started or writer.is_closing():
                    raise OwnerFailure('ACCESS_DENIED', 'delivery', 'RESPONSE_CLOSED')
                assert principal is not None
                self.identity_source().start_delivery(principal, lambda: writer.write(wire))
                started = True
            def encode_query(value) -> bytes:
                encoded = json.dumps({'version': 1, 'outcome': 'OBSERVED', 'data': {'status': 'FOUND', 'value': public_value(value)}},
                    ensure_ascii=False, separators=(',', ':')).encode()
                if len(encoded) > self.settings.integer('management.body_max_bytes'):
                    raise OwnerFailure('RESOURCE_BUSY', 'delivery', 'RESPONSE_LIMIT')
                return response_bytes(200, encoded, [('Content-Type', 'application/json; charset=utf-8'),
                    ('Content-Length', str(len(encoded))), ('Cache-Control', 'no-store'),
                    ('X-Content-Type-Options', 'nosniff'), ('Connection', 'close')])
            async def dispatch_bound():
                # The native owner starts delivery synchronously under its mode
                # barrier; current identity permission is checked at that point.
                with observe_effects(effects), DeliveryScope(encode_query, lambda: not self.closed and not writer.is_closing()
                        and principal is not None and principal.expires_at_us > time.time_ns() // 1000, start_query):
                    return await self.dispatch(principal, method, path, payload)
            dispatched = True
            from companion_memory.persistence.completion import start_owned
            work, result_ready = start_owned(dispatch_bound())
            owned_work = work
            self.work.add(work)
            work.add_done_callback(self._completed)
            done, _ = await asyncio.wait((result_ready,), timeout=max(0, deadline - time.monotonic()))
            if started:
                await asyncio.wait_for(writer.drain(), timeout)
                return
            if not done:
                nested = payload.get('input')
                original = nested if type(nested) is dict else payload
                await self.send(writer, 202, {'version': 1, 'outcome': 'UNCONFIRMED', 'state': 'UNCONFIRMED',
                    'operation_key': original.get('key', original.get('operation_key')), 'cleanup_pending': True}, first_write=write_response);return
            result = result_ready.result()
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
                secure = '; Secure' if self.external_origin.scheme == 'https' else ''
                lifetime = self.settings.integer('management.session_seconds')
                cookies_out = (f'iris_session={session["session"]}; Path=/; HttpOnly; SameSite=Strict; Max-Age={lifetime}{secure}',
                    f'iris_csrf={session["csrf"]}; Path=/; SameSite=Strict; Max-Age={lifetime}{secure}')
                result = {'authenticated': True}
            if path == '/api/audit/login' and type(result) is dict and 'audit_session' in result:
                session = cast(dict[str, str], result['audit_session'])
                assert self.audit_source is not None
                principal = self.audit_source().authenticate(session['credential'])
                secure = '; Secure' if self.external_origin.scheme == 'https' else ''
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
            identity_principal = principal if principal is not None and principal.kind != 'developer_audit' else None
            # A successful self-revocation returns only its original receipt.
            if path in ('/api/logout', '/api/password') and type(result) is Committed:
                identity_principal = None
            await self.send(writer, status, envelope, cookies=cookies_out,
                authorize=(lambda: audit.authorize_delivery(principal, path))
                if principal is not None and principal.kind == 'developer_audit' and audit is not None else None,
                first_write=(lambda wire: self.identity_source().start_delivery(identity_principal, lambda: write_response(wire)))
                if identity_principal is not None else write_response)
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
        except (h11.RemoteProtocolError, ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, RecursionError):
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
            if owned_work is not None and not owned_work.done():
                owned_work.add_done_callback(lambda _: self.connections.discard(slot))
            else:
                self.connections.discard(slot)

    def _completed(self, task: asyncio.Task) -> None:
        self.work.discard(task)
        if not task.cancelled():task.exception()

    async def close(self) -> bool:
        """Stop admission and bound waiting without cancelling owned business work."""
        self.closed = True
        if self.communication_source is not None:
            await self.communication_source().close()
        if self.server is not None:
            self.server.close();await self.server.wait_closed()
        pending = tuple(self.tasks | self.work)
        if pending:
            await asyncio.wait(pending, timeout=self.settings.integer('management.maintenance_timeout_seconds'))
        return not self.tasks and not self.work
