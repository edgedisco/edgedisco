from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .fingerprint_library import AGENT_FINGERPRINTS, LIBRARY_VERSION, known_binary, sha256_file
from .models import Asset
from .path_policy import allowed_path

_APPLICATION_SIGNATURES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
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

AGENT_CLI_SIGNATURES = tuple(
    (entry.name, entry.vendor, entry.executables, entry.package_markers)
    for entry in AGENT_FINGERPRINTS
)
SIGNATURES = tuple(
    (entry.name, entry.vendor, entry.display_markers) for entry in AGENT_FINGERPRINTS
) + _APPLICATION_SIGNATURES

AGENT_RUNTIME_NAMES = {
    "CrewAI", "AutoGen", "LangGraph/LangChain", "MCP Server", "Dify",
    *(name for name, _vendor, _executables, _markers in AGENT_CLI_SIGNATURES),
}
HOST_APP_NAMES = {
    "ChatGPT", "Claude", "Cursor", "GitHub Copilot", "Windsurf", "LM Studio",
    "AnythingLLM", "Open WebUI", *AGENT_RUNTIME_NAMES,
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _classify(text: str) -> tuple[str, str] | None:
    lowered = text.lower()
    for name, vendor, needles in SIGNATURES:
        if any(_contains_signature(lowered, needle) for needle in needles):
            return name, vendor
    return None


def _contains_signature(text: str, needle: str) -> bool:
    """Match short product names as tokens instead of arbitrary substrings."""
    if needle.isalnum():
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", text))
    return needle in text


def _safe_command_fingerprint(parts: Iterable[str]) -> str:
    raw_parts = list(parts)
    if len(raw_parts) == 1:
        try:
            raw_parts = shlex.split(raw_parts[0], posix=platform.system() != "Windows")
        except ValueError:
            raw_parts = [raw_parts[0].split(None, 1)[0]]
    if not raw_parts:
        return digest("")
    runtime = Path(raw_parts[0]).name.lower()
    identity_index = _identity_token_index(raw_parts, runtime)

    safe = [runtime]
    for index, part in enumerate(raw_parts[1:], 1):
        if index == identity_index:
            safe.append(part.lower().replace("\\", "/"))
        elif part.startswith("--"):
            safe.append(part.split("=", 1)[0] + ("=[REDACTED]" if "=" in part else ""))
        elif part.startswith("-"):
            safe.append(part[:2])
        else:
            safe.append("[ARG]")
    return digest("\0".join(safe))


@dataclass(frozen=True)
class ProcessObservation:
    pid: int | None
    ppid: int | None
    executable: str
    args: list[str]


class ProcessScanUnavailable(RuntimeError):
    """A failed process snapshot must never be interpreted as stopped processes."""


def _process_rows() -> list[ProcessObservation]:
    system = platform.system()
    rows: list[ProcessObservation] = []
    try:
        if system == "Windows":
            script = (
                "Get-CimInstance Win32_Process | ForEach-Object { "
                "$o=Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction SilentlyContinue; "
                "if ($o.User -eq $env:USERNAME) { $_ } } | "
                "Select-Object ProcessId,ParentProcessId,Name,CommandLine "
                "| ConvertTo-Json -Compress"
            )
            raw = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=15, check=True,
            ).stdout
            if not raw.strip():
                raise ProcessScanUnavailable("process enumeration returned no output")
            parsed = json.loads(raw)
            if not isinstance(parsed, (list, dict)):
                raise ProcessScanUnavailable("invalid process enumeration")
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if not isinstance(item, dict) or type(item.get("ProcessId")) is not int:
                    raise ProcessScanUnavailable("invalid process row")
                cmd = str(item.get("CommandLine") or "")
                rows.append(ProcessObservation(item.get("ProcessId"), item.get("ParentProcessId"), str(item.get("Name") or ""), [cmd]))
        else:
            raw = subprocess.run(
                ["ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,comm=,args="], capture_output=True,
                text=True, timeout=15, check=True,
            ).stdout
            for line in raw.splitlines():
                match = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
                if match:
                    rows.append(ProcessObservation(int(match.group(1)), int(match.group(2)), match.group(3), [match.group(4)]))
                elif line.strip():
                    raise ProcessScanUnavailable("malformed process row")
            if not rows:
                raise ProcessScanUnavailable("process enumeration returned no rows")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ProcessScanUnavailable("current-user process enumeration failed") from exc
    return rows


