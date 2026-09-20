from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import AgentClient, write_example_config
from .server import serve
from .runtime import MAX_HOOK_INPUT_BYTES, RuntimeClient
from .adapters import install_adapters
from .demo import run_demo
from .self_service import setup_macos, status as self_service_status, uninstall_macos


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog=Path(sys.argv[0]).name)
    sub = root.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server", help="run centralized inventory server")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8080)
    server.add_argument("--db", type=Path, default=Path("data/inventory.db"))
    agent = sub.add_parser("agent", help="run endpoint collector")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    for name in ("scan", "send", "run", "enroll"):
        action = agent_sub.add_parser(name)
        action.add_argument("--config", type=Path, required=True)
    init = agent_sub.add_parser("init-config")
    init.add_argument("--config", type=Path, required=True)
    hook = sub.add_parser("hook", help="receive app lifecycle hook events")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)
    emit = hook_sub.add_parser("emit")
    emit.add_argument("--config", type=Path, required=True)
    emit.add_argument("--app", choices=["cursor", "claude-code", "github-copilot"], required=True)
    emit.add_argument("--event", required=True)
    emit.add_argument("--protocol", choices=["cursor", "silent"], default="silent")
    adapters = sub.add_parser("adapters", help="install runtime hooks")
    adapter_sub = adapters.add_subparsers(dest="adapter_command", required=True)
    install = adapter_sub.add_parser("install")
    install.add_argument("--config", type=Path, required=True)
    install.add_argument("--apps", nargs="+", choices=["cursor", "claude-code", "github-copilot"], required=True)
    setup = sub.add_parser("setup", help="one-command macOS setup")
    setup.add_argument("--root", type=Path)
    setup.add_argument("--port", type=int)
    setup.add_argument("--all-adapters", action="store_true")
    setup.add_argument("--no-open", action="store_true")
    status = sub.add_parser("status", help="show self-service installation status")
    status.add_argument("--root", type=Path)
    uninstall = sub.add_parser("uninstall", help="remove macOS background services")
    uninstall.add_argument("--root", type=Path)
    uninstall.add_argument("--purge", action="store_true", help="also delete local data")
    uninstall.add_argument("--yes", action="store_true", help="confirm data deletion")
    mcp = sub.add_parser("mcp", help="run the read-only MCP compliance server")
    mcp.add_argument("--db", type=Path, default=Path.home() / ".edgedisco/data/inventory.db")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8081)
    mcp.add_argument("--audit-log", type=Path)
    sub.add_parser(
        "demo",
        help="run a local EdgeDisco lab with simulated workloads and real discovery",
    )
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "server":
        serve(args.host, args.port, args.db)
        return
    if args.command == "demo":
        raise SystemExit(run_demo())
    if args.command == "hook":
        raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
        if len(raw) > MAX_HOOK_INPUT_BYTES:
            raise SystemExit("hook input exceeds 1 MB")
        try:
            payload = json.loads(raw or b"{}")
            RuntimeClient(args.config).emit_hook(args.app, args.event, payload)
        except Exception as exc:
            print(f"EdgeDisco hook warning: {exc}", file=sys.stderr)
        if args.protocol == "cursor":
            print("{}")
        return
    if args.command == "adapters":
        paths = install_adapters(args.config, args.apps)
        for path in paths:
            print(f"installed: {path}")
        return
    if args.command == "setup":
        result = setup_macos(
            root=args.root, port=args.port, all_adapters=args.all_adapters,
            open_dashboard=not args.no_open,
        )
        print("EdgeDisco is ready")
        print(f"Dashboard: {result['dashboard']}")
        print(f"Detected adapters: {', '.join(result['adapters']) or 'none'}")
        print(f"Initial inventory: {result['asset_count']} assets")
        print(f"CLI: {result['cli']}")
        if result.get("cli_path_integrated"):
            print("Open a new Terminal window so 'edgedisco' is on your PATH.")
        return
    if args.command == "status":
        result = self_service_status(args.root)
        for key, value in result.items():
            print(f"{key.replace('_', ' ').title()}: {value}")
        return
    if args.command == "uninstall":
        result = uninstall_macos(
            root=args.root, purge=args.purge, assume_yes=args.yes,
        )
        print("EdgeDisco background services removed")
        if result.get("launcher_removed"):
            print("CLI launcher removed from PATH")
        if result["data_purged"]:
            print("Local configuration, credentials, logs, and evidence removed")
        else:
            print(f"Local data retained at {result['root']}")
        return
    if args.command == "mcp":
        from .mcp_server import serve as serve_mcp
        serve_mcp(args.db, args.host, args.port, args.audit_log)
        return
    if args.agent_command == "init-config":
        write_example_config(args.config)
        print(f"created {args.config}")
        return
    client = AgentClient(args.config)
    if args.agent_command == "enroll":
        client.enroll()
        print("device enrolled")
    elif args.agent_command == "scan":
        print(json.dumps(client.scan_payload(), indent=2))
    elif args.agent_command == "send":
        print(json.dumps(client.send_once(), indent=2))
    else:
        client.run()


if __name__ == "__main__":
    main()
