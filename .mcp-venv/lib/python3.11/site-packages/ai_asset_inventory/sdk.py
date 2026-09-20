from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import time
import uuid

from .runtime import RuntimeClient


class EdgeDisco:
    """Minimal metadata-only SDK for custom local agent runtimes."""

    def __init__(self, config: str | Path, app: str = "sdk"):
        self.client = RuntimeClient(Path(config))
        self.app = app

    def emit(self, event_type: str, *, session_id: str, agent_id: str | None = None,
             agent_type: str | None = None, tool_name: str | None = None,
             model: str | None = None, status: str | None = None,
             duration_ms: int | None = None) -> dict[str, Any]:
        payload = {
            "session_id": session_id, "agent_id": agent_id, "agent_type": agent_type,
            "tool_name": tool_name, "model": model, "status": status,
            "duration_ms": duration_ms,
        }
        return self.client.emit_hook(self.app, event_type, payload)

    @contextmanager
    def session(self, *, agent_type: str, model: str | None = None,
                session_id: str | None = None) -> Iterator[str]:
        session_id = session_id or str(uuid.uuid4())
        started = time.monotonic()
        self.emit("sessionStart", session_id=session_id, agent_type=agent_type, model=model)
        try:
            yield session_id
        except Exception:
            self.emit("sessionEnd", session_id=session_id, agent_type=agent_type,
                      model=model, status="failed", duration_ms=int((time.monotonic() - started) * 1000))
            raise
        else:
            self.emit("sessionEnd", session_id=session_id, agent_type=agent_type,
                      model=model, status="completed", duration_ms=int((time.monotonic() - started) * 1000))
