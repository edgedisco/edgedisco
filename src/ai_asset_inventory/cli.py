from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import AgentClient, write_example_config
from .server import serve


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ai-inventory")
    sub = root.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server", help="run centralized inventory server")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8080)
    server.add_argument("--db", type=Path, default=Path("data/inventory.db"))
    agent = sub.add_parser("agent", help="run endpoint collector")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    for name in ("scan", "run", "enroll"):
        action = agent_sub.add_parser(name)
        action.add_argument("--config", type=Path, required=True)
    init = agent_sub.add_parser("init-config")
    init.add_argument("--config", type=Path, required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "server":
        serve(args.host, args.port, args.db)
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
    else:
        client.run()


if __name__ == "__main__":
    main()
