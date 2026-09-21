import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
import sys

from ai_asset_inventory.database import Database
from ai_asset_inventory.mcp_server import AuditLog, create_server

try:
    from mcp import Client
except ImportError:
    Client = None


class MCPServerTests(unittest.TestCase):
    def test_inventory_sync_tools_are_registered_and_callable(self):
        class FakeMCPServer:
            def __init__(self, *args, **kwargs):
                self.tools = {}

            def tool(self):
                def register(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return register

        fake_server = ModuleType("mcp.server")
        fake_server.MCPServer = FakeMCPServer
        fake_mcp = ModuleType("mcp")
        fake_mcp.server = fake_server
        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "inventory.db"
            database = Database(database_path)
            device_id, _ = database.enroll({"hostname": "host", "os": "Darwin"})
            database.ingest(device_id, {"scan_id": "one", "observed_at":
                "2026-09-20T00:00:00+00:00", "assets": [{
                    "fingerprint": "one", "kind": "agent_runtime", "name": "CrewAI",
                    "vendor": "CrewAI", "running": True,
                    "metadata": {"host_app": "Cursor", "relationship": "spawned_by"}}]})
            with patch.dict(sys.modules, {"mcp": fake_mcp, "mcp.server": fake_server}):
                server = create_server(database_path)
            snap = server.tools["inventory_snapshot"]()
            self.assertEqual(len(snap["items"]), 1)
            delta = server.tools["inventory_changes"]()
            self.assertEqual(delta["items"][0]["operation"], "upsert")

    def test_audit_log_contains_only_tool_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit.jsonl"
            AuditLog(path).record("list_devices", {"limit": 10}, 2)
            entry = json.loads(path.read_text())
            self.assertEqual(entry["tool"], "list_devices")
            self.assertEqual(entry["result_count"], 2)
            self.assertNotIn("token", entry)

    def test_mcp_queries_are_bounded_and_sanitized(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Database(Path(temp) / "inventory.db")
            device_id, _ = database.enroll({"hostname": "mac-01", "os": "Darwin"})
            database.ingest(device_id, {
                "scan_id": "scan", "observed_at": "2026-09-20T00:00:00+00:00",
                "privacy": {"content_captured": False},
                "assets": [{
                    "fingerprint": "one", "kind": "mcp_server", "name": "filesystem",
                    "vendor": "MCP", "running": True,
                    "path_hash": "private-path-hash", "command_hash": "private-command-hash",
                    "metadata": {},
                }],
            })
            assets = database.list_assets(kind="mcp_server", limit=1000)
            self.assertEqual(len(assets), 1)
            self.assertNotIn("path_hash", assets[0])
            self.assertNotIn("command_hash", assets[0])


@unittest.skipUnless(Client, "optional MCP dependency is not installed")
class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_list_and_summary_call(self):
        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "inventory.db"
            database = Database(database_path)
            database.enroll({"hostname": "mcp-test", "os": "Darwin"})
            server = create_server(database_path)
            async with Client(server) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertIn("get_compliance_summary", names)
                self.assertIn("list_running_agents", names)
                result = await client.call_tool("get_compliance_summary")
                self.assertEqual(result.structured_content["devices"], 1)


if __name__ == "__main__":
    unittest.main()
