"""Bounded local business HTTP backed only by issued native information ports.

Tokens are explicit expiring test sessions. Each body-reading owner prepares the full
HTTP response before entering its final short mode/authorization barrier; that
barrier starts the actual first socket write, not an intermediate native return.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import secrets
import time
from types import MappingProxyType
from urllib.parse import parse_qsl
from companion_memory.information.observation import InformationObserver
from companion_memory.information.business import InformationPort, BusinessPending
from companion_memory.information.errors import InformationError, InformationRejected, InformationNotCommitted, InformationUnconfirmed
from companion_memory.information.records import Record
from companion_memory.persistence import Found, NotFound, Committed, Value
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge
from companion_memory.retrieval.query_service import RecallPending
from companion_memory.retrieval.delivery import DeliveryScope
from companion_memory.runtime.content_gate import ContentGate
from companion_memory.configuration.information_persistence import StoredInformationConfiguration
from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration,stored_semantic_configuration_issue


def failure_value(error: InformationError) -> Record:
    return MappingProxyType({'code': error.code, 'operation': error.operation, 'field': error.field,
        'reason': error.reason, 'cleanup_pending': error.cleanup_pending})


def response_value(result: object) -> tuple[int, Record]:
    if type(result) is Found: return 200, MappingProxyType({'status': 'FOUND', 'value': result.value})
    if type(result) is NotFound: return 200, MappingProxyType({'status': 'ABSENT'})
    if type(result) is Committed:
        receipt = result.receipt
        return 200, MappingProxyType({'status': 'COMMITTED', 'source': result.source, 'receipt': MappingProxyType({
            'database_id': receipt.identity.database_id, 'scope_id': receipt.identity.scope_id,
            'operation_kind': receipt.identity.operation_kind, 'operation_key': receipt.identity.operation_key,
            'commit_id': receipt.commit_id, 'fingerprint': receipt.fingerprint, 'recorded_at': receipt.recorded_at, 'result': receipt.result})})
    if isinstance(result, (InformationRejected, InformationNotCommitted)):
        error = result.error
        status = {'INVALID_INPUT': 400, 'ACCESS_DENIED': 403, 'IDEMPOTENCY_CONFLICT': 409, 'PRECONDITION_FAILED': 409,
            'RESOURCE_BUSY': 429, 'TIMEOUT': 504}.get(error.code, 503)
        if error.code == 'INVALID_INPUT' and error.reason == 'LIMIT_EXCEEDED': status = 413
        return status, MappingProxyType({'status': result.status, 'error': failure_value(error)})
    if type(result) is InformationUnconfirmed:
        return 202, MappingProxyType({'status': 'UNCONFIRMED', 'operation_key': result.reference.identity.operation_key,
            'confirmation': result.reference.identity.operation_kind, 'error': failure_value(result.error)})
    if type(result) is BusinessPending:
        return 202, MappingProxyType({'status': 'UNCONFIRMED', 'operation_key': result.operation_key,
            'confirmation': result.confirmation, 'error': failure_value(result.error)})
    if type(result) is RecallPending:
        return 202, MappingProxyType({'status': 'UNCONFIRMED', 'operation_key': result.request_key,
            'intent_digest': result.intent_digest, 'confirmation': 'resolve_recall', 'error': failure_value(result.error)})
    return 503, MappingProxyType({'status': 'FAILED', 'error': failure_value(InformationError('STORAGE_FAILED', 'http', 'storage', 'INTEGRITY_FAILURE'))})


def frame(status: int, value: Record) -> bytes:
    body = encode_content(value, 131072)
    label = {200: 'OK', 202: 'Accepted', 400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden', 404: 'Not Found',
        405: 'Method Not Allowed', 409: 'Conflict', 413: 'Content Too Large', 429: 'Too Many Requests', 503: 'Service Unavailable', 504: 'Gateway Timeout'}[status]
    return ('HTTP/1.1 ' + str(status) + ' ' + label + '\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: ' + str(len(body))
        + '\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n').encode('ascii') + body


@dataclass(frozen=True, slots=True)
class TestSession:
    port: InformationPort | InformationObserver
    expires_at: float


class InformationHTTP:
    """One loopback listener, four connections, sixteen explicitly scoped sessions."""
    def __init__(self, configuration: StoredInformationConfiguration | StoredTextConfiguration | StoredSemanticConfiguration, gate: ContentGate):
        if type(configuration) not in (StoredInformationConfiguration,StoredTextConfiguration) and stored_semantic_configuration_issue(configuration) is not None:
            raise ValueError('HTTP requires the native persisted information configuration.')
        self.gate = gate
        self.settings = configuration.candidate.information.record('management.host')
        self.sessions: dict[str, TestSession] = {}
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[object]] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.closed = False

    def issue_test_session(self, port: InformationPort, expires_at: float) -> str:
        if self.closed or type(port) is not InformationPort or len(self.sessions) >= 16 or not time.monotonic() < expires_at <= time.monotonic() + 3600:
            raise ValueError('An explicit finite native test session is required.')
        token = secrets.token_urlsafe(32)
        self.sessions[token] = TestSession(port, expires_at)
        return token

    def revoke_test_session(self, token: str) -> None:
        with self.gate.lock: self.sessions.pop(token, None)

    def issue_observation_session(self, port: InformationObserver, expires_at: float) -> str:
        if self.closed or type(port) is not InformationObserver or len(self.sessions) >= 16 or not time.monotonic() < expires_at <= time.monotonic() + 3600:
            raise ValueError('An explicit finite observation session is required.')
        token = secrets.token_urlsafe(32)
        self.sessions[token] = TestSession(port, expires_at)
        return token

    async def start(self) -> tuple[str, int]:
        if self.closed or self.server is not None: raise ValueError('HTTP listener already initialized.')
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, limit=8193)
        address = self.server.sockets[0].getsockname()
        return str(address[0]), int(address[1])

    async def dispatch(self, port: InformationPort | InformationObserver, method: str, path: str, payload: object) -> object:
        if type(port) is InformationObserver:
            if method != 'GET' or not path.startswith('/api/observe/'):
                return InformationRejected(InformationError('ACCESS_DENIED', 'observe_information', 'capability', 'OPERATION_NOT_GRANTED'))
            return await port.read(path.removeprefix('/api/observe/'), payload)
        if type(port) is not InformationPort:
            return InformationRejected(InformationError('ACCESS_DENIED', 'http', 'capability', 'BINDING_MISMATCH'))
        if method == 'GET':
            if path == '/api/host/state': return await port.get_state_view()
            if path == '/api/host/goals':
                if type(payload) is not dict or set(payload) - {'cursor'} or 'cursor' in payload and type(payload['cursor']) is not str:
                    return InformationRejected(InformationError('INVALID_INPUT', 'list_open_goals', 'goal', 'INVALID_SHAPE'))
                return await port.list_open_goals(payload.get('cursor'))
        if method == 'POST':
            if path == '/api/host/prepare': return await port.prepare_reply(payload)
            if path == '/api/host/memory/search': return await port.search_memory(payload)
            if path == '/api/host/memory/deep-recall': return await port.deep_recall(payload)
            if path == '/api/host/usage': return await port.record_usage(payload)
            if path == '/api/host/usage/resolve': return await port.resolve_usage(payload)
            if path == '/api/host/state/set': return await port.set_state(payload)
            if path == '/api/host/state/update': return await port.update_state(payload)
            if path == '/api/host/state/end': return await port.end_activity(payload)
            if path == '/api/host/goals/inject': return await port.inject_goal(payload)
            if path == '/api/host/goals/status': return await port.update_goal_status(payload)
            if path == '/api/host/goals/deadline': return await port.change_deadline(payload)
            if path == '/api/host/recalls/resolve':
                if type(payload) is not dict or set(payload) != {'query', 'prepared', 'deep'} or type(payload['prepared']) is not bool or type(payload['deep']) is not bool:
                    raise InvalidValue()
                return await port.resolve_recall(payload['query'], prepared=payload['prepared'], deep=payload['deep'])
            if path == '/api/host/operations/resolve':
                if type(payload) is not dict or set(payload) != {'operation', 'input'} or type(payload['operation']) is not str: raise InvalidValue()
                return await port.resolve_operation(payload['operation'], payload['input'])
        raise InvalidValue()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None: writer.transport.abort(); return
        if self.closed or len(self.tasks) >= 4:
            writer.transport.abort(); return
        self.tasks.add(task); self.writers.add(writer)
        started = False
        token = ''; session: TestSession | None = None
        def authorized() -> bool:
            return not self.closed and not writer.is_closing() and session is not None and self.sessions.get(token) is session and time.monotonic() < session.expires_at
        def start(wire: bytes) -> None:
            nonlocal started
            if started: raise RuntimeError('A response can only start once.')
            writer.write(wire); started = True
        async def error(status: int, reason: str) -> None:
            if started or writer.is_closing(): return
            start(frame(status, MappingProxyType({'status': 'REJECTED', 'error': failure_value(InformationError(
                'ACCESS_DENIED' if status in (401, 403) else 'TIMEOUT' if status == 504 else 'INVALID_INPUT', 'http', 'input', reason))})))
            await asyncio.wait_for(writer.drain(), 2)
        try:
            try: header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
            except asyncio.LimitOverrunError: await error(413, 'LIMIT_EXCEEDED'); return
            if len(header) > 8192: await error(413, 'LIMIT_EXCEEDED'); return
            lines = header.decode('ascii').split('\r\n')
            parts = lines[0].split(' ')
            if len(parts) != 3 or parts[2] != 'HTTP/1.1': raise InvalidValue()
            method, path = parts[:2]
            if method not in ('GET', 'POST'): await error(405, 'INVALID_SHAPE'); return
            path, separator, raw_query = path.partition('?')
            observation = path.startswith('/api/observe/')
            if not (path.startswith('/api/host/') or observation) or '#' in path or '%' in path or separator and not (observation or method == 'GET' and path == '/api/host/goals'):
                await error(404, 'INVALID_SHAPE'); return
            parameters = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, errors='strict', max_num_fields=8) if separator else []
            if len({key for key, _ in parameters}) != len(parameters): raise InvalidValue()
            headers: dict[str, str] = {}
            for line in lines[1:-2]:
                name, separator, value = line.partition(':')
                if separator != ':' or not name or name != name.strip() or not name.isascii() or any(not (c.isalnum() or c == '-') for c in name): raise InvalidValue()
                name = name.lower()
                if name in headers: raise InvalidValue()
                headers[name] = value.strip()
            if 'transfer-encoding' in headers or 'expect' in headers or 'host' not in headers: raise InvalidValue()
            length = headers.get('content-length', '0')
            if not length.isascii() or not length.isdecimal() or len(length) > 6: raise InvalidValue()
            size = int(length)
            if size > 16384: await error(413, 'LIMIT_EXCEEDED'); return
            if method == 'GET' and size or method == 'POST' and not size: raise InvalidValue()
            body = await asyncio.wait_for(reader.readexactly(size), 2) if size else b''
            collected = time.monotonic()
            credential = headers.get('authorization', '')
            if not credential.startswith('Bearer '): await error(401, 'BINDING_MISMATCH'); return
            token = credential[7:]; session = self.sessions.get(token)
            if not authorized() or session is None: await error(401, 'BINDING_MISMATCH'); return
            payload: object = decode_content(body, 16384) if body else dict(parameters)
            if observation and path == '/api/observe/provider/usage':
                if type(payload) is not dict or not set(payload) <= {'start', 'end', 'caller_scope', 'capability', 'task_role', 'profile_id', 'account_id', 'group_by'}:
                    raise InvalidValue()
                payload = {'caller_scope': None, 'capability': None, 'task_role': None, 'profile_id': None, 'account_id': None, 'group_by': 'NONE'} | payload
            query_route = path in ('/api/host/prepare', '/api/host/memory/search', '/api/host/memory/deep-recall')
            with DeadlineScope(collected + (1 if query_route or method == 'GET' else 5)), DeliveryScope(
                lambda value: frame(200, MappingProxyType({'status': 'FOUND', 'value': value})), authorized, start):
                result = await self.dispatch(session.port, method, path, payload)
                if not started:
                    if not authorized(): await error(401, 'BINDING_MISMATCH'); return
                    status, value = response_value(result); start(frame(status, value))
            await asyncio.wait_for(writer.drain(), 2)
        except (TimeoutError, asyncio.IncompleteReadError):
            try: await error(504, 'DEADLINE_EXCEEDED')
            except (TimeoutError, ConnectionError, OSError): pass
        except ValueTooLarge:
            try: await error(413, 'LIMIT_EXCEEDED')
            except (TimeoutError, ConnectionError, OSError): pass
        except (InvalidValue, UnicodeError, ValueError):
            try: await error(400, 'INVALID_SHAPE')
            except (TimeoutError, ConnectionError, OSError): pass
        except (ConnectionError, OSError): pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except (ConnectionError, OSError): pass
            self.writers.discard(writer); self.tasks.discard(task)

    def stop_admission(self) -> None:
        with self.gate.lock: self.closed = True
        if self.server is not None: self.server.close()

    async def close(self) -> bool:
        self.stop_admission()
        if self.server is not None: await self.server.wait_closed()
        if self.tasks: await asyncio.wait(tuple(self.tasks), timeout=2)
        return not self.tasks and not self.writers
