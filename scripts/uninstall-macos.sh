#!/bin/bash
set -euo pipefail

INSTALL_ROOT="${EDGEDISCO_HOME:-$HOME/.edgedisco}"
DOMAIN="gui/$(id -u)"
PURGE=false

if [[ "${1:-}" == "--purge" ]]; then
  PURGE=true
fi

launchctl bootout "$DOMAIN/com.edgedisco.agent" >/dev/null 2>&1 || true
launchctl bootout "$DOMAIN/com.edgedisco.server" >/dev/null 2>&1 || true

python3 - \
  "$HOME/Library/LaunchAgents/com.edgedisco.agent.plist" \
  "$HOME/Library/LaunchAgents/com.edgedisco.server.plist" \
  "$INSTALL_ROOT" \
  "$HOME" <<'PY'
from pathlib import Path
import sys

plist_agent, plist_server, install_root, home = map(Path, sys.argv[1:5])
for value in (plist_agent, plist_server):
    value.unlink(missing_ok=True)

managed = install_root / "bin" / "edgedisco"
state = install_root / "cli-launcher.path"
candidates = []
if state.exists():
    raw = state.read_text(errors="replace").strip()
    if raw:
        candidates.append(Path(raw))
candidates.extend([Path("/usr/local/bin/edgedisco"), home / ".local/bin/edgedisco"])

def is_ours(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    try:
        if path.is_symlink():
            return path.resolve() == managed.resolve()
    except OSError:
        return False
    try:
        return "EdgeDisco managed launcher" in path.read_text(errors="replace")
    except OSError:
        return False

seen = set()
for public in candidates:
    if public in seen:
        continue
    seen.add(public)
    if is_ours(public):
        public.unlink(missing_ok=True)
managed.unlink(missing_ok=True)
state.unlink(missing_ok=True)

profile = home / ".zprofile"
begin, end = "# >>> edgedisco PATH >>>", "# <<< edgedisco PATH <<<"
if profile.exists():
    text = profile.read_text(errors="replace")
    if begin in text and end in text:
        start = text.index(begin)
        stop = text.index(end) + len(end)
        if stop < len(text) and text[stop] == "\n":
            stop += 1
        if start >= 2 and text[start - 2:start] == "\n\n":
            start -= 1
        updated = text[:start] + text[stop:]
        if updated.strip():
            profile.write_text(updated)
        else:
            profile.unlink(missing_ok=True)
PY

if [[ "$PURGE" == true ]]; then
  read -r -p "Delete all EdgeDisco configuration, credentials, logs, and evidence? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    python3 - "$INSTALL_ROOT" <<'PY'
from pathlib import Path
import shutil, sys
target = Path(sys.argv[1]).resolve()
if target.name != ".edgedisco":
    raise SystemExit(f"Refusing to delete unexpected path: {target}")
shutil.rmtree(target)
PY
    echo "EdgeDisco and its local data were removed."
  else
    echo "Services removed; data retained at $INSTALL_ROOT"
  fi
else
  echo "EdgeDisco services were removed. Data remains at $INSTALL_ROOT"
  echo "Run with --purge to remove local data after an additional confirmation."
fi