def scan_processes() -> list[Asset]:
    found: dict[str, Asset] = {}
    rows = _process_rows()
    classifications: dict[int, tuple[str, str]] = {}
    for row in rows:
        classified = _classify_process(row)
        if classified and row.pid is not None:
            classifications[row.pid] = classified

    for row in rows:
        executable, args = row.executable, row.args
        executable_name = Path(executable).name
        classified = classifications.get(row.pid) if row.pid is not None else _classify_process(row)
        if not classified:
            continue
        name, vendor = classified
        path_hash = digest(executable)
        binary_path = _process_binary_candidate(row)
        binary_sha256, binary_status = _binary_evidence(name, binary_path)
        fp = digest(f"process:{name}:{path_hash}")
        found[fp] = Asset(
            fingerprint=fp,
            kind="process",
            name=name,
            vendor=vendor,
            running=True,
            path_hash=path_hash,
            command_hash=_safe_command_fingerprint(args),
            binary_sha256=binary_sha256,
            binary_fingerprint_status=binary_status,
            fingerprint_library_version=LIBRARY_VERSION if binary_sha256 else None,
            metadata={"executable": executable_name},
        )
    found.update(_agent_runtime_assets(rows, classifications))
    return list(found.values())


_GENERIC_RUNTIMES = {"python", "python3", "node", "node.exe", "npx", "npx.exe", "uvx", "docker"}
_VERSIONED_PYTHON = re.compile(r"^python\d+(\.\d+)*$")
_DOCKER_BOOLEAN_OPTIONS = {
    "-d", "--detach", "-i", "--interactive", "--init", "--privileged",
    "--read-only", "--rm", "-t", "--tty",
}


def _is_generic_runtime_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in _GENERIC_RUNTIMES or bool(_VERSIONED_PYTHON.match(lowered))


def _identity_token_index(parts: list[str], runtime: str) -> int | None:
    """Locate only the module, script, package, or image token of a runtime."""
    python = runtime.startswith("python")
    node = runtime in {"node", "node.exe"}
    runner = runtime in {"npx", "npx.exe", "uvx"}
    if python or node or runner:
        options_with_values = ({"-W", "-X"} if python else
            {"-r", "--require", "--import", "--loader", "--experimental-loader", "--conditions", "-C"}
            if node else {"--cache", "--registry", "--package", "-p", "--from", "--with", "--python", "--index-url"})
        boolean_options = ({"-B", "-E", "-I", "-s", "-S", "-u", "-v", "-O", "-OO", "-q"} if python else
            {"--no-warnings", "--enable-source-maps", "--inspect", "--inspect-brk"} if node else
            {"-y", "--yes", "--no-install", "--offline", "--no-cache"})
        index = 1
        while index < len(parts):
            part = parts[index]
            if part == "--":
                return index + 1 if index + 1 < len(parts) else None
            if part == "-" or (python and part.startswith("-c")) or (node and part in {"-e", "--eval", "-p", "--print"}):
                return None
            if python and part == "-m":
                return index + 1 if index + 1 < len(parts) else None
            if not part.startswith("-"):
                return index
            if part in options_with_values:
                index += 2
            elif part in boolean_options or ("=" in part and part.split("=", 1)[0] in options_with_values):
                index += 1
            elif python and (part.startswith("-W") or part.startswith("-X")):
                index += 1
            else:
                # Unknown interpreter options are ambiguous; do not inspect
                # their values or later application arguments for identities.
                return None
        return None
    if runtime == "docker" and "run" in parts:
        skip_value = False
        for index in range(parts.index("run") + 1, len(parts)):
            part = parts[index]
            if skip_value:
                skip_value = False
            elif part.startswith("-"):
                skip_value = "=" not in part and part not in _DOCKER_BOOLEAN_OPTIONS
            else:
                return index
    return None


