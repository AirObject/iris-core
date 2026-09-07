"""Tenant-scoped Provider secrets; independent from Console authentication keys.

Only this adapter handles plaintext. Its return values are private ports;
Console views explicitly select the masked metadata from ``describe``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.provider_configs import ProviderSecret

MAX_SECRET_BYTES = 4096


def unavailable() -> ConflictError:
    return ConflictError("provider secret is unavailable", details={"kind": "secret_unavailable"})


def _reference(reference: str) -> None:
    if reference.startswith("env:") and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]{0,127}", reference[4:]
    ):
        return
    if reference.startswith("file:"):
        path = Path(reference[5:])
        if path.is_absolute() and ".." not in path.parts and len(reference) <= 2048:
            return
    raise unavailable()


def read_private_file(path: Path, *, maximum: int) -> bytes:
    """Open every path component without following symlinks, then bound read.

    Nonblocking open prevents an attacker replacing a file with a FIFO from
    hanging the process before fstat can reject it. Ownership/permissions and
    type are checked on the open descriptor, not a prior pathname stat.
    """
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise unavailable()
    directory: int | None = None
    descriptor: int | None = None
    try:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for component in path.parts[1:-1]:
            next_directory = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & ~0o600
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise unavailable()
        content = os.read(descriptor, maximum + 1)
        if len(content) > maximum:
            raise unavailable()
        return content
    except (OSError, ValueError):
        raise unavailable() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _plaintext(value: bytes) -> str:
    try:
        decoded = value.decode("ascii").strip()
    except UnicodeError:
        raise unavailable() from None
    if not 8 <= len(decoded) <= MAX_SECRET_BYTES or any(
        ord(character) < 33 or ord(character) > 126 for character in decoded
    ):
        raise unavailable()
    return decoded


def _aad(tenant_id: str, config_id: str, revision: int) -> bytes:
    if not tenant_id or not config_id or type(revision) is not int or revision < 1:
        raise unavailable()
    return json.dumps([tenant_id, config_id, revision], separators=(",", ":")).encode("utf-8")


class ProviderSecrets:
    def __init__(
        self,
        *,
        allowed_references: Mapping[str, frozenset[str]] | None = None,
        master_key_file: Path | None = None,
        reserved_files: frozenset[Path] = frozenset(),
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._allowed = {
            tenant: frozenset(refs) for tenant, refs in (allowed_references or {}).items()
        }
        owners: dict[str, str] = {}
        for tenant, refs in self._allowed.items():
            if not tenant:
                raise unavailable()
            for ref in refs:
                _reference(ref)
                if ref in owners and owners[ref] != tenant:
                    raise unavailable()
                owners[ref] = tenant
        self._environment = os.environ if environment is None else environment
        self._reserved = reserved_files | ({master_key_file} if master_key_file else set())
        self._master: bytes | None = None
        if master_key_file is not None:
            self._master = read_private_file(master_key_file, maximum=32)
            if len(self._master) != 32:
                raise unavailable()

    def _cipher(self) -> Any:
        if self._master is None:
            raise unavailable()
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise unavailable() from None
        return AESGCM(self._master)

    def _resolve_reference(self, tenant_id: str, reference: str) -> str:
        if reference not in self._allowed.get(tenant_id, frozenset()):
            raise unavailable()
        _reference(reference)
        if reference.startswith("env:"):
            raw = self._environment.get(reference[4:], "")
            if len(raw) > MAX_SECRET_BYTES:
                raise unavailable()
            try:
                return _plaintext(raw.encode("ascii"))
            except UnicodeError:
                raise unavailable() from None
        path = Path(reference[5:])
        if path in self._reserved:
            raise unavailable()
        return _plaintext(read_private_file(path, maximum=MAX_SECRET_BYTES))

    def reference(self, tenant_id: str, reference: str) -> ProviderSecret:
        # Syntax and tenant allowlist must be valid even while a deployment
        # secret is temporarily absent. No caller may probe an unregistered path.
        _reference(reference)
        if reference not in self._allowed.get(tenant_id, frozenset()):
            raise unavailable()
        try:
            value = self._resolve_reference(tenant_id, reference)
        except ConflictError:
            return ProviderSecret("secret_ref", reference=reference)
        return ProviderSecret(
            "secret_ref",
            reference=reference,
            digest_prefix=hashlib.sha256(value.encode()).hexdigest()[:8],
            hint=value[-4:],
        )

    def seal(self, tenant_id: str, config_id: str, revision: int, value: str) -> ProviderSecret:
        try:
            plaintext = _plaintext(value.encode("ascii"))
        except UnicodeError:
            raise unavailable() from None
        aad = _aad(tenant_id, config_id, revision)
        nonce = os.urandom(12)
        encrypted: bytes = self._cipher().encrypt(nonce, plaintext.encode(), aad)
        return ProviderSecret(
            "sealed",
            ciphertext=base64.b64encode(nonce + encrypted).decode("ascii"),
            digest_prefix=hashlib.sha256(plaintext.encode()).hexdigest()[:8],
            hint=plaintext[-4:],
        )

    def resolve(self, tenant_id: str, config_id: str, revision: int, secret: ProviderSecret) -> str:
        if (
            secret.mode == "secret_ref"
            and secret.reference is not None
            and secret.ciphertext is None
        ):
            return self._resolve_reference(tenant_id, secret.reference)
        if secret.mode != "sealed" or secret.reference is not None or secret.ciphertext is None:
            raise unavailable()
        try:
            if len(secret.ciphertext) > 6000:
                raise unavailable()
            encrypted = base64.b64decode(secret.ciphertext, validate=True)
            if len(encrypted) < 28:
                raise unavailable()
            plaintext: bytes = self._cipher().decrypt(
                encrypted[:12], encrypted[12:], _aad(tenant_id, config_id, revision)
            )
            value = _plaintext(plaintext)
        except (ValueError, binascii.Error):
            raise unavailable() from None
        except Exception:
            # InvalidTag and optional-library failures must not expose backend
            # exception detail or ciphertext. Domain failure is intentionally uniform.
            raise unavailable() from None
        if hashlib.sha256(value.encode()).hexdigest()[:8] != secret.digest_prefix:
            raise unavailable()
        return value

    def fingerprint(
        self, tenant_id: str, config_id: str, revision: int, secret: ProviderSecret
    ) -> str:
        """Private full digest binds successful probes to the resolved secret."""
        value = self.resolve(tenant_id, config_id, revision, secret)
        return hashlib.sha256(value.encode()).hexdigest()

    def describe(
        self, tenant_id: str, config_id: str, revision: int, secret: ProviderSecret
    ) -> dict[str, object]:
        try:
            value = self.resolve(tenant_id, config_id, revision, secret)
        except ConflictError:
            return {
                "secret_mode": secret.mode,
                "secret_hint": secret.hint,
                "secret_digest_prefix": secret.digest_prefix,
                "resolved": False,
            }
        return {
            "secret_mode": secret.mode,
            "secret_hint": value[-4:],
            "secret_digest_prefix": hashlib.sha256(value.encode()).hexdigest()[:8],
            "resolved": True,
        }

    def reseal(
        self,
        target: ProviderSecrets,
        tenant_id: str,
        config_id: str,
        revision: int,
        secret: ProviderSecret,
    ) -> ProviderSecret:
        """Offline rotation primitive; the caller commits all rows atomically."""
        if secret.mode != "sealed":
            return secret
        return target.seal(
            tenant_id, config_id, revision, self.resolve(tenant_id, config_id, revision, secret)
        )
