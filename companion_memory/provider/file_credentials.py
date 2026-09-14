"""Protected file references resolved only by the native Provider credential port.

The trusted host binds one account/reference/revision to an existing private
regular file. Reads are bounded, reject links and unsafe permissions, and never
include bytes, paths or underlying exceptions in returned errors. Local recovery
can bind an unavailable resolver without opening the credential file.
"""
from __future__ import annotations
import os
from pathlib import Path
import stat
import threading
from .credentials import Available,CredentialLease,CredentialResolver,CredentialUnavailable,CredentialResult
from .values import is_identifier


class InheritedFileCredential:
    """Open a protected reference before a trusted launcher permanently drops UID.

    Construction reads metadata only. The native resolver alone reads bytes from
    the retained descriptor after privilege drop. The launcher owns this binding
    and closes it after all Provider consumers have actually ended.
    """
    def __init__(self,path: Path,*,secret_ref: str,secret_revision: str,account_ref: str,recipient_uid: int|None=None):
        if not path.is_absolute() or not all(is_identifier(v) for v in (secret_ref,secret_revision,account_ref)):
            raise ValueError('Protected reference binding required.')
        self._lock=threading.Lock();self._descriptor:int|None=None;self._owner=os.geteuid()
        if recipient_uid is not None and (type(recipient_uid) is not int or not 0<recipient_uid<2**32):
            raise ValueError('A fixed nonroot recipient UID is required.')
        self._recipient=self._owner if recipient_uid is None else recipient_uid
        self._identity:tuple[int,int]|None=None
        directory=None
        try:
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            info=os.fstat(directory)
            if info.st_uid!=self._owner or stat.S_IMODE(info.st_mode)!=0o700:raise ValueError()
            self._descriptor=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            info=os.fstat(self._descriptor)
            if info.st_uid!=self._owner:raise ValueError()
            self._identity=(info.st_dev,info.st_ino)
            self._validate()
        except (OSError,ValueError):
            self.close()
            raise ValueError('Protected inherited credential unavailable.') from None
        finally:
            if directory is not None:os.close(directory)
        def resolve(secret: str,revision: str,account: str) -> CredentialResult:
            if (secret,revision,account)!=(secret_ref,secret_revision,account_ref):return CredentialUnavailable('UNAVAILABLE')
            with self._lock:
                try:
                    self._validate();assert self._descriptor is not None
                    value=os.pread(self._descriptor,4098,0)
                    if value.endswith(b'\n'):value=value[:-1]
                    return Available(CredentialLease(value))
                except (OSError,ValueError):return CredentialUnavailable('FAILED')
        self.resolver=CredentialResolver(resolve)

    def _validate(self) -> None:
        if self._descriptor is None:raise ValueError()
        info=os.fstat(self._descriptor)
        # Desktop bind mounts may project the same inode's owner as the
        # recipient UID in worker threads after setuid. Accept only that fixed
        # recipient, with the retained device/inode and private mode unchanged.
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (self._owner,self._recipient)
                or os.geteuid() not in (self._owner,self._recipient) or (info.st_dev,info.st_ino)!=self._identity or info.st_nlink!=1
                or stat.S_IMODE(info.st_mode) not in (0o400,0o600) or not 1<=info.st_size<=4097):raise ValueError()

    def close(self) -> None:
        """Revoke future resolutions; already issued leases keep native ownership."""
        with self._lock:
            if self._descriptor is not None:os.close(self._descriptor);self._descriptor=None


def protected_file_resolver(path: Path, *, secret_ref: str, secret_revision: str, account_ref: str) -> CredentialResolver:
    """Bind safe IDs without reading the file; only Provider's resolve reads it.

    The containing directory must be owned by the current user and mode 0700;
    the regular file must be mode 0600 or 0400, singly linked, and owned by that
    user. One trailing newline is accepted as file formatting. Each successful
    lookup lends a fresh native lease whose actual consumer owns its release.
    """
    if type(path) is not Path and not isinstance(path,Path):
        raise TypeError('A protected file path is required.')
    if not path.is_absolute() or not all(is_identifier(v) for v in (secret_ref,secret_revision,account_ref)):
        raise ValueError('An absolute protected reference binding is required.')
    def resolve(secret: str,revision: str,account: str) -> CredentialResult:
        if (secret,revision,account)!=(secret_ref,secret_revision,account_ref):
            return CredentialUnavailable('UNAVAILABLE')
        directory=None;descriptor=None
        try:
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            info=os.fstat(directory)
            if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
                return CredentialUnavailable('FAILED')
            descriptor=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            info=os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1
                    or stat.S_IMODE(info.st_mode) not in (0o400,0o600) or not 1<=info.st_size<=4097):
                return CredentialUnavailable('FAILED')
            value=os.read(descriptor,4098)
            if value.endswith(b'\n'):value=value[:-1]
            return Available(CredentialLease(value))
        except (OSError,ValueError):
            return CredentialUnavailable('FAILED')
        finally:
            if descriptor is not None:os.close(descriptor)
            if directory is not None:os.close(directory)
    return CredentialResolver(resolve)
