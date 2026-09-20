from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


CURSOR_EVENTS = (
    "sessionStart", "sessionEnd", "postToolUse", "postToolUseFailure",
    "subagentStart", "subagentStop",
)
CLAUDE_EVENTS = (
    "SessionStart", "SessionEnd", "PostToolUse", "PostToolUseFailure",
    "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted",
)
COPILOT_EVENTS = ("sessionStart", "sessionEnd", "postToolUse")


def _command(config_path: Path, app: str, event: str, protocol: str) -> str:
    parts = [
        sys.executable, "-m", "ai_asset_inventory", "hook", "emit",
        "--config", str(config_path.resolve()), "--app", app,
        "--event", event, "--protocol", protocol,
    ]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cannot update invalid JSON file: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"configuration must contain a JSON object: {path}")
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".edgedisco.bak")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
            if os.name != "nt":
                backup.chmod(0o600)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)


def _append_unique(items: list[dict[str, Any]], definition: dict[str, Any]) -> None:
    command = definition.get("command") or definition.get("bash")
    if not any((item.get("command") or item.get("bash")) == command for item in items if isinstance(item, dict)):
        items.append(definition)


def install_cursor(config_path: Path, target: Path | None = None) -> Path:
    target = target or Path.home() / ".cursor/hooks.json"
    data = _load(target)
    data["version"] = max(1, int(data.get("version", 1)))
    hooks = data.setdefault("hooks", {})
    for event in CURSOR_EVENTS:
        entries = hooks.setdefault(event, [])
        _append_unique(entries, {"command": _command(config_path, "cursor", event, "cursor"), "timeout": 5})
    _write(target, data)
    return target


def install_claude(config_path: Path, target: Path | None = None) -> Path:
    target = target or Path.home() / ".claude/settings.json"
    data = _load(target)
    hooks = data.setdefault("hooks", {})
    for event in CLAUDE_EVENTS:
        groups = hooks.setdefault(event, [])
        command = _command(config_path, "claude-code", event, "silent")
        if not any(command == hook.get("command") for group in groups if isinstance(group, dict)
                   for hook in group.get("hooks", []) if isinstance(hook, dict)):
            groups.append({"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": 5}]})
    _write(target, data)
    return target


def install_copilot(config_path: Path, target: Path | None = None) -> Path:
    target = target or Path.home() / ".copilot/hooks/edgedisco.json"
    data = _load(target)
    data["version"] = 1
    hooks = data.setdefault("hooks", {})
    for event in COPILOT_EVENTS:
        entries = hooks.setdefault(event, [])
        command = _command(config_path, "github-copilot", event, "silent")
        definition = {"type": "command", "bash": command, "powershell": command, "timeoutSec": 5}
        if not any(isinstance(item, dict) and item.get("bash") == command for item in entries):
            entries.append(definition)
    _write(target, data)
    return target


def install_adapters(config_path: Path, apps: list[str]) -> list[Path]:
    installers = {"cursor": install_cursor, "claude-code": install_claude, "github-copilot": install_copilot}
    return [installers[app](config_path) for app in apps]
