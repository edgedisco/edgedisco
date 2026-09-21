from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

from . import __version__
from .database import Database, utc_now
from .inventory_sync import changes, snapshot


Result = TypeVar("Result")
READ_ONLY_TOOL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
}


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def record(self, tool: str, arguments: dict[str, Any], result_count: int | None,
               *, outcome: str = "success", error_type: str | None = None) -> None:
        entry = {
            "observed_at": utc_now(),
            "tool": tool,
            "arguments": arguments,
            "result_count": result_count,
            "outcome": outcome,
        }
        if error_type is not None:
            entry["error_type"] = error_type
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")


def create_server(db_path: Path, audit_path: Path | None = None):
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError(
            "MCP support requires Python 3.10+ and the optional dependency: "
            "python -m pip install 'ai-asset-inventory[mcp]'"
        ) from exc

    database = Database(db_path)
    audit = AuditLog(audit_path or db_path.parent / "mcp-audit.jsonl")
    mcp = MCPServer(
        "EdgeDisco",
        version=__version__,
        instructions=(
            "Read-only compliance inventory for discovered AI applications, active agent "
            "runtimes, agent sessions, and MCP servers. No content or credentials are exposed."
        ),
    )

    def audited(tool: str, arguments: dict[str, Any],
                operation: Callable[[], Result]) -> Result:
        try:
            result = operation()
        except Exception as exc:
            audit.record(tool, arguments, None, outcome="error", error_type=type(exc).__name__)
            raise
        if isinstance(result, dict) and isinstance(result.get("items"), list):
            result_count = len(result["items"])
        else:
            result_count = len(result) if isinstance(result, (dict, list)) else 1
        audit.record(tool, arguments, result_count)
        return result

    @mcp.tool(annotations=READ_ONLY_TOOL)
    def get_compliance_summary() -> dict[str, int]:
        """Return fleet-level counts for devices, AI assets, agents, sessions, and MCP servers."""
        return audited("get_compliance_summary", {}, database.compliance_counts)

    @mcp.tool(annotations=READ_ONLY_TOOL)
    def list_devices(limit: int = 100) -> list[dict[str, Any]]:
        """List enrolled devices and their last-seen metadata. Maximum 500 records."""
        return audited("list_devices", {"limit": limit}, lambda: database.list_devices(limit))

    @mcp.tool(annotations=READ_ONLY_TOOL)
    def list_ai_assets(kind: str | None = None, running_only: bool = False,
                       limit: int = 100) -> list[dict[str, Any]]:
        """List sanitized AI asset inventory, optionally filtered by type or running state."""
        arguments = {"kind": kind, "running_only": running_only, "limit": limit}
        return audited(
            "list_ai_assets", arguments,
            lambda: database.list_assets(kind=kind, running_only=running_only, limit=limit),
        )

    @mcp.tool(annotations=READ_ONLY_TOOL)
    def list_running_agents(limit: int = 100) -> list[dict[str, Any]]:
        """List currently observed agent runtimes and their host-device metadata."""
        return audited(
            "list_running_agents", {"limit": limit},
            lambda: database.list_assets(kind="agent_runtime", running_only=True, limit=limit),
        )

    @mcp.tool(annotations=READ_ONLY_TOOL)
    def list_agent_sessions(status: str | None = None, app: str | None = None,
                            limit: int = 100) -> list[dict[str, Any]]:
        """List sanitized agent sessions, optionally filtered by status or host application."""
        arguments = {"status": status, "app": app, "limit": limit}
        return audited(
            "list_agent_sessions", arguments,
            lambda: database.list_agent_sessions(status=status, app=app, limit=limit),
        )

    @mcp.tool(annotations=READ_ONLY_TOOL)
    def list_mcp_servers(limit: int = 100) -> list[dict[str, Any]]:
        """List discovered MCP servers without arguments, environment values, or credentials."""
        return audited(
            "list_mcp_servers", {"limit": limit},
            lambda: database.list_assets(kind="mcp_server", limit=limit),
        )

    @mcp.tool(annotations=READ_ONLY_TOOL)
    def inventory_device_status(after: str = "", limit: int = 100) -> dict[str, Any]:
        """Page inventory-report freshness by device ID. Maximum 500 records per page."""
        arguments = {"after": after, "limit": limit}
        return audited(
            "inventory_device_status", arguments,
            lambda: database.inventory_device_status(after=after, limit=limit),
        )

    @mcp.tool(annotations=READ_ONLY_TOOL)
    def inventory_snapshot(watermark: int | None = None, after: str = "",
                           limit: int = 100) -> dict[str, Any]:
        """Page through privacy-safe assets. Reuse watermark on every page."""
        arguments = {"watermark": watermark, "after": after, "limit": limit}

        def operation() -> dict[str, Any]:
            with database.connect() as conn:
                return snapshot(conn, watermark=watermark, after=after, limit=limit)

        return audited("inventory_snapshot", arguments, operation)

    @mcp.tool(annotations=READ_ONLY_TOOL)
    def inventory_changes(cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read ordered asset upserts and deletions after a cursor."""
        arguments = {"cursor": cursor, "limit": limit}

        def operation() -> dict[str, Any]:
            with database.connect() as conn:
                return changes(conn, cursor=cursor, limit=limit)

        return audited("inventory_changes", arguments, operation)

    return mcp


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8081,
          audit_path: Path | None = None) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(
            "EdgeDisco MCP binds to localhost only. Put it behind an authenticated HTTPS "
            "reverse proxy for remote access."
        )
    mcp = create_server(db_path, audit_path)
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=1_000_000,
    )
