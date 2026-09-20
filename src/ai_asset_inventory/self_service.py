from __future__ import annotations

import json
import os
import platform
import plistlib
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import install_adapters
from .agent import AgentClient


SERVER_LABEL = "com.edgedisco.server"
AGENT_LABEL = "com.edgedisco.agent"
CLI_NAME = "edgedisco"
PATH_MARKER_BEGIN = "# >>> edgedisco PATH >>>"
PATH_MARKER_END = "# <<< edgedisco PATH <<<"
LAUNCHER_MARKER = "# EdgeDisco managed launcher — do not edit"


@dataclass(frozen=True)
class Layout:
    root: Path
    venv: Path
    config: Path
    env: Path
    database: Path
    logs: Path
    bin: Path
    launch_agents: Path


@dataclass(frozen=True)
class CliLauncher:
    """Public ``edgedisco`` entrypoint created by macOS self-service setup."""

    managed: Path
    public: Path
    path_profile: Path | None
    path_integrated: bool


def default_layout(root: Path | None = None, home: Path | None = None) -> Layout:
    home = home or Path.home()
    root = root or home / ".edgedisco"
    return Layout(
        root=root,
        venv=root / "venv",
        config=root / "agent.json",
        env=root / "server.env",
        database=root / "data/inventory.db",
        logs=root / "logs",
        bin=root / "bin",
        launch_agents=home / "Library/LaunchAgents",
    )


def _cli_executable(layout: Layout) -> Path:
    cli = layout.venv / "bin" / CLI_NAME
    if cli.exists():
        return cli
    fallback = layout.venv / "bin" / "ai-inventory"
    if fallback.exists():
        return fallback
    return cli


def _launcher_state_path(layout: Layout) -> Path:
    return layout.root / "cli-launcher.path"


def _managed_launcher_path(layout: Layout) -> Path:
    return layout.bin / CLI_NAME


def select_public_bin_dir(home: Path | None = None) -> tuple[Path, bool]:
    """Pick a user-writable bin directory for the public ``edgedisco`` command.

    Prefer ``/usr/local/bin`` when installing for the real user home and that
    directory is writable, because it is already on the default macOS PATH.
    Otherwise use ``~/.local/bin`` and integrate it via a managed ``~/.zprofile``
    block. Custom ``home`` values (tests, alternate prefixes) always use the
    home-local bin directory so system paths are never touched.
    """
    home = home or Path.home()
    if home.resolve() == Path.home().resolve():
        usr_local = Path("/usr/local/bin")
        if usr_local.is_dir() and os.access(usr_local, os.W_OK):
            return usr_local, False
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    return local_bin, True


