#!/bin/bash
set -Eeuo pipefail

INSTALL_ROOT="${EDGEDISCO_HOME:-$HOME/.edgedisco}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ARCHIVE_URL="${EDGEDISCO_ARCHIVE_URL:-https://codeload.github.com/edgedisco/edgedisco/tar.gz/refs/heads/main}"
ASSUME_YES=false
SETUP_ARGS=(--root "$INSTALL_ROOT")
TEMP_DIR=""
INSTALL_COMPLETE=false
UPGRADE_BACKUP=""
UPGRADE_STARTED=false

usage() {
  cat <<'EOF'
Usage: bash install.sh [options]

Install or upgrade the per-user EdgeDisco service on macOS or systemd Linux.
When run from a source checkout, installs that checkout. When run through
stdin or outside a checkout, downloads the configured source archive.

Options:
  -h, --help          Show this help and exit without making changes.
  --yes               Skip the interactive installation confirmation.
  --port PORT         Bind the local server to PORT instead of 8080.
  --all-adapters      Install all supported app hooks, detected or not.
  --no-open           Do not open a browser during setup.

Environment overrides:
  EDGEDISCO_HOME         Installation root (default: ~/.edgedisco).
  PYTHON_BIN             Python 3.9+ interpreter command (default: python3).
  EDGEDISCO_ARCHIVE_URL  Source archive used by remote/stdin installation.

Linux support requires systemd, systemctl, and an active systemd user session.
Containers, WSL without systemd, and non-systemd desktops use manual deployment.

Examples:
  bash install.sh
  bash install.sh --yes --no-open
  bash install.sh --port 8090 --all-adapters
  EDGEDISCO_HOME="$HOME/.edgedisco-test" bash install.sh --yes --no-open
EOF
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ "$INSTALL_COMPLETE" != true && "$UPGRADE_STARTED" == true ]]; then
    echo "Restoring the previous EdgeDisco installation..." >&2
    if PYTHONPATH="$PACKAGE_SOURCE/src" "$BASE_PYTHON" -m ai_asset_inventory.upgrade restore --backup "$UPGRADE_BACKUP"; then
      echo "Previous installation restored. Backup and failed-attempt evidence: $UPGRADE_BACKUP" >&2
    else
      echo "Automatic rollback could not finish. Recovery backup: $UPGRADE_BACKUP" >&2
    fi
    status=1
  fi
  if [[ -n "$TEMP_DIR" ]]; then
    /bin/rm -rf "$TEMP_DIR" || status=1
  fi
  if [[ "$INSTALL_COMPLETE" != true && "$status" -eq 0 ]]; then
    status=1
  fi
  exit "$status"
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
    -h|--help)
      usage
      INSTALL_COMPLETE=true
      exit 0
      ;;
    --yes) ASSUME_YES=true ;;
    --port)
      SETUP_ARGS+=("$1" "${2:?missing value for $1}")
      shift
      ;;
    --all-adapters|--no-open) SETUP_ARGS+=("$1") ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

SYSTEM_NAME="$(uname -s)"
if [[ "$SYSTEM_NAME" != "Darwin" && "$SYSTEM_NAME" != "Linux" ]]; then
  echo "The self-service installer supports macOS and systemd-based Linux." >&2
  exit 1
fi
if [[ "$SYSTEM_NAME" == "Linux" ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    echo "Linux self-service installation must run as the target non-root user, not with sudo." >&2
    exit 1
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "Linux self-service installation requires systemd and systemctl." >&2
    exit 1
  fi
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "Linux self-service installation requires an active systemd user session." >&2
    echo "Containers, WSL without systemd, and non-systemd desktops must use manual deployment." >&2
    exit 1
  fi
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
  - start per-user background services that survive login sessions

It does not collect prompts, responses, source code, tool arguments, credentials,
screenshots, browser history, or raw command lines.
NOTICE
  read -r -p "Continue? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "Installation cancelled."; exit 1; }
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
# Run recovery code from the source tree, independently of the managed venv.
UPGRADE_BACKUP="$(PYTHONPATH="$PACKAGE_SOURCE/src" "$BASE_PYTHON" -m ai_asset_inventory.upgrade snapshot --root "$INSTALL_ROOT")"
echo "Installation backup: $UPGRADE_BACKUP"
UPGRADE_STARTED=true
# The snapshot command has already quiesced services before capturing the DB.
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
for command in server agent hook adapters setup status dashboard uninstall mcp demo; do
  if ! "$CLI" "$command" --help >/dev/null; then
    echo "Installed EdgeDisco CLI is missing '$command'; setup was not started." >&2
    exit 1
  fi
done

echo "Configuring local services..."
"$CLI" setup "${SETUP_ARGS[@]}"

PUBLIC="$(cat "$INSTALL_ROOT/cli-launcher.path")"
if [[ ! -x "$PUBLIC" ]]; then
  echo "EdgeDisco installed, but the managed launcher is not executable: $PUBLIC" >&2
  exit 1
fi
FRESH_HELP="$(/usr/bin/env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME "$PUBLIC" --help)"
if [[ "$FRESH_HELP" != *"demo"* || "$FRESH_HELP" != *"dashboard"* ]]; then
  echo "The managed EdgeDisco launcher is missing required subcommands." >&2
  exit 1
fi
cat <<EOF

EdgeDisco installation verified.
CLI: $PUBLIC
Open a new Terminal window, then run:
  edgedisco status
  edgedisco dashboard
  edgedisco demo
  edgedisco uninstall

Configuration and logs:
  $INSTALL_ROOT
EOF
CURRENT_COMMAND="$(command -v edgedisco || true)"
if [[ -n "$CURRENT_COMMAND" && "$CURRENT_COMMAND" != "$PUBLIC" ]]; then
  cat <<EOF

Another edgedisco command is currently active from:
  $CURRENT_COMMAND

The managed EdgeDisco installation is:
  $PUBLIC

This commonly happens when an older Python virtual environment is active.
Open a new Terminal and run:
  edgedisco demo
Or run the managed installation directly:
  $PUBLIC demo
EOF
fi
INSTALL_COMPLETE=true
