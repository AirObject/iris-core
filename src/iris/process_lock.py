"""OS-owned database lease; automatically released by a killed process."""
import os
from pathlib import Path


class StoreLease:
    def __init__(self, database):
        database = Path(database).resolve()
        self.path = database.with_name(database.name + ".owner.lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
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
        except OSError as error:
            self.file.close()
            self.file = None
            raise ValueError("数据库正由服务或离线命令使用；请先停止服务再执行 iris learn 等离线写入命令。") from error
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
            self.file = None