def _is_managed_public_launcher(path: Path, managed: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    try:
        if path.is_symlink():
            return path.resolve() == managed.resolve()
    except OSError:
        return False
    try:
        return LAUNCHER_MARKER in path.read_text(errors="replace")
    except OSError:
        return False


def _write_managed_launcher(layout: Layout) -> Path:
    managed = _managed_launcher_path(layout)
    target = _cli_executable(layout)
    content = (
        "#!/bin/sh\n"
        f"{LAUNCHER_MARKER}\n"
        f"exec {shlex.quote(str(target))} \"$@\"\n"
    )
    _atomic_write(managed, content, 0o700)
    return managed


def _path_block(local_bin: Path) -> str:
    rendered = str(local_bin)
    return (
        f"{PATH_MARKER_BEGIN}\n"
        "# Managed by EdgeDisco; do not edit this block.\n"
        f'case ":$PATH:" in *:{rendered}:*) ;; *) export PATH="{rendered}:$PATH" ;; esac\n'
        f"{PATH_MARKER_END}\n"
    )


def ensure_local_bin_on_path(home: Path | None = None, local_bin: Path | None = None) -> Path:
    """Idempotently ensure ``~/.local/bin`` is on PATH for new macOS login shells."""
    home = home or Path.home()
    local_bin = local_bin or (home / ".local" / "bin")
    profile = home / ".zprofile"
    block = _path_block(local_bin)
    existing = profile.read_text(errors="replace") if profile.exists() else ""
    if PATH_MARKER_BEGIN in existing and PATH_MARKER_END in existing:
        start = existing.index(PATH_MARKER_BEGIN)
        end = existing.index(PATH_MARKER_END) + len(PATH_MARKER_END)
        # Consume a single trailing newline after the end marker when present.
        if end < len(existing) and existing[end] == "\n":
            end += 1
        updated = existing[:start] + block + existing[end:]
    elif existing.strip():
        updated = existing.rstrip() + "\n\n" + block
    else:
        updated = block
    _atomic_write(profile, updated, 0o600)
    return profile


def remove_local_bin_path_block(home: Path | None = None) -> bool:
    """Remove only the EdgeDisco-managed PATH block from ``~/.zprofile``."""
    home = home or Path.home()
    profile = home / ".zprofile"
    if not profile.exists():
        return False
    existing = profile.read_text(errors="replace")
    if PATH_MARKER_BEGIN not in existing or PATH_MARKER_END not in existing:
        return False
    start = existing.index(PATH_MARKER_BEGIN)
    end = existing.index(PATH_MARKER_END) + len(PATH_MARKER_END)
    if end < len(existing) and existing[end] == "\n":
        end += 1
    # Drop a blank line immediately before the block when we inserted one.
    if start >= 2 and existing[start - 2:start] == "\n\n":
        start -= 1
    updated = existing[:start] + existing[end:]
    if updated.strip():
        _atomic_write(profile, updated, 0o600)
    else:
        profile.unlink(missing_ok=True)
    return True


def install_cli_launcher(layout: Layout, home: Path | None = None) -> CliLauncher:
    """Install a stable public ``edgedisco`` command for normal macOS shells."""
    home = home or Path.home()
    managed = _write_managed_launcher(layout)
    public_dir, needs_path = select_public_bin_dir(home)
    public = public_dir / CLI_NAME
    if public.exists() or public.is_symlink():
        if not _is_managed_public_launcher(public, managed):
            raise RuntimeError(
                f"refusing to overwrite existing command at {public}; "
                "remove it or choose another install location"
            )
        public.unlink()
    public.symlink_to(managed)
    path_profile = ensure_local_bin_on_path(home, public_dir) if needs_path else None
    _atomic_write(_launcher_state_path(layout), f"{public}\n")
    return CliLauncher(
        managed=managed,
        public=public,
        path_profile=path_profile,
        path_integrated=needs_path,
    )


def uninstall_cli_launcher(layout: Layout, home: Path | None = None) -> dict[str, Any]:
    """Remove the EdgeDisco-managed public launcher without touching unrelated files."""
    home = home or Path.home()
    managed = _managed_launcher_path(layout)
    removed: list[str] = []
    state_path = _launcher_state_path(layout)
    candidates: list[Path] = []
    if state_path.exists():
        raw = state_path.read_text(errors="replace").strip()
        if raw:
            candidates.append(Path(raw))
    candidates.extend(
        [
            Path("/usr/local/bin") / CLI_NAME,
            home / ".local" / "bin" / CLI_NAME,
        ]
    )
    seen: set[Path] = set()
    for public in candidates:
        key = public
        if key in seen:
            continue
        seen.add(key)
        if _is_managed_public_launcher(public, managed):
            public.unlink(missing_ok=True)
            removed.append(str(public))
    if managed.exists() or managed.is_symlink():
        managed.unlink(missing_ok=True)
        removed.append(str(managed))
    state_path.unlink(missing_ok=True)
    path_removed = remove_local_bin_path_block(home)
    return {
        "removed": removed,
        "path_block_removed": path_removed,
    }


def _atomic_write(path: Path, content: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if isinstance(content, bytes):
        temporary.write_bytes(content)
    else:
        temporary.write_text(content)
    if os.name != "nt":
        temporary.chmod(mode)
    temporary.replace(path)


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:]
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip("'\"")
    return values


def ensure_credentials(layout: Layout, port: int | None = None) -> dict[str, str]:
    values = _parse_env(layout.env)
    values.setdefault("AAI_ADMIN_TOKEN", secrets.token_urlsafe(32))
    values.setdefault("AAI_ENROLLMENT_TOKEN", secrets.token_urlsafe(32))
    values["EDGEDISCO_PORT"] = str(port or int(values.get("EDGEDISCO_PORT", "8080")))
    rendered = "".join(f"export {key}={value}\n" for key, value in values.items())
    _atomic_write(layout.env, rendered)
    return values


def _plist(label: str, arguments: list[str], stdout: Path, stderr: Path) -> bytes:
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
    }, sort_keys=True)


