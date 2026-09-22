from __future__ import annotations

import json
import os
import platform
import plistlib
import re
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
from .adapters import install_adapters, uninstall_adapters
from .agent import AgentClient, UploadError
from .configuration import load_config, migrate_config
from .detector import _installed_cli_candidates
from .path_policy import allowed_path


SERVER_LABEL = "com.edgedisco.server"
AGENT_LABEL = "com.edgedisco.agent"
EXPORTER_LABEL = "com.edgedisco.otlp-export"
SERVICE_LABELS = (SERVER_LABEL, AGENT_LABEL, EXPORTER_LABEL)
SERVICE_NAME_MAP = {
    "server": SERVER_LABEL,
    "agent": AGENT_LABEL,
    "otlp-export": EXPORTER_LABEL,
    "exporter": EXPORTER_LABEL,
    SERVER_LABEL: SERVER_LABEL,
    AGENT_LABEL: AGENT_LABEL,
    EXPORTER_LABEL: EXPORTER_LABEL,
}
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
    systemd_user: Path


@dataclass(frozen=True)
class CliLauncher:
    """Public ``edgedisco`` entrypoint created by self-service setup."""

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
        systemd_user=home / ".config/systemd/user",
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
    """Use the user's bin directory, ahead of older system commands on PATH."""
    home = home or Path.home()
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


def _login_profile(home: Path) -> Path:
    if Path(os.environ.get("SHELL", "/bin/zsh")).name == "bash":
        for name in (".bash_profile", ".bash_login", ".profile"):
            candidate = home / name
            if candidate.exists():
                return candidate
        return home / ".bash_profile"
    return home / ".zprofile"


def ensure_local_bin_on_path(home: Path | None = None, local_bin: Path | None = None) -> Path:
    """Idempotently add the managed bin to the user's login-shell PATH."""
    home = home or Path.home()
    local_bin = local_bin or (home / ".local" / "bin")
    profile = _login_profile(home)
    block = _path_block(local_bin)
    existing = profile.read_text(errors="replace") if profile.exists() else ""
    if PATH_MARKER_BEGIN in existing and PATH_MARKER_END in existing:
        start = existing.index(PATH_MARKER_BEGIN)
        end = existing.index(PATH_MARKER_END) + len(PATH_MARKER_END)
        # Consume a single trailing newline after the end marker when present.
        if end < len(existing) and existing[end] == "\n":
            end += 1
        # Put our block last so older PATH edits cannot shadow the launcher.
        remaining = (existing[:start] + existing[end:]).rstrip()
        updated = remaining + "\n\n" + block if remaining else block
    elif existing.strip():
        updated = existing.rstrip() + "\n\n" + block
    else:
        updated = block
    _atomic_write(profile, updated, 0o600)
    return profile


def remove_local_bin_path_block(home: Path | None = None) -> bool:
    """Remove only EdgeDisco-managed blocks from supported login profiles."""
    home = home or Path.home()
    removed = False
    for name in (".zprofile", ".bash_profile", ".bash_login", ".profile"):
        removed = _remove_path_block(home / name) or removed
    return removed


def _remove_path_block(profile: Path) -> bool:
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
    """Install a stable public ``edgedisco`` command for normal user shells."""
    home = home or Path.home()
    managed = _write_managed_launcher(layout)
    public_dir, needs_path = select_public_bin_dir(home)
    public = public_dir / CLI_NAME
    if (public.exists() or public.is_symlink()) and not _is_managed_public_launcher(public, managed):
        # Preserve an unrelated command and put our managed bin first on PATH.
        public_dir, public = layout.bin, managed
    if public.exists() or public.is_symlink():
        if public != managed:
            public.unlink()
    if public != managed:
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
            try:
                parsed = shlex.split(value, comments=False)
                values[key] = parsed[0] if len(parsed) == 1 else value.strip()
            except ValueError:
                raise RuntimeError("Invalid server.env quoting") from None
    return values


