from __future__ import annotations

import hashlib
import os
import json
import re
import stat
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from contextlib import contextmanager


CATALOG_RESOURCE = "fingerprints.json"
CATALOG_SCHEMA_VERSION = 1
MAX_BINARY_BYTES = 512 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILE_HASH_CACHE: dict[tuple[str, int, int, int, int, int], str] = {}


@dataclass(frozen=True)
class BinaryFingerprint:
    sha256: str
    version: str
    platform: str
    architecture: str
    source: str


@dataclass(frozen=True)
class AgentFingerprint:
    name: str
    vendor: str
    executables: tuple[str, ...]
    package_markers: tuple[str, ...]
    display_markers: tuple[str, ...]
    sources: tuple[str, ...]
    binary_fingerprints: tuple[BinaryFingerprint, ...]


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RuntimeError(f"invalid fingerprint library field: {field}")
    return tuple(value)


def _load_library() -> tuple[str, tuple[AgentFingerprint, ...]]:
    try:
        data = json.loads(files("ai_asset_inventory").joinpath(CATALOG_RESOURCE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot load the EdgeDisco fingerprint library") from exc
    if not isinstance(data, dict) or set(data) != {"schema_version", "updated", "hash_algorithm", "agents"}:
        raise RuntimeError("unsupported fingerprint library shape")
    if data["schema_version"] != CATALOG_SCHEMA_VERSION or data["hash_algorithm"] != "sha256":
        raise RuntimeError("unsupported fingerprint library version or hash algorithm")
    if not isinstance(data["updated"], str) or not data["updated"]:
        raise RuntimeError("invalid fingerprint library update identifier")
    if not isinstance(data["agents"], list):
        raise RuntimeError("fingerprint library agents must be a list")

    entries: list[AgentFingerprint] = []
    names: set[str] = set()
    for raw in data["agents"]:
        expected = {
            "name", "vendor", "executables", "package_markers", "display_markers",
            "sources", "binary_fingerprints",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise RuntimeError("unsupported agent fingerprint entry")
        name = raw["name"]
        vendor = raw["vendor"]
        if not isinstance(name, str) or not name or name in names or not isinstance(vendor, str) or not vendor:
            raise RuntimeError("invalid or duplicate agent fingerprint identity")
        names.add(name)
        binaries: list[BinaryFingerprint] = []
        if not isinstance(raw["binary_fingerprints"], list):
            raise RuntimeError("binary_fingerprints must be a list")
        for binary in raw["binary_fingerprints"]:
            fields = {"sha256", "version", "platform", "architecture", "source"}
            if not isinstance(binary, dict) or set(binary) != fields:
                raise RuntimeError("unsupported binary fingerprint entry")
            if not isinstance(binary["sha256"], str) or not _SHA256.fullmatch(binary["sha256"]):
                raise RuntimeError("invalid binary SHA-256 fingerprint")
            if not all(isinstance(binary[key], str) and binary[key] for key in fields - {"sha256"}):
                raise RuntimeError("invalid binary fingerprint metadata")
            binaries.append(BinaryFingerprint(**binary))
        entries.append(AgentFingerprint(
            name=name,
            vendor=vendor,
            executables=_strings(raw["executables"], "executables"),
            package_markers=_strings(raw["package_markers"], "package_markers"),
            display_markers=_strings(raw["display_markers"], "display_markers"),
            sources=_strings(raw["sources"], "sources"),
            binary_fingerprints=tuple(binaries),
        ))
    return data["updated"], tuple(entries)


LIBRARY_VERSION, AGENT_FINGERPRINTS = _load_library()


def known_binary(name: str, sha256: str) -> BinaryFingerprint | None:
    for entry in AGENT_FINGERPRINTS:
        if entry.name == name:
            return next((item for item in entry.binary_fingerprints if item.sha256 == sha256), None)
    return None


@contextmanager
def _open_regular(path: Path):
    """Open without following swapped symlinks or blocking on a substituted FIFO."""
    parent_fd = None
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        if os.name == "posix":
            parent_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            for part in path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        else:
            descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0))
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


def sha256_file(path: Path, *, max_bytes: int = MAX_BINARY_BYTES) -> str | None:
    """Hash one regular file, rejecting oversized, changing, or unreadable files."""
    try:
        resolved = Path(os.path.abspath(path))
        before = resolved.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            return None
        key = (
            str(resolved), before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        cached = _FILE_HASH_CACHE.get(key)
        if cached:
            return cached
        hasher = hashlib.sha256()
        with _open_regular(resolved) as handle:
            opened = os_fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
                return None
            remaining = max_bytes
            while True:
                chunk = handle.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                remaining -= len(chunk)
                if remaining < 0:
                    return None
                hasher.update(chunk)
            after = os_fstat(handle.fileno())
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns,
        ):
            return None
        result = hasher.hexdigest()
        if len(_FILE_HASH_CACHE) >= 512:
            _FILE_HASH_CACHE.clear()
        _FILE_HASH_CACHE[key] = result
        return result
    except (OSError, RuntimeError):
        return None


def os_fstat(file_descriptor: int):
    # Kept as a tiny seam for deterministic TOCTOU tests.
    import os
    return os.fstat(file_descriptor)
