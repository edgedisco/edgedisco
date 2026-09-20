#!/bin/bash
set -euo pipefail

INSTALL_ROOT="${EDGEDISCO_HOME:-$HOME/.edgedisco}"

if [[ ! -x "$INSTALL_ROOT/venv/bin/edgedisco" ]]; then
  echo "EdgeDisco is not installed at $INSTALL_ROOT" >&2
  exit 1
fi

exec "$INSTALL_ROOT/venv/bin/edgedisco" status --root "$INSTALL_ROOT"
