from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Asset:
    fingerprint: str
    kind: str
    name: str
    vendor: str
    running: bool
    version: str | None = None
    path_hash: str | None = None
    command_hash: str | None = None
    binary_sha256: str | None = None
    binary_fingerprint_status: str | None = None
    fingerprint_library_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
