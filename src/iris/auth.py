"""Local administrator sessions; host API authentication remains a later milestone."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .db import dumps, now

COOKIE = 'iris_session'
SESSION_SECONDS = 12 * 60 * 60
MAX_ANONYMOUS_SESSIONS = 128


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    # OWASP scrypt profile: 32 MiB, r=8, p=3. Parameters are versioned in the hash.
    result = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2**15, r=8, p=3, maxmem=64*1024*1024)
    return f'scrypt$32768$8$3${salt.hex()}${result.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = stored.split('$')
        if (algorithm, n, r, p) != ('scrypt', '32768', '8', '3'):
            return False
        result = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), maxmem=64*1024*1024)
        return hmac.compare_digest(result, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(token):
    return hmac.new(token.encode(), b'iris-admin-csrf-v1', hashlib.sha256).hexdigest()


def local(request):
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        return False


def error(code, message, status=400, **extra):
    return JSONResponse({'error': {'code': code, 'message': message, **extra}}, status_code=status)


def audit(conn, action, details=None, actor='admin'):
    conn.execute('INSERT INTO admin_operations(actor,action,details_json,created_at) VALUES(?,?,?,?)',
                 (actor, action, dumps(details or {}), now()))


class Password(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: SecretStr = Field(min_length=8, max_length=256)


class Sessions:
    def __init__(self, store):
        self.store = store
        self.login_lock = threading.Lock()

    def exists(self):
        with self.store.read() as conn:
            return conn.execute('SELECT 1 FROM admin_credentials').fetchone() is not None

    def lookup(self, request):
        token = request.cookies.get(COOKIE, '')
        if not token or len(token) > 128:
            return None
        with self.store.read() as conn:
            row = conn.execute('SELECT * FROM admin_sessions WHERE token_hash=? AND expires_at>?', (digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def issue(self, request, response, *, authenticated):
        token = secrets.token_urlsafe(32)
        lifetime = SESSION_SECONDS if authenticated else 30 * 60
        with self.store.write() as conn:
            conn.execute('DELETE FROM admin_sessions WHERE expires_at<=? OR token_hash=?',
                         (time.time(), digest(request.cookies.get(COOKIE, ''))))
            if not authenticated:
                conn.execute("""DELETE FROM admin_sessions WHERE token_hash IN (
                    SELECT token_hash FROM admin_sessions WHERE authenticated=0
                    ORDER BY expires_at DESC, token_hash LIMIT -1 OFFSET ?)""", (MAX_ANONYMOUS_SESSIONS-1,))
            conn.execute('INSERT INTO admin_sessions VALUES(?,?,?)', (digest(token), int(authenticated), time.time()+lifetime))
        response.set_cookie(COOKIE, token, max_age=lifetime, httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https', path='/')
        return token

    def csrf_valid(self, request):
        token = request.cookies.get(COOKIE, '')
        supplied = request.headers.get('x-iris-csrf', '')
        origin = request.headers.get('origin')
        expected_origin = f'{request.url.scheme}://{request.headers.get("host", "")}'
        return bool(self.lookup(request) and supplied.isascii() and len(supplied) == 64 and hmac.compare_digest(supplied, csrf_token(token))
                    and (origin is None or origin == expected_origin)
                    and request.headers.get('sec-fetch-site') not in ('cross-site', 'same-site')
                    and request.headers.get('content-type', '').split(';')[0] == 'application/json')


def install_auth(app):
    router = APIRouter(prefix='/admin/api')

    @app.middleware('http')
    async def administrator_boundary(request, call_next):
        path = request.scope['path']
        if path.startswith('/api/v1') or path.startswith('/assets/'):
            return await call_next(request)
        if not app.state.ready:
            return await call_next(request)
        configured = app.state.store.setting('setup_complete', False)
        sessions = app.state.sessions
        if not configured and not local(request):
            return error('local_setup_only', '首次设置只允许从本机完成', 403)
        session = sessions.lookup(request)
        authenticated = bool(session and session['authenticated'])
        if path.startswith('/admin/api'):
            public = path in ('/admin/api/session', '/admin/api/setup/password', '/admin/api/login')
            if not public and not authenticated:
                return error('login_required' if configured else 'setup_required',
                             '请先登录' if configured else '请先完成首次设置', 401 if configured else 409,
                             redirect='/login' if configured else '/setup')
            if not configured and authenticated and not (path.startswith('/admin/api/settings') or path in (
                    '/admin/api/setup/complete', '/admin/api/session', '/admin/api/logout', '/admin/api/login')):
                return error('setup_required', '请先完成首次设置', 409, redirect='/setup')
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and not sessions.csrf_valid(request):
                return error('csrf_failed', '请求校验失败，请刷新页面后重试', 403)
        elif path not in ('/setup', '/login'):
            if not configured or not authenticated:
                return RedirectResponse('/setup' if not configured else '/login', status_code=303)
        elif path == '/setup' and configured:
            return RedirectResponse('/' if authenticated else '/login', status_code=303)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @router.get('/session')
    def session_info(request: Request):
        sessions = app.state.sessions
        origin = request.headers.get('origin')
        if (request.headers.get('sec-fetch-site') in ('cross-site', 'same-site')
                or (origin is not None and origin != f'{request.url.scheme}://{request.headers.get("host", "")}')):
            return error('csrf_failed', '请求校验失败，请从本机页面访问', 403)
        with sessions.store.write() as conn:
            conn.execute('DELETE FROM admin_sessions WHERE expires_at<=?', (time.time(),))
        row = sessions.lookup(request)
        body = {'configured': bool(app.state.store.setting('setup_complete', False)),
                'admin_exists': sessions.exists(), 'authenticated': bool(row and row['authenticated'])}
        response = JSONResponse({})
        token = request.cookies.get(COOKIE) if row else sessions.issue(request, response, authenticated=False)
        body['csrf_token'] = csrf_token(token)
        response.body = JSONResponse(body).body
        response.headers['content-length'] = str(len(response.body))
        return response

    @router.post('/setup/password')
    def create_password(payload: Password, request: Request):
        sessions = app.state.sessions
        if not local(request):
            return error('local_setup_only', '首次设置只允许从本机完成', 403)
        with sessions.login_lock:
            if sessions.exists():
                return error('already_initialized', '管理员密码已设置，请登录', 409)
            hashed = hash_password(payload.password.get_secret_value())
            with app.state.store.write() as conn:
                conn.execute('INSERT INTO admin_credentials VALUES(1,?,?)', (hashed, now()))
                audit(conn, 'setup_password')
            response = JSONResponse({'ok': True})
            sessions.issue(request, response, authenticated=True)
            return response

    @router.post('/login')
    def login(payload: Password, request: Request):
        sessions = app.state.sessions
        # Serial verification also prevents concurrent attempts bypassing the counter.
        with sessions.login_lock:
            current = time.time()
            with app.state.store.read() as conn:
                limit = conn.execute('SELECT * FROM admin_login_limits WHERE id=1').fetchone()
                credential = conn.execute('SELECT password_hash FROM admin_credentials WHERE id=1').fetchone()
            if limit and limit['blocked_until'] > current:
                response = error('rate_limited', '登录尝试过多，请一分钟后再试', 429)
                response.headers['Retry-After'] = str(max(1, int(limit['blocked_until']-current)))
                return response
            valid = credential and verify_password(payload.password.get_secret_value(), credential[0])
            with app.state.store.write() as conn:
                if not valid:
                    fresh = not limit or current-limit['window_start'] >= 300
                    count = 1 if fresh else limit['failures']+1
                    conn.execute('INSERT OR REPLACE INTO admin_login_limits VALUES(1,?,?,?)',
                                 (count, current if fresh else limit['window_start'], current+60 if count >= 5 else 0))
                    return error('invalid_password', '密码不正确', 401)
                conn.execute('DELETE FROM admin_login_limits')
                audit(conn, 'login')
            response = JSONResponse({'ok': True})
            sessions.issue(request, response, authenticated=True)
            return response

    @router.post('/logout')
    def logout(request: Request):
        with app.state.store.write() as conn:
            conn.execute('DELETE FROM admin_sessions WHERE token_hash=?', (digest(request.cookies.get(COOKIE, '')),))
            audit(conn, 'logout')
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, path='/', httponly=True, samesite='strict')
        return response

    app.include_router(router)
