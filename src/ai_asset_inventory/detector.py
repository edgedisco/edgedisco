from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import Asset

SIGNATURES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ChatGPT", "OpenAI", ("chatgpt", "openai.chat")),
    ("Claude", "Anthropic", ("claude", "anthropic")),
    ("Cursor", "Anysphere", ("cursor",)),
    ("GitHub Copilot", "GitHub", ("github copilot", "copilot-agent", "copilot chat")),
    ("Windsurf", "Codeium", ("windsurf", "codeium")),
    ("Ollama", "Ollama", ("ollama",)),
    ("LM Studio", "LM Studio", ("lm studio", "lmstudio")),
    ("Jan", "Jan", ("jan.app", "jan.exe", "/jan")),
    ("AnythingLLM", "Mintplex Labs", ("anythingllm",)),
    ("Open WebUI", "Open WebUI", ("open-webui", "open_webui")),
    ("LocalAI", "LocalAI", ("local-ai", "localai")),
    ("Dify", "Dify", ("dify",)),
    ("CrewAI", "CrewAI", ("crewai",)),
    ("AutoGen", "Microsoft", ("autogen", "agentchat")),
    ("LangGraph/LangChain", "LangChain", ("langgraph", "langchain")),
    ("MCP Server", "Unknown", ("mcp-server", "mcp_server", "@modelcontextprotocol", "server-filesystem", "server-postgres", "server-github")),
)

AGENT_RUNTIME_NAMES = {"CrewAI", "AutoGen", "LangGraph/LangChain", "MCP Server", "Dify"}
HOST_APP_NAMES = {"ChatGPT", "Claude", "Cursor", "GitHub Copilot", "Windsurf", "LM Studio", "AnythingLLM", "Open WebUI"}

SENSITIVE_ARG = re.compile(
    r"(?i)(token|secret|password|passwd|api[-_]?key|authorization|cookie|credential)"
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _classify(text: str) -> tuple[str, str] | None:
    lowered = text.lower()
    for name, vendor, needles in SIGNATURES:
        if any(needle in lowered for needle in needles):
            return name, vendor
    return None


def _safe_command_fingerprint(parts: Iterable[str]) -> str:
    safe: list[str] = []
    redact_next = False
    for part in parts:
        if redact_next:
            safe.append("[REDACTED]")
            redact_next = False
            continue
        if SENSITIVE_ARG.search(part):
            if "=" in part:
                safe.append(part.split("=", 1)[0] + "=[REDACTED]")
            else:
                safe.append(part)
                redact_next = True
        elif len(part) > 200:
            safe.append("[LONG_VALUE]")
        else:
            safe.append(part)
    return digest("\0".join(safe))


@dataclass(frozen=True)
class ProcessObservation:
    pid: int | None
    ppid: int | None
    executable: str
    args: list[str]


def _process_rows() -> list[ProcessObservation]:
    system = platform.system()
    rows: list[ProcessObservation] = []
    try:
        if system == "Windows":
            script = (
                "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine "
                "| ConvertTo-Json -Compress"
            )
            raw = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=15, check=True,
            ).stdout
            parsed = json.loads(raw or "[]")
            for item in parsed if isinstance(parsed, list) else [parsed]:
                cmd = str(item.get("CommandLine") or "")
                rows.append(ProcessObservation(item.get("ProcessId"), item.get("ParentProcessId"), str(item.get("Name") or ""), [cmd]))
        else:
            raw = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,comm=,args="], capture_output=True,
                text=True, timeout=15, check=True,
            ).stdout
            for line in raw.splitlines():
                match = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
                if match:
                    rows.append(ProcessObservation(int(match.group(1)), int(match.group(2)), match.group(3), [match.group(4)]))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    return rows


def scan_processes() -> list[Asset]:
    found: dict[str, Asset] = {}
    rows = _process_rows()
    classifications: dict[int, tuple[str, str]] = {}
    for row in rows:
        classified = _classify(_process_classification_text(row))
        if classified and row.pid is not None:
            classifications[row.pid] = classified

    for row in rows:
        executable, args = row.executable, row.args
        executable_name = Path(executable).name
        text = _process_classification_text(row)
        classified = _classify(text)
        if not classified:
            continue
        name, vendor = classified
        fp = digest(f"process:{name}:{executable_name.lower()}")
        found[fp] = Asset(
            fingerprint=fp,
            kind="process",
            name=name,
            vendor=vendor,
            running=True,
            path_hash=digest(executable),
            command_hash=_safe_command_fingerprint(args),
            metadata={"executable": executable_name},
        )
    found.update(_agent_runtime_assets(rows, classifications))
    return list(found.values())


def _process_classification_text(row: ProcessObservation) -> str:
    executable_name = Path(row.executable).name
    generic_runtimes = {"python", "python3", "node", "node.exe", "npx", "npx.exe", "uvx", "docker"}
    shell_names = {"sh", "bash", "zsh", "fish", "cmd.exe", "powershell.exe", "pwsh.exe"}
    if executable_name.lower() in shell_names:
        return executable_name
    if executable_name.lower() in generic_runtimes:
        return f"{executable_name} {' '.join(row.args)}"
    return row.executable


def _host_app(row: ProcessObservation, by_pid: dict[int, ProcessObservation], classifications: dict[int, tuple[str, str]]) -> str | None:
    parent = row.ppid
    visited: set[int] = set()
    for _ in range(8):
        if parent is None or parent in visited:
            return None
        visited.add(parent)
        classified = classifications.get(parent)
        if classified and classified[0] in HOST_APP_NAMES:
            return classified[0]
        ancestor = by_pid.get(parent)
        if ancestor is None:
            return None
        parent = ancestor.ppid
    return None


