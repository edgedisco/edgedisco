from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .detector import digest

MAX_HOOK_INPUT_BYTES = 1_000_000
MAX_SPOOL_BYTES = 10_000_000
MAX_BATCH_EVENTS = 1_000

ALLOWED_APPS = {"cursor", "claude-code", "github-copilot", "sdk"}
SAFE_METADATA_KEYS = {"source_event", "cursor_version", "permission_mode", "source"}
SENSITIVE_KEY = re.compile(
    r"(?i)(prompt|response|message|content|input|output|command|argument|secret|token|password|authorization|cookie|transcript|file_path|cwd)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_string(value: Any, limit: int = 128) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    if not rendered:
        return None
    return rendered[:limit]


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload[key]
    return None


def _hash_value(value: Any) -> str | None:
    rendered = _safe_string(value, 4096)
    return digest(rendered) if rendered else None


def _workspace_hash(payload: dict[str, Any]) -> str | None:
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots:
        return digest("\0".join(sorted(str(item) for item in roots)))
    return _hash_value(_first(payload, "workspace", "project_dir", "cwd"))


def _tool_fields(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    tool = _safe_string(_first(payload, "tool_name", "tool", "toolName"))
    if not tool:
        return None, None
    mcp_server = None
    if tool.startswith("MCP:"):
        parts = tool[4:].split("/", 1)
        mcp_server = _safe_string(parts[0])
    elif tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        if len(parts) > 1:
            mcp_server = _safe_string(parts[1])
    return tool, mcp_server


def _event_status(event_type: str, payload: dict[str, Any]) -> str:
    lowered = event_type.lower()
    explicit = _safe_string(_first(payload, "status", "outcome"), 32)
    if explicit:
        return explicit.lower()
    if "failure" in lowered or "error" in lowered:
        return "failed"
    if lowered in {"sessionend", "subagentstop", "taskcompleted"}:
        return "completed"
    if lowered in {"stop", "notification"}:
        return "idle"
    return "active"


def normalize_hook_event(app: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if app not in ALLOWED_APPS:
        raise ValueError(f"unsupported app: {app}")
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be an object")
    source_event = _safe_string(_first(payload, "hook_event_name", "hookEventName")) or event_type
    session_value = _first(payload, "conversation_id", "session_id", "sessionId", "run_id", "runId")
    if session_value is None:
        session_value = f"{app}:{os.getppid()}"
    agent_value = _first(payload, "subagent_id", "agent_id", "agentId", "task_id", "taskId")
    agent_type = _safe_string(_first(payload, "subagent_type", "agent_type", "agentType", "agent_name", "agentName"))
    tool_name, mcp_server = _tool_fields(payload)
    duration = _first(payload, "duration", "duration_ms", "durationMs")
    try:
        duration_ms = max(0, min(int(duration), 86_400_000)) if duration is not None else None
    except (TypeError, ValueError):
        duration_ms = None
    metadata: dict[str, str] = {"source_event": source_event}
    for key in ("cursor_version", "permission_mode", "source"):
        value = _safe_string(payload.get(key))
        if value:
            metadata[key] = value
    return {
        "event_id": str(uuid.uuid4()),
        "observed_at": utc_now(),
        "app": app,
        "event_type": _safe_string(event_type, 64) or "unknown",
        "session_hash": digest(str(session_value)),
        "agent_hash": digest(str(agent_value)) if agent_value is not None else "root",
        "agent_type": agent_type,
        "tool_name": tool_name,
        "mcp_server": mcp_server,
        "model": _safe_string(_first(payload, "model_id", "model", "model_name", "modelName")),
        "status": _event_status(event_type, payload),
        "duration_ms": duration_ms,
        "workspace_hash": _workspace_hash(payload),
        "user_hash": _hash_value(_first(payload, "user_email", "user_id", "userId")),
        "metadata": metadata,
    }


def validate_normalized_event(event: dict[str, Any]) -> None:
    allowed = {
        "event_id", "observed_at", "app", "event_type", "session_hash", "agent_hash",
        "agent_type", "tool_name", "mcp_server", "model", "status", "duration_ms",
        "workspace_hash", "user_hash", "metadata",
    }
    unknown = set(event) - allowed
    if unknown:
        raise ValueError(f"runtime event contains unsupported fields: {sorted(unknown)}")
    for field in ("event_id", "observed_at", "app", "event_type", "session_hash", "agent_hash", "status"):
        if not isinstance(event.get(field), str) or not event[field]:
            raise ValueError(f"runtime event missing field: {field}")
    if event["app"] not in ALLOWED_APPS:
        raise ValueError("runtime event app is unsupported")
    limits = {
        "event_id": 64, "observed_at": 64, "event_type": 64, "agent_type": 128,
        "tool_name": 128, "mcp_server": 128, "model": 128, "status": 32,
        "workspace_hash": 64, "user_hash": 64,
    }
    for key, limit in limits.items():
        value = event.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValueError(f"runtime event field is invalid: {key}")
    if event["status"] not in {"active", "completed", "failed", "idle"}:
        raise ValueError("runtime event status is unsupported")
    for key in ("session_hash", "workspace_hash", "user_hash"):
        value = event.get(key)
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"runtime event hash is invalid: {key}")
    agent_hash = event["agent_hash"]
    if agent_hash != "root" and not re.fullmatch(r"[0-9a-f]{64}", agent_hash):
        raise ValueError("runtime event agent hash is invalid")
    duration = event.get("duration_ms")
    if duration is not None and (not isinstance(duration, int) or duration < 0 or duration > 86_400_000):
        raise ValueError("runtime event duration is invalid")
    for key, value in event.items():
        if SENSITIVE_KEY.search(key) and value not in (None, ""):
            raise ValueError(f"sensitive field rejected: {key}")
    metadata = event.get("metadata", {})
    if not isinstance(metadata, dict) or set(metadata) - SAFE_METADATA_KEYS:
        raise ValueError("runtime event metadata contains unsupported fields")
    if any(not isinstance(value, str) or len(value) > 128 for value in metadata.values()):
        raise ValueError("runtime event metadata value is invalid")


def spool_path(config_path: Path, config: dict[str, Any]) -> Path:
    configured = config.get("runtime_spool")
    if configured:
        return Path(str(configured)).expanduser()
    return config_path.parent / "runtime-events.jsonl"


@contextmanager
def _file_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def append_event(path: Path, event: dict[str, Any]) -> None:
    validate_normalized_event(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n").encode()
    with _file_lock(path):
        if path.exists() and path.stat().st_size >= MAX_SPOOL_BYTES:
            raise RuntimeError("runtime event spool reached its 10 MB safety limit")
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)


def claim_spool(path: Path) -> Path | None:
    pending = path.with_suffix(path.suffix + ".pending")
    with _file_lock(path):
        if pending.exists():
            return pending
        if not path.exists() or path.stat().st_size == 0:
            return None
        path.replace(pending)
        return pending


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines()[:MAX_BATCH_EVENTS]:
        try:
            event = json.loads(line)
            validate_normalized_event(event)
            events.append(event)
        except (json.JSONDecodeError, ValueError):
            continue
    return events


class RuntimeClient:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = json.loads(config_path.read_text())

    def emit_hook(self, app: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = normalize_hook_event(app, event_type, payload)
        append_event(spool_path(self.config_path, self.config), event)
        return event
