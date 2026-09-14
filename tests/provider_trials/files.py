"""Protected append-only trial artifacts; never archive preparation credentials.

Exclusive creation and fsync retain review versions and interrupted evidence.
Only caller-selected nonsecret JSON belongs in these files.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat


def canonical(value: object) -> bytes:
    """Encode nonsecret JSON deterministically, rejecting non-finite values."""
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def write_new(path: Path, data: bytes) -> None:
    """Create a private immutable evidence version; never overwrite an old one."""
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    except BaseException:
        # A partially created file is retained as evidence, never replaced.
        raise


def read_json(path: Path, limit: int = 2097152) -> object:
    """Read bounded protected nonsecret data with duplicate-key rejection."""
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size>limit:
            raise ValueError('Protected bounded regular file required.')
        with os.fdopen(descriptor,'rb',closefd=False) as stream:raw=stream.read(limit+1)
    finally:os.close(descriptor)
    if len(raw)>limit:raise ValueError('Review artifact exceeds its bound.')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate review field.')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite review number.')))
