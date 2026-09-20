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


def _managed_hook_identity(command: str) -> tuple[str, str] | None:
    if not isinstance(command, str):
        return None
    try:
        parts = shlex.split(command, posix=os.name != "nt")
        module = parts.index("-m")
        if parts[module + 1:module + 4] != ["ai_asset_inventory", "hook", "emit"]:
            return None
        return (parts[parts.index("--app") + 1], parts[parts.index("--event") + 1])
    except (ValueError, IndexError, TypeError):
        return None


def _same_managed_hook(existing: str, replacement: str) -> bool:
    key = _managed_hook_identity(existing)
    return key is not None and key == _managed_hook_identity(replacement)


def _append_unique(items: list[dict[str, Any]], definition: dict[str, Any]) -> None:
    command = definition.get("command") or definition.get("bash")
    items[:] = [item for item in items if not isinstance(item, dict) or
                not _same_managed_hook(item.get("command") or item.get("bash"), command)]
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
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                group["hooks"][:] = [hook for hook in group["hooks"] if not isinstance(hook, dict)
                                     or not _same_managed_hook(hook.get("command"), command)]
        groups[:] = [group for group in groups if not isinstance(group, dict) or group.get("hooks") != []]
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
        _append_unique(entries, definition)
    _write(target, data)
    return target


def install_adapters(config_path: Path, apps: list[str]) -> list[Path]:
    installers = {"cursor": install_cursor, "claude-code": install_claude, "github-copilot": install_copilot}
    return [installers[app](config_path) for app in apps]


def _remove_cursor_hooks(path: Path) -> bool:
    if not path.exists():
        return False
    data = _load(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        retained = [
            item for item in entries
            if not isinstance(item, dict) or _managed_hook_identity(item.get("command")) is None
        ]
        changed = changed or retained != entries
        if retained:
            hooks[event] = retained
        elif retained != entries:
            del hooks[event]
    if changed:
        _write(path, data)
    return changed


def _remove_claude_hooks(path: Path) -> bool:
    if not path.exists():
        return False
    data = _load(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        retained_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                retained_groups.append(group)
                continue
            retained = [
                hook for hook in group["hooks"]
                if not isinstance(hook, dict) or _managed_hook_identity(hook.get("command")) is None
            ]
            changed = changed or retained != group["hooks"]
            if retained:
                retained_groups.append({**group, "hooks": retained})
        if retained_groups:
            hooks[event] = retained_groups
        elif retained_groups != groups:
            del hooks[event]
    if changed:
        _write(path, data)
    return changed


def uninstall_adapters(home: Path | None = None) -> list[Path]:
    """Remove only EdgeDisco-managed hooks, preserving every unrelated hook."""
    home = home or Path.home()
    removed: list[Path] = []
    cursor = home / ".cursor/hooks.json"
    claude = home / ".claude/settings.json"
    copilot = home / ".copilot/hooks/edgedisco.json"
    if _remove_cursor_hooks(cursor):
        removed.append(cursor)
    if _remove_claude_hooks(claude):
        removed.append(claude)
    if copilot.exists():
        data = _load(copilot)
        hooks = data.get("hooks")
        changed = False
        if isinstance(hooks, dict):
            for event in list(hooks):
                entries = hooks[event]
                if not isinstance(entries, list):
                    continue
                retained = [item for item in entries if not isinstance(item, dict) or all(
                    _managed_hook_identity(item.get(key)) is None for key in ("bash", "powershell")
                )]
                changed = changed or retained != entries
                if retained:
                    hooks[event] = retained
                elif retained != entries:
                    del hooks[event]
        if changed:
            if hooks:
                _write(copilot, data)
            else:
                copilot.unlink()
            removed.append(copilot)
    return removed
