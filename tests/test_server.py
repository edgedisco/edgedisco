import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ai_asset_inventory.database import Database
from ai_asset_inventory.server import InventoryServer
from ai_asset_inventory.agent import AgentClient
from ai_asset_inventory.runtime import RuntimeClient


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = InventoryServer(("127.0.0.1", 0), Database(Path(self.temp.name) / "db.sqlite"), "admin", "enroll")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def post(self, path, token, payload):
        request = urllib.request.Request(self.base + path, json.dumps(payload).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())

    def test_full_enroll_report_summary_flow(self):
        status, enrolled = self.post("/api/v1/enroll", "enroll", {"hostname": "host", "os": "Linux"})
        self.assertEqual(status, 201)
        report = {"scan_id": "unique", "observed_at": "2026-09-19T00:00:00+00:00", "device": {},
                  "assets": [], "privacy": {"content_captured": False}}
        status, accepted = self.post("/api/v1/reports", enrolled["device_token"], report)
        self.assertEqual(status, 202)
        request = urllib.request.Request(self.base + "/api/v1/summary", headers={"Authorization": "Bearer admin"})
        with urllib.request.urlopen(request) as response:
            summary = json.loads(response.read())
        self.assertEqual(summary["devices"], 1)

    def test_summary_requires_admin(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/api/v1/summary")
        self.assertEqual(caught.exception.code, 401)

    def test_runtime_event_upload(self):
        _, enrolled = self.post("/api/v1/enroll", "enroll", {"hostname": "host", "os": "Linux"})
        event = {
            "event_id": "event-1", "observed_at": "2026-09-19T00:00:00+00:00",
            "app": "claude-code", "event_type": "SessionStart", "session_hash": "a" * 64,
            "agent_hash": "root", "agent_type": None, "tool_name": None,
            "mcp_server": None, "model": "claude", "status": "active",
            "duration_ms": None, "workspace_hash": None, "user_hash": None,
            "metadata": {"source_event": "SessionStart"},
        }
        status, accepted = self.post("/api/v1/runtime-events", enrolled["device_token"], {"events": [event]})
        self.assertEqual(status, 202)
        self.assertEqual(accepted["runtime_event_count"], 1)

    def test_endpoint_spool_to_server_flow(self):
        config = Path(self.temp.name) / "agent.json"
        config.write_text(json.dumps({
            "server_url": self.base,
            "enrollment_token": "enroll",
            "runtime_spool": str(Path(self.temp.name) / "runtime.jsonl"),
        }))
        client = AgentClient(config)
        client.enroll()
        RuntimeClient(config).emit_hook(
            "cursor", "sessionStart", {"conversation_id": "runtime-session", "model": "test"}
        )
        result = AgentClient(config).send_once()
        self.assertEqual(result["runtime_event_count"], 1)
        request = urllib.request.Request(
            self.base + "/api/v1/summary", headers={"Authorization": "Bearer admin"}
        )
        with urllib.request.urlopen(request) as response:
            summary = json.loads(response.read())
        self.assertEqual(summary["active_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
