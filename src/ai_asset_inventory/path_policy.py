"""Resolve discovered paths without following links into unapproved locations."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterable


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
