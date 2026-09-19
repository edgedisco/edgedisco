import tempfile
import unittest
from pathlib import Path

from ai_asset_inventory.database import Database


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

    def test_unknown_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / "test.db")
            self.assertIsNone(db.device_for_token("wrong"))


if __name__ == "__main__":
    unittest.main()