def ensure_credentials(layout: Layout, port: int | None = None) -> dict[str, str]:
    values = _parse_env(layout.env)
    values.setdefault("AAI_ADMIN_TOKEN", secrets.token_urlsafe(32))
    values.setdefault("AAI_ENROLLMENT_TOKEN", secrets.token_urlsafe(32))
    values["EDGEDISCO_PORT"] = str(port or int(values.get("EDGEDISCO_PORT", "8080")))
    rendered = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items())
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


def _systemd_quote(path: Path) -> str:
    rendered = str(path)
    if "\n" in rendered or "\r" in rendered:
        raise RuntimeError("service paths cannot contain newlines")
    return '"' + rendered.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def write_systemd_units(layout: Layout, port: int) -> tuple[Path, Path]:
    """Write unprivileged user services for systemd-based Linux desktops."""
    cli = _cli_executable(layout)
    server_runner = layout.bin / "run-server.sh"
    _atomic_write(server_runner, (
        "#!/bin/sh\nset -a\n"
        f". {shlex.quote(str(layout.env))}\nset +a\n"
        f"exec {shlex.quote(str(cli))} server --host 127.0.0.1 --port {port} "
        f"--db {shlex.quote(str(layout.database))}\n"
    ), 0o700)
    common = ("Restart=always\nRestartSec=15\nNoNewPrivileges=true\nPrivateTmp=true\n"
              f"ProtectSystem=strict\nReadWritePaths={_systemd_quote(layout.root)}\n")
    server = layout.systemd_user / f"{SERVER_LABEL}.service"
    agent = layout.systemd_user / f"{AGENT_LABEL}.service"
    _atomic_write(server, (
        "[Unit]\nDescription=EdgeDisco local inventory server\nAfter=network-online.target\n\n"
        f"[Service]\nType=simple\nExecStart={_systemd_quote(server_runner)}\n{common}\n"
        "[Install]\nWantedBy=default.target\n"
    ))
    _atomic_write(agent, (
        "[Unit]\nDescription=EdgeDisco endpoint collector for the logged-in user\n"
        f"After=network-online.target {SERVER_LABEL}.service\nRequires={SERVER_LABEL}.service\n\n"
        f"[Service]\nType=simple\nExecStart={_systemd_quote(cli)} agent run --config {_systemd_quote(layout.config)}\n{common}\n"
        "[Install]\nWantedBy=default.target\n"
    ))
    return server, agent


def _systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(key, None)
    return subprocess.run(["systemctl", "--user", *arguments], env=environment,
                          capture_output=True, text=True, check=check)


def require_systemd_user() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("Linux self-service setup must run as the target non-root user")
    if shutil.which("systemctl") is None:
        raise RuntimeError("Linux self-service installation requires systemd and systemctl")
    result = _systemctl("show-environment", check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "Linux self-service installation requires an active systemd user session; "
            "containers, WSL without systemd, and non-systemd desktops must use manual deployment"
        )


def _restart_systemd_service(name: str, *, wait_for_port: int | None = None) -> None:
    _systemctl("stop", name, check=False)
    if wait_for_port is not None:
        deadline = time.monotonic() + 5
        while _health(wait_for_port) is not None:
            if time.monotonic() >= deadline:
                raise RuntimeError("a stale server is still running after systemd stop")
            time.sleep(0.2)
    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", name, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"could not start {name}: {result.stderr.strip() or result.stdout.strip()}")


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(key, None)
    return subprocess.run(["launchctl", *arguments], env=environment, capture_output=True, text=True, check=check)


def _restart_service(label: str, plist: Path, *, wait_for_port: int | None = None) -> None:
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{label}", check=False)
    if wait_for_port is not None:
        deadline = time.monotonic() + 5
        while _health(wait_for_port) is not None:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"port {wait_for_port} is still served after stopping {label}; "
                    "refusing to verify the upgrade against a stale server"
                )
            time.sleep(0.2)
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
    except (OSError, json.JSONDecodeError):
        return None


