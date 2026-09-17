"""Descriptor-relative file access stays on exclusively owned physical directories.

Path identity checks detect replacement; dir_fd operations remain on the held
inode even if a rename races after that check. Descriptors outlive all workers.
"""
from __future__ import annotations
import os
import stat
from collections.abc import Callable
from pathlib import Path
from companion_memory.persistence.owned_statements import OwnerFailure


class PhysicalDirectories:
    """Own finite directory descriptors, never follow replacement path components."""
    def __init__(self, root: Path, root_fd: int, directories: tuple[Path, ...], owner_fd: int, invalidated: Callable[[], None], *, siblings: tuple[Path, ...] = ()):
        self.invalidated = invalidated
        self.root = root; self.root_fd = root_fd; self.owner_fd = owner_fd; self.fds: dict[Path, int] = {}
        self.siblings = siblings
        if any(path.parent != root.parent or path == root or path not in directories for path in siblings):
            self.fail()
        try:
            for path in directories:
                fd = self.open_sibling(path) if path in siblings else self.open_child(root_fd, path.relative_to(root))
                self.fds[path] = fd
                if os.fstat(fd).st_dev != os.fstat(root_fd).st_dev: self.fail()
            self.validate()
        except BaseException:
            self.close(); raise

    @staticmethod
    def open_sibling(path: Path) -> int:
        """Traverse an explicit sibling from the filesystem root without aliases."""
        root = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = PhysicalDirectories.open_child(root, path.relative_to('/'))
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                os.close(fd)
                PhysicalDirectories.fail()
            return fd
        finally:
            os.close(root)

    @staticmethod
    def open_child(root_fd: int, relative: Path, create: bool = False) -> int:
        """Traverse with at most two temporary descriptors, rejecting every symlink."""
        if relative.is_absolute() or not relative.parts or any(p in ('.', '..') for p in relative.parts):
            PhysicalDirectories.fail()
        current = os.dup(root_fd)
        try:
            for part in relative.parts:
                if create:
                    try: os.mkdir(part, 0o700, dir_fd=current)
                    except FileExistsError: pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current); current = child
            return current
        except BaseException:
            os.close(current); raise

    @staticmethod
    def fail():
        raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH')

    def validate(self) -> None:
        try:
            root = self.root.lstat(); held = os.fstat(self.root_fd)
            if not stat.S_ISDIR(root.st_mode) or (root.st_dev, root.st_ino) != (held.st_dev, held.st_ino): self.fail()
            owner = os.stat('.owner', dir_fd=self.root_fd, follow_symlinks=False); expected_owner = os.fstat(self.owner_fd)
            if not stat.S_ISREG(owner.st_mode) or (owner.st_dev, owner.st_ino) != (expected_owner.st_dev, expected_owner.st_ino): self.fail()
            for path, fd in self.fds.items():
                current_fd = self.open_sibling(path) if path in self.siblings else self.open_child(self.root_fd, path.relative_to(self.root))
                try: current = os.fstat(current_fd)
                finally: os.close(current_fd)
                expected = os.fstat(fd)
                if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino): self.fail()
        except (OSError, OwnerFailure) as error:
            self.invalidated()
            raise OwnerFailure('FILE_FAILED', 'media', 'RESOURCE_IDENTITY_MISMATCH') from error

    def directory(self, path: Path) -> int:
        self.validate()
        if path not in self.fds: self.fail()
        return self.fds[path]

    def open(self, path: Path, flags: int, mode: int = 0o600) -> int:
        return os.open(path.name, flags | os.O_NOFOLLOW, mode, dir_fd=self.directory(path.parent))

    def stat(self, path: Path):
        return os.stat(path.name, dir_fd=self.directory(path.parent), follow_symlinks=False)

    def unlink(self, path: Path) -> None:
        os.unlink(path.name, dir_fd=self.directory(path.parent))

    def link(self, source: Path, target: Path) -> None:
        source_fd = self.directory(source.parent); target_fd = self.directory(target.parent)
        os.link(source.name, target.name, src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False)

    def close(self) -> None:
        for fd in self.fds.values(): os.close(fd)
        self.fds.clear()
