import tempfile
import unittest
from pathlib import Path

from ai_asset_inventory.database import Database, utc_now


class DatabaseTests(unittest.TestCase):
    def test_enrollment_and_ingest(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "test.db")
            device_id, token = db.enroll({"hostname": "mac-01", "os": "Darwin"})
            self.assertEqual(db.device_for_token(token)["id"], device_id)
            report = {
                "scan_id": "scan-1", "observed_at": "2026-09-19T00:00:00+00:00",
                "privacy": {"content_captured": False},
                "assets": [{"fingerprint": "abc", "kind": "process", "name": "Claude",
                            "vendor": "Anthropic", "running": True, "metadata": {}}],
            }
            self.assertEqual(db.ingest(device_id, report), 1)
            summary = db.summary()
            self.assertEqual(summary["devices"], 1)
            self.assertEqual(summary["running"], 1)
            self.assertEqual(summary["items"][0]["name"], "Claude")
            self.assertEqual(db.list_devices()[0]["hostname"], "mac-01")
            self.assertEqual(db.list_assets(running_only=True)[0]["name"], "Claude")
            self.assertEqual(db.list_assets(kind="mcp_server"), [])
            self.assertEqual(db.compliance_counts()["devices"], 1)

    def test_unknown_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "test.db")
            self.assertIsNone(db.device_for_token("wrong"))

    def test_runtime_events_create_agent_session(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "test.db")
            device_id, _ = db.enroll({"hostname": "mac-01", "os": "Darwin"})
            event = {
                "event_id": "event-1", "observed_at": utc_now(),
                "app": "cursor", "event_type": "postToolUse", "session_hash": "session",
                "agent_hash": "root", "agent_type": "coding", "tool_name": "Shell",
                "mcp_server": None, "model": "model", "status": "active",
                "duration_ms": 5, "workspace_hash": "workspace", "user_hash": "user",
                "metadata": {"source_event": "postToolUse"},
            }
            self.assertEqual(db.ingest_runtime_events(device_id, [event]), 1)
            self.assertEqual(db.ingest_runtime_events(device_id, [event]), 0)
            summary = db.summary()
            self.assertEqual(summary["active_sessions"], 1)
            self.assertEqual(summary["session_items"][0]["tool_count"], 1)
            sessions = db.list_agent_sessions(status="active", app="cursor")
            self.assertEqual(len(sessions), 1)
            self.assertNotIn("session_hash", sessions[0])


if __name__ == "__main__":
    unittest.main()
