"""One-time operator provisioning of an empty secrets volume.

Run interactively as root with only the new secrets volume mounted writable.
The application itself always runs as uid 10001 with this volume read-only.
Credential input is hidden and is never printed or accepted in arguments.
"""
import getpass
import os
from pathlib import Path
import stat


def main() -> None:
    root = Path('/run/secrets')
    if os.geteuid() != 0 or root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise SystemExit('仅允许首次初始化空秘密卷；已有内容保持不变。')
    if not os.isatty(0):
        raise SystemExit('请使用交互终端输入引导凭据，禁止命令行或日志传递秘密。')
    supplied = getpass.getpass('设置一次性引导凭据（32–4096 UTF-8 字节；请先保存在密码管理器）：')
    repeated = getpass.getpass('再次输入：')
    raw = supplied.encode()
    if supplied != repeated or not 32 <= len(raw) <= 4096 or '\n' in supplied:
        raise SystemExit('输入不一致或长度不符合要求；未创建凭据。')
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit('秘密卷类型不符。')
    os.chown(root, 10001, 10001)
    os.chmod(root, 0o700)
    fd = os.open(root / 'bootstrap', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchown(fd, 10001, 10001)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    print('引导凭据已写入受保护秘密卷；启动服务时必须只读挂载。')


if __name__ == '__main__':
    main()