def _wait_for_server(port: int, admin_token: str | None = None, timeout: int = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health(port, admin_token) is not None:
            return
        time.sleep(0.5)
    raise RuntimeError("server did not become healthy; inspect ~/.edgedisco/logs/server.err.log")


def _verify_browser_bootstrap(port: int, admin_token: str) -> str:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/browser-bootstrap",
        data=b"", method="POST", headers={"Authorization": f"Bearer {admin_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            path = json.load(response).get("bootstrap_path", "")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"EdgeDisco server on port {port} does not support automatic dashboard sign-in "
            f"(HTTP {exc.code}); check for an older server process"
        ) from None
    except (OSError, ValueError, KeyError):
        raise RuntimeError(f"Could not verify automatic dashboard sign-in on port {port}") from None
    if not isinstance(path, str) or not re.fullmatch(r"/browser-bootstrap/[A-Za-z0-9_-]+", path):
        raise RuntimeError(f"Unexpected browser bootstrap response from port {port}")
    return f"http://127.0.0.1:{port}{path}"


def detect_adapters(home: Path | None = None) -> list[str]:
    home = home or Path.home()
    found: list[str] = []
    clis = {name for name, _vendor, _path in _installed_cli_candidates()}
    if allowed_path(Path("/Applications/Cursor.app"), (Path("/Applications"),)) is not None:
        found.append("cursor")
    if "Claude Code" in clis or allowed_path(home / ".claude", (home / ".claude",)) is not None:
        found.append("claude-code")
    if "GitHub Copilot" in clis or allowed_path(home / ".copilot", (home / ".copilot",)) is not None:
        found.append("github-copilot")
    return found


def setup_macos(*, root: Path | None = None, port: int | None = None,
                all_adapters: bool = False, open_dashboard: bool = True,
                home: Path | None = None) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("macOS setup requires Darwin")
    layout = default_layout(root, home)
    return _setup_self_service(
        layout, port, all_adapters, open_dashboard,
        write_services=write_launch_agents,
        restart_server=lambda existing, selected: _restart_service(
            SERVER_LABEL, layout.launch_agents / f"{SERVER_LABEL}.plist",
            wait_for_port=selected if existing else None,
        ),
        restart_agent=lambda: _restart_service(
            AGENT_LABEL, layout.launch_agents / f"{AGENT_LABEL}.plist"
        ),
        home=home,
    )


def setup_linux(*, root: Path | None = None, port: int | None = None,
                all_adapters: bool = False, open_dashboard: bool = True,
                home: Path | None = None) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise RuntimeError("Linux setup requires Linux")
    require_systemd_user()
    layout = default_layout(root, home)
    return _setup_self_service(
        layout, port, all_adapters, open_dashboard,
        write_services=write_systemd_units,
        restart_server=lambda existing, selected: _restart_systemd_service(
            f"{SERVER_LABEL}.service", wait_for_port=selected if existing else None
        ),
        restart_agent=lambda: _restart_systemd_service(f"{AGENT_LABEL}.service"),
        home=home,
    )


def setup_self_service(**kwargs: Any) -> dict[str, Any]:
    if platform.system() == "Darwin":
        return setup_macos(**kwargs)
    if platform.system() == "Linux":
        return setup_linux(**kwargs)
    raise RuntimeError("self-service setup supports macOS and systemd-based Linux")


def _setup_self_service(layout: Layout, port: int | None, all_adapters: bool,
                        open_browser: bool, *, write_services, restart_server,
                        restart_agent, home: Path | None) -> dict[str, Any]:
    current = load_config(layout.config)
    layout.database.parent.mkdir(parents=True, exist_ok=True)
    layout.logs.mkdir(parents=True, exist_ok=True)
    values = ensure_credentials(layout, port)
    # Stop delivery before unrelated setup steps can fail or enqueue a new scan.
    if values.get("EDGEDISCO_OTLP_EXPORT_ENABLED") != "true":
        configure_exporter_service(layout, values)
    selected_port = int(values["EDGEDISCO_PORT"])
    write_services(layout, selected_port)

    existing = _health(selected_port)
    authorized = _health(selected_port, values["AAI_ADMIN_TOKEN"]) if existing else None
    if existing is not None and authorized is None:
        raise RuntimeError(
            f"port {selected_port} is already used by a server with different credentials; "
            "stop it or rerun setup with --port another-port"
        )
    restart_server(existing is not None, selected_port)
    _wait_for_server(selected_port, values["AAI_ADMIN_TOKEN"])
    _verify_browser_bootstrap(selected_port, values["AAI_ADMIN_TOKEN"])

    current = migrate_config(current, server_url=f"http://127.0.0.1:{selected_port}",
                             spool=layout.root / "runtime-events.jsonl")
    if not current.get("device_token"):
        current.pop("device_id", None)
        current["enrollment_token"] = values["AAI_ENROLLMENT_TOKEN"]
        _atomic_write(layout.config, json.dumps(current, indent=2) + "\n")
        AgentClient(layout.config).enroll()
    else:
        current.pop("enrollment_token", None)
        _atomic_write(layout.config, json.dumps(current, indent=2) + "\n")

    apps = ["cursor", "claude-code", "github-copilot"] if all_adapters else detect_adapters()
    adapter_paths = install_adapters(layout.config, apps) if apps else []
    try:
        report = AgentClient(layout.config).send_once()
    except UploadError as exc:
        if exc.status != 401:
            raise
        # Only a definitive authentication rejection justifies a new identity.
        # A timeout or server error must not create duplicate device records.
        current = load_config(layout.config)
        current.pop("device_id", None)
        current.pop("device_token", None)
        current["enrollment_token"] = values["AAI_ENROLLMENT_TOKEN"]
        _atomic_write(layout.config, json.dumps(current, indent=2) + "\n")
        client = AgentClient(layout.config)
        client.enroll()
        report = client.send_once()
    restart_agent()
    if values.get("EDGEDISCO_OTLP_EXPORT_ENABLED") == "true":
        configure_exporter_service(layout, values)
    launcher = install_cli_launcher(layout, home)
    dashboard = f"http://127.0.0.1:{selected_port}"
    if open_browser:
        # Mint immediately before opening: setup may outlive the 60-second code.
        webbrowser.open(_verify_browser_bootstrap(selected_port, values["AAI_ADMIN_TOKEN"]))
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


def open_dashboard(root: Path | None = None, *, browser_fn=webbrowser.open) -> str:
    """Open a fresh authenticated local dashboard without exposing its token."""
    layout = default_layout(root)
    values = _parse_env(layout.env)
    admin_token = values.get("AAI_ADMIN_TOKEN")
    if not admin_token:
        raise RuntimeError(f"Missing administrator credential in {layout.env}")
    try:
        port = int(values.get("EDGEDISCO_PORT", "8080"))
    except ValueError:
        raise RuntimeError(f"Invalid EDGEDISCO_PORT in {layout.env}") from None
    if _health(port, admin_token) is None:
        raise RuntimeError(f"EdgeDisco server on port {port} is unavailable or rejected its credential")
    url = _verify_browser_bootstrap(port, admin_token)
    if not browser_fn(url):
        raise RuntimeError("Could not open the dashboard in a browser")
    return f"http://127.0.0.1:{port}"


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
    """Reuse a healthy local server or start one through self-service setup."""
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
    setup_self_service(root=layout.root, home=home, open_dashboard=False)
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
    exporter_plist = layout.launch_agents / f"{EXPORTER_LABEL}.plist"
    if exporter_plist.exists():
        _launchctl("bootout", f"{domain}/{EXPORTER_LABEL}", check=False)
        exporter_plist.unlink()
    (layout.launch_agents / f"{AGENT_LABEL}.plist").unlink(missing_ok=True)
    (layout.launch_agents / f"{SERVER_LABEL}.plist").unlink(missing_ok=True)
    launcher = uninstall_cli_launcher(layout, home)
    removed_adapters = uninstall_adapters(home)
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
        "adapters_removed": [str(path) for path in removed_adapters],
    }


