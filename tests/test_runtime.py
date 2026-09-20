import json
import tempfile
import unittest
from pathlib import Path

from ai_asset_inventory.runtime import RuntimeClient, normalize_hook_event, read_events, validate_normalized_event


class RuntimeTests(unittest.TestCase):
    def test_cursor_payload_is_reduced_to_safe_metadata(self):
        payload = {
            "conversation_id": "conversation-1", "model": "claude-test",
            "hook_event_name": "postToolUse", "tool_name": "MCP:github/create_issue",
            "tool_input": {"token": "secret", "body": "private prompt"},
            "tool_output": "private response", "workspace_roots": ["/secret/project"],
            "user_email": "person@example.com", "duration": 42,
        }
        event = normalize_hook_event("cursor", "postToolUse", payload)
        encoded = json.dumps(event)
        self.assertEqual(event["tool_name"], "MCP:github/create_issue")
        self.assertEqual(event["mcp_server"], "github")
        self.assertNotIn("private prompt", encoded)
        self.assertNotIn("private response", encoded)
        self.assertNotIn("person@example.com", encoded)
        self.assertNotIn("/secret/project", encoded)

    def test_hook_event_is_spooled(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "agent.json"
            spool = Path(temp) / "events.jsonl"
            config.write_text(json.dumps({"runtime_spool": str(spool)}))
            RuntimeClient(config).emit_hook("claude-code", "SessionStart", {"session_id": "abc"})
            events = read_events(spool)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["app"], "claude-code")
            self.assertEqual(events[0]["status"], "active")

    def test_explicit_failed_session_status_is_preserved(self):
        event = normalize_hook_event(
            "sdk", "sessionEnd", {"session_id": "abc", "status": "failed"}
        )
        self.assertEqual(event["status"], "failed")

    def test_server_schema_rejects_content_fields(self):
        event = normalize_hook_event("cursor", "sessionStart", {"conversation_id": "abc"})
        event["prompt"] = "private"
        with self.assertRaises(ValueError):
            validate_normalized_event(event)


if __name__ == "__main__":
    unittest.main()
