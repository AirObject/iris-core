"""Trusted resource injection and real borrowed console/file adapters.

Assembly supplies binary host streams, bounded thread-safe clocks/UUID source,
and the same complete nonsecret protected directory layout used for configuration
validation. Resource construction performs no I/O. All prepare/write/flush/probe/
close methods are synchronous and must be scheduled by the service's bounded
owners. Host streams are never closed. File ownership lasts until descriptor
cleanup finishes, including after the caller's close deadline.
"""

from collections.abc import Callable
from dataclasses import dataclass
import errno
import fcntl
import os
import re
import stat
from typing import Literal, Protocol

from ._file_records import _check_jsonl


class BinaryOutput(Protocol):
    """Borrowed binary stream; writes report an exact byte count and may block."""

    def write(self, data: bytes, /) -> int: ...
    def flush(self) -> None: ...
    def writable(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class LoggingResources:
    """Explicit resource capabilities, with no configuration overrides or defaults.

    protected_directories is an immutable tuple of (category, tuple of paths),
    containing media, database, audit, provider_usage and backup exactly once.
    All paths must describe the same assembly as the checked snapshot. Streams
    use binary UTF-8 bytes; pass a host text stream's binary buffer when suitable.
    Clock callbacks must be bounded and thread-safe; monotonic_clock returns ns.
    """

    stdout: BinaryOutput | None
    stderr: BinaryOutput
    protected_directories: tuple[tuple[str, tuple[str, ...]], ...]
    id_source: Callable[[], object]
    utc_clock: Callable[[], object]
    monotonic_clock: Callable[[], int]


class _ResourceFailure(Exception):
    """Only a fixed reason crosses the resource execution boundary."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _Resource(Protocol):
    """One exclusively scheduled output, with idempotent owned-resource cleanup."""

    def prepare(self) -> None: ...
    def write(self, data: bytes, stream: Literal["stdout", "stderr"] | None) -> int: ...
    def flush(self) -> None: ...
    def probe(self) -> None: ...
    def close(self) -> None: ...


class _ConsoleResource:
    """Route complete byte records; probe checks/flushes without replay or notices."""

    def __init__(self, resources: LoggingResources, split: bool):
        self.stderr = resources.stderr
        self.stdout = resources.stdout if split else None

    def prepare(self) -> None:
        for output in (self.stderr, self.stdout):
            if output is not None:
                if not all(callable(getattr(output, name, None)) for name in ("write", "flush", "writable")):
                    raise _ResourceFailure("RESOURCE_INVALID")
                try:
                    writable = output.writable()
                except Exception:
                    raise _ResourceFailure("RESOURCE_INVALID") from None
                if writable is not True:
                    raise _ResourceFailure("RESOURCE_INVALID")

    def write(self, data: bytes, stream: Literal["stdout", "stderr"] | None) -> int:
        output = self.stdout if stream == "stdout" else self.stderr
        if output is None:
            raise _ResourceFailure("WRITE_FAILED")
        return output.write(data)

    def flush(self) -> None:
        self.stderr.flush()
        if self.stdout is not None and self.stdout is not self.stderr:
            self.stdout.flush()

    def probe(self) -> None:
        self.prepare()
        self.flush()

    def close(self) -> None:
        """Borrowed streams retain host ownership even after failed preparation."""


_CATEGORIES = frozenset(("media", "database", "audit", "provider_usage", "backup"))
_SEGMENT = re.compile(r"runtime\.([1-9][0-9]*)\.jsonl\Z")


def _resource_shape(resources: object, split: bool) -> bool:
    if type(resources) is not LoggingResources:
        return False
    if not all(callable(source) for source in
               (resources.id_source, resources.utc_clock, resources.monotonic_clock)):
        return False
    if resources.stderr is None or (split and resources.stdout is None):
        return False
    directories = resources.protected_directories
    if type(directories) is not tuple or len(directories) != len(_CATEGORIES):
        return False
    seen: set[str] = set()
    for entry in directories:
        if type(entry) is not tuple or len(entry) != 2:
            return False
        key, paths = entry
        if type(key) is not str or key not in _CATEGORIES or key in seen:
            return False
        seen.add(key)
        if type(paths) is not tuple or not paths or any(not _canonical(path) for path in paths):
            return False
    return True


def _canonical(path: object) -> bool:
    return (type(path) is str and 0 < len(path) <= 4096 and path.startswith("/")
            and not any(ord(char) < 32 or ord(char) == 127 for char in path)
            and (path == "/" or all(part not in ("", ".", "..") for part in path[1:].split("/"))))


def _directory_with_ancestors(path: str) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Open components without symlinks and retain bounded ancestor identities."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    ancestors: list[tuple[int, int]] = []
    try:
        metadata = os.fstat(descriptor)
        ancestors.append((metadata.st_dev, metadata.st_ino))
        for part in path[1:].split("/") if path != "/" else ():
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
            metadata = os.fstat(descriptor)
            ancestors.append((metadata.st_dev, metadata.st_ino))
        return descriptor, tuple(ancestors)
    except BaseException:
        os.close(descriptor)
        raise


def _directory(path: str) -> int:
    """Return the safely opened final directory; the caller owns its descriptor."""
    descriptor, _ = _directory_with_ancestors(path)
    return descriptor


class _FileResource:
    """Exclusive directory-relative JSONL writer with conservative recovery.

    Valid existing segments are consecutive, within the current size/count
    limits, and contain complete runtime JSON objects. Recovery only observes;
    ambiguous rotation/retention states never trigger automatic repair. Directory
    flock protects cooperating owners across processes and service instances.
    Trusted assembly must exclude noncooperating writers and directory replacement.
    """

    def __init__(self, directory: str, protected: tuple[tuple[str, tuple[str, ...]], ...],
                 rotation_bytes: int, retained_segments: int, event_max_bytes: int):
        self.directory = directory
        self.protected = protected
        self.limit = rotation_bytes
        self.retained = retained_segments
        self.event_limit = event_max_bytes
        self.directory_fd: int | None = None
        self.active_fd: int | None = None
        self.size = 0
        self.next_segment = 1
        self.segments: tuple[int, ...] = ()
        self.unconfirmed = False
        self.disk_space: Literal["OK", "LOW", "UNKNOWN"] = "UNKNOWN"

    def prepare(self) -> None:
        try:
            if not _canonical(self.directory) or self.directory == "/":
                raise _ResourceFailure("RESOURCE_INVALID")
            self.directory_fd = _directory(self.directory)
            try:
                fcntl.flock(self.directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _ResourceFailure("RESOURCE_CONFLICT") from None
            self._check_layout()
            if not os.access(".", os.W_OK, dir_fd=self.directory_fd):
                raise _ResourceFailure("RESOURCE_INVALID")
            self._inspect(initial=True)
            self.active_fd = self._open("runtime.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        except _ResourceFailure:
            raise
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                raise _ResourceFailure("RESOURCE_INVALID") from None
            raise _ResourceFailure("RESOURCE_OPEN_FAILED") from None

    def _check_layout(self) -> None:
        assert self.directory_fd is not None
        current, logging_ancestors = _directory_with_ancestors(self.directory)
        try:
            owned = os.fstat(self.directory_fd)
            if (owned.st_dev, owned.st_ino) != (os.fstat(current).st_dev, os.fstat(current).st_ino):
                raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
        finally:
            os.close(current)
        for _, paths in self.protected:
            for path in paths:
                if (path == self.directory or self.directory.startswith(path.rstrip("/") + "/")
                        or path.startswith(self.directory + "/")):
                    raise _ResourceFailure("RESOURCE_INVALID")
                protected, protected_ancestors = _directory_with_ancestors(path)
                try:
                    other = os.fstat(protected)
                    if ((owned.st_dev, owned.st_ino) in protected_ancestors
                            or (other.st_dev, other.st_ino) in logging_ancestors):
                        raise _ResourceFailure("RESOURCE_INVALID")
                finally:
                    os.close(protected)

    def _stat(self, name: str) -> os.stat_result:
        assert self.directory_fd is not None
        result = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(result.st_mode) or result.st_nlink != 1:
            raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
        return result

    def _open(self, name: str, flags: int) -> int:
        assert self.directory_fd is not None
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.directory_fd)
        try:
            opened = os.fstat(descriptor)
            actual = self._stat(name)
            if (opened.st_dev, opened.st_ino) != (actual.st_dev, actual.st_ino):
                raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _inspect(self, *, initial: bool) -> None:
        assert self.directory_fd is not None
        segments: list[int] = []
        active = False
        try:
            # Stop on the first excess entry, without materializing an arbitrary
            # directory listing supplied by another owner.
            with os.scandir(self.directory_fd) as entries:
                for entry in entries:
                    if entry.name == "runtime.jsonl":
                        active = True
                    else:
                        match = _SEGMENT.fullmatch(entry.name)
                        if match is None or len(segments) >= self.retained:
                            raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
                        segments.append(int(match[1]))
                    size = self._stat(entry.name).st_size
                    if size > self.limit:
                        raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
                    self._check_records(entry.name)
            segments.sort()
            if any(right != left + 1 for left, right in zip(segments, segments[1:])):
                raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
            if segments and not active:
                raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
            following = segments[-1] + 1 if segments else 1
            if not initial and (tuple(segments) != self.segments or following != self.next_segment
                                or not active or self.unconfirmed):
                raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
            self.segments = tuple(segments)
            self.next_segment = following
            self.size = self._stat("runtime.jsonl").st_size if active else 0
        except (OSError, ValueError, _ResourceFailure):
            raise _ResourceFailure("RESOURCE_INVALID" if initial else "FILE_STATE_UNCONFIRMED") from None

    def _check_records(self, name: str) -> None:
        descriptor = self._open(name, os.O_RDONLY)
        try:
            _check_jsonl(descriptor, self.event_limit)
        finally:
            os.close(descriptor)

    def _check_names(self) -> None:
        assert self.directory_fd is not None
        segments: list[int] = []
        active = False
        with os.scandir(self.directory_fd) as entries:
            for entry in entries:
                if entry.name == "runtime.jsonl":
                    active = True
                else:
                    match = _SEGMENT.fullmatch(entry.name)
                    if match is None or len(segments) >= self.retained:
                        raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
                    segments.append(int(match[1]))
                if self._stat(entry.name).st_size > self.limit:
                    raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
        if not active or tuple(sorted(segments)) != self.segments:
            raise _ResourceFailure("FILE_STATE_UNCONFIRMED")

    def _check_active(self) -> None:
        self._check_layout()
        self._check_names()
        assert self.active_fd is not None
        opened, actual = os.fstat(self.active_fd), self._stat("runtime.jsonl")
        if (opened.st_dev, opened.st_ino, opened.st_size) != (actual.st_dev, actual.st_ino, self.size):
            raise _ResourceFailure("FILE_STATE_UNCONFIRMED")

    def write(self, data: bytes, stream: Literal["stdout", "stderr"] | None) -> int:
        try:
            self._check_active()
            if self.size and self.size + len(data) > self.limit:
                self._rotate()
            assert self.active_fd is not None
            count = os.write(self.active_fd, data)
            self.size += count
            return count
        except OSError as error:
            if error.errno == errno.ENOSPC:
                self.disk_space = "LOW"
            raise

    def _rotate(self) -> None:
        assert self.directory_fd is not None and self.active_fd is not None
        self.unconfirmed = True
        destination = "runtime." + str(self.next_segment) + ".jsonl"
        try:
            self._stat("runtime.jsonl")
            # link is exclusive at the destination; it never overwrites an
            # unexpected file. Any interrupted intermediate state stays faulted.
            os.link("runtime.jsonl", destination, src_dir_fd=self.directory_fd,
                    dst_dir_fd=self.directory_fd, follow_symlinks=False)
            os.unlink("runtime.jsonl", dir_fd=self.directory_fd)
            os.close(self.active_fd)
            self.active_fd = None
            self.active_fd = self._open("runtime.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL)
            segments = (*self.segments, self.next_segment)
            self.next_segment += 1
            self.size = 0
        except (OSError, _ResourceFailure):
            raise _ResourceFailure("ROTATION_FAILED") from None
        try:
            while len(segments) > self.retained:
                name = "runtime." + str(segments[0]) + ".jsonl"
                self._stat(name)
                os.unlink(name, dir_fd=self.directory_fd)
                segments = segments[1:]
        except (OSError, _ResourceFailure):
            raise _ResourceFailure("RETENTION_FAILED") from None
        self.segments = segments
        self.unconfirmed = False

    def flush(self) -> None:
        # os.write uses an unbuffered descriptor. Checking the active identity
        # confirms this adapter's empty userspace buffer, without claiming fsync.
        self._check_active()

    def probe(self) -> None:
        try:
            self._check_layout()
            self._inspect(initial=False)
            if self.active_fd is None:
                raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
            opened, actual = os.fstat(self.active_fd), self._stat("runtime.jsonl")
            if (opened.st_dev, opened.st_ino) != (actual.st_dev, actual.st_ino):
                raise _ResourceFailure("FILE_STATE_UNCONFIRMED")
            # Open a second descriptor only within this same serial operation,
            # after consistency passes; no event or probe bytes are written.
            probe = self._open("runtime.jsonl", os.O_WRONLY | os.O_APPEND)
            os.close(probe)
            self.disk_space = "UNKNOWN"
        except (OSError, _ResourceFailure):
            raise _ResourceFailure("FILE_STATE_UNCONFIRMED") from None

    def close(self) -> None:
        if self.active_fd is not None:
            os.close(self.active_fd)
            self.active_fd = None
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None
