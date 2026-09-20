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

python3 - "$HOME/Library/LaunchAgents/com.edgedisco.agent.plist" "$HOME/Library/LaunchAgents/com.edgedisco.server.plist" <<'PY'
from pathlib import Path
import sys
for value in sys.argv[1:]:
    Path(value).unlink(missing_ok=True)
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