def write_launch_agents(layout: Layout, port: int) -> tuple[Path, Path]:
    executable = Path(sys.executable)
    cli = executable.parent / "edgedisco"
    if not cli.exists():
        cli = executable.parent / "ai-inventory"
    server_runner = layout.bin / "run-server.sh"
    runner = (
        "#!/bin/sh\n"
        "set -a\n"
        f". {shlex.quote(str(layout.env))}\n"
        "set +a\n"
        f"exec {shlex.quote(str(cli))} server --host 127.0.0.1 "
        f"--port {port} --db {shlex.quote(str(layout.database))}\n"
    )
    _atomic_write(server_runner, runner, 0o700)
    server_plist = layout.launch_agents / f"{SERVER_LABEL}.plist"
    agent_plist = layout.launch_agents / f"{AGENT_LABEL}.plist"
    _atomic_write(
        server_plist,
        _plist(SERVER_LABEL, [str(server_runner)], layout.logs / "server.log", layout.logs / "server.err.log"),
    )
    _atomic_write(
        agent_plist,
        _plist(
            AGENT_LABEL,
            [str(cli), "agent", "run", "--config", str(layout.config)],
            layout.logs / "agent.log",
            layout.logs / "agent.err.log",
        ),
    )
    return server_plist, agent_plist


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *arguments], capture_output=True, text=True, check=check)


def _restart_service(label: str, plist: Path) -> None:
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{label}", check=False)
    result = _launchctl("bootstrap", domain, str(plist), check=False)
    if result.returncode != 0:
        raise RuntimeError(f"could not start {label}: {result.stderr.strip() or result.stdout.strip()}")


def _health(port: int, admin_token: str | None = None) -> dict[str, Any] | None:
    path = "/api/v1/summary" if admin_token else "/healthz"
    headers = {"Authorization": f"Bearer {admin_token}"} if admin_token else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError):
        return None


