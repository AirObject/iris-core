CREATE TABLE admin_credentials (
    id INTEGER PRIMARY KEY CHECK (id=1),
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE admin_sessions (
    token_hash TEXT PRIMARY KEY,
    authenticated INTEGER NOT NULL CHECK (authenticated IN (0,1)),
    expires_at REAL NOT NULL
);
CREATE INDEX admin_sessions_expiry ON admin_sessions(expires_at);
CREATE TABLE admin_login_limits (
    id INTEGER PRIMARY KEY CHECK (id=1),
    failures INTEGER NOT NULL,
    window_start REAL NOT NULL,
    blocked_until REAL NOT NULL
);
CREATE TABLE admin_operations (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
