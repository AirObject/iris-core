"""Exclusive bounded request-body and response evidence for controlled trials.

Only Provider transport can supply these facts. Headers and credential leases
are never accepted. Files are private, fsynced and never overwritten; they are
review artifacts, separate from native accounting and ordinary diagnostics.
"""
import json
import os
from pathlib import Path
import stat
import time


class WireEvidence:
    """A single exchange's caller-owned private evidence directory."""
    def __init__(self,directory: Path,*,multiple:bool=False):
        info=directory.stat()
        if not directory.is_absolute() or directory.is_symlink() or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise ValueError('Private owned evidence directory required.')
        if type(multiple) is not bool:raise ValueError("Explicit evidence layout required.")
        self.directory=directory;self._multiple=multiple;self._ordinal=0;self._current:WireEvidence|None=None

    def _write(self,name: str,value: bytes) -> None:
        descriptor=os.open(self.directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(value);stream.flush();os.fsync(stream.fileno())
        descriptor=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)

    def request(self,body: bytes) -> None:
        """Preserve exact nonsecret wire body before credential resolution."""
        if type(body) is not bytes or not 0<len(body)<=131072:raise ValueError('Invalid evidence body.')
        if self._multiple:
            if self._current is not None:raise ValueError('Previous evidence exchange has not ended.')
            self._ordinal+=1
            directory=self.directory/str(self._ordinal).zfill(2);directory.mkdir(mode=0o700)
            self._current=WireEvidence(directory);self._current.request(body);return
        self._write('request-body.json',body)
        self._write('request-prepared.json',json.dumps({'time_ns':time.time_ns(),'http_started':False}).encode())

    def response(self,state: str,status: int|None,body: bytes|None,reason: str|None) -> None:
        """Preserve bounded remote observations without claiming local commit."""
        if self._multiple:
            if self._current is None:raise ValueError('Original request evidence is missing.')
            self._current.response(state,status,body,reason);self._current=None;return
        if body is not None:
            if type(body) is not bytes or len(body)>262144:raise ValueError('Invalid evidence body.')
            self._write('response-body.json',body)
        self._write('wire-result.json',json.dumps({'state':state,'status':status,'reason':reason,'time_ns':time.time_ns()}).encode())
