"""Local host credentials and literal entry scopes. No raw credential is persisted.

256-bit random secrets use a per-token salt and SHA-256, not a password KDF:
these are uniformly random machine credentials, not guessable user passwords.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from typing import Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from .db import dumps, now
from .memory_ops import operation

DEFAULT_LIMITS = {'rate_per_second': 20, 'burst': 60}
CREDENTIAL = re.compile(r'iris_ht_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})', re.ASCII)


def valid_entry_id(value):
    return (isinstance(value, str) and 1 <= len(value) <= 200 and bool(value.strip())
            and not any(c in '?#' or unicodedata.category(c) == 'Cc' for c in value))


class Scope(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    kind: Literal['all', 'entries', 'prefix']
    entries: tuple[str, ...] = Field(default=(), max_length=1000)
    prefix: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def consistent(self):
        if self.kind == 'entries':
            if not self.entries or self.prefix is not None or len(set(self.entries)) != len(self.entries):
                raise ValueError('入口列表须非空、不重复，且不能同时指定前缀')
            if any(not valid_entry_id(value) for value in self.entries):
                raise ValueError('入口 ID 须为 1—200 个字符，不能为空白或含 ?、#、控制字符')
        elif self.entries or (self.kind == 'prefix') != (self.prefix is not None):
            raise ValueError('范围字段须与 kind 一致')
        if self.prefix is not None and not valid_entry_id(self.prefix):
            raise ValueError('前缀不能为空白或含 ?、#、控制字符')
        return self

    def allows(self, entry_id):
        # Null denotes character-global data, not an entry wildcard.
        return entry_id is None or self.kind == 'all' or (
            entry_id in self.entries if self.kind == 'entries' else entry_id.startswith(self.prefix))

    def require(self, entry_id):
        if entry_id is not None and not valid_entry_id(entry_id):
            raise TokenError(400, 'invalid_request', '入口 ID 须为 1—200 个字符，不能为空白或含 ?、#、控制字符')
        if not self.allows(entry_id):
            raise TokenError(403, 'entry_forbidden', '令牌无权访问该入口')

    def allows_write(self, entry_id):
        return self.allows(entry_id) and (entry_id is not None or self.kind == 'all')

    def require_write(self, entry_id):
        self.require(entry_id)
        if not self.allows_write(entry_id):
            raise TokenError(403, 'entry_forbidden', '创建或修改全局目标需要 all 范围令牌')

    def sql(self, column, *, write=False):
        """column is an internal SQL identifier, never supplied by a host."""
        if self.kind == 'all':
            return '1', []
        global_clause = '' if write else f'{column} IS NULL OR '
        if self.kind == 'entries':
            return f'({global_clause}{column} IN (SELECT value FROM json_each(?)))', [dumps(self.entries)]
        return f'({global_clause}substr({column},1,length(?))=?)', [self.prefix, self.prefix]


class NewToken(BaseModel):
    model_config = ConfigDict(extra='forbid')
    host: str = Field(min_length=1, max_length=100)
    scope: Scope

    @field_validator('host')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('宿主名称不能为空白')
        return value.strip()


class RateLimits(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rate_per_second: int = Field(default=20, strict=True, ge=1, le=1000)
    burst: int = Field(default=60, strict=True, ge=1, le=10000)


class TokenError(Exception):
    def __init__(self, status=401, code='invalid_token', message='需要有效的宿主 Bearer 令牌', retry_after=None):
        super().__init__(message)
        self.status, self.code, self.retry_after = status, code, retry_after

    def response(self):
        headers = {'Cache-Control': 'no-store'}
        body = {'code': self.code, 'message': str(self)}
        if self.status == 401:
            headers['WWW-Authenticate'] = 'Bearer'
        if self.retry_after is not None:
            headers['Retry-After'] = str(self.retry_after)
            body['retry_after_seconds'] = self.retry_after
        return JSONResponse({'error': body}, status_code=self.status, headers=headers)


@dataclass(frozen=True)
class Principal:
    id: str
    host: str
    scope: Scope


def token_projection(row):
    return {**{k: row[k] for k in ('id', 'host', 'created_at', 'last_used_at', 'revoked_at')},
            'scope': json.loads(row['scope_json'])}


def list_tokens(conn):
    return [token_projection(row) for row in conn.execute(
        'SELECT id,host,scope_json,created_at,last_used_at,revoked_at FROM host_tokens ORDER BY created_at DESC,id')]


def _digest(salt, credential):
    return hashlib.sha256(bytes.fromhex(salt) + credential.encode('ascii')).hexdigest()


class Tokens:
    def __init__(self, store, *, clock=time.monotonic):
        self.store, self.clock = store, clock
        # The Store writer lock serializes both bucket updates and revocations.
        # Single-process service: a restart replenishes buckets, never credentials.
        self._buckets = {}

    def create(self, *, host, scope, actor='admin'):
        payload = NewToken(host=host, scope=scope)
        token_id = secrets.token_hex(16)
        credential = 'iris_ht_' + token_id + '.' + secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        with self.store.write() as conn:
            conn.execute('''INSERT INTO host_tokens(id,host,scope_json,salt,token_hash,created_at)
                VALUES(?,?,?,?,?,?)''', (token_id, payload.host, payload.scope.model_dump_json(), salt,
                                       _digest(salt, credential), now()))
            operation(conn, 'token_create', 'host_token', token_id,
                      {'host': payload.host, 'scope': payload.scope.model_dump()}, actor=actor)
            result = token_projection(conn.execute('SELECT * FROM host_tokens WHERE id=?', (token_id,)).fetchone())
        return {**result, 'token': credential}

    def revoke(self, token_id, *, actor='admin'):
        with self.store.write() as conn:
            row = conn.execute('SELECT * FROM host_tokens WHERE id=?', (token_id,)).fetchone()
            if row is None:
                raise KeyError(token_id)
            if row['revoked_at'] is None:
                conn.execute('UPDATE host_tokens SET revoked_at=? WHERE id=?', (now(), token_id))
                operation(conn, 'token_revoke', 'host_token', token_id, {'host': row['host']}, actor=actor)
            self._buckets.pop(token_id, None)
        return {'id': token_id, 'revoked': True}

    def authenticate(self, authorization):
        parts = authorization.split(' ') if authorization and len(authorization) < 256 else []
        match = CREDENTIAL.fullmatch(parts[1]) if len(parts) == 2 and parts[0].lower() == 'bearer' else None
        if match is None:
            raise TokenError()
        with self.store.write() as conn:
            row = conn.execute('SELECT * FROM host_tokens WHERE id=? AND revoked_at IS NULL', (match[1],)).fetchone()
            if row is None or not hmac.compare_digest(row['token_hash'], _digest(row['salt'], parts[1])):
                raise TokenError()
            try:
                scope = Scope.model_validate_json(row['scope_json'])
            except ValueError:
                # Legacy ranges can contain entry characters now forbidden.
                # Treat them as invalid credentials, never as an HTTP 500.
                raise TokenError() from None
            setting = conn.execute("SELECT value_json FROM runtime_settings WHERE key='host_tokens'").fetchone()
            limits = RateLimits.model_validate(json.loads(setting[0]) if setting else DEFAULT_LIMITS)
            current = self.clock()
            remaining, previous = self._buckets.get(row['id'], (float(limits.burst), current))
            remaining = min(limits.burst, remaining + max(0, current-previous)*limits.rate_per_second)
            if remaining < 1:
                self._buckets[row['id']] = (remaining, current)
                raise TokenError(429, 'rate_limited', '宿主请求过于频繁',
                                 max(1, math.ceil((1-remaining)/limits.rate_per_second)))
            self._buckets[row['id']] = (remaining-1, current)
            conn.execute('UPDATE host_tokens SET last_used_at=? WHERE id=?', (now(), row['id']))
            return Principal(row['id'], row['host'], scope)

    def bind_recall(self, recall_id, principal):
        with self.store.write() as conn:
            conn.execute('INSERT INTO host_recalls(recall_id,token_id) VALUES(?,?)', (recall_id, principal.id))

    def require_recall(self, recall_id, principal):
        with self.store.read() as conn:
            row = conn.execute('''SELECT r.entry_id,h.token_id FROM recalls r
                LEFT JOIN host_recalls h ON h.recall_id=r.id WHERE r.id=?''', (recall_id,)).fetchone()
        if row is None:
            raise KeyError(recall_id)
        if row['token_id'] != principal.id:
            raise TokenError(403, 'recall_forbidden', '令牌无权反馈该次召回')
        principal.scope.require(row['entry_id'])


def install_tokens(app):
    @app.exception_handler(TokenError)
    async def token_error(request, error):
        return error.response()

    @app.middleware('http')
    async def host_boundary(request, call_next):
        path = request.scope['path']
        if not (path == '/api/v1' or path.startswith('/api/v1/')) or not app.state.ready:
            return await call_next(request)
        try:
            values = request.headers.getlist('authorization')
            if len(values) != 1:
                raise TokenError()
            principal = await run_in_threadpool(app.state.tokens.authenticate, values[0])
            request.state.principal = principal
            if path.startswith('/api/v1/entries/'):
                principal.scope.require(path[len('/api/v1/entries/'):].split('/')[0])
        except TokenError as error:
            return error.response()
        except sqlite3.OperationalError:
            return TokenError(503, 'unavailable', '数据库暂时不可用', retry_after=1).response()
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response
