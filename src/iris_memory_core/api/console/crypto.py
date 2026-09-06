"""Lazy Console cryptography and deployment-local authentication key material."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any


class ConsoleCrypto:
    def __init__(self, key: bytes) -> None:
        # Import only during explicit Console construction, never host startup.
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise RuntimeError(
                "Console requires the console extra: uv sync --extra console"
            ) from None
        if len(key) != 32:
            raise RuntimeError("invalid Console authentication key")
        self._key = key
        self._cipher: Any = AESGCM(self._derive("refresh-cache"))
        # Verify the backend at startup, before a listener accepts any request.
        probe = self.seal(b"console-startup", b"startup")
        if self.open_sealed(probe, b"startup") != b"console-startup":
            raise RuntimeError("Console encryption backend failed startup verification")

    def _derive(self, purpose: str) -> bytes:
        return hmac.digest(self._key, ("imc-console-v1:" + purpose).encode(), "sha256")

    def fingerprint(self, material: str) -> str:
        return hmac.new(
            self._derive("authentication-buckets"), material.encode(), hashlib.sha256
        ).hexdigest()

    def csrf(self, session_id: str, epoch: int) -> str:
        raw = hmac.digest(self._derive("csrf"), f"{session_id}:{epoch}".encode(), "sha256")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def sign_cursor(self, material: bytes) -> bytes:
        return hmac.digest(self._derive("cursor"), material, "sha256")

    def seal(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + bytes(self._cipher.encrypt(nonce, plaintext, aad))

    def open_sealed(self, ciphertext: bytes, aad: bytes) -> bytes:
        return bytes(self._cipher.decrypt(ciphertext[:12], ciphertext[12:], aad))


def load_authentication_key(path: Path) -> bytes:
    """Create once with an atomic, no-replace link; all workers share this key.

    Existing malformed or insecure files fail closed. Losing the file requires
    an explicit offline recovery, not an automatic replacement on every start.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and not path.is_symlink():
        descriptor, temporary = tempfile.mkstemp(prefix=".console-key-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(secrets.token_bytes(32))
                output.flush()
                os.fsync(output.fileno())
            with suppress(FileExistsError):
                os.link(temporary, path, follow_symlinks=False)
        finally:
            os.unlink(temporary)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
            key = source.read(33)
            if len(key) != 32:
                raise ValueError
            return key
    except (OSError, ValueError):
        raise RuntimeError("Console authentication key is unavailable or insecure") from None
