"""OS-owned leases; automatically released by a killed process."""
import errno
import os
from pathlib import Path
from time import monotonic, sleep


_LEASE_BUSY = "数据库正由服务或离线命令使用；请先停止服务再执行 iris learn 等离线写入命令。"


class LeaseBusyError(ValueError):
    """The lease remained occupied until its acquisition deadline."""


class StoreLease:
    def __init__(self, database, *, timeout=0.0):
        database = Path(database).resolve()
        self.path = database.with_name(database.name + ".owner.lock")
        self.file = None
        self.timeout = timeout

    def __enter__(self):
        deadline = monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        self.file.seek(0, 2)
                        if self.file.tell() == 0:
                            self.file.write(b"\0")
                            self.file.flush()
                        self.file.seek(0)
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise ValueError(_LEASE_BUSY) from error
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise LeaseBusyError(_LEASE_BUSY) from error
                    sleep(min(.05, remaining))
        except BaseException:
            self.file.close()
            self.file = None
            raise
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
            self.file = None