def _args_leading_executable_name(args: list[str]) -> str | None:
    """Return the basename of the executable represented at the start of args.

    On Unix, ``ps`` often stores the full command line as a single args entry.
    Tests and some platforms may already provide discrete argv tokens.
    """
    if not args:
        return None
    first = args[0].strip().strip("\"'")
    if not first:
        return None
    token = first.split(None, 1)[0].strip("\"'")
    if not token:
        return None
    return Path(token).name


def _process_identity_text(row: ProcessObservation) -> str:
    """Return executable/module identity while excluding prompts and arguments."""
    try:
        parts = shlex.split(" ".join(row.args), posix=platform.system() != "Windows")
    except ValueError:
        parts = []
    executable = Path(row.executable).name.lower()
    if not parts:
        return executable
    leading = Path(parts[0]).name.lower()
    runtime = leading if _is_generic_runtime_name(leading) else executable
    identity = [runtime]
    identity_index = _identity_token_index(parts, runtime)
    if identity_index is not None:
        identity.append(parts[identity_index])
    return " ".join(identity).lower().replace("\\", "/")


def _classify_process(row: ProcessObservation) -> tuple[str, str] | None:
    """Classify strong executable/package signals without matching prompt text."""
    executable_name = Path(row.executable).name.lower()
    leading = (_args_leading_executable_name(row.args) or "").lower()
    effective = leading if _is_generic_runtime_name(leading) else executable_name
    for name, vendor, executables, _markers in AGENT_CLI_SIGNATURES:
        if effective in executables or executable_name in executables or leading in executables:
            return name, vendor
    if _is_generic_runtime_name(effective):
        command = _process_identity_text(row)
        for name, vendor, _executables, markers in AGENT_CLI_SIGNATURES:
            if any(marker in command for marker in markers):
                return name, vendor
        classified = _classify(command)
        if classified and classified[0] in {"CrewAI", "AutoGen", "LangGraph/LangChain", "MCP Server", "Dify"}:
            return classified
        return None
    return _classify(row.executable)


def _process_binary_candidate(row: ProcessObservation) -> Path | None:
    executable_name = Path(row.executable).name.lower()
    known_executables = {
        executable for _name, _vendor, executables, _markers in AGENT_CLI_SIGNATURES
        for executable in executables
    }
    if executable_name in known_executables or not _is_generic_runtime_name(executable_name):
        return Path(row.executable)
    try:
        parts = shlex.split(" ".join(row.args), posix=platform.system() != "Windows")
    except ValueError:
        return None
    if parts and Path(parts[0]).name.lower() in known_executables:
        return Path(parts[0])
    return None


def _host_app(
    row: ProcessObservation,
    by_pid: dict[int, ProcessObservation],
    classifications: dict[int, tuple[str, str]],
    runtime_name: str,
) -> str | None:
    parent = row.ppid
    visited: set[int] = set()
    for _ in range(8):
        if parent is None or parent in visited:
            return None
        visited.add(parent)
        classified = classifications.get(parent)
        if classified and classified[0] in HOST_APP_NAMES and classified[0] != runtime_name:
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
        host = _host_app(row, by_pid, classifications, name) or "Direct/local"
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
    info = allowed_path(app_path / "Contents" / "Info.plist", _application_roots())
    if info is None:
        return None
    try:
        with info.open("rb") as handle:
            data = plistlib.load(handle)
        return str(data.get("CFBundleShortVersionString") or data.get("CFBundleVersion") or "") or None
    except (OSError, plistlib.InvalidFileException, AttributeError):
        return None


def _application_roots() -> tuple[Path, ...]:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return (Path("/Applications"), home / "Applications")
    if system == "Windows":
        localappdata = os.environ.get("LOCALAPPDATA")
        roots = [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            str(Path(localappdata) / "Programs") if localappdata else None,
        ]
        return tuple(Path(root) for root in roots if root)
    return (Path("/usr/share/applications"), home / ".local/share/applications")


