#!/bin/bash
set -euo pipefail

REPOSITORY="https://github.com/nsabharwal/edgedisco.git"
INSTALL_ROOT="${EDGEDISCO_HOME:-$HOME/.edgedisco}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ASSUME_YES=false
SETUP_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=true ;;
    --port)
      SETUP_ARGS+=("$1" "${2:?missing value for $1}")
      shift
      ;;
    --all-adapters|--no-open) SETUP_ARGS+=("$1") ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "The self-service installer currently supports macOS." >&2
  echo "Use the manual deployment guide for Windows or Linux." >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.9 or newer is required." >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9 or newer is required")
print(f"Python {sys.version_info.major}.{sys.version_info.minor} detected")
PY

if [[ "$ASSUME_YES" != true ]]; then
  cat <<'NOTICE'

EdgeDisco will:
  - install under ~/.edgedisco
  - inventory supported AI applications and local agent runtimes
  - install metadata-only hooks for detected Cursor, Claude Code, and Copilot apps
  - start local background services that survive reboot

It does not collect prompts, responses, source code, tool arguments, credentials,
screenshots, browser history, or raw command lines.
NOTICE
  read -r -p "Continue? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "Installation cancelled."; exit 0; }
fi

mkdir -p "$INSTALL_ROOT"

if [[ ! -x "$INSTALL_ROOT/venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$INSTALL_ROOT/venv"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [[ -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/src/ai_asset_inventory" ]]; then
  PACKAGE_SOURCE="$SCRIPT_DIR"
else
  PACKAGE_SOURCE="git+$REPOSITORY@main"
fi

echo "Installing EdgeDisco..."
"$INSTALL_ROOT/venv/bin/python" -m pip install --upgrade "$PACKAGE_SOURCE"

echo "Configuring local services..."
"$INSTALL_ROOT/venv/bin/edgedisco" setup --root "$INSTALL_ROOT" "${SETUP_ARGS[@]}"

cat <<EOF

Useful commands:
  $INSTALL_ROOT/venv/bin/edgedisco status

Configuration and logs:
  $INSTALL_ROOT
EOF