def uninstall_linux(*, root: Path | None = None, purge: bool = False,
                    assume_yes: bool = False, home: Path | None = None) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise RuntimeError("Linux uninstall requires Linux")
    layout = default_layout(root, home)
    labels = [AGENT_LABEL, SERVER_LABEL]
    if (layout.systemd_user / f"{EXPORTER_LABEL}.service").exists():
        labels.insert(0, EXPORTER_LABEL)
    for label in labels:
        _systemctl("disable", "--now", f"{label}.service", check=False)
        (layout.systemd_user / f"{label}.service").unlink(missing_ok=True)
    _systemctl("daemon-reload", check=False)
    launcher = uninstall_cli_launcher(layout, home)
    removed_adapters = uninstall_adapters(home)
    purged = _purge_installation(layout, purge, assume_yes)
    return {
        "services_removed": True, "data_purged": purged, "root": str(layout.root),
        "launcher_removed": launcher["removed"], "path_block_removed": launcher["path_block_removed"],
        "adapters_removed": [str(path) for path in removed_adapters],
    }


def _purge_installation(layout: Layout, purge: bool, assume_yes: bool) -> bool:
    if not purge:
        return False
    if layout.root.name != ".edgedisco":
        raise RuntimeError(f"refusing to purge unexpected path: {layout.root}")
    approved = assume_yes
    if not approved:
        approved = input("Delete all EdgeDisco credentials, logs, and evidence? [y/N] ").lower() == "y"
    if approved and layout.root.exists():
        shutil.rmtree(layout.root)
        return True
    return False