def _installed_candidates() -> Iterable[tuple[str, Path, str | None]]:
    system = platform.system()
    if system == "Darwin":
        for root in _application_roots():
            if allowed_path(root, _application_roots()) is not None:
                for path in root.glob("*.app"):
                    if allowed_path(path, _application_roots()) is None:
                        continue
                    yield path.stem, path, _mac_bundle_version(path)
    elif system == "Windows":
        for root in _application_roots():
            if allowed_path(root, _application_roots()) is not None:
                try:
                    for path in root.iterdir():
                        yield path.name, path, None
                except OSError:
                    continue
    else:
        for root in _application_roots():
            if allowed_path(root, _application_roots()) is not None:
                for path in root.glob("*.desktop"):
                    path = allowed_path(path, _application_roots())
                    if path is None:
                        continue
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
        path_hash = digest(str(path))
        binary_path = _mac_bundle_executable(path) if platform.system() == "Darwin" else None
        binary_sha256, binary_status = _binary_evidence(name, binary_path)
        fp = digest(f"application:{name}:{path_hash}")
        assets.append(Asset(
            fingerprint=fp, kind="application", name=name, vendor=vendor,
            running=False, version=version, path_hash=path_hash,
            binary_sha256=binary_sha256,
            binary_fingerprint_status=binary_status,
            fingerprint_library_version=LIBRARY_VERSION if binary_sha256 else None,
            metadata={"package": path.name},
        ))
    return assets