def _wait_for_server(port: int, admin_token: str, timeout: int = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health(port, admin_token) is not None:
            return
        time.sleep(0.5)
    raise RuntimeError("server did not become healthy; inspect ~/.edgedisco/logs/server.err.log")


def detect_adapters(home: Path | None = None) -> list[str]:
    home = home or Path.home()
    found: list[str] = []
    if Path("/Applications/Cursor.app").exists() or shutil.which("cursor"):
        found.append("cursor")
    if shutil.which("claude") or (home / ".claude").exists():
        found.append("claude-code")
    if shutil.which("copilot") or (home / ".copilot").exists():
        found.append("github-copilot")
    return found


def setup_macos(*, root: Path | None = None, port: int | None = None,
                all_adapters: bool = False, open_dashboard: bool = True,
                home: Path | None = None) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("self-service setup currently supports macOS; use the manual deployment guide elsewhere")
    layout = default_layout(root, home)
    layout.database.parent.mkdir(parents=True, exist_ok=True)
    layout.logs.mkdir(parents=True, exist_ok=True)
    values = ensure_credentials(layout, port)
    selected_port = int(values["EDGEDISCO_PORT"])
    server_plist, agent_plist = write_launch_agents(layout, selected_port)

    existing = _health(selected_port)
    authorized = _health(selected_port, values["AAI_ADMIN_TOKEN"]) if existing else None
    if existing is not None and authorized is None:
        raise RuntimeError(
            f"port {selected_port} is already used by a server with different credentials; "
            "stop it or rerun setup with --port another-port"
        )
    _restart_service(SERVER_LABEL, server_plist)
    _wait_for_server(selected_port, values["AAI_ADMIN_TOKEN"])

    current = {}
    if layout.config.exists():
        try:
            current = json.loads(layout.config.read_text())
        except json.JSONDecodeError:
            current = {}
    if not current.get("device_token"):
        current = {
            "server_url": f"http://127.0.0.1:{selected_port}",
            "enrollment_token": values["AAI_ENROLLMENT_TOKEN"],
            "scan_interval_seconds": 300,
            "runtime_spool": str(layout.root / "runtime-events.jsonl"),
        }
        _atomic_write(layout.config, json.dumps(current, indent=2) + "\n")
        AgentClient(layout.config).enroll()
    else:
        current["server_url"] = f"http://127.0.0.1:{selected_port}"
        current.setdefault("scan_interval_seconds", 300)
        current.setdefault("runtime_spool", str(layout.root / "runtime-events.jsonl"))
        _atomic_write(layout.config, json.dumps(current, indent=2) + "\n")

    apps = ["cursor", "claude-code", "github-copilot"] if all_adapters else detect_adapters()
    adapter_paths = install_adapters(layout.config, apps) if apps else []
    report = AgentClient(layout.config).send_once()
    _restart_service(AGENT_LABEL, agent_plist)
    launcher = install_cli_launcher(layout, home)
    dashboard = f"http://127.0.0.1:{selected_port}"
    if open_dashboard:
        webbrowser.open(dashboard)
    return {
        "version": __version__,
        "dashboard": dashboard,
        "admin_token": values["AAI_ADMIN_TOKEN"],
        "root": str(layout.root),
        "adapters": apps,
        "adapter_paths": [str(path) for path in adapter_paths],
        "asset_count": report.get("asset_count", 0),
        "runtime_event_count": report.get("runtime_event_count", 0),
        "cli": str(launcher.public),
        "cli_path_integrated": launcher.path_integrated,
    }


def status(root: Path | None = None) -> dict[str, Any]:
    layout = default_layout(root)
    values = _parse_env(layout.env)
    port = int(values.get("EDGEDISCO_PORT", "8080"))
    summary = _health(port, values.get("AAI_ADMIN_TOKEN")) if values.get("AAI_ADMIN_TOKEN") else None
    enrolled = False
    if layout.config.exists():
        try:
            enrolled = bool(json.loads(layout.config.read_text()).get("device_token"))
        except json.JSONDecodeError:
            pass
    return {
        "version": __version__,
        "server": "healthy" if summary is not None else "unavailable",
        "endpoint": "enrolled" if enrolled else "not enrolled",
        "dashboard": f"http://127.0.0.1:{port}",
        "devices": summary.get("devices", 0) if summary else 0,
        "assets": summary.get("assets", 0) if summary else 0,
        "live_sessions": summary.get("active_sessions", 0) if summary else 0,
        "root": str(layout.root),
    }


@dataclass(frozen=True)
class LocalServer:
    """Local EdgeDisco server endpoint used by setup and demo flows."""

    root: Path
    dashboard: str
    admin_token: str
    agent_config: Path
    port: int
    reused: bool


def ensure_local_server(
    *,
    root: Path | None = None,
    home: Path | None = None,
    start_if_needed: bool = True,
) -> LocalServer:
    """Reuse a healthy local server or start one through the supported macOS path."""
    home = home or Path.home()
    layout = default_layout(root, home)
    values = _parse_env(layout.env)
    port = int(values.get("EDGEDISCO_PORT", "8080"))
    admin = values.get("AAI_ADMIN_TOKEN")
    if admin and _health(port, admin) is not None:
        return LocalServer(
            root=layout.root,
            dashboard=f"http://127.0.0.1:{port}",
            admin_token=admin,
            agent_config=layout.config,
            port=port,
            reused=True,
        )
    if not start_if_needed:
        raise RuntimeError(
            f"EdgeDisco server is not available on http://127.0.0.1:{port}; "
            "start it with edgedisco setup or the self-service installer"
        )
    if platform.system() != "Darwin":
        raise RuntimeError(
            "EdgeDisco server is not running. Start it first, or use the macOS "
            "self-service installer / edgedisco setup."
        )
    setup_macos(root=layout.root, home=home, open_dashboard=False)
    values = _parse_env(layout.env)
    port = int(values["EDGEDISCO_PORT"])
    admin = values["AAI_ADMIN_TOKEN"]
    if _health(port, admin) is None:
        raise RuntimeError("EdgeDisco server did not become healthy after setup")
    return LocalServer(
        root=layout.root,
        dashboard=f"http://127.0.0.1:{port}",
        admin_token=admin,
        agent_config=layout.config,
        port=port,
        reused=False,
    )


def uninstall_macos(*, root: Path | None = None, purge: bool = False,
                    assume_yes: bool = False, home: Path | None = None) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("self-service uninstall currently supports macOS")
    layout = default_layout(root, home)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{AGENT_LABEL}", check=False)
    _launchctl("bootout", f"{domain}/{SERVER_LABEL}", check=False)
    (layout.launch_agents / f"{AGENT_LABEL}.plist").unlink(missing_ok=True)
    (layout.launch_agents / f"{SERVER_LABEL}.plist").unlink(missing_ok=True)
    launcher = uninstall_cli_launcher(layout, home)
    purged = False
    if purge:
        if layout.root.name != ".edgedisco":
            raise RuntimeError(f"refusing to purge unexpected path: {layout.root}")
        approved = assume_yes
        if not approved:
            answer = input("Delete all EdgeDisco credentials, logs, and evidence? [y/N] ")
            approved = answer.lower() == "y"
        if approved and layout.root.exists():
            shutil.rmtree(layout.root)
            purged = True
    return {
        "services_removed": True,
        "data_purged": purged,
        "root": str(layout.root),
        "launcher_removed": launcher["removed"],
        "path_block_removed": launcher["path_block_removed"],
    }