def uninstall_self_service(**kwargs: Any) -> dict[str, Any]:
    if platform.system() == "Darwin":
        return uninstall_macos(**kwargs)
    if platform.system() == "Linux":
        return uninstall_linux(**kwargs)
    raise RuntimeError("self-service uninstall supports macOS and systemd-based Linux")


def _resolve_service_labels(
    layout: Layout,
    requested: list[str] | None,
    *,
    is_macos: bool,
) -> list[str]:
    def _service_file(lbl: str) -> Path:
        return (layout.launch_agents / f"{lbl}.plist") if is_macos else (layout.systemd_user / f"{lbl}.service")

    if requested:
        resolved: list[str] = []
        for item in requested:
            lbl = SERVICE_NAME_MAP.get(item.lower() if isinstance(item, str) else item)
            if not lbl:
                raise RuntimeError(
                    f"Unknown service: {item}; choose from server, agent, otlp-export"
                )
            file_path = _service_file(lbl)
            if not file_path.exists():
                raise RuntimeError(f"Service {lbl} is not installed ({file_path} not found)")
            if lbl not in resolved:
                resolved.append(lbl)
        return resolved

    installed = [lbl for lbl in SERVICE_LABELS if _service_file(lbl).exists()]
    if not installed:
        location = layout.launch_agents if is_macos else layout.systemd_user
        raise RuntimeError(f"No EdgeDisco services found in {location}; run 'edgedisco setup' first")
    return installed