def _agent_runtime_assets(rows: list[ProcessObservation], classifications: dict[int, tuple[str, str]]) -> dict[str, Asset]:
    by_pid = {row.pid: row for row in rows if row.pid is not None}
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.pid is None:
            continue
        classified = classifications.get(row.pid)
        if not classified or classified[0] not in AGENT_RUNTIME_NAMES:
            continue
        name, vendor = classified
        host = _host_app(row, by_pid, classifications) or "Direct/local"
        executable_name = Path(row.executable).name
        command_hash = _safe_command_fingerprint(row.args)
        key = (name, host, executable_name.lower(), command_hash)
        current = grouped.setdefault(key, {"count": 0, "vendor": vendor})
        current["count"] += 1

    assets: dict[str, Asset] = {}
    for (name, host, executable_name, command_hash), state in grouped.items():
        fp = digest(f"agent_runtime:{name}:{host}:{executable_name}:{command_hash}")
        assets[fp] = Asset(
            fingerprint=fp,
            kind="agent_runtime",
            name=name,
            vendor=state["vendor"],
            running=True,
            command_hash=command_hash,
            metadata={
                "host_app": host,
                "runtime": executable_name,
                "instance_count": state["count"],
                "relationship": "spawned_by" if host != "Direct/local" else "local_process",
            },
        )
    return assets


def _mac_bundle_version(app_path: Path) -> str | None:
    info = app_path / "Contents" / "Info.plist"
    try:
        with info.open("rb") as handle:
            data = plistlib.load(handle)
        return str(data.get("CFBundleShortVersionString") or data.get("CFBundleVersion") or "") or None
    except (OSError, plistlib.InvalidFileException):
        return None


def _installed_candidates() -> Iterable[tuple[str, Path, str | None]]:
    system = platform.system()
    if system == "Darwin":
        roots = [Path("/Applications"), Path.home() / "Applications"]
        for root in roots:
            if root.exists():
                for path in root.glob("*.app"):
                    yield path.stem, path, _mac_bundle_version(path)
    elif system == "Windows":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")]
        for raw_root in roots:
            if not raw_root:
                continue
            root = Path(raw_root)
            if root.exists():
                for path in root.glob("*AI*"):
                    yield path.name, path, None
    else:
        roots = [Path("/usr/share/applications"), Path.home() / ".local/share/applications"]
        for root in roots:
            if root.exists():
                for path in root.glob("*.desktop"):
                    try:
                        text = path.read_text(errors="replace")[:32_768]
                    except OSError:
                        continue
                    name_match = re.search(r"^Name=(.+)$", text, re.MULTILINE)
                    yield name_match.group(1) if name_match else path.stem, path, None


def scan_installed_apps() -> list[Asset]:
    assets: list[Asset] = []
    for display_name, path, version in _installed_candidates():
        classified = _classify(display_name)
        if not classified:
            continue
        name, vendor = classified
        fp = digest(f"application:{name}:{path.name.lower()}")
        assets.append(Asset(
            fingerprint=fp, kind="application", name=name, vendor=vendor,
            running=False, version=version, path_hash=digest(str(path)),
            metadata={"package": path.name},
        ))
    return assets


def _mcp_paths() -> Iterable[tuple[str, Path]]:
    home = Path.home()
    system = platform.system()
    candidates = [
        ("Claude Desktop", home / ".config/Claude/claude_desktop_config.json"),
        ("Cursor", home / ".cursor/mcp.json"),
        ("VS Code", home / ".vscode/mcp.json"),
    ]
    if system == "Darwin":
        candidates.append(("Claude Desktop", home / "Library/Application Support/Claude/claude_desktop_config.json"))
        candidates.append(("VS Code", home / "Library/Application Support/Code/User/mcp.json"))
    elif system == "Windows":
        appdata = Path(os.environ.get("APPDATA", str(home)))
        candidates.append(("Claude Desktop", appdata / "Claude/claude_desktop_config.json"))
        candidates.append(("VS Code", appdata / "Code/User/mcp.json"))
    for owner, path in candidates:
        if path.exists():
            yield owner, path


def scan_mcp_configs() -> list[Asset]:
    assets: list[Asset] = []
    for owner, path in _mcp_paths():
        try:
            data: Any = json.loads(path.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        if not isinstance(servers, dict):
            continue
        for server_name, config in servers.items():
            if not isinstance(config, dict):
                config = {}
            command = Path(str(config.get("command", "unknown"))).name
            transport = "remote" if config.get("url") else "stdio"
            fp = digest(f"mcp:{owner}:{server_name}")
            assets.append(Asset(
                fingerprint=fp, kind="mcp_server", name=str(server_name)[:128],
                vendor="Unknown", running=False, path_hash=digest(str(path)),
                command_hash=digest(command),
                metadata={"configured_in": owner, "transport": transport, "executable": command},
            ))
    return assets


def collect_inventory() -> list[Asset]:
    merged: dict[str, Asset] = {}
    for asset in [*scan_installed_apps(), *scan_mcp_configs(), *scan_processes()]:
        current = merged.get(asset.fingerprint)
        if current and asset.running and not current.running:
            merged[asset.fingerprint] = asset
        elif current is None:
            merged[asset.fingerprint] = asset
    return sorted(merged.values(), key=lambda item: (item.kind, item.name.lower()))
