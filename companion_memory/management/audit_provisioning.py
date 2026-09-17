"""Offline local provisioning of finite developer audit authority.

The caller already holds the stopped instance lease and checked its retained
identity. Credentials are generated into private new files, never printed or
accepted in command-line arguments. Mount only the verifier grant read-only as
/run/secrets/developer_audit. Removing or atomically replacing that file revokes
existing reads; no HTTP endpoint can issue or broaden this authority.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import stat
import time
from companion_memory.runtime.managed_resources import ManagedResources
from .developer_audit import closed_object, decode_grant, GRANT_REFERENCE
from .identity import digest


def issue_audit_grant(resources: ManagedResources, scope_file: Path, output: Path, lifetime_seconds: int) -> dict[str, object]:
    """Create one explicitly bounded grant; reject existing output and unsafe files."""
    if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= resources.settings.integer('management.token_max_seconds'):
        raise ValueError('Invalid explicit audit lifetime.')
    if output.resolve().is_relative_to(resources.root.resolve()):
        raise ValueError('Audit credentials must be outside the backed-up data volume.')
    descriptor = os.open(scope_file, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('A private local scope file is required.')
        encoded = stream.read(4097)
    if len(encoded) > 4096:
        raise ValueError('Audit scope exceeds its fixed carrier.')
    scope = json.loads(encoded, object_pairs_hook=closed_object)
    if type(scope) is not dict or set(scope) != {'operations', 'histories'}:
        raise ValueError('Explicit operation and history allowlists are required.')
    credential = secrets.token_urlsafe(32)
    value = {'format': 'DEVELOPER_AUDIT_V1', 'grant_id': secrets.token_hex(16), 'revision': 1,
        'database_id': resources.database_id, 'instance_id': resources.instance_id,
        'verifier': digest(credential), 'expires_at_us': time.time_ns() // 1000 + lifetime_seconds * 1000000, **scope}
    body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    decode_grant(body)
    output.mkdir(mode=0o700)
    for name, content in ((GRANT_REFERENCE, body), ('developer_audit_credential', credential.encode())):
        descriptor = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(directory)
    finally: os.close(directory)
    return {'grant_file': str(output / GRANT_REFERENCE), 'credential_file': str(output / 'developer_audit_credential'),
        'expires_at_us': value['expires_at_us'], 'startup_sends': 0}