def start_macos(
    *,
    root: Path | None = None,
    services: list[str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    if platform.system() != "Darwin":
        raise RuntimeError("macOS service start requires Darwin")
    layout = default_layout(root, home)
    targets = _resolve_service_labels(layout, services, is_macos=True)
    targets.sort(key=lambda s: SERVICE_LABELS.index(s))
    domain = f"gui/{os.getuid()}"
    results: dict[str, str] = {}
    values = _parse_env(layout.env) if layout.env.exists() else {}
    port = int(values.get("EDGEDISCO_PORT", "8080"))

    for label in targets:
        plist = layout.launch_agents / f"{label}.plist"
        target = f"{domain}/{label}"
        state = _launchctl("print", target, check=False)
        if state.returncode == 0:
            if label == SERVER_LABEL:
                if _health(port) is not None:
                    results[label] = "already running"
                    continue
                _launchctl("kickstart", "-k", target, check=False)
                if values.get("AAI_ADMIN_TOKEN"):
                    _wait_for_server(port, values["AAI_ADMIN_TOKEN"])
                results[label] = "started"
            else:
                results[label] = "already running"
        else:
            res = _launchctl("bootstrap", domain, str(plist), check=False)
            if res.returncode != 0:
                raise RuntimeError(f"could not start {label}: {res.stderr.strip() or res.stdout.strip()}")
            if label == SERVER_LABEL and values.get("AAI_ADMIN_TOKEN"):
                _wait_for_server(port, values["AAI_ADMIN_TOKEN"])
            results[label] = "started"
    return results


def stop_macos(
    *,
    root: Path | None = None,
    services: list[str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    if platform.system() != "Darwin":
        raise RuntimeError("macOS service stop requires Darwin")
    layout = default_layout(root, home)
    targets = _resolve_service_labels(layout, services, is_macos=True)
    targets.sort(key=lambda s: SERVICE_LABELS.index(s), reverse=True)
    domain = f"gui/{os.getuid()}"
    results: dict[str, str] = {}

    for label in targets:
        target = f"{domain}/{label}"
        state = _launchctl("print", target, check=False)
        if state.returncode != 0:
            results[label] = "already stopped"
            continue
        _launchctl("bootout", target, check=False)
        if label == SERVER_LABEL and layout.env.exists():
            port = int(_parse_env(layout.env).get("EDGEDISCO_PORT", "8080"))
            deadline = time.monotonic() + 5
            while _health(port) is not None:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"port {port} is still served after stopping {label}")
                time.sleep(0.1)
        results[label] = "stopped"
    return results


def restart_macos(
    *,
    root: Path | None = None,
    services: list[str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    if platform.system() != "Darwin":
        raise RuntimeError("macOS service restart requires Darwin")
    layout = default_layout(root, home)
    targets = _resolve_service_labels(layout, services, is_macos=True)
    stop_macos(root=root, services=targets, home=home)
    start_macos(root=root, services=targets, home=home)
    return {label: "restarted" for label in targets}


def start_linux(
    *,
    root: Path | None = None,
    services: list[str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    if platform.system() != "Linux":
        raise RuntimeError("Linux service start requires Linux")
    require_systemd_user()
    layout = default_layout(root, home)
    targets = _resolve_service_labels(layout, services, is_macos=False)
    targets.sort(key=lambda s: SERVICE_LABELS.index(s))
    _systemctl("daemon-reload", check=False)
    results: dict[str, str] = {}
    values = _parse_env(layout.env) if layout.env.exists() else {}
    port = int(values.get("EDGEDISCO_PORT", "8080"))

    for label in targets:
        unit = f"{label}.service"
        state = _systemctl("is-active", unit, check=False)
        if state.stdout.strip() == "active":
            if label == SERVER_LABEL:
                if _health(port) is not None:
                    results[label] = "already running"
                    continue
                _systemctl("restart", unit, check=False)
                if values.get("AAI_ADMIN_TOKEN"):
                    _wait_for_server(port, values["AAI_ADMIN_TOKEN"])
                results[label] = "started"
            else:
                results[label] = "already running"
        else:
            res = _systemctl("start", unit, check=False)
            if res.returncode != 0:
                raise RuntimeError(f"could not start {label}: {res.stderr.strip() or res.stdout.strip()}")
            if label == SERVER_LABEL and values.get("AAI_ADMIN_TOKEN"):
                _wait_for_server(port, values["AAI_ADMIN_TOKEN"])
            results[label] = "started"
    return results


def stop_linux(
    *,
    root: Path | None = None,
    services: list[str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    if platform.system() != "Linux":
        raise RuntimeError("Linux service stop requires Linux")
    require_systemd_user()
    layout = default_layout(root, home)
    targets = _resolve_service_labels(layout, services, is_macos=False)
    targets.sort(key=lambda s: SERVICE_LABELS.index(s), reverse=True)
    results: dict[str, str] = {}

    for label in targets:
        unit = f"{label}.service"
        state = _systemctl("is-active", unit, check=False)
        is_active = (state.stdout.strip() == "active")
        _systemctl("stop", unit, check=False)
        if label == SERVER_LABEL and layout.env.exists():
            port = int(_parse_env(layout.env).get("EDGEDISCO_PORT", "8080"))
            deadline = time.monotonic() + 5
            while _health(port) is not None:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"port {port} is still served after stopping {label}")
                time.sleep(0.1)
        results[label] = "stopped" if is_active else "already stopped"
    return results


def restart_linux(
    *,
    root: Path | None = None,
    services: list[str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    if platform.system() != "Linux":
        raise RuntimeError("Linux service restart requires Linux")
    require_systemd_user()
    layout = default_layout(root, home)
    targets = _resolve_service_labels(layout, services, is_macos=False)
    stop_linux(root=root, services=targets, home=home)
    start_linux(root=root, services=targets, home=home)
    return {label: "restarted" for label in targets}


def start_services(**kwargs: Any) -> dict[str, str]:
    if platform.system() == "Darwin":
        return start_macos(**kwargs)
    if platform.system() == "Linux":
        return start_linux(**kwargs)
    raise RuntimeError("self-service start supports macOS and systemd-based Linux")


def stop_services(**kwargs: Any) -> dict[str, str]:
    if platform.system() == "Darwin":
        return stop_macos(**kwargs)
    if platform.system() == "Linux":
        return stop_linux(**kwargs)
    raise RuntimeError("self-service stop supports macOS and systemd-based Linux")


def restart_services(**kwargs: Any) -> dict[str, str]:
    if platform.system() == "Darwin":
        return restart_macos(**kwargs)
    if platform.system() == "Linux":
        return restart_linux(**kwargs)
    raise RuntimeError("self-service restart supports macOS and systemd-based Linux")


def configure_exporter_service(layout: Layout, values: dict[str, str]) -> None:
    """Enable a separate delivery worker only for explicitly configured egress."""
    from .otlp_config import ExportConfig
    macos = platform.system() == "Darwin"
    service = (layout.launch_agents / f"{EXPORTER_LABEL}.plist" if macos else
               layout.systemd_user / f"{EXPORTER_LABEL}.service")
    if values.get("EDGEDISCO_OTLP_EXPORT_ENABLED") != "true":
        if service.exists():
            if macos:
                target = f"gui/{os.getuid()}/{EXPORTER_LABEL}"
                result = _launchctl("bootout", target, check=False)
                state = _launchctl("print", target, check=False)
                stopped = state.returncode != 0 and "Could not find service" in state.stderr
                # bootout also fails when the job was already unloaded. Only an
                # explicit service-not-found response makes that case safe.
                if result.returncode != 0 and not stopped:
                    raise RuntimeError("Could not stop OTLP exporter; service definition retained")
            else:
                result = _systemctl("disable", "--now", service.name, check=False)
                if result.returncode != 0:
                    raise RuntimeError("Could not stop OTLP exporter; service definition retained")
                state = _systemctl("show", service.name, "--property=ActiveState", "--value", check=False)
                stopped = state.returncode == 0 and state.stdout.strip() in {"inactive", "failed"}
            if not stopped:
                raise RuntimeError("Could not verify OTLP exporter stopped; service definition retained")
            service.unlink()
            if not macos:
                _systemctl("daemon-reload")
        return
    ExportConfig.from_env(values)
    try:
        import opentelemetry.proto.collector.logs.v1.logs_service_pb2  # noqa: F401
    except ImportError:
        raise RuntimeError("OTLP export requires ai-asset-inventory[otlp]") from None
    runner = layout.bin / "run-otlp-export.sh"
    _atomic_write(runner, (
        "#!/bin/sh\nset -a\n"
        f". {shlex.quote(str(layout.env))}\nset +a\n"
        f"exec {shlex.quote(str(_cli_executable(layout)))} otlp-export --db {shlex.quote(str(layout.database))}\n"
    ), 0o700)
    if macos:
        content = plistlib.loads(_plist(EXPORTER_LABEL, [str(runner)],
            layout.logs / "otlp-export.log", layout.logs / "otlp-export.err.log"))
        content["ThrottleInterval"] = 15
        _atomic_write(service, plistlib.dumps(content))
        _restart_service(EXPORTER_LABEL, service)
    else:
        _atomic_write(service, (
            "[Unit]\nDescription=EdgeDisco OTLP Logs exporter\n"
            f"After=network-online.target {SERVER_LABEL}.service\n\n"
            f"[Service]\nType=simple\nExecStart={_systemd_quote(runner)}\n"
            "Restart=always\nRestartSec=15\nNoNewPrivileges=true\nPrivateTmp=true\n"
            f"ProtectSystem=strict\nReadWritePaths={_systemd_quote(layout.root)}\n"
            "\n[Install]\nWantedBy=default.target\n"
        ))
        _restart_systemd_service(service.name)
