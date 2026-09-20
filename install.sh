#!/bin/bash
set -Eeuo pipefail

INSTALL_ROOT="${EDGEDISCO_HOME:-$HOME/.edgedisco}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ARCHIVE_URL="${EDGEDISCO_ARCHIVE_URL:-https://codeload.github.com/nsabharwal/edgedisco/tar.gz/refs/heads/main}"
ASSUME_YES=false
SETUP_ARGS=()
TEMP_DIR=""

cleanup() {
  if [[ -n "$TEMP_DIR" ]]; then
    /bin/rm -rf "$TEMP_DIR"
  fi
}
failed() {
  local code=$?
  echo "EdgeDisco installation failed at line $1 (exit $code). Re-run this installer to repair the managed installation." >&2
  if [[ -f "$INSTALL_ROOT/cli-launcher.path" ]]; then
    echo "An existing edgedisco command may be stale until repair succeeds; check edgedisco --help." >&2
  fi
  exit "$code"
}
trap cleanup EXIT
trap 'failed $LINENO' ERR

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
  exit 1
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.9 or newer is required." >&2
  exit 1
fi

# An unrelated venv may be active. Use its base interpreter, then discard
# Python and pip environment overrides before creating our private venv.
BASE_PYTHON="$("$PYTHON_BIN" -c 'import sys; print(getattr(sys, "_base_executable", sys.executable))')"
if [[ ! -x "$BASE_PYTHON" ]]; then
  echo "Could not locate the base Python interpreter: $BASE_PYTHON" >&2
  exit 1
fi
unset VIRTUAL_ENV PYTHONHOME PYTHONPATH PYTHONUSERBASE PIP_TARGET PIP_PREFIX PIP_REQUIRE_VIRTUALENV
export PYTHONNOUSERSITE=1 PIP_CONFIG_FILE=/dev/null
"$BASE_PYTHON" - <<'PY'
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

# bash -c/stdin has no script path. A local checkout installs directly;
# otherwise fetch this repository's archive without requiring Git.
PACKAGE_SOURCE=""
SCRIPT_PATH="${BASH_SOURCE[0]-}"
if [[ -n "$SCRIPT_PATH" && -f "$SCRIPT_PATH" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
  if [[ -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/src/ai_asset_inventory" ]]; then
    PACKAGE_SOURCE="$SCRIPT_DIR"
  fi
fi
TEMP_DIR="$(/usr/bin/mktemp -d)"
if [[ -z "$PACKAGE_SOURCE" ]]; then
  echo "Downloading EdgeDisco source archive..."
  /usr/bin/curl -fsSL --retry 3 "$ARCHIVE_URL" -o "$TEMP_DIR/edgedisco.tar.gz"
  /usr/bin/tar -xzf "$TEMP_DIR/edgedisco.tar.gz" -C "$TEMP_DIR"
  PACKAGE_SOURCE="$TEMP_DIR/edgedisco-main"
  if [[ ! -f "$PACKAGE_SOURCE/pyproject.toml" || ! -d "$PACKAGE_SOURCE/src/ai_asset_inventory" ]]; then
    echo "The downloaded archive does not contain the EdgeDisco package." >&2
    exit 1
  fi
fi

/bin/mkdir -p "$INSTALL_ROOT"
VENV_PYTHON="$INSTALL_ROOT/venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  "$BASE_PYTHON" -m venv --without-pip "$INSTALL_ROOT/venv"
fi
"$VENV_PYTHON" - "$INSTALL_ROOT/venv" <<'PY'
from pathlib import Path
import sys
if Path(sys.prefix).resolve() != Path(sys.argv[1]).resolve() or sys.prefix == sys.base_prefix:
    raise SystemExit("Refusing to install outside the EdgeDisco managed venv")
PY

if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
  echo "Bootstrapping pip inside the EdgeDisco managed venv..."
  if ! "$VENV_PYTHON" -m ensurepip --upgrade; then
    /usr/bin/curl -fsSL --retry 3 https://bootstrap.pypa.io/get-pip.py -o "$TEMP_DIR/get-pip.py"
    "$VENV_PYTHON" "$TEMP_DIR/get-pip.py"
  fi
  "$VENV_PYTHON" -m pip --version >/dev/null
fi

echo "Installing EdgeDisco..."
"$VENV_PYTHON" -m pip install --upgrade "$PACKAGE_SOURCE"
CLI="$INSTALL_ROOT/venv/bin/edgedisco"
"$CLI" --help >/dev/null
for command in server agent hook adapters setup status uninstall mcp demo; do
  if ! "$CLI" "$command" --help >/dev/null; then
    echo "Installed EdgeDisco CLI is missing '$command'; setup was not started." >&2
    exit 1
  fi
done

echo "Configuring local services..."
"$CLI" setup --root "$INSTALL_ROOT" "${SETUP_ARGS[@]}"

PUBLIC="$(cat "$INSTALL_ROOT/cli-launcher.path")"
RESOLVED="$(/usr/bin/env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin /bin/zsh -lic 'command -v edgedisco' | /usr/bin/tail -n 1)"
if [[ "$RESOLVED" != "$PUBLIC" ]]; then
  echo "EdgeDisco installed, but a fresh interactive login shell resolves 'edgedisco' to '$RESOLVED' instead of '$PUBLIC'." >&2
  echo "Check command -v edgedisco and edgedisco --help before using the demo." >&2
  exit 1
fi
FRESH_HELP="$(/usr/bin/env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin /bin/zsh -lic 'edgedisco --help')"
if [[ "$FRESH_HELP" != *"demo"* ]]; then
  echo "A fresh login shell found an EdgeDisco command without the demo subcommand." >&2
  exit 1
fi
if [[ "$(command -v edgedisco || true)" != "$PUBLIC" ]]; then
  echo "Your current shell may still use an older edgedisco command. Open a new Terminal window." >&2
fi

cat <<EOF

EdgeDisco installation verified.
CLI: $PUBLIC
Open a new Terminal window, then run:
  edgedisco status
  edgedisco demo
  edgedisco uninstall

Configuration and logs:
  $INSTALL_ROOT
EOF