def _executable_roots() -> tuple[Path, ...]:
    home = Path.home()
    roots = [
        home / ".local/bin",
        home / ".cargo/bin",
        home / ".bun/bin",
        home / ".local/share/pnpm",
        home / ".opencode/bin",
        home / "bin",
        home / "go/bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    if platform.system() == "Windows":
        appdata = Path(os.environ.get("APPDATA", str(home)))
        localappdata = Path(os.environ.get("LOCALAPPDATA", str(home)))
        roots.extend((appdata / "npm", localappdata / "Programs"))
    return tuple(roots)


def _hash_roots() -> tuple[Path, ...]:
    home = Path.home()
    roots = [*_executable_roots()]
    system = platform.system()
    if system == "Darwin":
        roots.extend((Path("/Applications"), home / "Applications", Path("/opt/homebrew"), Path("/usr/local")))
    elif system == "Windows":
        for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
            if os.environ.get(name):
                roots.append(Path(os.environ[name]))
    else:
        roots.extend((Path("/usr/bin"), Path("/usr/local"), Path("/opt"), home / ".local"))
    return tuple(dict.fromkeys(Path(os.path.abspath(root)) for root in roots))


def _safe_binary_path(path: Path | None) -> Path | None:
    return allowed_path(path, _hash_roots()) if path is not None else None


def _binary_evidence(name: str, path: Path | None) -> tuple[str | None, str | None]:
    safe_path = _safe_binary_path(path)
    if safe_path is None:
        return None, None
    sha256 = sha256_file(safe_path)
    if sha256 is None:
        return None, None
    return sha256, "matched" if known_binary(name, sha256) else "unlisted"


def _mac_bundle_executable(app_path: Path) -> Path | None:
    info = allowed_path(app_path / "Contents" / "Info.plist", _application_roots())
    if info is None:
        return None
    try:
        with info.open("rb") as handle:
            executable = plistlib.load(handle).get("CFBundleExecutable")
    except (OSError, plistlib.InvalidFileException, AttributeError):
        return None
    if not isinstance(executable, str) or not executable or Path(executable).name != executable:
        return None
    return app_path / "Contents" / "MacOS" / executable


def _installed_cli_candidates() -> Iterable[tuple[str, str, Path]]:
    """Find allowlisted CLI entry points without recursively crawling the disk."""
    seen: set[str] = set()
    roots = _executable_roots()
    windows = platform.system() == "Windows"
    for name, vendor, executables, _markers in AGENT_CLI_SIGNATURES:
        for executable in executables:
            candidates: list[Path] = []
            candidates.extend(root / executable for root in roots)
            if windows:
                candidates.extend(
                    path for root in roots if allowed_path(root, _hash_roots()) is not None
                    for path in root.glob(f"*/{executable}")
                )
            for path in candidates:
                try:
                    safe_path = allowed_path(path, _hash_roots())
                    if safe_path is None:
                        continue
                    key = str(safe_path)
                    if key in seen or not safe_path.is_file():
                        continue
                except OSError:
                    continue
                seen.add(key)
                yield name, vendor, path


def scan_installed_clis() -> list[Asset]:
    assets: list[Asset] = []
    for name, vendor, path in _installed_cli_candidates():
        package = path.name
        path_hash = digest(str(path))
        binary_sha256, binary_status = _binary_evidence(name, path)
        assets.append(Asset(
            fingerprint=digest(f"agent_cli:{name}:{path_hash}"),
            kind="application",
            name=name,
            vendor=vendor,
            running=False,
            path_hash=path_hash,
            binary_sha256=binary_sha256,
            binary_fingerprint_status=binary_status,
            fingerprint_library_version=LIBRARY_VERSION if binary_sha256 else None,
            metadata={"package": package, "discovery_source": "Executable search path"},
        ))
    return assets


def _mcp_candidates() -> tuple[tuple[str, Path], ...]:
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
    return tuple(candidates)


def _mcp_paths() -> Iterable[tuple[str, Path]]:
    roots = tuple(path.parent for _owner, path in _mcp_candidates())
    for owner, path in _mcp_candidates():
        safe_path = allowed_path(path, roots)
        if safe_path is not None:
            yield owner, safe_path


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


def _merge_inventory(assets: Iterable[Asset]) -> list[Asset]:
    merged: dict[str, Asset] = {}
    for asset in assets:
        current = merged.get(asset.fingerprint)
        if current and asset.running and not current.running:
            merged[asset.fingerprint] = asset
        elif current is None:
            merged[asset.fingerprint] = asset
    return sorted(merged.values(), key=lambda item: (item.kind, item.name.lower()))


def scan_static_inventory() -> list[Asset]:
    """Scan evidence that changes much less often than the process table."""
    return _merge_inventory((*scan_installed_apps(), *scan_installed_clis(), *scan_mcp_configs()))


def _static_watch_paths() -> tuple[Path, ...]:
    """Return bounded paths whose metadata cheaply signals a likely static change."""
    paths = [*_application_roots(), *_executable_roots()]
    for _owner, path in _mcp_candidates():
        paths.extend((path, path.parent))
    return tuple(dict.fromkeys(paths))


def _static_revision() -> tuple[tuple[str, int | None, int | None, int | None, int | None], ...]:
    revision = []
    roots = (*_application_roots(), *_hash_roots(),
             *(p.parent for _owner, p in _mcp_candidates()))
    for path in _static_watch_paths():
        try:
            safe_path = allowed_path(path, roots)
            if safe_path is None:
                raise OSError("unavailable path")
            state = safe_path.stat()
            revision.append((str(path), state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns))
        except OSError:
            revision.append((str(path), None, None, None, None))
    return tuple(revision)


class InventoryScanner:
    """Refresh processes every call while incrementally caching static evidence."""

    def __init__(self, static_refresh_seconds: int = 900, *, clock=None):
        self.static_refresh_seconds = max(60, int(static_refresh_seconds))
        self._clock = clock or time.monotonic
        self._static_assets: tuple[Asset, ...] | None = None
        self._cached_revision = None
        self._last_static_scan = 0.0

    def collect_inventory(self, *, force_static: bool = False) -> list[Asset]:
        now = self._clock()
        revision = _static_revision()
        expired = now - self._last_static_scan >= self.static_refresh_seconds
        if force_static or self._static_assets is None or revision != self._cached_revision or expired:
            self._static_assets = tuple(scan_static_inventory())
            self._cached_revision = revision
            self._last_static_scan = now
        return _merge_inventory((*self._static_assets, *scan_processes()))


def collect_inventory() -> list[Asset]:
    """Perform a complete one-shot scan for CLI commands and diagnostics."""
    return _merge_inventory((*scan_static_inventory(), *scan_processes()))
