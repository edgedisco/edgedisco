"""Resolve discovered paths without following links into unapproved locations."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator


def allowed_path(path: Path, roots: Iterable[Path]) -> Path | None:
    roots = tuple(Path(os.path.abspath(root)) for root in roots)
    candidate = Path(os.path.abspath(path))
    # Never resolve the allowlist itself: a symlink must not extend its scope.
    for _ in range(40):
        if not any(candidate == root or root in candidate.parents for root in roots):
            return None
        current = Path(candidate.anchor)
        try:
            for index, part in enumerate(candidate.parts[1:], 1):
                current /= part
                if stat.S_ISLNK(current.lstat().st_mode):
                    target = Path(os.readlink(current))
                    if not target.is_absolute():
                        target = current.parent / target
                    candidate = Path(os.path.abspath(target.joinpath(*candidate.parts[index + 1:])))
                    break
            else:
                return candidate
        except (OSError, ValueError):
            return None
    return None


@contextmanager
def open_regular_file(path: Path) -> Iterator[BinaryIO]:
    """Open a regular file without following a swapped final symlink or blocking on a FIFO."""
    parent_fd = None
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        if os.name == "posix":
            parent_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            for part in path.parts[1:-1]:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
                os.close(parent_fd)
                parent_fd = child
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        else:
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("not a regular file")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


def read_regular_file(path: Path, *, max_bytes: int) -> bytes | None:
    """Read at most max_bytes from a regular file, rejecting oversized or changing files."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None
        with open_regular_file(path) as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev, before.st_ino, before.st_size,
            ):
                return None
            data = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
        if len(data) > max_bytes or (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns
        ):
            return None
        return data
    except (OSError, RuntimeError):
        return None
